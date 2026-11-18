#!/usr/bin/env python3
"""Genera las paginas de prueba locales en ..\\testpages (solo hace falta si quieres regenerarlas).
Requiere: pip install pillow numpy imageio-ffmpeg
HTML/CSS/JS deliberadamente conservadores (ES3/ES5, sin flex/grid obligatorios) para que carguen en Firefox 10.
"""
import math
import random
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
import imageio_ffmpeg

OUT = Path(__file__).resolve().parent.parent / "testpages"
OUT.mkdir(exist_ok=True)
rng = random.Random(7)

WORDS = ("rendimiento navegador pagina carga imagen video texto estilo script red cache memoria motor render "
         "diseno lectura sitio contenido tabla fuente color capa borde margen relleno enlace lista cabecera "
         "pie nota seccion articulo prueba medida tiempo version sistema proceso hilo evento marco ventana").split()


def sentence(n=None):
    n = n or rng.randint(8, 18)
    return " ".join(rng.choice(WORDS) for _ in range(n)).capitalize() + "."


def paragraph():
    return " ".join(sentence() for _ in range(rng.randint(4, 8)))


def page(title, body, head_extra="", body_attr=""):
    return ("<!DOCTYPE html>\n<html lang=\"es\"><head><meta charset=\"utf-8\"><title>%s</title>%s</head>\n"
            "<body%s>\n%s\n</body></html>\n" % (title, head_extra, body_attr, body))


BASE_CSS = ("<style type=\"text/css\">body{margin:0;font-family:Arial,Helvetica,sans-serif;color:#222;background:#fff}"
            "#w{max-width:900px;margin:0 auto;padding:24px}h1{font-size:28px}h2{font-size:20px;margin-top:28px}"
            "p{line-height:1.5}</style>")

# ---------------------------------------------------------------- text
body = "<div id=\"w\"><h1>Articulo de texto</h1>" + "".join(
    "<h2>%s</h2><p>%s</p><p>%s</p>" % (sentence(4)[:-1], paragraph(), paragraph()) for _ in range(40)) + "</div>"
(OUT / "text.html").write_text(page("text", body, BASE_CSS), encoding="utf-8")


# ---------------------------------------------------------------- photos
def photo(seed, w=1280, h=800):
    r = np.random.RandomState(seed)
    small = r.rand(8, 12, 3)
    img = Image.fromarray((small * 255).astype("uint8")).resize((w, h), Image.BICUBIC)
    arr = np.asarray(img).astype("float32")
    yy, xx = np.mgrid[0:h, 0:w]
    arr += (25 * np.sin(xx / (30 + seed % 7 * 9.0)) * np.cos(yy / (40 + seed % 5 * 11.0)))[..., None]
    arr += r.normal(0, 7, arr.shape)  # grano tipo foto
    img = Image.fromarray(np.clip(arr, 0, 255).astype("uint8")).filter(ImageFilter.GaussianBlur(0.8))
    return img


imgs = []
for i in range(12):
    name = "photo_%02d.jpg" % i
    photo(i + 1).save(OUT / name, quality=82, optimize=True)
    imgs.append(name)
body = "<div id=\"w\"><h1>Galeria de fotos</h1>" + "".join(
    "<p><img src=\"%s\" alt=\"foto %d\" style=\"width:100%%;max-width:860px;display:block\"></p>" % (n, i)
    for i, n in enumerate(imgs)) + "</div>"
(OUT / "photos.html").write_text(page("photos", body, BASE_CSS), encoding="utf-8")


# ---------------------------------------------------------------- video de fondo
W, H, FPS, SECS = 640, 360, 24, 6
frames = FPS * SECS
yy, xx = np.mgrid[0:H, 0:W].astype("float32")
raw = Path(tempfile.mkdtemp()) / "plasma.raw"
with open(raw, "wb") as f:
    for k in range(frames):
        t = 2 * math.pi * k / frames  # bucle perfecto
        v = (np.sin(xx / 70 + t) + np.sin(yy / 50 - t) + np.sin((xx + yy) / 90 + 2 * t)
             + np.sin(np.hypot(xx - W / 2, yy - H / 2) / 40 - t)) / 4
        frame = np.stack([128 + 110 * np.sin(v * 3.1), 128 + 110 * np.sin(v * 3.1 + 2.1),
                          128 + 110 * np.sin(v * 3.1 + 4.2)], axis=-1).astype("uint8")
        if k == 0:
            Image.fromarray(frame).save(OUT / "poster.jpg", quality=80)
        f.write(frame.tobytes())
ff = imageio_ffmpeg.get_ffmpeg_exe()
src = ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H), "-r", str(FPS), "-i", str(raw)]
for args, name in (
        (["-c:v", "libvpx", "-b:v", "900k", "-crf", "12", "-an", "-pix_fmt", "yuv420p"], "bg.webm"),
        (["-c:v", "libx264", "-crf", "28", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
          "-an"], "bg.mp4"),
        (["-c:v", "libtheora", "-q:v", "5", "-an", "-f", "ogg"], "bg.ogv")):
    subprocess.run([ff, "-y", "-loglevel", "error"] + src + args + [str(OUT / name)], check=True)
raw.unlink()
body = ("<div style=\"position:fixed;top:0;left:0;width:100%;height:100%;overflow:hidden;background:#123\">"
        "<video autoplay loop muted poster=\"poster.jpg\" style=\"min-width:100%;min-height:100%;width:auto;height:auto\">"
        "<source src=\"bg.webm\" type=\"video/webm\"><source src=\"bg.mp4\" type=\"video/mp4\">"
        "<source src=\"bg.ogv\" type=\"video/ogg\"></video></div>"
        "<div style=\"position:relative;color:#fff;text-align:center;padding-top:25%\">"
        "<h1 style=\"font-size:44px;margin:0\">Video de fondo</h1><p>Portada con video en bucle</p></div>")
(OUT / "video-bg.html").write_text(page("video-bg", body, "<style type=\"text/css\">body{margin:0;font-family:Arial}</style>"),
                                   encoding="utf-8")

# ---------------------------------------------------------------- muchos recursos pequenos
icons = []
for i in range(100):
    c = (rng.randint(30, 230), rng.randint(30, 230), rng.randint(30, 230))
    im = Image.new("RGB", (24, 24), c)
    for _ in range(6):
        x, y = rng.randint(0, 20), rng.randint(0, 20)
        im.paste((rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)), (x, y, x + 4, y + 4))
    name = "icon_%03d.png" % i
    im.save(OUT / name)
    icons.append(name)
heads = ""
for i in range(1, 4):
    (OUT / ("c%d.css" % i)).write_text(".b%d{border:1px solid #%d%d%d;padding:2px;margin:2px;float:left}\n" % (i, i, i * 2, i * 3) * 30,
                                       encoding="utf-8")
    heads += "<link rel=\"stylesheet\" type=\"text/css\" href=\"c%d.css\">" % i
scripts = ""
for i in range(1, 6):
    (OUT / ("s%d.js" % i)).write_text("var m%d = 0; function f%d(n){var a=0,i;for(i=0;i<n;i++){a+=i%%7;}return a;} m%d = f%d(20000);\n" % (i, i, i, i) * 8,
                                       encoding="utf-8")
    scripts += "<script type=\"text/javascript\" src=\"s%d.js\"></script>" % i
body = ("<div id=\"w\"><h1>Muchos recursos pequenos</h1><div>" + "".join(
    "<img class=\"b%d\" src=\"%s\" width=\"24\" height=\"24\" alt=\"\">" % (i % 3 + 1, n) for i, n in enumerate(icons))
    + "</div><div style=\"clear:both\"></div>" + scripts + "</div>")
(OUT / "many-assets.html").write_text(page("many-assets", body, BASE_CSS + heads), encoding="utf-8")

# ---------------------------------------------------------------- JS pesado
js = """
var t0 = new Date().getTime();
// 1) criba de primos
var N = 3000000, sieve = [], i, j, count = 0;
for (i = 0; i <= N; i++) { sieve[i] = 1; }
for (i = 2; i * i <= N; i++) { if (sieve[i]) { for (j = i * i; j <= N; j += i) { sieve[j] = 0; } } }
for (i = 2; i <= N; i++) { if (sieve[i]) { count++; } }
// 2) cadenas
var s = '', k;
for (k = 0; k < 150000; k++) { s += String.fromCharCode(97 + k % 26); }
var parts = s.split('a').join('-').length;
// 3) DOM
var box = document.getElementById('box'), frag = document.createDocumentFragment(), d;
for (k = 0; k < 4000; k++) {
  d = document.createElement('div');
  d.style.cssText = 'float:left;width:40px;height:20px;margin:1px;background:#' + ((k * 2654435) & 0xffffff).toString(16);
  d.appendChild(document.createTextNode(k));
  frag.appendChild(d);
}
box.appendChild(frag);
// 4) canvas (si existe)
var cv = document.getElementById('cv');
if (cv && cv.getContext) {
  var ctx = cv.getContext('2d');
  for (k = 0; k < 4000; k++) {
    ctx.fillStyle = 'rgb(' + (k % 255) + ',' + ((k * 7) % 255) + ',' + ((k * 13) % 255) + ')';
    ctx.fillRect((k * 37) % 600, (k * 17) % 300, 30, 20);
  }
}
document.getElementById('res').innerHTML = 'primos: ' + count + ' / ' + parts + ' / ms: ' + (new Date().getTime() - t0);
"""
body = ("<div id=\"w\"><h1>JavaScript pesado</h1><p id=\"res\">calculando</p><canvas id=\"cv\" width=\"640\" height=\"340\"></canvas>"
        "<div id=\"box\"></div><div style=\"clear:both\"></div><script type=\"text/javascript\">" + js + "</script></div>")
(OUT / "js-heavy.html").write_text(page("js-heavy", body, BASE_CSS), encoding="utf-8")

# ---------------------------------------------------------------- tabla grande (layout)
rows = "".join("<tr>" + "".join("<td style=\"border:1px solid #ccc;padding:2px 6px\">%s</td>" % rng.choice(WORDS)
                                for _ in range(8)) + "</tr>" for _ in range(1500))
body = "<div style=\"padding:12px\"><h1>Tabla grande</h1><table style=\"border-collapse:collapse;width:100%%\">%s</table></div>" % rows
(OUT / "table.html").write_text(page("table", body, "<style type=\"text/css\">body{font-family:Arial;font-size:13px}</style>"),
                                encoding="utf-8")

(OUT / "index.html").write_text(page("pruebas", "<div id=\"w\"><h1>Paginas de prueba</h1><ul>" + "".join(
    "<li><a href=\"%s.html\">%s</a></li>" % (n, n) for n in ("text", "photos", "video-bg", "many-assets", "js-heavy", "table"))
    + "</ul></div>", BASE_CSS), encoding="utf-8")
print("ok", OUT)
