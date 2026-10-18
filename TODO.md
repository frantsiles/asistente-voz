# Próximos ajustes

Ideas y pendientes para el asistente, ordenados por bloque. Cada uno indica **prioridad** (🔴 alta · 🟡 media · ⚪ baja) y **esfuerzo** (S pequeño · M medio · L grande).

Regla de diseño que se mantiene en todo: **solo acciones de una lista blanca**, y una acción que propone el LLM solo se acepta si la frase tiene palabras que la respalden (`EVIDENCIA`). Las acciones que no se pueden deshacer piden confirmación.

---

## 1. Fallos detectados en las pruebas

- [x] 🔴 S — **«Pon algo de música» abre kitty.** El LLM elige `abrir_app` y la evidencia lo acepta porque `pon` cuenta como verbo de abrir. Opciones: quitar `pon` de `EVIDENCIA["abrir_app"]`, exigir en `interpretar()` que `resolver_app(param)` exista al aceptar `abrir_app` del LLM, y añadir regla rápida «pon/reproduce música» → `multimedia("reproducir")` (o abrir reproductor si no hay ninguno activo en MPRIS).
- [x] 🔴 S — **Nombres parciales y sufijos en `cargar_apps` / `resolver_app` (caso «Abre Heroic Games»).** El `.desktop` se llama `Heroic Games Launcher` (`heroicgameslauncher`), por lo que `"heroicgames"` da ratio `0.73 < 0.82` en `difflib` y falla. Además, `id.split(".")[-1]` en `org.telegram.desktop` registra la clave genérica `"desktop"` apuntando a Telegram. Solución:
  - Indexar también el nombre sin sufijos genéricos (`Launcher`, `Desktop`, `Client`, `Browser`, `Player`).
  - Ignorar segmentos genéricos (`desktop`, `app`, `client`) al indexar `id.split(".")[-1]`.
- [x] 🔴 S — **Apps de usuario en Waydroid (caso «Abre Legión Anime Full»).** `cargar_apps()` descarta todo `id.startswith("waydroid.")` para que las apps base de Android (`calculator2`, `settings`) no pisen a las nativas de KDE, pero eso oculta apps instaladas por el usuario (`waydroid.aplicaciones.paleta.legionanimefull`, `waydroid.com.mojang.minecraftpe`). Solución: ignorar solo `waydroid.com.android.*`, `waydroid.org.lineageos.*` y `waydroid.com.google.android.*` (o cargar Waydroid al final con `setdefault`).
- [x] 🔴 S — **Transcripciones de herramientas de consola y editores (`btop`, `KWrite`).** En pruebas reales Whisper transcribió `btop` como «Pit Top» y `KWrite` como «grite». Añadir `btop` y `nvtop` a `pista_whisper()` y agregar alias fonéticos (`pittop`, `betop`, `bitop` → `btop`; `grite`, `krite` → `org.kde.kwrite`).
- [x] 🔴 S — **No existe «Cierra X» / «Close X».** Cerrar de forma normal (como la X de la ventana), solo apps conocidas:
  - Para «cierra la ventana»: `busctl --user call org.kde.kglobalaccel /component/kwin org.kde.kglobalaccel.Component invokeShortcut s "Window Close"`.
  - Para «cierra `<app>`»: micro-script de KWin vía `busctl --user call org.kde.KWin /Scripting org.kde.kwin.Scripting loadScript` que busque la ventana por `desktopFileName` / `resourceClass` y llame a `window.closeWindow()` (nativo en Wayland, sin `kdotool` ni `kill -9`); como último recurso `pkill -TERM` del ejecutable del `.desktop`.
- [x] 🟡 S — **La pista de vocabulario empeora algunas frases**: «Cierra Spotify» → «Sierra Spotify» solo con la pista (`CIERRA_MAL_OIDO` corrige «Sierra X» → «cierra X»).
- [x] 🟡 S — **Con música sonando, la grabación no termina** (el ruido de fondo no baja del umbral, hasta 7 s). Bajar el volumen del sistema mientras escucha (*ducking*): guardar el volumen, ponerlo al 20 % con `wpctl` y restaurarlo al terminar.
- [ ] 🟡 S — **Probar en la Dell** después de `git pull && ./instalar.sh`: OpenVINO en GPU Intel, `comando_actualizar` con dnf, idioma detectado = `None`.

## 2. Voz e interacción

- [x] 🟡 M — **Detección de voz con Silero VAD** en lugar del umbral RMS. Distingue voz de música y ruido y procesa bloques de 30 ms en `< 0,5 ms`. En NVIDIA ya viene incluido en `faster_whisper.vad`; en Intel/OpenVINO `piper-tts` ya instala `onnxruntime` en el `venv`, por lo que el mismo `silero_vad.onnx` (~1,8 MB) corre en ambos motores sin dependencias nuevas.
- [x] 🟡 S — **Interrumpir la respuesta hablada (*barge-in*)**: guardar el proceso `Popen` de `pw-play` en `Voz`; si llega `SIGUSR1` mientras Piper habla, hacer `terminate()` de `pw-play` y pasar directamente a escuchar.
- [x] 🟡 M — **Respuestas en streaming**: pedir a Ollama `stream: true` y sintetizar con Piper oración por oración (cortando en `. `, `? `, `! `, `\n`). La primera frase de una pregunta suena en ~0,5 s en vez de esperar toda la respuesta.
- [x] 🟡 S — **Confirmación por voz de 1 paso para acciones sensibles**: guardar `self.pendiente = (accion, param, expira)` en `Asistente`; el asistente pregunta «¿Confirmas…?» y abre escucha 4 s para aceptar «sí / confirma / yes» o cancelar.
- [ ] ⚪ M — **Conversación de seguimiento**: después de responder una pregunta, escuchar 5 s más sin atajo (opcional en `config.json`) y mandar a Ollama el turno anterior como contexto («¿y en Python?»).
- [x] ⚪ L — **Palabra de activación** opcional («Oye Nova»), desactivada por defecto: Silero VAD + Whisper base como filtro y confirmación con el Whisper principal. Alternativa a evaluar: [openWakeWord](https://github.com/dscripka/openWakeWord) (Apache-2.0, permite entrenar una palabra propia y gasta menos CPU).

## 3. Escritorio KDE (vía `busctl --user`, sin paquetes extra)

- [x] 🔴 M — **Control de ventanas**: «maximiza», «minimiza», «manda esto a la otra pantalla», «escritorio 2». Usar `busctl --user` (que está en Arch y Fedora, a diferencia de `qdbus6`/`qdbus-qt6`):
  `busctl --user call org.kde.kglobalaccel /component/kwin org.kde.kglobalaccel.Component invokeShortcut s "Window Maximize"`. Lista blanca de atajos permitidos.
- [x] 🟡 S — **Portapapeles nativo con Klipper (sin `wl-paste`)**: KDE Plasma 6 expone `org.kde.klipper` por D-Bus de serie (`busctl --user call org.kde.klipper /klipper org.kde.klipper.klipper getClipboardContents`). Permite «lee lo que copié», «resume / traduce lo que copié» o «explica esto» sin instalar `wl-clipboard`.
- [x] 🟡 S — **Temporizadores y recordatorios**: «avísame en 10 minutos», «recuérdame la reunión a las 3». Con `systemd-run --user --on-active=` (sobrevive a reinicios del servicio) que lanza `notify-send` + `pw-play`.
- [x] 🟡 S — **Brillo y Luz Nocturna por D-Bus (sin `brightnessctl`)**:
  - Brillo: `busctl --user call org.kde.Solid.PowerManagement /org/kde/Solid/PowerManagement/Actions/BrightnessControl org.kde.Solid.PowerManagement.Actions.BrightnessControl setBrightness i <valor>` (o leyendo `brightnessMax`).
  - Luz nocturna: `org.kde.KWin.NightLight` (`inhibit` / `uninhibit`).
  - Red y Bluetooth: `nmcli` y `bluetoothctl` (ya instalados).
- [x] 🟡 S — **Integración con KDE Connect (`kdeconnect-cli` ya instalado)**:
  - «¿Dónde está mi teléfono?» / «Haz sonar mi teléfono» → `kdeconnect-cli -d <id> --ring`.
  - «Batería del teléfono» → lectura por D-Bus de `org.kde.kdeconnect`.
  - «Manda el portapapeles al teléfono» → `kdeconnect-cli --share-text`.
- [x] 🟡 M — **Dictado**: «escribe …» teclea el texto en la ventana activa. En KDE/Wayland `wtype` no funciona; usar `kdotool`/`wdotool` o `ydotool` (necesita acceso a `/dev/uinput`), o bien copiar al portapapeles de Klipper y enviar `Ctrl+V`.
- [x] 🟡 M — **Redactar**: «redacta un correo para…», «escríbeme una función que…» → el LLM escribe el texto o el código y lo deja en el portapapeles.
- [x] 🟡 M — **Imágenes con Fooocus / OpenVINO y superresolución x4**: «genera una imagen de…» y «mejora esta imagen» (seleccionada en el explorador o en el portapapeles) con 3 motores (`realesrgan`, `openvino`, `realesrgan_x4plus`).
- [ ] ⚪ M — **Mejorar la superresolución en fotografías reales y retratos**: evaluar restauración facial en ONNX (`GFPGANv1.4` / `CodeFormer`) o modelos específicos para fotos comprimidas (`Swin2SR` / `Real-HAT-GAN`) combinados con `escalar_imagen`.
- [ ] ⚪ M — **«¿Qué hay en pantalla?»**: captura con Spectacle (`spectacle -b -n -o`) y descripción con un modelo de visión local. Para texto en pantalla, `qwen2.5vl` funciona mejor que `llama3.2-vision`; con 6 GB de VRAM, la versión 3B. Descargar el modelo de voz o de visión según haga falta (`keep_alive: 0` tras responder).
- [x] ⚪ S — **Suspender / reiniciar / apagar** con confirmación por voz («¿seguro?» → «sí»).

## 4. Desarrollo

- [x] 🔴 S — **Tareas de proyecto de una lista blanca** en `config.json`: `"tareas": {"tests de validaciones": "dotnet test ~/Proyectos/X"}`. Se ejecutan en la terminal con `cmd_terminal()`, como la actualización. Nada fuera de la lista.
- [x] 🟡 S — **Git por voz**: «estado del proyecto X» (rama actual, cambios sin guardar, commits por subir con `git -C <repo> status -sb`), «último commit de X». Solo lectura; los `pull`/`push` abren la terminal.
- [x] 🟡 S — **Abrir terminal o lazygit en un proyecto**: «abre lazygit en validaciones» o «abre la terminal en el proyecto X» reutilizando `buscar_proyecto()`.
- [x] 🟡 S — **Docker con más detalle**: «logs de X» (abre `docker logs -f` en la terminal), «reinicia X» (solo contenedores del compose configurado).
- [x] 🟡 S — **Diagnóstico de puertos locales**: «¿qué corre en el puerto 5000?» o «¿qué puertos tengo abiertos?» usando `ss -tulpn` (solo lectura) para identificar rápido procesos de desarrollo (`node`, `dotnet`, `python`, `postgres`).
- [x] ⚪ M — **Preguntar sobre el portapapeles / selección**: «explica el código que copié» toma el contenido de Klipper y lo manda con la pregunta a Ollama.

## 5. Sistema

- [x] 🟡 S — **«¿Qué está consumiendo CPU / memoria?»**: lee los 2–3 procesos con mayor uso (`ps -eo comm,%cpu,rss --sort=-%cpu`) y responde en una frase («Firefox usa 35 por ciento de procesador y 2,4 gigas de memoria»). Muy útil cuando suben la temperatura o los ventiladores.
- [x] 🟡 S — **Avisar cuántas actualizaciones hay** sin sudo: `checkupdates` + `yay -Qua` en Arch, `dnf check-update` en Fedora. «¿Hay actualizaciones?» → «Hay 23 paquetes por actualizar».
- [x] 🟡 S — **Estado más completo**: uso y VRAM de la GPU (`nvidia-smi`), batería (`/sys/class/power_supply/BAT0/capacity` y `status`), red activa.
- [x] ⚪ S — **Limpieza**: «limpia la caché de paquetes» → `paccache -r` / `yay -Sc` / `dnf clean` en la terminal.

## 6. Arquitectura y calidad

- [x] 🔴 M — **Pruebas automáticas de frases**: un archivo `pruebas/frases.json` con frase → `(accion, parametro)` esperado (incluyendo transcripciones reales con errores: «Aure, Fire, Firefox», «ahora es Spotify», «Abre Pit Top», «Pon algo de música») y un script/test que valide `interpretar()` y `resolver_app()` sin micrófono ni GPU. Evita romper reglas al añadir otras.
- [x] 🟡 M — **Tabla declarativa de acciones**: unificar cada acción en una estructura/registro (reglas rápidas, regex de `EVIDENCIA`, descripción para `PROMPT_COMANDOS`, textos `es`/`en` y función ejecutora) para no tocar 5 sitios distintos de `asistente.py` al añadir un comando.
- [x] 🟡 S — **Validar `config.json` al arrancar**: claves desconocidas, rutas inexistentes (`proyectos`, `compose`), terminal sin instalar → aviso claro en el log al iniciar el servicio.
- [ ] ⚪ M — **Probar un modelo con mejor *tool calling*** para los comandos (p. ej. `qwen3` / `qwen2.5:3b`), midiendo tiempo y VRAM: con 6 GB, Whisper (1,2 GB) + LLM tienen que caber juntos.
- [ ] ⚪ L — **Servidor MCP local** que exponga las mismas acciones de la lista blanca, para usarlas también desde otros clientes de IA.

## 7. Seguridad

Lo que ya está cubierto: el LLM solo elige acciones de la lista blanca (esquema JSON), cada acción necesita evidencia en la frase, los parámetros se validan contra lo que existe, el texto dictado nunca pasa por un shell, `sudo` se escribe en una terminal visible y solo escucha al pulsar el atajo. Detalle en [docs/ARQUITECTURA.md](docs/ARQUITECTURA.md#capas-de-seguridad).

- [x] 🔴 L — **Auditoría completa**: recorrer cada camino de ejecución (`tareas`, `comando_actualizar`, scripts de KWin, llamadas D-Bus, KDE Connect, Docker) buscando inyección de comandos, rutas y permisos de archivos. Corregir lo que aparezca, añadir pruebas y documentarlo en `SECURITY.md` (también cómo reportar problemas).
- [x] 🟡 S — **`buscar_web`**: la frase se pasa a `firefox --search`; si empezara por `-`, Firefox podría tomarla como opción. Añadir `--` o quitar los guiones iniciales.
- [x] 🟡 S — **Archivos temporales**: si falta `XDG_RUNTIME_DIR` (al ejecutarlo a mano), el script de KWin y los WAV se escriben en `/tmp`, donde otro usuario podría crearlos antes. Usar `tempfile.mkdtemp()` como respaldo o negarse a arrancar.
- [x] 🟡 M — **Dependencias y modelos verificados**: `requirements*.txt` fija versiones pero no hashes (`pip install --require-hashes`), y los modelos de Hugging Face se descargan sin fijar `revision`.
- [x] ⚪ S — **Portapapeles con instrucciones**: probar a propósito que un texto copiado con órdenes («ignora lo anterior y…») no cambia nada al resumirlo o traducirlo. Hoy la respuesta solo se lee en voz alta, pero no hay prueba que lo garantice.
- [x] ⚪ S — **Textos que empiezan por `-` en `notify-send`**: una respuesta del LLM que empiece por `--` se tomaría como opción. Pasar `--` antes del título y el cuerpo.
- [x] 🟡 M — **CI**: GitHub Actions que corra `pruebas/test_frases.py` en cada push (sin GPU ni LLM), para que una regla rota no llegue a `main`.

---

## Fuentes

- [openWakeWord](https://pypi.org/project/openwakeword) — palabra de activación local
- [Vocalinux](https://phoronix.com/news/Vocalinux-0.14-Beta) y [herramientas de dictado en Linux 2026](https://openwhispr.com/blog/best-dictation-tools-linux-2026) — dictado offline en X11/Wayland
- [Ved](https://news.ainauten.com/en/story/show-hn-ved-ai-voice-assistant) — asistente local con faster-whisper + LLM + Piper e interrupción
- [Atajos de KWin por D-Bus](https://www.linuxjournal.com/node/1341039) y [scripts de KWin en Plasma 6](https://discuss.kde.org/t/does-kwin-emit-a-signal-via-dbus-on-window-change/43923)
- [wtype no funciona en KWin](https://www.mankier.com/1/wtype), [wdotool](https://github.com/cushycush/wdotool) y [ydotool en KDE](https://discuss.kde.org/t/voxd-and-ydotool-incorporation-in-kde-linux/40644) — escribir en la ventana activa
- [Comparativa de modelos de visión](https://docs.photoprism.app/developer-guide/vision/model-comparison/) — Qwen2.5-VL frente a Llama 3.2 Vision
- [Interrupción y latencia en asistentes de voz](https://www.youngju.dev/blog/ai-platform/2026-03-08-ai-platform-realtime-voice-chatbot-barge-in-guide.en) — Silero VAD, presupuesto de ~100 ms
- [Temporizadores de Home Assistant Assist](https://www.home-assistant.io/blog/2024/12/19/voice-chapter-8-assist-in-the-home/)
- [Talon y otras opciones de programación por voz](https://www.shervinemami.com/speechRecognition.html)
