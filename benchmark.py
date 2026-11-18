#!/usr/bin/env python3
"""
Firefox benchmark (Windows), Firefox 10 en adelante, con el MISMO metodo de medida para todas las versiones.

Entradas:
  - --setups:       carpeta con los instaladores completos de Firefox ("Firefox Setup 52.0.exe"...). Se
                    descomprimen con 7-Zip en --browsers (una sola vez) y la version se lee de application.ini.
  - --geckodrivers: carpeta con los zip oficiales ("geckodriver-v0.17.0-win64.zip"...). Para cada Firefox
                    >= 48 se elige el geckodriver compatible segun la tabla oficial de soporte.

Cada medida es una ejecucion de este mismo fichero en modo --worker (un navegador nuevo y un perfil nuevo):
  - Firefox <= 47  -> motor "legacy": Selenium 2.53.x (FirefoxDriver, extension) en un entorno virtual aparte
                      que el script crea solo (.venv-legacy).
  - Firefox >= 48  -> motor "marionette": Selenium + geckodriver (uno compatible con cada version).
El proceso principal lanza el worker, muestrea la RAM de los procesos de ese firefox.exe con psutil,
guarda el CSV y calcula medianas por version, web y modo de cache.

Requiere Python 3.9+ y: pip install selenium psutil
"""
from __future__ import annotations

import argparse
import configparser
import csv
import functools
import http.server
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

psutil = None  # solo lo necesita el proceso principal (el worker puede correr sin psutil)

SITES = {
    "google": "https://www.google.com/",
    "youtube": "https://www.youtube.com/",
    "meneame": "https://www.meneame.net/",
}
LEGACY_MAX_MAJOR = 47          # ultima version con FirefoxDriver (extension); Marionette empieza en la 48
LEGACY_SELENIUM = "selenium==2.53.6"
POLL_SECONDS = 0.5
RESOURCE_BUFFER_DEFAULT = 250

# (geckodriver, Firefox minimo, Firefox maximo o None). Fuente: tabla oficial de soporte de geckodriver
# (SeleniumHQ/selenium, common/geckodriver/geckodriver-support.json).
GECKO_SUPPORT = [
    ("0.37.1", 115, None),
    ("0.37.0", 115, None),
    ("0.36.0", 115, None),
    ("0.35.0", 115, None),
    ("0.34.0", 115, None),
    ("0.33.0", 102, 120),
    ("0.32.2", 102, 120),
    ("0.32.1", 102, 120),
    ("0.32.0", 102, 120),
    ("0.31.0", 91, 120),
    ("0.30.0", 78, 90),
    ("0.29.1", 60, 90),
    ("0.29.0", 60, 90),
    ("0.28.0", 60, 90),
    ("0.27.0", 60, 90),
    ("0.26.0", 60, 90),
    ("0.25.0", 57, 90),
    ("0.24.0", 57, 79),
    ("0.23.0", 57, 79),
    ("0.22.0", 57, 79),
    ("0.21.0", 57, 79),
    ("0.20.1", 55, 62),
    ("0.20.0", 55, 62),
    ("0.19.1", 55, 62),
    ("0.19.0", 55, 62),
    ("0.18.0", 53, 62),
    ("0.17.0", 52, 62),
]
BEST_EFFORT_RANGE = (48, 51)   # Marionette existe desde Firefox 48; geckodriver < 0.17 queda fuera de la tabla
SETUP_RE = re.compile(r"firefox[ _-]*setup[ _-]*(?P<v>\d+(?:\.\d+)*(?:esr|b\d+)?)", re.I)
GECKO_ZIP_RE = re.compile(r"^geckodriver-v(?P<v>\d+(?:\.\d+)+)-(?P<arch>win64|win32|win-aarch64)\.zip$", re.I)

# Preferencias para reducir ruido. Las que no existan en una version antigua son inofensivas.
QUIET_PREFS = {
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "startup.homepage_welcome_url": "",
    "startup.homepage_welcome_url.additional": "",
    "browser.aboutwelcome.enabled": False,
    "browser.rights.override": True,
    "browser.rights.3.shown": True,
    "app.update.enabled": False,
    "app.update.auto": False,
    "browser.search.update": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    "datareporting.healthreport.service.enabled": False,
    "toolkit.telemetry.enabled": False,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "browser.sessionstore.resume_from_crash": False,
    "network.captive-portal-service.enabled": False,
    "xpinstall.signatures.required": False,
    "extensions.update.enabled": False,
}

FIELDS = [
    "timestamp_utc", "round", "version_label", "binary_version", "engine", "geckodriver", "site", "url", "cache",
    "iteration", "status", "error",
    "domContentLoadedMs", "loadMs", "responseEndMs", "ttfbMs", "fcpMs",
    "resourceCount", "resourceBufferFull", "transferBytes",
    "navigationWallMs", "networkQuiet", "ramAvgMB", "ramMaxMB", "processCountMax", "wallSeconds", "finalUrl",
]
METRIC_FIELDS = [
    "domContentLoadedMs", "loadMs", "responseEndMs", "ttfbMs", "fcpMs", "resourceCount", "transferBytes",
    "navigationWallMs", "ramAvgMB", "ramMaxMB", "processCountMax",
]
SUMMARY_FIELDS = ["version_label", "binary_version", "engine", "site", "cache", "runs_ok", "runs_total"] \
    + METRIC_FIELDS + ["note"]

# ES5 puro: tiene que ejecutarse en Firefox 10. Devuelve una cadena JSON.
TIMING_JS = """
var out = {};
var p = window.performance;
var t = p && p.timing;
var nav = null, s = 0, dcl = 0, load = 0, resp = 0, first = 0, fcp = null, count = 0, bytes = 0, i;
if (p && p.getEntriesByType) {
  var n = p.getEntriesByType('navigation');
  if (n && n.length) { nav = n[0]; }
  var pt = p.getEntriesByType('paint') || [];
  for (i = 0; i < pt.length; i++) { if (pt[i].name === 'first-contentful-paint') { fcp = Math.round(pt[i].startTime); } }
  var res = p.getEntriesByType('resource') || [];
  count = res.length;
  for (i = 0; i < res.length; i++) { bytes += (res[i].transferSize || 0); }
}
if (nav) {
  s = nav.startTime; dcl = nav.domContentLoadedEventEnd; load = nav.loadEventEnd;
  resp = nav.responseEnd; first = nav.responseStart; bytes += (nav.transferSize || 0);
} else if (t) {
  s = t.navigationStart; dcl = t.domContentLoadedEventEnd; load = t.loadEventEnd;
  resp = t.responseEnd; first = t.responseStart;
}
function rel(v) { return (v > 0) ? Math.round(v - s) : null; }
out.domContentLoadedMs = rel(dcl);
out.loadMs = rel(load);
out.responseEndMs = rel(resp);
out.ttfbMs = rel(first);
out.fcpMs = fcp;
out.resourceCount = count;
out.resourceBufferFull = count >= __BUF__;
out.transferBytes = bytes;
out.finalUrl = String(location.href);
return JSON.stringify(out);
""".replace("__BUF__", str(RESOURCE_BUFFER_DEFAULT))

QUIET_JS = ("var p = window.performance; "
            "var c = (p && p.getEntriesByType) ? p.getEntriesByType('resource').length : 0; "
            "return c + '|' + document.readyState;")


# =========================================================================== utilidades

def short(text, limit: int = 400) -> str:
    return " ".join(str(text).split())[-limit:]


def natural_key(text: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def slug(text: str) -> str:
    return re.sub(r"[^\w.-]", "_", text)


def parse_major(text: str):
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def firefox_version(binary: Path):
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


def find_binaries(root: Path, only):
    """Busca <root>\\<etiqueta>\\firefox.exe y <root>\\<etiqueta>\\core\\firefox.exe (extraccion con 7-Zip)."""
    found = {}
    for b in root.glob("*/core/firefox.exe"):
        found[b.parent.parent.name] = b
    for b in root.glob("*/firefox.exe"):
        found[b.parent.name] = b
    items = sorted(found.items(), key=lambda kv: natural_key(kv[0]))
    if only:
        for w in sorted(only):
            if not any(label_matches(k, w) for k, _ in items):
                print(f"AVISO: --versions incluye '{w}' pero no coincide con ninguna version en {root}",
                      file=sys.stderr)
        items = [(k, v) for k, v in items if any(label_matches(k, w) for w in only)]
    return items


def label_matches(label: str, wanted: str) -> bool:
    """'52' coincide con Firefox52.0 y Firefox52.0.2; '115.0' con Firefox115.0esr; '5' no coincide con 52."""
    l = label.lower()
    w = wanted.lower().strip()
    ver = l.removeprefix("firefox")
    return w in (l, ver) or (ver.startswith(w) and not ver[len(w):len(w) + 1].isdigit())


class LocalHandler(http.server.SimpleHTTPRequestHandler):
    """Sirve las paginas de prueba con cache HTTP (para el modo warm) y soporte de Range (video en bucle)."""
    protocol_version = "HTTP/1.1"
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".webm": "video/webm", ".mp4": "video/mp4", ".ogv": "video/ogg",
                      ".js": "application/javascript", ".css": "text/css", ".html": "text/html; charset=utf-8",
                      ".jpg": "image/jpeg", ".png": "image/png", "": "application/octet-stream"}

    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def do_GET(self):
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
        path = self.translate_path(self.path.split("?", 1)[0])
        if not m or not os.path.isfile(path):
            return super().do_GET()
        size = os.path.getsize(path)
        if m.group(1) == "":
            start, end = max(0, size - int(m.group(2) or 0)), size - 1
        else:
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)


def start_local_server(root: Path):
    handler = functools.partial(LocalHandler, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def wants_local(spec) -> bool:
    return bool(spec) and any(t.strip() == "local" or t.strip().startswith("local-") for t in spec.split(","))


def parse_sites(spec, local_pages=None):
    local_pages = local_pages or {}
    if not spec:
        return dict(SITES)
    out = {}
    for item in (s.strip() for s in spec.split(",")):
        if not item:
            continue
        if item == "local":
            out.update(local_pages)
        elif item.startswith("local-"):
            if item not in local_pages:
                sys.exit(f"Pagina local desconocida '{item}'. Disponibles: {', '.join(local_pages) or 'ninguna'}.")
            out[item] = local_pages[item]
        elif item.startswith(("http://", "https://")):
            name = (urlparse(item).hostname or item).removeprefix("www.").split(".")[0]
            out[name] = item
        elif "=" in item:
            name, url = item.split("=", 1)
            out[name.strip()] = url.strip()
        elif item in SITES:
            out[item] = SITES[item]
        else:
            sys.exit(f"Web desconocida en --sites: '{item}'. Usa {', '.join(SITES)}, local, local-<pagina>, "
                     f"una URL o nombre=url.")
    if not out:
        sys.exit("--sites no contiene ninguna web valida.")
    return out


def find_7z(args):
    if args.sevenzip:
        return args.sevenzip if Path(args.sevenzip).is_file() else None
    for name in ("7z", "7za", "7zr"):
        found = shutil.which(name)
        if found:
            return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramW6432")):
        if base:
            cand = Path(base) / "7-Zip" / "7z.exe"
            if cand.is_file():
                return str(cand)
    return None


def pe_arch(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0x3C)
            off = int.from_bytes(f.read(4), "little")
            f.seek(off)
            if f.read(4) != b"PE\0\0":
                return "unknown"
            machine = int.from_bytes(f.read(2), "little")
        return {0x14C: "x86", 0x8664: "x64", 0xAA64: "arm64"}.get(machine, "unknown")
    except Exception:
        return "unknown"


def _run_7z(sevenzip: str, archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    p = subprocess.run([sevenzip, "x", "-y", f"-o{dest}", str(archive)], capture_output=True, text=True,
                       timeout=900)
    if p.returncode not in (0, 1):  # 1 = avisos
        raise RuntimeError(f"7-Zip fallo (codigo {p.returncode}): {short(p.stderr or p.stdout, 300)}")


def _find_firefox_exe(folder: Path):
    for cand in sorted(folder.rglob("firefox.exe"), key=lambda c: len(c.parts)):
        if (cand.parent / "application.ini").is_file() or (cand.parent / "omni.ja").is_file() \
                or (cand.parent / "browser").is_dir():
            return cand
    return None


def extract_setup(installer: Path, browsers: Path, sevenzip: str):
    """Descomprime un instalador completo y deja la carpeta de la aplicacion en browsers\\<etiqueta>.
    La etiqueta sale de la version real (application.ini). Devuelve (etiqueta, mensaje)."""
    tmp = Path(tempfile.mkdtemp(prefix=".extract_", dir=browsers))
    try:
        _run_7z(sevenzip, installer, tmp)
        exe = _find_firefox_exe(tmp)
        if exe is None:  # algunos instaladores antiguos llevan otro ejecutable dentro (setup.exe)
            for i, nested in enumerate(sorted(tmp.rglob("*.exe"))[:5]):
                try:
                    _run_7z(sevenzip, nested, tmp / f"_nested{i}")
                except Exception:
                    continue
                exe = _find_firefox_exe(tmp / f"_nested{i}")
                if exe:
                    break
        if exe is None:
            hint = " (parece el instalador web/stub: descarga el instalador completo)" \
                if installer.stat().st_size < 3_000_000 else ""
            raise RuntimeError(f"no se encontro firefox.exe dentro del instalador{hint}")
        version, major = firefox_version(exe)
        mver = re.search(r"\d+(?:\.\d+)*(?:[ab]\d+)?", version)
        ver = mver.group(0) if mver else slug(installer.stem)
        if re.search(r"esr", installer.name, re.I):
            ver += "esr"
        arch = pe_arch(exe)
        base = f"Firefox{ver}"
        label = base
        marker = {"file": installer.name, "size": installer.stat().st_size, "mtime": int(installer.stat().st_mtime)}
        n = 1
        while (browsers / label).exists():
            other = _read_marker(browsers / label)
            if other == marker:
                return label, "ya estaba descomprimido"
            n += 1
            label = f"{base}-{arch}" if n == 2 else f"{base}-{arch}-{n - 1}"
        shutil.move(str(exe.parent), str(browsers / label))
        (browsers / label / ".source.json").write_text(json.dumps(marker), encoding="utf-8")
        return label, f"{version}, {arch}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _read_marker(folder: Path):
    try:
        return json.loads((folder / ".source.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def extract_setups(args, browsers: Path, only) -> None:
    """Descomprime (si hace falta) los instaladores de --setups en --browsers."""
    setups = Path(args.setups)
    if not setups.is_dir():
        print(f"(No existe la carpeta de instaladores {setups}; se usan solo las versiones ya presentes en {browsers})")
        return
    installers = sorted((p for p in setups.iterdir() if p.is_file() and p.suffix.lower() == ".exe"),
                        key=lambda p: natural_key(p.name))
    if not installers:
        print(f"(No hay .exe en {setups})")
        return
    known = {}
    for d in browsers.iterdir():
        mk = _read_marker(d) if d.is_dir() else None
        if mk:
            known[(mk["file"], mk["size"], mk["mtime"])] = d.name
    sevenzip = None
    for inst in installers:
        m = SETUP_RE.search(inst.name)
        if only and m and not any(label_matches("Firefox" + m.group("v"), w) for w in only):
            continue
        key = (inst.name, inst.stat().st_size, int(inst.stat().st_mtime))
        if key in known:
            print(f"{inst.name}: ya descomprimido como {known[key]}")
            continue
        if sevenzip is None:
            sevenzip = find_7z(args)
            if not sevenzip:
                sys.exit("Necesito 7-Zip para descomprimir los instaladores. Instalalo (https://www.7-zip.org/) "
                         "o indica 7z.exe con --7z.")
        print(f"Descomprimiendo {inst.name} ...")
        try:
            label, msg = extract_setup(inst, browsers, sevenzip)
            print(f"  -> {label} ({msg})")
        except Exception as e:
            print(f"  ERROR con {inst.name}: {short(e, 300)}", file=sys.stderr)


def vtuple(v: str):
    return tuple(int(x) for x in v.split("."))


def index_geckodrivers(folder: Path):
    """{version: {arquitectura: zip}} con los geckodriver-vX.Y.Z-winNN.zip de la carpeta."""
    index = {}
    if folder.is_dir():
        for z in folder.iterdir():
            m = GECKO_ZIP_RE.match(z.name) if z.is_file() else None
            if m:
                index.setdefault(m.group("v"), {})[m.group("arch").lower()] = z
    return index


def pick_geckodriver(major, index, cache_dir: Path):
    """Elige y extrae el geckodriver para un Firefox. Devuelve (ruta|None, descripcion, problema|None)."""
    if not index:
        return None, "", "no hay zips de geckodriver (geckodriver-vX.Y.Z-win64.zip) en la carpeta --geckodrivers"
    note = ""
    if major is None:
        chosen = max(index, key=vtuple)
        note = " (version de Firefox desconocida: se usa el mas reciente)"
    else:
        cands = [v for v, mn, mx in GECKO_SUPPORT if mn <= major and (mx is None or major <= mx) and v in index]
        if not cands and BEST_EFFORT_RANGE[0] <= major <= BEST_EFFORT_RANGE[1]:
            cands = [v for v in index if vtuple(v) < (0, 17, 0)]
            note = " (fuera de la tabla oficial: mejor esfuerzo)"
        if not cands:
            needed = [v for v, mn, mx in GECKO_SUPPORT if mn <= major and (mx is None or major <= mx)]
            hint = (f"la tabla oficial admite: {', '.join(needed[:6])}" if needed
                    else "ninguna version de la tabla oficial lo admite")
            if BEST_EFFORT_RANGE[0] <= major <= BEST_EFFORT_RANGE[1]:
                hint = "para Firefox 48-51 sirve (sin garantias) un geckodriver anterior a 0.17"
            return None, "", f"no hay geckodriver compatible con Firefox {major} entre los zips disponibles; {hint}"
        chosen = max(cands, key=vtuple)
    prefs = ["win64", "win32", "win-aarch64"] if sys.maxsize > 2 ** 32 else ["win32", "win64", "win-aarch64"]
    arch = next(a for a in prefs if a in index[chosen])
    target = cache_dir / f"{chosen}-{arch}" / "geckodriver.exe"
    if not target.is_file():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(index[chosen][arch]) as zf:
                member = next((n for n in zf.namelist() if n.lower().endswith("geckodriver.exe")), None)
                if member is None:
                    return None, "", f"{index[chosen][arch].name} no contiene geckodriver.exe"
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        except Exception as e:
            return None, "", f"no se pudo extraer {index[chosen][arch].name}: {short(e, 200)}"
    return str(target), f"{chosen} ({arch}){note}", None


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def selenium_version_of(python: str):
    """Version de Selenium instalada en ese interprete, o None."""
    try:
        p = subprocess.run([python, "-c", "import selenium; print(selenium.__version__)"],
                           capture_output=True, text=True, timeout=60)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip().splitlines()[-1].strip()
    except Exception:
        pass
    return None


def ensure_legacy_python(args):
    """Interprete con Selenium 2.53.x para Firefox <= 47. Lo crea si hace falta. Devuelve (ruta|None, mensaje)."""
    if args.python_legacy:
        v = selenium_version_of(args.python_legacy)
        if v and v.startswith("2."):
            return args.python_legacy, f"Selenium {v}"
        return None, f"--python-legacy no tiene Selenium 2.x (encontrado: {v})"
    venv = Path(args.legacy_venv).resolve()
    py = venv_python(venv)
    if py.exists():
        v = selenium_version_of(str(py))
        if v and v.startswith("2."):
            return str(py), f"Selenium {v} ({venv})"
    if args.no_auto_setup:
        return None, f"no existe {venv}; crea el entorno o quita --no-auto-setup"
    print(f"Creando entorno para Firefox <= {LEGACY_MAX_MAJOR} en {venv} (instala {LEGACY_SELENIUM})...")
    try:
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=300)
        subprocess.run([str(py), "-m", "pip", "install", "--disable-pip-version-check", LEGACY_SELENIUM],
                       check=True, timeout=900)
    except Exception as e:
        return None, f"no se pudo crear el entorno legacy: {short(e, 300)}"
    v = selenium_version_of(str(py))
    if v and v.startswith("2."):
        return str(py), f"Selenium {v} ({venv})"
    return None, "el entorno legacy se creo pero Selenium 2.x no se importa"


# =========================================================================== procesos / RAM (principal)

def exe_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


class ProcessTracker(threading.Thread):
    """Muestrea en segundo plano la RAM (RSS sumado) de los procesos de ESTE firefox.exe creados durante la
    ejecucion, y recuerda tambien los descendientes del worker (geckodriver...) para limpiarlos al final."""

    def __init__(self, exe: Path, started: float, worker_pid: int, interval: float):
        super().__init__(daemon=True)
        self._exe = exe_key(exe)
        self._t_started = started
        self._worker_pid = worker_pid
        self._interval = interval
        self._stop_evt = threading.Event()
        self.samples = []   # (monotonic, rss_mb, nprocs)
        self.seen = {}      # pid -> Process (todo lo que habria que matar al final)

    def _firefox_procs(self):
        found = []
        for p in psutil.process_iter(["exe", "create_time"]):
            try:
                e = p.info.get("exe")
                if e and exe_key(e) == self._exe and (p.info.get("create_time") or 0) >= self._t_started - 1:
                    found.append(p)
            except Exception:
                continue
        return found

    def tick(self):
        procs = self._firefox_procs()
        rss = 0
        alive = 0
        for p in procs:
            self.seen[p.pid] = p
            try:
                rss += p.memory_info().rss
                alive += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        try:
            for c in psutil.Process(self._worker_pid).children(recursive=True):
                self.seen[c.pid] = c
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        if alive:
            self.samples.append((time.monotonic(), round(rss / (1024 * 1024), 1), alive))

    def run(self):
        while not self._stop_evt.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self._stop_evt.wait(self._interval)

    def stop(self):
        self._stop_evt.set()
        if self.is_alive():
            self.join(timeout=10)

    def stats(self, loaded_at):
        """ramMax/processCountMax: toda la ejecucion; ramAvg: solo tras la carga (estado estable)."""
        if not self.samples:
            return {"ramAvgMB": "", "ramMaxMB": "", "processCountMax": ""}
        after = [s for s in self.samples if loaded_at is not None and s[0] >= loaded_at]
        return {
            "ramAvgMB": round(sum(s[1] for s in after) / len(after), 1) if after else "",
            "ramMaxMB": max(s[1] for s in self.samples),
            "processCountMax": max(s[2] for s in self.samples),
        }

    def cleanup(self):
        """Mata restos: lo visto durante la ejecucion y cualquier proceso de este firefox.exe nuevo."""
        try:
            self.tick()
        except Exception:
            pass
        procs = list(self.seen.values())
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
        procs = root.children(recursive=True) + [root]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass
    try:
        psutil.wait_procs(procs, timeout=5)
    except Exception:
        pass


# =========================================================================== ejecucion de una medida (principal)

def run_once(args, python: str, label: str, binary: Path, engine: str, gecko, url: str, cache: str,
             work_dir: Path) -> dict:
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)
    job = {
        "engine": engine, "binary": str(binary), "geckodriver": gecko, "url": url, "cache": cache,
        "load_timeout": args.load_timeout, "ram_window": 0 if args.no_ram else args.ram_window,
        "start_timeout": args.start_timeout,
        "headless": args.headless, "prefs": {} if args.keep_default_prefs else QUIET_PREFS,
        "timing_js": TIMING_JS, "quiet_js": QUIET_JS,
    }
    job_path = work_dir / "job.json"
    job_path.write_text(json.dumps(job), encoding="utf-8")
    log_path = work_dir / "worker.log"

    out = {"status": "ok", "error": ""}
    lines = []
    marks = {"loaded_at": None}
    started = time.time()
    proc = None
    tracker = None
    reader = None
    rc = None
    try:
        with log_path.open("wb") as errlog:
            proc = subprocess.Popen([python, str(Path(__file__).resolve()), "--worker", str(job_path)],
                                    stdout=subprocess.PIPE, stderr=errlog, stdin=subprocess.DEVNULL,
                                    text=True, encoding="utf-8", errors="replace")

            def read_stdout():
                for line in proc.stdout:
                    line = line.rstrip("\r\n")
                    if line == "LOADED":
                        marks["loaded_at"] = time.monotonic()
                    lines.append(line)

            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()
            tracker = ProcessTracker(binary, started, proc.pid, POLL_SECONDS)
            if not args.no_ram:
                tracker.start()  # con --no-ram solo se usa para limpiar restos al final
            try:
                rc = proc.wait(timeout=args.run_timeout)
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid)
                out.update(status="timeout", error=f"el worker supero {args.run_timeout}s")
    except OSError as e:
        out.update(status="error", error=f"no se pudo lanzar el worker: {e}")
    finally:
        if reader:
            reader.join(timeout=10)
        if tracker:
            tracker.stop()
            out.update({"ramAvgMB": "", "ramMaxMB": "", "processCountMax": ""} if args.no_ram
                       else tracker.stats(marks["loaded_at"]))
            tracker.cleanup()
        out["wallSeconds"] = round(time.time() - started, 1)

    result = None
    fail = None
    for line in lines:
        if line.startswith("RESULT "):
            try:
                result = json.loads(line[7:])
            except ValueError:
                pass
        elif line.startswith("FAIL "):
            fail = line[5:]
    if out["status"] == "ok":
        if result is not None:
            out.update({k: v for k, v in result.items() if v is not None})
            if result.get("loadMs") is None:
                out.update(status="incomplete", error="load event no completado")
        elif fail:
            kind, _, msg = fail.partition(" ")
            out.update(status={"start": "driver_error", "timeout": "timeout"}.get(kind, "error"),
                       error=msg[:2000])
        else:
            tail = short(log_path.read_text(encoding="utf-8", errors="replace"), 1500) if log_path.exists() else ""
            out.update(status="error", error=f"el worker termino sin resultado (codigo {rc}): {tail}")
    return out


# =========================================================================== worker (corre en el interprete de cada motor)

def worker_emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def start_driver(job: dict, smajor: int):
    from selenium import webdriver
    binary = job["binary"]
    prefs = job["prefs"]
    if job["engine"] == "legacy":
        if smajor != 2:
            raise RuntimeError(f"el motor legacy necesita Selenium 2.x y este interprete tiene Selenium {smajor}.x")
        from selenium.webdriver.firefox.firefox_binary import FirefoxBinary
        from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
        profile = FirefoxProfile()
        for k, v in prefs.items():
            profile.set_preference(k, v)
        profile.update_preferences()
        return webdriver.Firefox(firefox_profile=profile, firefox_binary=FirefoxBinary(binary),
                                 timeout=job["start_timeout"])
    if smajor < 3:
        raise RuntimeError("el motor marionette necesita Selenium 3.x o 4.x; este interprete tiene Selenium 2.x")
    from selenium.webdriver.firefox.options import Options
    opts = Options()
    opts.binary_location = binary
    if job["headless"]:
        opts.add_argument("-headless")
    for k, v in prefs.items():
        opts.set_preference(k, v)
    gecko = job["geckodriver"]
    if smajor >= 4:
        # Selenium 4 envia siempre moz:debuggerAddress; geckodriver antiguos (p. ej. 0.17) la rechazan
        # con "Unrecognised option moz:debuggerAddress". No se usa para nada aqui.
        try:
            opts._caps.pop("moz:debuggerAddress", None)
        except Exception:
            pass
        from selenium.webdriver.firefox.service import Service

        class PlainService(Service):
            # Selenium 4 anade --websocket-port, que geckodriver antiguos (Firefox <~91) no conocen.
            def command_line_args(self):
                return ["--port", f"{self.port}"]

        return webdriver.Firefox(options=opts, service=PlainService(executable_path=gecko))
    return webdriver.Firefox(options=opts, executable_path=gecko or "geckodriver")


def worker_wait_quiet(driver, quiet_js: str, quiet_for: float = 2.0, timeout: float = 10.0) -> bool:
    start = time.time()
    last = None
    last_change = start
    while time.time() - start < timeout:
        try:
            count, state = str(driver.execute_script(quiet_js)).split("|")
        except Exception:
            return False
        now = time.time()
        if count != last or state != "complete":
            last = count
            last_change = now
        elif now - last_change >= quiet_for:
            return True
        time.sleep(0.25)
    return False


def worker_main(job_path: str) -> int:
    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)
    try:
        import selenium
        smajor = int(str(selenium.__version__).split(".")[0])
    except Exception as e:
        worker_emit(f"FAIL start no se pudo importar selenium: {short(e)}")
        return 2
    driver = None
    try:
        try:
            driver = start_driver(job, smajor)
        except Exception as e:
            worker_emit(f"FAIL start {type(e).__name__}: {short(e, 1500)}")
            return 2
        url = job["url"]
        try:
            driver.set_page_load_timeout(job["load_timeout"])
            driver.set_script_timeout(30)
        except Exception:
            pass
        try:
            if job["cache"] == "warm":  # primera carga descartada; la medida es la segunda, con cache caliente
                try:
                    driver.get(url)
                    worker_wait_quiet(driver, job["quiet_js"])
                except Exception as e:
                    sys.stderr.write(f"calentamiento fallido: {short(e, 300)}\n")
                driver.get("about:blank")
            t0 = time.time()
            driver.get(url)
            wall = round((time.time() - t0) * 1000)
            loaded = time.time()
            worker_emit("LOADED")
            quiet = worker_wait_quiet(driver, job["quiet_js"])
            result = json.loads(driver.execute_script(job["timing_js"]))
            result["navigationWallMs"] = wall
            result["networkQuiet"] = quiet
            remaining = job["ram_window"] - (time.time() - loaded)
            if remaining > 0:
                time.sleep(remaining)
            worker_emit("RESULT " + json.dumps(result))
            return 0
        except Exception as e:
            kind = "timeout" if type(e).__name__ == "TimeoutException" else "error"
            worker_emit(f"FAIL {kind} {type(e).__name__}: {short(e, 1500)}")
            return 3
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


# =========================================================================== resumen

def _num(v):
    if v in ("", None):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def summarize(rows):
    groups = {}
    for r in rows:
        groups.setdefault((r["version_label"], r["site"], r.get("cache", "")), []).append(r)
    out = []
    for (label, site, cache), rs in groups.items():
        measured = [r for r in rs if r.get("iteration")]
        ok = [r for r in measured if r.get("status") == "ok"]
        entry = {"version_label": label, "binary_version": rs[0].get("binary_version", ""),
                 "engine": rs[0].get("engine", ""), "site": site, "cache": cache,
                 "runs_ok": len(ok), "runs_total": len(measured), "note": ""}
        for key in METRIC_FIELDS:
            vals = [x for x in (_num(r.get(key)) for r in ok) if x is not None]
            entry[key] = round(statistics.median(vals), 1) if vals else ""
        if not ok:
            bad = [r for r in rs if r.get("status") != "ok"]
            entry["note"] = bad[0]["status"] if bad else ""
        out.append(entry)
    return out


def print_table(rows, cols) -> None:
    if not rows:
        return
    widths = [max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols]
    line = "  ".join(c.ljust(w) for c, w in zip(cols, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(w) for c, w in zip(cols, widths)))


# =========================================================================== main

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Benchmark de versiones de Firefox (10 en adelante) en Windows.")
    ap.add_argument("--worker", metavar="JOB_JSON", help=argparse.SUPPRESS)
    ap.add_argument("--setups", default="setups",
                    help="Carpeta con los instaladores completos de Firefox (Firefox Setup X.exe)")
    ap.add_argument("--geckodrivers", default="geckodrivers",
                    help="Carpeta con los zip geckodriver-vX.Y.Z-win64.zip (o win32 / win-aarch64)")
    ap.add_argument("--browsers", default="browsers",
                    help="Donde se descomprimen los instaladores (una subcarpeta por version; tambien vale "
                         "para versiones ya extraidas a mano: <version>\\firefox.exe)")
    ap.add_argument("--7z", dest="sevenzip", default=None, help="Ruta a 7z.exe si no esta en el PATH")
    ap.add_argument("--out", default="results.csv")
    ap.add_argument("--repeats", type=int, default=3, help="Medidas por web, modo de cache y ronda")
    ap.add_argument("--rounds", type=int, default=1, help="Repite toda la bateria N veces")
    ap.add_argument("--shuffle", action="store_true", help="Orden aleatorio de versiones en cada ronda")
    ap.add_argument("--seed", type=int, default=None, help="Semilla para --shuffle")
    ap.add_argument("--cache", choices=["warm", "cold", "both"], default=None,
                    help="cold: primera carga; warm: la misma URL cargada antes en la misma sesion; "
                         "both: ambas (por defecto: warm, o cold con --no-warm)")
    ap.add_argument("--no-warm", action="store_true",
                    help="Sin calentamiento ni cache caliente: solo la primera carga con cache vacia "
                         "(equivale a --cache cold)")
    ap.add_argument("--versions", default=None,
                    help="Versiones a ejecutar, separadas por comas (p. ej. 52,115.0esr,Firefox140.0)")
    ap.add_argument("--sites", default=None,
                    help="Webs, separadas por comas: google, youtube, meneame, URLs, nombre=url, y las paginas "
                         "locales de prueba: 'local' (todas) o local-text, local-photos, local-video-bg, "
                         "local-many-assets, local-js-heavy, local-table")
    ap.add_argument("--testpages", default="testpages", help="Carpeta de las paginas de prueba locales")
    ap.add_argument("--geckodriver", default=None,
                    help="Forzar este geckodriver.exe para todas las versiones >= 48 (ignora --geckodrivers)")
    ap.add_argument("--python-marionette", default=sys.executable,
                    help="Python con Selenium 4 (o 3) para Firefox >= 48 (por defecto, este)")
    ap.add_argument("--python-legacy", default=None, help="Python con Selenium 2.53.x para Firefox <= 47")
    ap.add_argument("--legacy-venv", default=".venv-legacy", help="Donde crear el entorno legacy automatico")
    ap.add_argument("--no-auto-setup", action="store_true", help="No crear el entorno legacy automaticamente")
    ap.add_argument("--legacy-max-major", type=int, default=LEGACY_MAX_MAJOR,
                    help="Ultima version que usa el motor legacy")
    ap.add_argument("--min-major", type=int, default=10, help="Versiones anteriores se marcan 'unsupported'")
    ap.add_argument("--load-timeout", type=int, default=90, help="Timeout de carga de pagina (s)")
    ap.add_argument("--start-timeout", type=int, default=90, help="Timeout de arranque del motor legacy (s)")
    ap.add_argument("--run-timeout", type=int, default=420, help="Timeout total de cada medida (s)")
    ap.add_argument("--ram-window", type=float, default=10,
                    help="Segundos de muestreo de RAM tras la carga (incluye la espera de 'quiet')")
    ap.add_argument("--no-ram", action="store_true",
                    help="No medir RAM ni esperar la ventana de muestreo (mas rapido); columnas ram* vacias")
    ap.add_argument("--headless", action="store_true", help="Sin ventana (solo Firefox >= 56; cambia el rendimiento)")
    ap.add_argument("--keep-default-prefs", action="store_true",
                    help="No aplicar las preferencias que reducen ruido (updates, telemetria, welcome...)")
    ap.add_argument("--work-dir", default="work", help="Carpeta temporal de las medidas")
    ap.add_argument("--keep-raw", action="store_true", help="Conservar job/log de las medidas ok")
    ap.add_argument("--pause", type=float, default=2.0, help="Pausa entre medidas (s)")
    ap.add_argument("--delimiter", default=",", help="Delimitador CSV (usa ';' para Excel en espanol)")
    args = ap.parse_args(argv)
    if args.worker:
        return args
    if args.repeats < 1 or args.rounds < 1:
        ap.error("--repeats y --rounds deben ser >= 1")
    if args.no_warm and args.cache in ("warm", "both"):
        ap.error(f"--no-warm es incompatible con --cache {args.cache}")
    if args.cache is None:
        args.cache = "cold" if args.no_warm else "warm"
    if args.geckodriver and not Path(args.geckodriver).is_file():
        ap.error(f"--geckodriver no existe: {args.geckodriver}")
    return args


def main(argv=None) -> int:
    global psutil
    args = parse_args(argv)
    if args.worker:
        return worker_main(args.worker)
    try:
        import psutil as _psutil
    except ImportError:
        sys.exit("Falta psutil: py -m pip install psutil")
    psutil = _psutil

    browser_root = Path(args.browsers).resolve()
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work_root = Path(args.work_dir).resolve()
    work_root.mkdir(parents=True, exist_ok=True)

    only = {v.strip() for v in args.versions.split(",") if v.strip()} if args.versions else None
    browser_root.mkdir(parents=True, exist_ok=True)
    extract_setups(args, browser_root, only)
    binaries = find_binaries(browser_root, only)
    if not binaries:
        sys.exit(f"No hay ningun Firefox utilizable: pon instaladores completos (.exe) en '{args.setups}' "
                 f"o carpetas <version>\\firefox.exe en '{browser_root}'.")
    httpd = None
    local_pages = {}
    if wants_local(args.sites):
        tp = Path(args.testpages).resolve()
        if not tp.is_dir():
            sys.exit(f"No existe la carpeta de paginas de prueba: {tp}")
        httpd, port = start_local_server(tp)
        local_pages = {f"local-{q.stem}": f"http://127.0.0.1:{port}/{q.name}"
                       for q in sorted(tp.glob("*.html")) if q.stem != "index"}
        print(f"Servidor local de paginas de prueba en http://127.0.0.1:{port}/ ({len(local_pages)} paginas)")
    sites = parse_sites(args.sites, local_pages)
    modes = ["cold", "warm"] if args.cache == "both" else [args.cache]

    versions = {}
    for label, binary in binaries:
        v, major = firefox_version(binary)
        if major is None:
            engine = "marionette"
        else:
            engine = "legacy" if major <= args.legacy_max_major else "marionette"
        versions[label] = (binary, v, major, engine)
        print(f"{label}: {v}  -> motor {engine}  ({binary})")

    # Interpretes de cada motor (solo si hay versiones que los usen y no estan fuera de rango).
    usable = [(l, vv) for l, vv in versions.items() if vv[2] is None or vv[2] >= args.min_major]
    engine_python = {}
    engine_problem = {}
    if any(vv[3] == "legacy" for _, vv in usable):
        py, msg = ensure_legacy_python(args)
        if py:
            engine_python["legacy"] = py
            print(f"Motor legacy: {msg}")
        else:
            engine_problem["legacy"] = msg
            print(f"AVISO motor legacy: {msg}", file=sys.stderr)
    if any(vv[3] == "marionette" for _, vv in usable):
        v = selenium_version_of(args.python_marionette)
        if v and not v.startswith("2."):
            engine_python["marionette"] = args.python_marionette
            print(f"Motor marionette: Selenium {v}")
        else:
            engine_problem["marionette"] = (f"{args.python_marionette} no tiene Selenium 3/4 "
                                            f"(encontrado: {v}); ejecuta: py -m pip install selenium")
            print(f"AVISO motor marionette: {engine_problem['marionette']}", file=sys.stderr)

    # geckodriver por version (solo Firefox >= 48 fuera de rango excluido).
    gecko_index = index_geckodrivers(Path(args.geckodrivers).resolve())
    gecko_cache = browser_root / ".geckodrivers"
    drivers = {}
    for label, vv in usable:
        binary, _, major, engine = vv
        if engine != "marionette":
            continue
        local = binary.parent / "geckodriver.exe"
        if args.geckodriver:
            drivers[label] = (args.geckodriver, f"forzado ({Path(args.geckodriver).name})", None)
        elif local.is_file():
            drivers[label] = (str(local), "junto a firefox.exe", None)
        else:
            drivers[label] = pick_geckodriver(major, gecko_index, gecko_cache)
        path, desc, problem = drivers[label]
        print(f"{label}: geckodriver {desc}" if path else f"AVISO {label}: {problem}",
              file=sys.stdout if path else sys.stderr)

    seed = args.seed if args.seed is not None else int(time.time())
    rng = random.Random(seed)
    if args.shuffle:
        print(f"Orden aleatorio, semilla {seed}")

    all_rows = []
    interrupted = False
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore", delimiter=args.delimiter)
        writer.writeheader()
        f.flush()

        def emit(row):
            writer.writerow(row)
            f.flush()
            all_rows.append(row)
            extra = f" | load={row['loadMs']}ms" if row.get("loadMs") not in (None, "") else ""
            err = f" | {row['error'][:200]}" if row.get("error") else ""
            print(f"{row['version_label']} | {row['site']} | {row['cache']} | r{row['round']} "
                  f"{row['iteration']}/{args.repeats} | {row['status']}{extra}{err}", flush=True)

        try:
            for rnd in range(1, args.rounds + 1):
                order = list(binaries)
                if args.shuffle:
                    rng.shuffle(order)
                for label, binary in order:
                    _, version, major, engine = versions[label]
                    gecko, gdesc, gproblem = drivers.get(label, (None, "", None))
                    for site_name, url in sites.items():
                        for cache in modes:
                            base = {"round": rnd, "version_label": label, "binary_version": version,
                                    "engine": engine, "geckodriver": gdesc, "site": site_name, "url": url,
                                    "cache": cache}
                            now = datetime.now(timezone.utc).isoformat()
                            if major is not None and major < args.min_major:
                                emit({**base, "timestamp_utc": now, "iteration": 0, "status": "unsupported",
                                      "error": f"Firefox {major} < --min-major {args.min_major}"})
                                continue
                            if engine in engine_problem:
                                emit({**base, "timestamp_utc": now, "iteration": 0, "status": "driver_error",
                                      "error": engine_problem[engine]})
                                continue
                            if gproblem:
                                emit({**base, "timestamp_utc": now, "iteration": 0, "status": "driver_error",
                                      "error": gproblem})
                                continue
                            for it in range(1, args.repeats + 1):
                                work = work_root / f"{slug(label)}_{slug(site_name)}_{cache}_r{rnd}_{it}"
                                row = {**base, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                                       "iteration": it}
                                row.update(run_once(args, engine_python[engine], label, binary, engine, gecko,
                                                    url, cache, work))
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
        print("\nMedianas por version, web y cache (solo medidas 'ok'):")
        print_table(summary, ["version_label", "engine", "site", "cache", "runs_ok", "runs_total", "loadMs",
                              "fcpMs", "transferBytes", "ramAvgMB", "ramMaxMB", "note"])
        print(f"\nResumen: {summary_path}")
    print(f"Terminado{' (interrumpido)' if interrupted else ''}. CSV: {out_path}")
    if httpd:
        httpd.shutdown()
    return 0 if any(r.get("status") == "ok" for r in all_rows) else 1


if __name__ == "__main__":
    sys.exit(main())
