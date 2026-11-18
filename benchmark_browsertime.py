#!/usr/bin/env python3
"""
Firefox benchmark (Windows) sobre Browsertime.

Browsertime (Node.js) hace la carga y las metricas de pagina; este script se encarga de:
  - recorrer versiones de Firefox (browsers\\<etiqueta>\\firefox.exe o ...\\core\\firefox.exe),
  - lanzar una ejecucion de Browsertime por medida (navegador nuevo cada vez),
  - muestrear la RAM del conjunto de procesos de Firefox con psutil,
  - guardar todo en CSV y generar medianas por version, web y modo de cache.

Requiere: Node.js 22+, `npm install -g browsertime`, y `pip install psutil`.
Un geckodriver compatible con cada Firefox: el que trae Browsertime, --geckodriver, o un
geckodriver.exe dentro de la carpeta de esa version.
"""
from __future__ import annotations

import argparse
import configparser
import csv
import json
import os
import random
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import psutil

SITES = {
    "google": "https://www.google.com/",
    "youtube": "https://www.youtube.com/",
    "meneame": "https://www.meneame.net/",
}
POLL_SECONDS = 0.5

FIELDS = [
    "timestamp_utc", "round", "version_label", "binary_version", "site", "url", "cache", "iteration",
    "status", "error",
    "domContentLoadedMs", "loadMs", "ttfbMs", "fcpMs", "lcpMs",
    "resourceCount", "servedFromCache",
    "memoryMB", "ramMaxMB", "processCountMax", "wallSeconds", "finalUrl",
]
METRIC_FIELDS = [
    "domContentLoadedMs", "loadMs", "ttfbMs", "fcpMs", "lcpMs", "resourceCount",
    "servedFromCache", "memoryMB", "ramMaxMB", "processCountMax",
]
SUMMARY_FIELDS = ["version_label", "binary_version", "site", "cache", "runs_ok", "runs_total"] \
    + METRIC_FIELDS + ["note"]


# --------------------------------------------------------------------------- utilidades

def short(text: str, limit: int = 400) -> str:
    return " ".join(str(text).split())[-limit:]


def natural_key(text: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def slug(text: str) -> str:
    return re.sub(r"[^\w.-]", "_", text)


def parse_major(text: str) -> int | None:
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def firefox_version(binary: Path) -> tuple[str, int | None]:
    """Version leida de application.ini (fiable en Windows); --version como alternativa."""
    ini = binary.parent / "application.ini"
    try:
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(ini, encoding="utf-8")
        version = cp.get("App", "Version", fallback="").strip()
        if version:
            name = cp.get("App", "Name", fallback="Firefox").strip() or "Firefox"
            return f"{name} {version}", parse_major(version)
    except Exception:
        pass
    try:
        p = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=15)
        out = (p.stdout or p.stderr).strip()
        if out:
            return out, parse_major(out)
    except Exception:
        pass
    return "unknown", None


def find_binaries(root: Path, only: set[str] | None) -> list[tuple[str, Path]]:
    """Busca <root>\\<etiqueta>\\firefox.exe y <root>\\<etiqueta>\\core\\firefox.exe (extraccion con 7-Zip)."""
    found: dict[str, Path] = {}
    for b in root.glob("*/core/firefox.exe"):
        found[b.parent.parent.name] = b
    for b in root.glob("*/firefox.exe"):
        found[b.parent.name] = b
    items = sorted(found.items(), key=lambda kv: natural_key(kv[0]))
    if only:
        wanted = {w.lower() for w in only}
        for m in sorted(wanted - {k.lower() for k, _ in items}):
            print(f"AVISO: --versions incluye '{m}' pero no existe en {root}", file=sys.stderr)
        items = [(k, v) for k, v in items if k.lower() in wanted]
    return items


def parse_sites(spec: str | None) -> dict[str, str]:
    if not spec:
        return dict(SITES)
    out: dict[str, str] = {}
    for item in (s.strip() for s in spec.split(",")):
        if not item:
            continue
        if item.startswith(("http://", "https://")):
            name = (urlparse(item).hostname or item).removeprefix("www.").split(".")[0]
            out[name] = item
        elif "=" in item:
            name, url = item.split("=", 1)
            out[name.strip()] = url.strip()
        elif item in SITES:
            out[item] = SITES[item]
        else:
            sys.exit(f"Web desconocida en --sites: '{item}'. Usa {', '.join(SITES)}, una URL o nombre=url.")
    if not out:
        sys.exit("--sites no contiene ninguna web valida.")
    return out


def resolve_geckodriver(binary: Path, label: str, args) -> str | None:
    """geckodriver.exe junto al firefox.exe o en la carpeta de la version; si no, --geckodriver; si no, el de Browsertime."""
    for cand in (binary.parent / "geckodriver.exe", binary.parent.parent / "geckodriver.exe"):
        if cand.is_file():
            return str(cand)
    return args.geckodriver


# --------------------------------------------------------------------------- procesos / RAM

def gather_firefox_procs(exe: Path, since: float) -> list[psutil.Process]:
    """Procesos cuyo ejecutable es este firefox.exe y que se crearon despues de `since`."""
    target = os.path.normcase(os.path.abspath(str(exe)))
    found = []
    for p in psutil.process_iter(["exe", "create_time"]):
        try:
            e = p.info.get("exe")
            if e and os.path.normcase(os.path.abspath(e)) == target \
                    and (p.info.get("create_time") or 0) >= since - 1:
                found.append(p)
        except Exception:
            continue
    return found


def total_rss(procs: list[psutil.Process]) -> tuple[float, int]:
    rss = 0
    alive = 0
    for p in procs:
        try:
            rss += p.memory_info().rss
            alive += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return round(rss / (1024 * 1024), 1), alive


def kill_procs(procs: list[psutil.Process]) -> None:
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass
    try:
        psutil.wait_procs(procs, timeout=5)
    except Exception:
        pass


def kill_tree(pid: int) -> None:
    try:
        root = psutil.Process(pid)
        kill_procs(root.children(recursive=True) + [root])
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


class RamSampler(threading.Thread):
    """Muestrea la RAM (RSS sumado) de los procesos de Firefox mientras corre Browsertime."""

    def __init__(self, gather, interval: float):
        super().__init__(daemon=True)
        self._gather = gather
        self._interval = interval
        self._stop_evt = threading.Event()
        self.samples: list[tuple[float, int]] = []

    def run(self):
        while not self._stop_evt.is_set():
            try:
                mb, n = total_rss(self._gather())
                if n:
                    self.samples.append((mb, n))
            except Exception:
                pass
            self._stop_evt.wait(self._interval)

    def stop(self):
        self._stop_evt.set()
        if self.is_alive():
            self.join(timeout=10)

    def stats(self) -> dict:
        if not self.samples:
            return {"ramMaxMB": "", "processCountMax": ""}
        return {"ramMaxMB": max(s[0] for s in self.samples), "processCountMax": max(s[1] for s in self.samples)}


# --------------------------------------------------------------------------- Browsertime

def dig(obj, *path):
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int) and -len(obj) <= key < len(obj):
            obj = obj[key]
        else:
            return None
    return obj


def positive(v):
    """Valores de timing 0/ausentes significan 'no disponible'."""
    return v if isinstance(v, (int, float)) and v > 0 else None


def parse_browsertime_json(path: Path) -> tuple[dict, str]:
    """Devuelve (metricas, error). Una iteracion por fichero (se ejecuta con -n 1)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    item = data[0] if isinstance(data, list) and data else data
    if not isinstance(item, dict):
        return {}, "JSON de Browsertime inesperado"
    errors = []
    for e in item.get("errors") or []:
        for x in (e if isinstance(e, list) else [e]):
            if x:
                errors.append(str(x))
    scripts = dig(item, "browserScripts", 0) or {}
    timings = scripts.get("timings") or {}
    nav = timings.get("navigationTiming") or {}
    lcp = timings.get("largestContentfulPaint") or {}
    mem = dig(item, "memory", 0)
    metrics = {
        "domContentLoadedMs": positive(nav.get("domContentLoadedEventEnd"))
        or positive(dig(timings, "pageTimings", "domContentLoadedTime")),
        "loadMs": positive(nav.get("loadEventEnd")),
        "ttfbMs": positive(dig(timings, "pageTimings", "backEndTime")),
        "fcpMs": positive(dig(timings, "paintTiming", "first-contentful-paint")),
        "lcpMs": positive(lcp.get("renderTime")) or positive(lcp.get("startTime")) if isinstance(lcp, dict) else None,
        "resourceCount": dig(scripts, "pageinfo", "resources", "count"),
        "servedFromCache": dig(scripts, "pageinfo", "resources", "servedFromCache"),
        "memoryMB": round(mem / (1024 * 1024), 1) if isinstance(mem, (int, float)) and mem > 0 else None,
        "finalUrl": dig(scripts, "pageinfo", "url"),
    }
    if not scripts:
        errors.append("Browsertime no produjo metricas")
    return metrics, "; ".join(errors)[:500]


def run_once(args, bt: str, label: str, binary: Path, gecko: str | None, url: str, cache: str,
             work_dir: Path) -> dict:
    """Una ejecucion de Browsertime (-n 1) con muestreo de RAM. Devuelve campos de la fila."""
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)
    cmd = [bt, "-b", "firefox", "-n", "1", "--firefox.binaryPath", str(binary),
           "--resultDir", str(work_dir / "result"), "--skipHar",
           "--timeouts.pageLoad", str(args.load_timeout * 1000)]
    if gecko:
        cmd += ["--firefox.geckodriverPath", gecko]
    if args.memory_report:
        cmd += ["--firefox.memoryReport"]
    if args.headless:
        cmd += ["--headless"]
    if cache == "warm":
        cmd += ["--preURL", url]  # la misma URL cargada antes en la misma sesion: cache caliente
    cmd += args.bt_extra
    cmd.append(url)

    out: dict = {"status": "ok", "error": ""}
    started = time.time()
    log_path = work_dir / "browsertime.log"
    sampler = RamSampler(lambda: gather_firefox_procs(binary, started), POLL_SECONDS)
    proc = None
    try:
        with log_path.open("wb") as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            sampler.start()
            try:
                rc = proc.wait(timeout=args.run_timeout)
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid)
                out.update(status="timeout", error=f"Browsertime supero {args.run_timeout}s")
                rc = None
    except OSError as e:
        out.update(status="error", error=f"No se pudo lanzar Browsertime: {e}")
        rc = None
    finally:
        sampler.stop()
        out.update(sampler.stats())
        out["wallSeconds"] = round(time.time() - started, 1)
        kill_procs(gather_firefox_procs(binary, started))  # restos del navegador

    if out["status"] == "ok":
        files = sorted((work_dir / "result").rglob("browsertime.json"))
        if not files:
            tail = short(log_path.read_text(encoding="utf-8", errors="replace")) if log_path.exists() else ""
            out.update(status="error", error=f"sin browsertime.json (exit {rc}): {tail}")
        else:
            try:
                metrics, err = parse_browsertime_json(files[0])
                out.update({k: v for k, v in metrics.items() if v is not None})
                if err:
                    out.update(status="error", error=err)
                elif rc not in (0, None):
                    out.update(status="error", error=f"Browsertime termino con codigo {rc}")
                elif out.get("loadMs") is None:
                    out.update(status="incomplete", error="load event no completado")
            except Exception as e:
                out.update(status="error", error=f"no se pudo leer browsertime.json: {e}")
    return out


# --------------------------------------------------------------------------- resumen

def _num(v):
    if v in ("", None):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def summarize(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["version_label"], r["site"], r.get("cache", "")), []).append(r)
    out = []
    for (label, site, cache), rs in groups.items():
        measured = [r for r in rs if r.get("iteration")]
        ok = [r for r in measured if r.get("status") == "ok"]
        entry = {"version_label": label, "binary_version": rs[0].get("binary_version", ""), "site": site,
                 "cache": cache, "runs_ok": len(ok), "runs_total": len(measured), "note": ""}
        for key in METRIC_FIELDS:
            vals = [x for x in (_num(r.get(key)) for r in ok) if x is not None]
            entry[key] = round(statistics.median(vals), 1) if vals else ""
        if not ok:
            bad = [r for r in rs if r.get("status") != "ok"]
            entry["note"] = bad[0]["status"] if bad else ""
        out.append(entry)
    return out


def print_table(rows: list[dict], cols: list[str]) -> None:
    if not rows:
        return
    widths = [max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols]
    line = "  ".join(c.ljust(w) for c, w in zip(cols, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(w) for c, w in zip(cols, widths)))


# --------------------------------------------------------------------------- main

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Benchmark de versiones de Firefox (Windows) sobre Browsertime.")
    ap.add_argument("--browsers", default="browsers", help="Carpeta con subcarpetas <version>\\firefox.exe")
    ap.add_argument("--out", default="results.csv")
    ap.add_argument("--repeats", type=int, default=3, help="Ejecuciones medidas por web, modo de cache y ronda")
    ap.add_argument("--rounds", type=int, default=1, help="Repite toda la bateria N veces")
    ap.add_argument("--shuffle", action="store_true", help="Orden aleatorio de versiones en cada ronda")
    ap.add_argument("--seed", type=int, default=None, help="Semilla para --shuffle")
    ap.add_argument("--cache", choices=["warm", "cold", "both"], default="warm",
                    help="cold: navegador nuevo; warm: carga previa de la misma URL en la misma sesion")
    ap.add_argument("--versions", default=None, help="Etiquetas a ejecutar, separadas por comas")
    ap.add_argument("--sites", default=None,
                    help="Webs: nombres predefinidos, URLs o nombre=url, separados por comas")
    ap.add_argument("--browsertime", default="browsertime", help="Ejecutable de Browsertime")
    ap.add_argument("--geckodriver", default=None, help="geckodriver.exe a usar si la version no trae el suyo")
    ap.add_argument("--bt-args", default="",
                    help="Opciones extra para Browsertime, entre comillas (p. ej. \"--connectivity.profile cable\")")
    ap.add_argument("--load-timeout", type=int, default=90, help="Timeout de carga de pagina (s)")
    ap.add_argument("--run-timeout", type=int, default=300, help="Timeout total de cada ejecucion (s)")
    ap.add_argument("--memory-report", action=argparse.BooleanOptionalAction, default=True,
                    help="Memoria residente tras la carga con el informe de Firefox (--firefox.memoryReport)")
    ap.add_argument("--min-major", type=int, default=48,
                    help="Versiones de Firefox anteriores se marcan 'unsupported' sin lanzarlas")
    ap.add_argument("--headless", action="store_true", help="Sin ventana (cambia el rendimiento)")
    ap.add_argument("--work-dir", default="bt-work", help="Carpeta temporal de resultados de Browsertime")
    ap.add_argument("--keep-raw", action="store_true", help="No borrar JSON/logs de Browsertime de las ejecuciones ok")
    ap.add_argument("--pause", type=float, default=2.0, help="Pausa entre ejecuciones (s)")
    ap.add_argument("--delimiter", default=",", help="Delimitador CSV (usa ';' para Excel en espanol)")
    args = ap.parse_args(argv)
    if args.repeats < 1 or args.rounds < 1:
        ap.error("--repeats y --rounds deben ser >= 1")
    if args.geckodriver and not Path(args.geckodriver).is_file():
        ap.error(f"--geckodriver no existe: {args.geckodriver}")
    args.bt_extra = shlex.split(args.bt_args, posix=(os.name != "nt")) if args.bt_args else []
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    bt = shutil.which(args.browsertime)
    if not bt:
        sys.exit("No encuentro Browsertime. Instala Node.js 22+ y ejecuta: npm install -g browsertime")
    try:
        p = subprocess.run([bt, "--version"], capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            sys.exit(f"Browsertime no arranca: {short(p.stderr or p.stdout)}")
        print(f"Browsertime {p.stdout.strip()}")
    except subprocess.TimeoutExpired:
        sys.exit("Browsertime --version no responde.")

    browser_root = Path(args.browsers).resolve()
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work_root = Path(args.work_dir).resolve()
    work_root.mkdir(parents=True, exist_ok=True)

    only = {v.strip() for v in args.versions.split(",") if v.strip()} if args.versions else None
    binaries = find_binaries(browser_root, only)
    if not binaries:
        sys.exit(f"No encuentro {browser_root}\\<version>\\firefox.exe")
    sites = parse_sites(args.sites)
    modes = ["cold", "warm"] if args.cache == "both" else [args.cache]

    versions = {}
    for label, binary in binaries:
        v, major = firefox_version(binary)
        versions[label] = (binary, v, major)
        print(f"{label}: {v}  ({binary})")
    seed = args.seed if args.seed is not None else int(time.time())
    rng = random.Random(seed)
    if args.shuffle:
        print(f"Orden aleatorio, semilla {seed}")

    all_rows: list[dict] = []
    interrupted = False
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore", delimiter=args.delimiter)
        writer.writeheader()
        f.flush()

        def emit(row: dict) -> None:
            writer.writerow(row)
            f.flush()
            all_rows.append(row)
            extra = f" | load={row['loadMs']}ms" if row.get("loadMs") not in (None, "") else ""
            err = f" | {row['error'][:120]}" if row.get("error") else ""
            print(f"{row['version_label']} | {row['site']} | {row['cache']} | r{row['round']} "
                  f"{row['iteration']}/{args.repeats} | {row['status']}{extra}{err}", flush=True)

        try:
            for rnd in range(1, args.rounds + 1):
                order = list(binaries)
                if args.shuffle:
                    rng.shuffle(order)
                for label, binary in order:
                    _, version, major = versions[label]
                    gecko = resolve_geckodriver(binary, label, args)
                    for site_name, url in sites.items():
                        for cache in modes:
                            base = {"round": rnd, "version_label": label, "binary_version": version,
                                    "site": site_name, "url": url, "cache": cache}
                            if major is not None and major < args.min_major:
                                emit({**base, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                                      "iteration": 0, "status": "unsupported",
                                      "error": f"Firefox {major} < --min-major {args.min_major}"})
                                continue
                            warned = False
                            for it in range(1, args.repeats + 1):
                                work = work_root / f"{slug(label)}_{slug(site_name)}_{cache}_r{rnd}_{it}"
                                row = {**base, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                                       "iteration": it}
                                row.update(run_once(args, bt, label, binary, gecko, url, cache, work))
                                if not warned and row.get("finalUrl"):
                                    want = (urlparse(url).hostname or "").removeprefix("www.")
                                    got = (urlparse(row["finalUrl"]).hostname or "").removeprefix("www.")
                                    if want and got and want != got:
                                        print(f"  AVISO: {site_name} termino en {got} (posible consentimiento/"
                                              f"redireccion); revisa finalUrl.", file=sys.stderr)
                                        warned = True
                                emit(row)
                                if row["status"] == "ok" and not args.keep_raw:
                                    shutil.rmtree(work, ignore_errors=True)
                                time.sleep(args.pause)
        except KeyboardInterrupt:
            interrupted = True
            print("\nInterrumpido: se conservan los resultados parciales.", file=sys.stderr)

    summary = summarize(all_rows)
    if summary:
        summary_path = out_path.with_name(out_path.stem + "_summary.csv")
        with summary_path.open("w", newline="", encoding="utf-8-sig") as sf:
            sw = csv.DictWriter(sf, fieldnames=SUMMARY_FIELDS, extrasaction="ignore", delimiter=args.delimiter)
            sw.writeheader()
            sw.writerows(summary)
        print("\nMedianas por version, web y cache (solo ejecuciones 'ok'):")
        print_table(summary, ["version_label", "site", "cache", "runs_ok", "runs_total", "loadMs", "fcpMs",
                              "lcpMs", "memoryMB", "ramMaxMB", "note"])
        print(f"\nResumen: {summary_path}")
    print(f"Terminado{' (interrumpido)' if interrupted else ''}. CSV: {out_path}")
    return 0 if any(r.get("status") == "ok" for r in all_rows) else 1


if __name__ == "__main__":
    sys.exit(main())
