# Cambios

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/). El proyecto aún no tiene versiones numeradas: cada entrada lleva la fecha y el commit.

## Sin publicar

### Añadido
- **Proveedor de LLM configurable** (`llm`): `ollama` (API nativa, por defecto), `openai` (cualquier servidor compatible: llama.cpp, LM Studio, LocalAI, vLLM, OpenVINO Model Server) o `ninguno` (solo reglas, para equipos con pocos recursos). `llm_url` y `llm_clave_env` (la clave se lee de una variable de entorno, nunca de `config.json`). Aviso al arrancar si el LLM está fuera del equipo.
- **Un sonido por estado** (`SONIDO_ESTADO`): escuchando, procesando, éxito, fallo, no escuché y ocupado. Al dejar de grabar la notificación pasa a «Procesando…»; antes seguía diciendo «Escuchando…» durante la transcripción.
- Con la palabra de activación, aviso visual en cuanto el filtro cree oír el nombre («¿Hades? Te escucho…»); suena solo cuando se confirma, para no sonar con cada falso candidato en una reunión. Si no era el nombre, la notificación se cierra.
- **Ninguna interacción termina en silencio**: «no escuché», «no entendí» y la confirmación sin respuesta suenan, lo explican y quedan en el log.
- El atajo pulsado mientras procesa ya no se pierde: avisa («Un momento…») y escucha en cuanto termina.
- **Palabra de activación** opcional (`palabra_activacion`, desactivada por defecto): «Oye Hades, abre kitty» de corrido o con pausa. Silero VAD + Whisper base en la CPU como filtro y confirmación con el Whisper principal. Funciona con OpenVINO y con faster-whisper.
- **Nombre configurable** (`nombre`): aparece en notificaciones, respuestas y en la pista de vocabulario de Whisper.
- Documentación: [arquitectura](docs/ARQUITECTURA.md), [cómo añadir acciones](docs/CONTRIBUIR.md) y este archivo.
- Repositorio protegido en GitHub: `main` sin force-push ni borrado, escaneo de secretos con bloqueo en el push, alertas y actualizaciones de seguridad de Dependabot.

### Corregido
- Pulsar el atajo mientras el servicio arrancaba lo mataba (`SIGUSR1` sin manejador durante la carga de los modelos). Ahora avisa «Arrancando…» y escucha en cuanto está listo.
- `notify-send` sin timeout podía bloquear el asistente si el servidor de notificaciones no respondía.
- OpenVINO: reintento en español cuando falla la detección de idioma (`Language token id -1 not found`).
- Con audio casi en silencio, Whisper «recitaba» la pista de vocabulario («VS Code, Visual Studio Code, Firefox») y se interpretaba como comando.

## 2026-10-16 — Nuevas acciones, Silero VAD y pruebas (`1893fbb`)

### Añadido
- **Acciones nuevas**: cerrar apps y ventanas; maximizar, minimizar, mosaico, otra pantalla y escritorios (atajos de KWin); terminal o lazygit en un proyecto; estado y último commit de git, con commits por subir y bajar; tareas de desarrollo definidas en `config.json`; leer, resumir, traducir y explicar el portapapeles; dictado al portapapeles; temporizadores con nota; brillo y luz nocturna; KDE Connect (hacer sonar el teléfono, batería, enviar el portapapeles); logs y reinicio de contenedores; diagnóstico de procesos, puertos y actualizaciones pendientes.
- **Silero VAD** para detectar la voz: distingue voz de música y ruido. Se mantiene el umbral de volumen como respaldo.
- Mientras escucha, el volumen del sistema baja al 20 %.
- Respuestas a preguntas **en streaming**: se leen oración por oración mientras Ollama genera.
- **Interrupción**: el atajo mientras habla la corta y vuelve a escuchar.
- **Confirmación por voz** para acciones sensibles (`confirmar`; por defecto, detener y reiniciar contenedores).
- Estado del sistema con uso y VRAM de la GPU, batería y red.
- Validación de `config.json` al arrancar.
- Acciones definidas en una tabla declarativa (`REGISTRO_ACCIONES`) en lugar de 5 sitios distintos.
- Pruebas automáticas (`pruebas/test_frases.py`): 132 comprobaciones de frases y salvaguardas, con y sin LLM.
- [`TODO.md`](TODO.md) con ideas y próximos pasos.

### Corregido
- «Reinicia la rapidez» reiniciaba el contenedor `api` (subcadena del nombre); ahora solo se aceptan nombres exactos, o parecidos si se dice «el contenedor».
- «Estado de la compu» o «de la batería» se interpretaban como estado de git.
- Interrumpir la voz no volvía a escuchar.
- En las respuestas largas, el streaming sobrescribía el audio que estaba sonando.
- Los temporizadores no entendían números en palabras («cinco minutos», «media hora») y una duración no válida ponía 1 minuto sin avisar.
- Con Ollama caído, una pregunta respondía «No te entendí» y sonaba el aviso de éxito.
- «¿Hay actualizaciones?» no contaba Fedora (dnf) ni el AUR.
- «Lee lo que copié» podía leer contraseñas en voz alta.
- «Cierra htop» cerraba con `pkill` todas las instancias, no solo sus ventanas.
- Si el servicio moría grabando, el volumen se quedaba al 20 %.
- «Pon algo de música» abría la terminal.

### Cambiado
- `instalar.sh` descarga Silero VAD desde la versión `v1.2.1` de faster-whisper, no desde `master`.

## 2026-10-15 — Apps y vocabulario sin reiniciar (`e2015f7`)

### Añadido
- Las apps se recargan al instalar, quitar o editar un `.desktop` (también Flatpak) o al instalar una herramienta de consola.
- La pista de vocabulario de Whisper se genera en cada grabación: los proyectos nuevos se reconocen sin reiniciar.
- El idioma detectado por Whisper desempata cuando la frase no tiene palabras clave («Spotify»).
- Soporte para más terminales: konsole, alacritty, gnome-terminal, ptyxis, wezterm y foot.

### Corregido
- El mensaje del perfil de energía decía «durante una hora»; el cambio es permanente.
- La lectura de temperaturas ya no falla con sensores que no responden.

## 2026-10-15 — Actualizar el sistema por voz (`00db0ba`)

### Añadido
- «Actualiza todo» abre la terminal con la actualización; `sudo` pide la contraseña ahí, el asistente nunca la ve. El comando se configura por equipo (`comando_actualizar`) o se detecta: `yay`/`paru`/`pacman` en Arch, `dnf` en Fedora, más `flatpak update`. `yay` y `paru` se ejecutan sin `sudo`.

## 2026-10-15 — GPU NVIDIA y Arch Linux (`664e0a2`)

### Añadido
- **GPU NVIDIA**: Whisper con faster-whisper (`large-v3-turbo`, `int8_float16`). CUDA 12 y cuDNN 9 vienen como paquetes de pip, sin depender del CUDA del sistema. Si CUDA falla, pasa a la CPU.
- **Arch Linux**: el instalador detecta la distribución (Fedora, Arch y derivadas) y la GPU, indica los paquetes que faltan y obtiene Python 3.13 con `uv` si no está.
- Dependencias separadas: `requirements-intel.txt` y `requirements-nvidia.txt`.
- Solo español o inglés (`idiomas`): el ruido ya no se transcribe como árabe.
- Temperatura de CPU AMD y de GPU NVIDIA.
- Aviso en el log cuando el audio del micrófono está saturado.

### Corregido
- El perfil de energía se lee de power-profiles-daemon: `/sys/firmware/acpi/platform_profile` no existe en todos los equipos.
- Los alias de apps admiten varios ids, porque cambian entre distribuciones y entre paquete nativo y Flatpak.
- El aviso de inicio ya no se graba junto con la voz.
- Palabras repetidas tras «abre» («Aure, Fire, Firefox»).

## 2026-10-14 — Primera versión (`b715d23`, `fc173e1`)

- Asistente de voz local para KDE Plasma en español e inglés: Whisper (OpenVINO), llama3.2 (Ollama) y Piper.
- Abrir apps y proyectos, Docker, volumen, música, estado del equipo, perfil de energía, búsqueda web, bloquear la pantalla y preguntas.
- Licencia GPL-3.0.
