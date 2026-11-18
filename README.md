# Firefox Benchmark (Windows) - Firefox 10 en adelante

Compara versiones de Firefox cargando webs reales y midiendo tiempos y memoria, con el MISMO método de medida para todas las versiones.

## Ficheros
- `benchmark.py`: programa principal.
- `benchmark_browsertime.py`: variante opcional con Browsertime, solo para versiones modernas (ver el final).

## Carpetas de entrada
```
setups\            instaladores COMPLETOS de Firefox:  "Firefox Setup 52.0.exe", "Firefox Setup 115.0esr.exe"...
geckodrivers\      zips oficiales:  geckodriver-v0.13.0-win64.zip, geckodriver-v0.17.0-win64.zip...
testpages\         páginas de prueba locales (ya incluidas, ver más abajo)
benchmark.py
```
- `setups\`: el script descomprime cada instalador con 7-Zip en `browsers\<etiqueta>\` (una sola vez; en las siguientes ejecuciones los reconoce y no repite el trabajo). La versión real se lee de `application.ini`, y de ahí sale la etiqueta (`Firefox52.0`, `Firefox115.0esr`). Si tienes un instalador de 32 y otro de 64 bits de la misma versión, el segundo lleva la arquitectura en la etiqueta (`Firefox52.0-x64`). No sirve el instalador web/stub (el pequeño "Firefox Installer.exe"): hace falta el instalador completo.
- `geckodrivers\`: acepta `geckodriver-vX.Y.Z-win64.zip`, `-win32.zip` y `-win-aarch64.zip` (prefiere win64). Los extrae a `browsers\.geckodrivers\` cuando hacen falta.
- También sirven carpetas ya extraídas a mano en `browsers\<etiqueta>\firefox.exe`.

## Cómo elige el motor y el geckodriver
Cada medida arranca un Firefox nuevo con un perfil nuevo.

| Firefox | Motor | Qué usa |
|---|---|---|
| 10 a 47 | `legacy` | Selenium 2.53.6 (FirefoxDriver, extensión). El script crea solo `.venv-legacy` e instala `selenium==2.53.6` (necesita internet la primera vez). No usa geckodriver. |
| 48 en adelante | `marionette` | Selenium 4 + el geckodriver de `geckodrivers\` que corresponda. |

Para Firefox >= 48 se elige, entre los zips disponibles, el geckodriver MÁS NUEVO que la tabla oficial de soporte de geckodriver declare compatible con esa versión. Ejemplos de esa tabla: 0.17.0 para Firefox 52–62; 0.29.1 para 60–90; 0.31.0 para 91–120; 0.37.1 y 0.34 a 0.36 para 115 en adelante. Al arrancar se imprime qué geckodriver usa cada versión (y la columna `geckodriver` del CSV lo guarda).
- Firefox 48 a 51: no entran en la tabla oficial (empieza en la 52). Se prueba con el geckodriver anterior a 0.17 más nuevo que tengas (por ejemplo el 0.13.0), marcado "mejor esfuerzo" y sin garantías.
- Si no hay ningún geckodriver compatible entre tus zips, esa versión queda como `driver_error` con un mensaje que dice qué versiones admite la tabla, y el script sigue con las demás. Selenium NO descarga drivers por su cuenta.
- Un `geckodriver.exe` colocado junto a `firefox.exe` tiene prioridad sobre la carpeta; `--geckodriver RUTA` fuerza uno para todas las versiones >= 48.

## Preparación
1. Python 3.9+ y `py -m pip install -r requirements.txt`.
2. Instala 7-Zip (https://www.7-zip.org/). Se busca en el PATH y en `Program Files\7-Zip`; si está en otro sitio usa `--7z RUTA`.
3. Rellena `setups\` y `geckodrivers\`.

## Ejecución
`py benchmark.py --out results.csv --repeats 3`

Opciones útiles:
- `--setups`, `--geckodrivers`, `--browsers`: cambian las carpetas (por defecto `setups`, `geckodrivers`, `browsers`).
- `--versions 52,115.0esr`: solo esas versiones (`52` coincide con 52.0 y 52.0.2; no con 520). También evita descomprimir las demás.
- `--cache cold|warm|both` (por defecto `warm`, o `cold` con `--no-warm`): `cold` = primera carga; `warm` (defecto) = la misma URL se carga antes en la misma sesión y se mide la segunda; `both` = ambas.
- `--rounds 3 --shuffle [--seed N]`: repite la batería y baraja el orden de versiones.
- `--sites google,youtube`, `--sites midominio=https://example.org/`, o las páginas locales: `--sites local` (todas) o `--sites local-text,local-photos`.
- `--no-warm`: sin calentamiento ni caché caliente; solo la primera carga con caché vacía (navegador y perfil nuevos en cada medida). Equivale a `--cache cold`, y es incompatible con `--cache warm|both`.
- `--no-ram`: no mide RAM ni espera la ventana de muestreo (`ramAvgMB`, `ramMaxMB` y `processCountMax` quedan vacías). `--no-warm --no-ram` es la ejecución más rápida.
- `--python-legacy RUTA` / `--no-auto-setup`: usa tu propio Python con Selenium 2.53.x en vez de crear `.venv-legacy`. `--python-marionette RUTA`: Python con Selenium 4 (por defecto, el que ejecuta el script).
- `--delimiter ";"`: CSV listo para Excel en configuración regional española.
- `--legacy-max-major 47`, `--min-major 10`, `--ram-window`, `--load-timeout`, `--start-timeout`, `--run-timeout`, `--pause`, `--headless` (solo Firefox >= 56), `--keep-default-prefs`, `--keep-raw`.

Un fallo en una versión (instalador que no se descomprime, no arranca, timeout, sin geckodriver) NO detiene la ejecución: queda en `status`/`error` y sigue. Las medidas fallidas conservan su log en `work\<medida>\worker.log`.

## Páginas de prueba locales (`--sites local`)
Las webs públicas actuales exigen HTTPS moderno y la mayoría no cargará en Firefox muy antiguo, así que para comparar desde la versión 10 lo fiable es servir las páginas tú mismo. `testpages\` trae seis, escritas con HTML/CSS/JS conservador (ES3/ES5) para que carguen en Firefox 10. El script las sirve por HTTP en `127.0.0.1` (con caché HTTP, para que `warm` signifique algo, y soporte de `Range` para el vídeo en bucle):

| Página | Qué ejercita |
|---|---|
| `local-text` | Solo texto: línea base de red, parseo y layout sencillo. |
| `local-photos` | 12 fotos JPEG de 1280x800: decodificación y memoria de imágenes. |
| `local-video-bg` | Vídeo de fondo a pantalla completa en bucle, con WebM (VP8), MP4 (H.264) y OGV (Theora) para que cada versión use el formato que soporte. |
| `local-many-assets` | 100 iconos, 3 CSS y 5 JS: muchas peticiones pequeñas. |
| `local-js-heavy` | Criba de primos, cadenas, 4000 nodos DOM y canvas: velocidad del motor JavaScript. |
| `local-table` | Tabla de 1500 filas: layout y reflow de un DOM grande. |

Si un navegador no soporta ninguno de los formatos de vídeo, la página carga igual (sin vídeo). `tools\make_testpages.py` regenera todo (necesita `pip install pillow numpy imageio-ffmpeg`). Mientras se mide, el servidor corre en el mismo proceso del script: el efecto es pequeño, pero no lo uses para nada más.

## Límites que debes conocer
- Probado solo con instaladores, 7-Zip y drivers simulados, no con Firefox reales. En particular: que 7-Zip extraiga bien cada instalador antiguo (el script reintenta con el `setup.exe` interno si hace falta, pero no puedo garantizarlo para todas las versiones) y que el FirefoxDriver de Selenium 2.53.6 (declara compatibilidad de Firefox 3.0 a 48.0) arranque en cada Firefox antiguo.
- Firefox 43 a 47 estables exigen extensiones firmadas; el script desactiva esa comprobación por preferencia, pero eso solo funciona en ESR, Developer y Nightly. Si una estable 43–47 no arranca con el motor legacy, esa es la causa más probable.
- Es muy probable que las versiones antiguas no puedan negociar TLS con las webs actuales (Google, YouTube...). Se registrará como `error` o `timeout`, no como un tiempo de carga. Para esas versiones conviene medir páginas servidas por HTTP o por un servidor local.
- Las métricas dependen de lo que exponga cada versión: Navigation Timing existe desde Firefox 7, pero `fcpMs` y las métricas de recursos solo en versiones recientes (si no, quedan vacías).
- Entre versiones muy distintas, "la misma web" no es la misma página (cada Firefox recibe contenido distinto). Úsalo como orientación.

## Interpretación (results.csv y results_summary.csv)
- `engine`: `legacy` o `marionette`. `geckodriver`: versión usada. `status`: `ok`, `incomplete` (el load no terminó), `timeout`, `error`, `driver_error` (no arrancó o falta driver) o `unsupported` (< `--min-major`). El resumen (medianas por versión, web y caché) solo usa filas `ok`.
- `domContentLoadedMs`, `loadMs` (fin del evento load), `responseEndMs`, `ttfbMs` (inicio de respuesta), `fcpMs`: relativos al inicio de la navegación; vacío si la versión no los expone.
- `resourceCount` / `transferBytes`: Resource Timing (cota inferior: recursos de otros orígenes sin `Timing-Allow-Origin` y servidos desde caché cuentan 0). `resourceBufferFull`: 250 o más recursos, el límite por defecto del buffer; en ese caso las cifras pueden estar truncadas.
- `navigationWallMs`: tiempo de `driver.get()`. `networkQuiet`: mejor esfuerzo (documento completo y sin recursos nuevos durante 2 s).
- `ramAvgMB`: RSS medio sumado de los procesos de ese `firefox.exe` desde que termina la carga hasta el final de la ventana. `ramMaxMB` y `processCountMax`: máximo durante toda la medida, arranque incluido. No uses ese mismo `firefox.exe` para otra cosa mientras se mide.
- `wallSeconds`: duración total de la medida (arranque incluido; no es un tiempo de carga). `finalUrl`: URL final; si una web redirige (p. ej. a un consentimiento de cookies), mides esa página.

No compares resultados de máquinas, resoluciones, conexiones o estados de caché distintos. Ejecuta varias rondas (`--rounds`, `--shuffle`) y evita actividad en segundo plano.

## benchmark_browsertime.py (opcional, solo versiones modernas)
Usa Browsertime (Node.js 22+, `npm install -g browsertime`) y añade LCP y el informe de memoria de Firefox. Trabaja con carpetas ya extraídas (`browsers\<etiqueta>\firefox.exe`, que puedes generar con `benchmark.py`) y un `geckodriver.exe` en cada carpeta o con `--geckodriver`. Como mide de otra forma y Browsertime solo funciona con Firefox recientes, no mezcles sus resultados con los de `benchmark.py` en una misma comparación.
