"""Asistente de voz local para KDE (español e inglés).

Flujo: atajo de teclado -> graba hasta que dejas de hablar -> Whisper (OpenVINO en Intel o
faster-whisper en NVIDIA, detecta el idioma) -> reglas rápidas o un LLM (Ollama u otro servidor) -> acción de una lista blanca ->
respuesta hablada con Piper en el mismo idioma + notificación.

Se activa enviando SIGUSR1 al servicio (eso hace el atajo):
    systemctl --user kill -s USR1 --kill-whom=main asistente-voz
Una segunda pulsación mientras escucha deja de grabar en ese momento.
Con `palabra_activacion` en config.json también se activa diciendo su nombre ("Oye Nova, abre kitty").

Logs: journalctl --user -u asistente-voz -f
"""
import collections
import configparser
import difflib
import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from typing import NamedTuple

# Sin manejador, SIGUSR1 (el atajo) mata el proceso. Cargar los modelos tarda ~7 s, así que se registra
# uno provisional antes de nada: la pulsación se recuerda y se atiende en cuanto el asistente está listo.
_PULSACION_AL_ARRANCAR = threading.Event()


def _senal_al_arrancar(*_):
    if not _PULSACION_AL_ARRANCAR.is_set():
        subprocess.Popen(["notify-send", "-a", "Asistente de voz", "-i", "audio-input-microphone", "-t", "4000",
                          "-h", "boolean:transient:true", "Arrancando… / Starting…",
                          "Te escucho en unos segundos / Listening in a few seconds"])
    _PULSACION_AL_ARRANCAR.set()


if __name__ == "__main__":
    signal.signal(signal.SIGUSR1, _senal_al_arrancar)

import numpy as np  # noqa: E402  (después del manejador de la señal)
from piper import PiperVoice  # noqa: E402

# --- Configuración -----------------------------------------------------------
HOME = Path.home()
BASE = Path(__file__).resolve().parent


def _cargar_config():
    """Configuración local en config.json (no se sube al repo; ver config.example.json)."""
    try:
        return json.loads((BASE / "config.json").read_text())
    except FileNotFoundError:
        return {}


CFG = _cargar_config()
CLAVES_CFG = {"proyectos", "compose", "terminal", "apps_terminal", "alias_apps", "tareas", "confirmar",
              "modelo_comandos", "modelo_preguntas", "whisper", "idiomas", "whisper_modelo",
              "whisper_precision", "comando_actualizar", "comando_limpiar", "hablar", "nombre", "palabra_activacion",
              "variantes_activacion", "umbral_activacion", "llm", "llm_url", "llm_clave_env", "clasificador",
              "umbral_clasificador", "modelo_redactar", "imagenes", "imagen_dispositivo", "fooocus",
              "carpeta_imagenes"}

# Motor de Whisper: "openvino" (GPU/NPU Intel o CPU), "cuda" (faster-whisper en GPU NVIDIA) o "auto"
WHISPER_MOTOR = CFG.get("whisper", "auto")
WHISPER = BASE / "modelos/whisper-medium-int8-ov"
# Se prueban en orden. GPU requiere el paquete intel-compute-runtime;
# NPU, que la distribución incluya el compilador del NPU en intel-npu-driver.
WHISPER_DISPOSITIVOS = ["GPU", "CPU"]
WHISPER_CUDA = CFG.get("whisper_modelo", "large-v3-turbo")   # modelo de faster-whisper
WHISPER_CUDA_TIPO = CFG.get("whisper_precision", "int8_float16")  # float16 ocupa el doble de VRAM
# Con audio ruidoso Whisper puede "detectar" árabe, ruso…: se elige el más probable entre estos
IDIOMAS = CFG.get("idiomas", ["es", "en"])
VOCES = {
    "es": BASE / "modelos/voces/es/es_MX/claude/high/es_MX-claude-high.onnx",
    "en": BASE / "modelos/voces/en/en_US/lessac/medium/en_US-lessac-medium.onnx",
}
HABLAR = CFG.get("hablar", True)   # False = solo notificaciones

# Modelo de lenguaje: interpreta las frases que no cubren las reglas, responde preguntas y procesa el portapapeles.
#   "ollama"  -> API nativa de Ollama
#   "openai"  -> cualquier servidor compatible con la API de OpenAI (/v1/chat/completions): llama.cpp
#                (llama-server), LM Studio, LocalAI, vLLM, OpenVINO Model Server, el /v1 de Ollama o uno en la nube
#   "ninguno" -> sin LLM: solo reglas (equipos con pocos recursos); preguntas y portapapeles no disponibles
LLM = CFG.get("llm", "ollama")
LLM_URL = (CFG.get("llm_url") or {"ollama": "http://127.0.0.1:11434", "openai": "http://127.0.0.1:8080/v1"}
           .get(LLM, "")).rstrip("/")
# La clave nunca va en config.json: se indica el nombre de la variable de entorno que la contiene
LLM_CLAVE = os.environ.get(CFG["llm_clave_env"], "") if CFG.get("llm_clave_env") else ""
# Clasificador por embeddings entre las reglas y el LLM (ver ClasificadorComandos)
USAR_CLASIFICADOR = CFG.get("clasificador", True)
UMBRAL_CLASIFICADOR = CFG.get("umbral_clasificador", 0.86)   # más alto = duda más y pregunta más al LLM
MODELO_COMANDOS = CFG.get("modelo_comandos", "llama3.2")
MODELO_PREGUNTAS = CFG.get("modelo_preguntas", "llama3.2")  # uno más grande responde mejor, pero carga más lento
# Redactar textos o código para el portapapeles. Para código rinde mejor uno de código (qwen2.5-coder)
MODELO_REDACTAR = CFG.get("modelo_redactar") or MODELO_PREGUNTAS
NUM_CTX = 4096                 # igual para ambos usos, así Ollama no recarga el modelo
KEEP_ALIVE = "60m"

PROYECTOS = Path(CFG.get("proyectos", "~/Proyectos")).expanduser()   # carpeta con tus repos git
COMPOSE = Path(CFG["compose"]).expanduser() if CFG.get("compose") else None  # docker-compose para levantar/detener

# Terminal para las herramientas de consola y herramientas que se abren dentro de ella
TERMINAL = CFG.get("terminal", "kitty")
# Motor de imágenes: "fooocus" (SDXL en su propio entorno, para GPU NVIDIA), "openvino" (LCM Dreamshaper con
# el openvino-genai del asistente, ~20 s en CPU), "auto" (Fooocus si está instalado, si no OpenVINO) o null = desactivado
IMAGENES = CFG.get("imagenes", "auto")
FOOOCUS = Path(CFG.get("fooocus", "~/Fooocus")).expanduser() if CFG.get("fooocus", "~/Fooocus") else None
IMAGEN_OV = BASE / "modelos/LCM_Dreamshaper_v7-int8-ov"
# En la GPU integrada Intel (i915) la difusión colgó la GPU: CPU por defecto
IMAGEN_DISPOSITIVO = CFG.get("imagen_dispositivo", "CPU")
CARPETA_IMAGENES = Path(CFG["carpeta_imagenes"]).expanduser() if CFG.get("carpeta_imagenes") else None
# Comando para "actualiza todo". Por defecto se detecta: yay/paru/pacman (Arch) o dnf (Fedora), + flatpak
COMANDO_ACTUALIZAR = CFG.get("comando_actualizar")
# Comando para "limpia la caché de paquetes". Por defecto: yay/paru -Sc, paccache o pacman -Sc (Arch), dnf (Fedora)
COMANDO_LIMPIAR = CFG.get("comando_limpiar")
# Tareas de desarrollo permitidas (nombre -> comando que se ejecuta en la terminal)
TAREAS = {k.strip(): v for k, v in CFG.get("tareas", {}).items() if isinstance(v, str) and v.strip()}
# Acciones sensibles que piden confirmación por voz de 1 paso («¿Confirmas…?» -> «sí»)
CONFIRMAR = set(CFG.get("confirmar", ["docker_detener", "docker_reiniciar"]))
# Siempre se confirman, aunque `confirmar` no las incluya: cierran la sesión y lo que no esté guardado
CONFIRMAR_SIEMPRE = {"energia_equipo"}
APPS_TERMINAL = CFG.get("apps_terminal", ["htop", "btop", "top", "nvtop", "lazygit", "lazydocker", "k9s",
                                          "ranger", "yazi", "ncdu"])

# Apps gráficas: se leen de los .desktop instalados (como el menú de KDE).
# Estos alias (clave normalizada sin espacios -> ids candidatos del .desktop o herramienta de consola)
# cubren nombres genéricos y errores típicos de transcripción. Se usa el primer id instalado:
# los nombres cambian entre distribuciones (Fedora/Arch) y entre paquete nativo y Flatpak.
ALIAS_APPS = {
    **dict.fromkeys(["vscode", "vs", "code", "visualstudiocode", "visualcode", "visualstudio",
                     "visualestudiocode", "vscodigo", "codigo"], ("com.microsoft.VSCode", "code", "code-oss")),
    **dict.fromkeys(["navegador", "browser", "webbrowser", "elnavegador"], ("org.mozilla.firefox", "firefox")),
    **dict.fromkeys(["terminal", "consola", "laterminal", "kiti", "kity", "kitti"], ("kitty", "org.kde.konsole")),
    **dict.fromkeys(["archivos", "explorador", "exploradordearchivos", "files", "filemanager"], "org.kde.dolphin"),
    **dict.fromkeys(["configuracion", "ajustes", "settings", "configuraciondelsistema"], "systemsettings"),
    **dict.fromkeys(["compass", "mongo", "mongodb", "mongocompass", "compas"],
                    ("com.mongodb.Compass", "mongodb-compass")),
    **dict.fromkeys(["monitordelsistema", "monitor", "systemmonitor"], "org.kde.plasma-systemmonitor"),
    **dict.fromkeys(["calculadora", "calculator"], "org.kde.kcalc"),
    **dict.fromkeys(["edge", "microsoftedge"], ("microsoft-edge", "com.microsoft.Edge")),
    **dict.fromkeys(["hetop", "hitop", "achetop"], "htop"),
    **dict.fromkeys(["btop", "betop", "bitop", "pittop", "vitop"], "btop"),
    **dict.fromkeys(["kwrite", "write", "grite", "kwriter"], "org.kde.kwrite"),
    **CFG.get("alias_apps", {}),
}
DIRS_DESKTOP = [HOME / ".local/share/applications", HOME / ".local/share/flatpak/exports/share/applications",
                Path("/var/lib/flatpak/exports/share/applications"), Path("/usr/share/applications")]

RATE = 16000
BLOQUE = 512                  # 32 ms (tamaño de ventana de Silero VAD a 16 kHz)
ESPERA_INICIO = 5.0           # segundos esperando a que empieces a hablar
SILENCIO_FIN = 1.0            # segundos de silencio que cierran la frase
MAX_GRABACION = 15.0

# Nombre del asistente: notificaciones, respuestas y pista de vocabulario de Whisper
NOMBRE = CFG.get("nombre", "Nova")
# Palabra de activación ("" = solo atajo de teclado; así el micrófono no queda abierto). Un Whisper pequeño
# la busca al inicio de cada frase que escucha y el modelo principal la confirma antes de activar nada.
PALABRA_ACTIVACION = CFG.get("palabra_activacion", "").strip().lower()
VARIANTES_ACTIVACION = CFG.get("variantes_activacion",
                               [f"oye {PALABRA_ACTIVACION}", f"hey {PALABRA_ACTIVACION}"] if PALABRA_ACTIVACION else [])
UMBRAL_ACTIVACION = CFG.get("umbral_activacion", 0.87)   # más alto = menos falsos positivos
# base reconoce nombres poco comunes mucho mejor que tiny ("Hades" -> «Hitties», «others») por 0,2 s más
WHISPER_ACTIVACION = BASE / "modelos/whisper-base-int8-ov"
VENTANA_ACTIVACION = 2.0      # segundos iniciales de cada frase en los que se busca la palabra
FILTRO_ACTIVACION = 0.70      # similitud mínima para pasar al modelo principal
FILTRO_SEGURO = 0.90          # si el filtro está así de seguro, basta una confirmación >= FILTRO_ACTIVACION

SONIDOS = Path("/usr/share/sounds/ocean/stereo")
# Carpeta privada para el script de KWin y los WAV. Sin XDG_RUNTIME_DIR (al ejecutarlo a mano) nunca /tmp a secas:
# otro usuario podría crear esos archivos antes. mkdtemp crea una carpeta nueva solo para este usuario (0700)
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.mkdtemp(prefix="asistente-voz-"))
os.environ["PATH"] = f"{HOME}/.local/bin:{os.environ.get('PATH', '/usr/bin')}"

# Textos que Whisper "alucina" cuando solo hay silencio o ruido
ALUCINACIONES = ("amara.org", "subtitulos", "gracias por ver", "suscribete", "gracias por su atencion",
                 "thanks for watching", "thank you for watching", "subscribe")

# --- Textos de respuesta -----------------------------------------------------------
MSG = {
    "es": {
        "app_no": "No encontré la aplicación {x}.", "abriendo": "Abriendo {x}.", "cerrando": "Cerrando {x}.",
        "proyecto_no": "No encontré un proyecto parecido a {x}.", "proyecto": "Abriendo {x} en Visual Studio Code.",
        "proyecto_term": "Abriendo {t} en {x}.",
        "git_estado": "En {x}, rama {r}{cambios}{sync}.",
        "git_limpio": ", sin cambios pendientes",
        "git_cambios": ", con {n} archivos modificados",
        "git_subir": ", {n} commits por subir", "git_bajar": ", {n} commits por bajar",
        "git_commit": "Último commit en {x}: {c}.",
        "tarea": "Ejecutando {x}.", "tarea_no": "No encontré la tarea {x}.",
        "docker_0": "No hay contenedores corriendo.", "docker_n": "Hay {n} contenedores corriendo: {x}.",
        "docker_up": "Levantando contenedores…", "docker_stop": "Deteniendo contenedores…",
        "docker_up_ok": "Contenedores levantados.", "docker_stop_ok": "Contenedores detenidos.",
        "docker_error": "Docker compose falló, revisa el registro.",
        "docker_sin_compose": "No hay un archivo docker compose configurado.",
        "docker_cont_no": "No encontré el contenedor {x}.",
        "docker_logs": "Abriendo registros de {x}.",
        "docker_restart": "Contenedor {x} reiniciado.",
        "confirmar_docker_detener": "¿Confirmas detener los contenedores?",
        "confirmar_docker_reiniciar": "¿Confirmas reiniciar el contenedor?",
        "cancelado": "Cancelado.",
        "vol_n": "Volumen al {x} por ciento.", "vol_on": "Sonido activado.",
        "brillo_n": "Brillo al {x} por ciento.", "brillo_no": "Este monitor no permite ajustar el brillo por software.",
        "luz_nocturna_on": "Luz nocturna activada.", "luz_nocturna_off": "Luz nocturna desactivada.",
        "luz_nocturna_toggle": "Luz nocturna alternada.",
        "clip_vacio": "El portapapeles está vacío.",
        "clip_secreto": "Lo que copiaste parece una contraseña, no lo leo en voz alta.",
        "clip_prompt_resumir": "Resume en español en máximo 2 frases cortas el siguiente texto:\n\n{x}",
        "clip_prompt_traducir": "Traduce al español (o al inglés si ya está en español) el siguiente texto, sin explicaciones adicionales:\n\n{x}",
        "clip_prompt_explicar": "Explica en español en máximo 3 frases cortas qué hace o significa esto (sin markdown ni bloques de código):\n\n{x}",
        "dictado_ok": "Copiado al portapapeles (Ctrl+V): {x}",
        "redactando": "Redactando…", "redactar_ok": "Listo, está en el portapapeles. Pégalo con Control V.",
        "redacta": "Escribe exactamente el texto que se pide, listo para pegar, en español salvo que pidan otro "
                   "idioma: sin introducción, sin comentarios al final y sin comillas alrededor. Si piden código, "
                   "devuelve solo el código, sin bloques de markdown.",
        "imagen_no": "No tengo cómo generar imágenes: falta Fooocus o el modelo de OpenVINO.",
        "imagen_ocupado": "Ya estoy generando una imagen, espera a que termine.",
        "imagen_generando": "Generando la imagen, te aviso cuando esté.",
        "imagen_ok": "La imagen está lista.", "imagen_error": "No pude generar la imagen, revisa el registro.",
        "timer_ok": "Temporizador de {t} activado.", "timer_fin": "Temporizador finalizado: {x}",
        "timer_no": "No entendí la duración del temporizador.",
        "tel_no": "El teléfono no está conectado.",
        "tel_sonar": "Haciendo sonar {x}.",
        "tel_bat": "La batería de {x} está al {b} por ciento{c}.",
        "tel_cargando": " y cargando",
        "tel_clip": "Portapapeles enviado a {x}.",
        "top_proc": "Los procesos que más consumen son: {x}.",
        "puerto_uso": "En el puerto {p} corre {x}.", "puerto_libre": "No hay nada escuchando en el puerto {p}.",
        "puertos_lista": "Puertos activos: {x}.", "puertos_ninguno": "No encontré puertos de desarrollo abiertos.",
        "updates_0": "El sistema está al día, no hay actualizaciones pendientes.",
        "updates_n": "Hay {n} paquetes por actualizar.",
        "media_no": "No entendí qué hacer con la música.", "media_0": "No hay ningún reproductor abierto.",
        "perfil_no": "Los perfiles son rendimiento, equilibrado o ahorro.",
        "perfil": "Perfil {x} activado.", "buscando": "Buscando {x}.",
        "actualizando": "Abriendo la terminal para actualizar. Escribe tu contraseña si te la pide.",
        "actualizar_no": "No sé cómo actualizar este sistema.", "actualizar_fin": "Listo. Pulsa Enter para cerrar.",
        "limpiando": "Abriendo la terminal para limpiar la caché de paquetes.",
        "limpiar_no": "No sé cómo limpiar la caché en este sistema.",
        "confirmar_energia_equipo_suspender": "¿Confirmas suspender el equipo?",
        "confirmar_energia_equipo_reiniciar": "¿Confirmas reiniciar el equipo? Se cerrarán las aplicaciones.",
        "confirmar_energia_equipo_apagar": "¿Confirmas apagar el equipo? Se cerrarán las aplicaciones.",
        "energia_suspender": "Suspendiendo.", "energia_reiniciar": "Reiniciando.", "energia_apagar": "Apagando.",
        "llm_error": "No pude conectar con el modelo de lenguaje.",
        "sin_llm": "No tengo un modelo de lenguaje configurado para eso.", "no_entendi": "No te entendí.",
        "fallo": "Algo falló al ejecutar el comando.", "pensando": "Pensando…",
        "estado": "El procesador está a {t} grados{gpu}, usas {u:.0f} de {m:.0f} gigas de memoria, "
                  "el disco tiene {d:.0f} gigas libres{bat}{red}, y el perfil de energía es {p}.",
        "gpu": ", la gráfica a {t} grados ({uso}% de uso y {vram:.1f} gigas de VRAM)",
        "bat": ", batería al {b}%",
        "red": ", conectado a {r}",
        "perfiles": {"performance": "rendimiento", "balanced": "equilibrado", "quiet": "silencioso", "cool": "fresco",
                     "power-saver": "ahorro"},
        "responde": f"Te llamas {NOMBRE}, un asistente de voz local. Responde en español, en máximo 3 frases cortas."
                    " El texto se leerá en voz alta: "
                    "sin markdown, sin listas, sin bloques de código.",
    },
    "en": {
        "app_no": "I couldn't find the application {x}.", "abriendo": "Opening {x}.", "cerrando": "Closing {x}.",
        "proyecto_no": "I couldn't find a project like {x}.", "proyecto": "Opening {x} in Visual Studio Code.",
        "proyecto_term": "Opening {t} in {x}.",
        "git_estado": "In {x}, branch {r}{cambios}{sync}.",
        "git_limpio": ", working tree clean",
        "git_cambios": ", with {n} modified files",
        "git_subir": ", {n} commits to push", "git_bajar": ", {n} commits to pull",
        "git_commit": "Last commit in {x}: {c}.",
        "tarea": "Running {x}.", "tarea_no": "I couldn't find the task {x}.",
        "docker_0": "No containers are running.", "docker_n": "There are {n} containers running: {x}.",
        "docker_up": "Starting containers…", "docker_stop": "Stopping containers…",
        "docker_up_ok": "Containers started.", "docker_stop_ok": "Containers stopped.",
        "docker_error": "Docker compose failed, check the log.",
        "docker_sin_compose": "No docker compose file is configured.",
        "docker_cont_no": "I couldn't find the container {x}.",
        "docker_logs": "Opening logs for {x}.",
        "docker_restart": "Container {x} restarted.",
        "confirmar_docker_detener": "Do you confirm stopping the containers?",
        "confirmar_docker_reiniciar": "Do you confirm restarting the container?",
        "cancelado": "Cancelled.",
        "vol_n": "Volume at {x} percent.", "vol_on": "Sound on.",
        "brillo_n": "Brightness at {x} percent.", "brillo_no": "This display does not support software brightness control.",
        "luz_nocturna_on": "Night light enabled.", "luz_nocturna_off": "Night light disabled.",
        "luz_nocturna_toggle": "Night light toggled.",
        "clip_vacio": "The clipboard is empty.",
        "clip_secreto": "What you copied looks like a password, so I won't read it aloud.",
        "clip_prompt_resumir": "Summarize the following text in English in at most 2 short sentences:\n\n{x}",
        "clip_prompt_traducir": "Translate the following text into English (or Spanish if it is already in English), without extra commentary:\n\n{x}",
        "clip_prompt_explicar": "Explain in English in at most 3 short sentences what this does or means (no markdown or code blocks):\n\n{x}",
        "dictado_ok": "Copied to clipboard (Ctrl+V): {x}",
        "redactando": "Writing…", "redactar_ok": "Done, it's on the clipboard. Paste it with Control V.",
        "redacta": "Write exactly the requested text, ready to paste, in English unless another language is "
                   "requested: no introduction, no closing remarks and no surrounding quotes. If code is "
                   "requested, return only the code, without markdown blocks.",
        "imagen_no": "I can't generate images: Fooocus or the OpenVINO model is missing.",
        "imagen_ocupado": "I'm already generating an image, please wait until it's done.",
        "imagen_generando": "Generating the image, I'll let you know when it's ready.",
        "imagen_ok": "The image is ready.", "imagen_error": "I couldn't generate the image, check the log.",
        "timer_ok": "Timer set for {t}.", "timer_fin": "Timer finished: {x}",
        "timer_no": "I didn't understand the timer duration.",
        "tel_no": "The phone is not connected.",
        "tel_sonar": "Ringing {x}.",
        "tel_bat": "{x} battery is at {b} percent{c}.",
        "tel_cargando": " and charging",
        "tel_clip": "Clipboard sent to {x}.",
        "top_proc": "Top resource processes are: {x}.",
        "puerto_uso": "Port {p} is running {x}.", "puerto_libre": "Nothing is listening on port {p}.",
        "puertos_lista": "Active ports: {x}.", "puertos_ninguno": "No open development ports found.",
        "updates_0": "The system is up to date, no pending updates.",
        "updates_n": "There are {n} packages to update.",
        "media_no": "I didn't understand what to do with the music.", "media_0": "No media player is open.",
        "perfil_no": "The profiles are performance, balanced or power saver.",
        "perfil": "{x} profile enabled.", "buscando": "Searching for {x}.",
        "actualizando": "Opening a terminal to update. Type your password if asked.",
        "actualizar_no": "I don't know how to update this system.", "actualizar_fin": "Done. Press Enter to close.",
        "limpiando": "Opening a terminal to clean the package cache.",
        "limpiar_no": "I don't know how to clean the cache on this system.",
        "confirmar_energia_equipo_suspender": "Do you confirm putting the computer to sleep?",
        "confirmar_energia_equipo_reiniciar": "Do you confirm restarting the computer? Apps will be closed.",
        "confirmar_energia_equipo_apagar": "Do you confirm shutting down the computer? Apps will be closed.",
        "energia_suspender": "Going to sleep.", "energia_reiniciar": "Restarting.", "energia_apagar": "Shutting down.",
        "llm_error": "I couldn't connect to the language model.",
        "sin_llm": "I don't have a language model configured for that.", "no_entendi": "Sorry, I didn't understand.",
        "fallo": "Something went wrong running the command.", "pensando": "Thinking…",
        "estado": "The CPU is at {t} degrees{gpu}, you're using {u:.0f} of {m:.0f} gigabytes of memory, "
                  "the disk has {d:.0f} gigabytes free{bat}{red}, and the power profile is {p}.",
        "gpu": ", graphics card at {t} degrees ({uso}% load and {vram:.1f} gigabytes VRAM)",
        "bat": ", battery at {b}%",
        "red": ", connected to {r}",
        "perfiles": {"performance": "performance", "balanced": "balanced", "quiet": "quiet", "cool": "cool",
                     "power-saver": "power saver"},
        "responde": f"Your name is {NOMBRE}, a local voice assistant. Answer in English, in at most 3 short sentences."
                    " The text will be read aloud: "
                    "no markdown, no lists, no code blocks.",
    },
}


def log(msg):
    print(msg, flush=True)


def llm_local():
    return urllib.parse.urlparse(LLM_URL).hostname in ("127.0.0.1", "localhost", "::1")


def validar_config():
    """Comprueba claves desconocidas, rutas y herramientas configuradas en config.json."""
    avisos = []
    desconocidas = sorted(set(CFG) - CLAVES_CFG)
    if desconocidas:
        avisos.append(f"claves desconocidas en config.json: {', '.join(desconocidas)}")
    if not PROYECTOS.is_dir():
        avisos.append(f"la carpeta de proyectos no existe: {PROYECTOS}")
    if COMPOSE and not COMPOSE.is_file():
        avisos.append(f"el archivo compose no existe: {COMPOSE}")
    if not shutil.which(TERMINAL):
        avisos.append(f"la terminal configurada ({TERMINAL!r}) no está instalada")
    if LLM not in ("ollama", "openai", "ninguno"):
        avisos.append(f"llm desconocido ({LLM!r}): usa ollama, openai o ninguno")
    elif LLM != "ninguno" and not llm_local():
        avisos.append(f"el LLM está fuera de este equipo ({LLM_URL}): las frases y preguntas salen por la red")
    if IMAGENES not in ("auto", "fooocus", "openvino", None):
        avisos.append(f"motor de imágenes desconocido ({IMAGENES!r}): usa auto, fooocus, openvino o null")
    elif IMAGENES == "fooocus" or (IMAGENES == "auto" and "fooocus" in CFG and not hay_openvino_imagen()):
        if not hay_fooocus():
            avisos.append(f"no encontré Fooocus con su entorno en {FOOOCUS} (fooocus): no podré generar imágenes")
    elif IMAGENES == "openvino" and not hay_openvino_imagen():
        avisos.append(f"falta el modelo de imágenes {IMAGEN_OV.name} u openvino-genai: ejecuta instalar.sh")
    if LLM_CLAVE and not llm_local() and urllib.parse.urlparse(LLM_URL).scheme != "https":
        avisos.append(f"la clave del LLM viajaría sin cifrar a {LLM_URL}: usa https")
    if CFG.get("llm_clave_env") and not LLM_CLAVE:
        avisos.append(f"la variable de entorno {CFG['llm_clave_env']} (llm_clave_env) no está definida")
    for a in avisos:
        log(f"AVISO config: {a}")
    return avisos


def normalizar(texto):
    t = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def clave(texto):
    return re.sub(r"[^a-z0-9]", "", normalizar(texto))


def palabras_norm(texto):
    return re.sub(r"[^a-z0-9 ]", " ", normalizar(texto)).split()


def puntaje_activacion(texto):
    """(similitud 0-1, nº de palabras que ocupa) de la palabra de activación al inicio del texto."""
    p = palabras_norm(texto)
    mejor = (0.0, 0)
    for frase in [PALABRA_ACTIVACION] + VARIANTES_ACTIVACION:
        objetivo = "".join(palabras_norm(frase))
        for i in range(min(3, len(p))):       # tolera relleno antes: "eh, hey nova"
            for k in range(1, len(palabras_norm(frase)) + 2):
                r = difflib.SequenceMatcher(None, "".join(p[i:i + k]), objetivo).ratio()
                if r > mejor[0]:
                    mejor = (r, i + k)
    return mejor


def quitar_palabras(texto, n):
    tokens = texto.split()
    while tokens and n > 0:
        n -= len(palabras_norm(tokens.pop(0)))
    return " ".join(tokens).lstrip(" ,.!¡¿?")


def recita_pista(texto, pista):
    """Con audio casi en silencio, Whisper a veces "recita" la pista de vocabulario."""
    t = " ".join(palabras_norm(texto))
    terminos = {" ".join(palabras_norm(x)) for x in pista.split(",")}
    return sum(1 for x in terminos if x and f" {x} " in f" {t} ") >= 3


MARCAS_EN = {"open", "launch", "start", "run", "close", "the", "search", "for", "volume", "turn", "up", "down",
             "mute", "unmute", "what", "how", "is", "are", "running", "containers", "lock", "screen", "play",
             "pause", "next", "previous", "song", "music", "stop", "show", "status", "mode", "power", "project",
             "my", "please", "explain", "why", "who", "can", "you", "set", "to", "internet", "computer", "doing",
             "draw", "write", "image", "picture", "email", "about", "an"}
MARCAS_ES = {"abre", "abrir", "cierra", "cerrar", "el", "la", "los", "las", "busca", "buscar", "volumen", "sube",
             "baja", "que", "como", "contenedores", "proyecto", "pantalla", "musica", "cancion", "pausa", "modo",
             "en", "de", "del", "por", "esta", "estan", "pon", "levanta", "deten", "explica", "cual", "compu",
             "una", "un"}


def idioma(texto, detectado=None):
    p = normalizar(texto).replace("?", " ").replace(",", " ").replace(".", " ").split()
    en = sum(w in MARCAS_EN for w in p)
    es = sum(w in MARCAS_ES for w in p)
    if en != es:
        return "en" if en > es else "es"
    return detectado if detectado in ("es", "en") else "es"


# --- Interfaz: sonidos, notificaciones, voz --------------------------------------
_notif_id = None

# Un sonido distinto por estado, para saber sin mirar la pantalla en qué punto está el asistente
SONIDO_ESTADO = {
    "escuchando": "button-pressed",            # empieza a grabar: habla
    "procesando": "button-pressed-modifier",   # te oyó y dejó de grabar: está pensando
    "exito": "completion-success",
    "fallo": "completion-fail",                # te oyó, pero no entendió o falló la acción
    "no_escuche": "completion-partial",        # no oyó voz
    "ocupado": "dialog-warning",               # atajo mientras procesa: te escucha al terminar
}


def sonido(nombre, esperar=False):
    f = SONIDOS / f"{SONIDO_ESTADO.get(nombre, nombre)}.oga"
    if f.exists() and shutil.which("pw-play"):
        p = subprocess.Popen(["pw-play", str(f)], stderr=subprocess.DEVNULL)
        if esperar:      # el aviso de inicio no debe colarse en la grabación
            p.wait()


def notificar(titulo, cuerpo="", icono="audio-input-microphone", segundos=6, transitoria=False):
    """Una sola notificación que se va reemplazando. `transitoria`: estados intermedios
    (escuchando, procesando) que no se quedan en el historial de notificaciones."""
    global _notif_id
    cmd = ["notify-send", "-p", "-a", NOMBRE, "-i", icono, "-t", str(segundos * 1000)]
    if transitoria:
        cmd += ["-h", "boolean:transient:true"]
    if _notif_id:
        cmd += ["-r", _notif_id]
    try:
        r = subprocess.run(cmd + ["--", titulo, cuerpo], capture_output=True, text=True, timeout=3)
        _notif_id = r.stdout.strip() or None
    except subprocess.TimeoutExpired:      # el servidor de notificaciones no responde: no bloquear
        log("notify-send no respondió")
        _notif_id = None
    except OSError:                        # sin notify-send (libnotify): el asistente sigue, solo con voz
        _notif_id = None


def cerrar_notificacion():
    """Quita la notificación actual (p. ej. una activación que resultó no ser el nombre)."""
    global _notif_id
    if _notif_id:
        subprocess.run(["busctl", "--user", "call", "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                        "org.freedesktop.Notifications", "CloseNotification", "u", _notif_id],
                       capture_output=True, timeout=3)
        _notif_id = None


_voz_activa = None


class Voz:
    def __init__(self):
        global _voz_activa
        self.voces = {k: PiperVoice.load(str(v)) for k, v in VOCES.items() if v.exists()} if HABLAR else {}
        self._proc = None
        self._interrumpido = threading.Event()
        self._activa = False      # True durante toda una respuesta, también entre oraciones
        self._cancelar_stream = threading.Event()
        self._lock = threading.Lock()   # el aviso de un temporizador y una respuesta no se pisan
        _voz_activa = self

    @property
    def hablando(self):
        return self._activa or (self._proc is not None and self._proc.poll() is None)

    def callar(self):
        """Interrumpe la síntesis/reproducción en curso (barge-in)."""
        self._interrumpido.set()
        self._cancelar_stream.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _reproducir_wav(self, archivo):
        if self._interrumpido.is_set():
            return
        self._proc = subprocess.Popen(["pw-play", str(archivo)], stderr=subprocess.DEVNULL)
        self._proc.wait()

    def decir(self, texto, lang):
        voz = self.voces.get(lang) or self.voces.get("es")
        if not voz or not texto:
            return
        with self._lock:
            self._interrumpido.clear()
            self._activa = True
            try:
                archivo = RUNTIME / "asistente-voz.wav"
                with wave.open(str(archivo), "wb") as w:
                    voz.synthesize_wav(texto, w)
                self._reproducir_wav(archivo)
            finally:
                self._activa = False

    def decir_stream(self, oraciones, lang, al_avanzar=None):
        """Sintetiza oración por oración en paralelo a la reproducción para reducir la latencia."""
        voz = self.voces.get(lang) or self.voces.get("es")
        # 3 archivos y cola de 1: mientras suena el archivo k, la cola guarda k+1 y se sintetiza k+2.
        # Con 2 archivos, la síntesis (más rápida que la reproducción) pisaba el que estaba sonando.
        cola = queue.Queue(maxsize=1)
        acumulado, errores = [], []
        # Propio de esta respuesta: si se interrumpe, el productor termina aunque luego empiece otra
        cancelar = self._cancelar_stream = threading.Event()

        def poner(item):
            while not cancelar.is_set():
                try:
                    cola.put(item, timeout=0.1)
                    return
                except queue.Full:
                    pass

        def productor():
            try:
                for oracion in oraciones:
                    if cancelar.is_set():
                        break
                    oracion = oracion.strip()
                    if not oracion:
                        continue
                    acumulado.append(oracion)
                    if al_avanzar:
                        al_avanzar(" ".join(acumulado))
                    if voz:
                        wav = RUNTIME / f"asistente-voz-{len(acumulado) % 3}.wav"   # solo cuenta las no vacías
                        with wave.open(str(wav), "wb") as w:
                            voz.synthesize_wav(oracion, w)
                        poner(wav)
            except Exception as e:      # p. ej. Ollama caído: se relanza en el hilo principal
                errores.append(e)
            finally:
                poner(None)

        with self._lock:
            self._interrumpido.clear()
            self._activa = True
            try:
                hilo = threading.Thread(target=productor, daemon=True)
                hilo.start()
                while not cancelar.is_set():
                    try:
                        wav = cola.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if wav is None:
                        break
                    self._reproducir_wav(wav)
                hilo.join(timeout=0.2)   # si sigue esperando a Ollama, termina solo al ver «cancelar»
            finally:
                self._activa = False
        if errores and not acumulado:
            raise errores[0]
        return " ".join(acumulado)


# --- Audio y detección de voz (Silero VAD) ---------------------------------------
class SileroVAD:
    """Detector de actividad de voz con Silero VAD v6 (ONNX). Distingue voz humana de música y ruido."""
    def __init__(self):
        self.session = None
        try:
            import onnxruntime
            candidatas = [BASE / "modelos/silero_vad_v6.onnx"]
            try:
                import faster_whisper.vad as fw_vad
                candidatas.append(Path(fw_vad.get_assets_path()) / "silero_vad_v6.onnx")
            except Exception:
                pass
            ruta = next((p for p in candidatas if p.is_file()), None)
            if not ruta:
                return
            opts = onnxruntime.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            opts.enable_cpu_mem_arena = False
            opts.log_severity_level = 4
            self.session = onnxruntime.InferenceSession(str(ruta), providers=["CPUExecutionProvider"], sess_options=opts)
        except Exception as e:
            log(f"Silero VAD no disponible, usando umbral RMS: {e}")

    def nuevo_estado(self):
        return (np.zeros((1, 1, 128), dtype=np.float32),
                np.zeros((1, 1, 128), dtype=np.float32),
                np.zeros((1, 64), dtype=np.float32))

    def probabilidad(self, bloque_int16, estado):
        h, c, ctx = estado
        chunk = (bloque_int16.astype(np.float32) / 32768.0).reshape(1, -1)
        if chunk.shape[1] < BLOQUE:
            chunk = np.pad(chunk, ((0, 0), (0, BLOQUE - chunk.shape[1])))
        inp = np.concatenate([ctx, chunk[:, :BLOQUE]], axis=1)
        out, h, c = self.session.run(None, {"input": inp, "h": h, "c": c})
        return float(out.squeeze()), (h, c, chunk[:, BLOQUE - 64:BLOQUE])


_VAD = SileroVAD()


VOLUMEN_GUARDADO = RUNTIME / "asistente-voz-volumen"


def _volumen_sistema():
    r = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], capture_output=True, text=True)
    m = re.search(r"Volume:\s*([0-9.]+)", r.stdout)
    return float(m.group(1)) if m and "[MUTED]" not in r.stdout else None


def _restaurar_volumen(vol=None):
    """Devuelve el volumen de antes de escuchar. Sin argumento, el que quedó guardado si el servicio
    murió mientras grababa (si no, el sistema se quedaría al 20 %)."""
    if vol is None:
        try:
            vol = float(VOLUMEN_GUARDADO.read_text())
        except (OSError, ValueError):
            return
        log(f"restaurando el volumen que quedó bajado: {vol:.2f}")
    subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{vol:.2f}"])
    VOLUMEN_GUARDADO.unlink(missing_ok=True)


def nivel(bloque):
    return float(np.sqrt(np.mean(bloque.astype(np.float32) ** 2)))


def pw_record():
    """Bloques de 32 ms de un pw-record propio."""
    p = subprocess.Popen(["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            data = p.stdout.read(BLOQUE * 2)
            if len(data) < BLOQUE * 2:
                return
            yield np.frombuffer(data, np.int16)
    finally:
        p.terminate()
        p.wait()


class Microfono:
    """Captura continua del micrófono (solo con palabra de activación), compartida entre el detector
    de activación y la grabación del comando."""

    def __init__(self):
        self.cola = queue.Queue(maxsize=int(30 * RATE / BLOQUE))
        threading.Thread(target=self._capturar, daemon=True).start()

    def _capturar(self):
        while True:                     # si PipeWire se reinicia o cambia el micrófono, se vuelve a abrir
            for bloque in pw_record():
                try:
                    self.cola.put_nowait(bloque)
                except queue.Full:
                    pass
            time.sleep(2)

    def vaciar(self):
        try:
            while True:
                self.cola.get_nowait()
        except queue.Empty:
            pass

    def bloques(self):
        while True:
            try:
                yield self.cola.get(timeout=2)
            except queue.Empty:          # pw-record caído: termina la grabación en vez de colgarse
                return


def grabar(detener, espera_inicio=ESPERA_INICIO, fuente=None, previo=(), hablando=False, silencio=0.0,
           umbral=None):
    """Graba hasta detectar el fin de la frase. Por defecto abre su propio pw-record; con la palabra de
    activación lee de `fuente` (Microfono) y, con `previo` y `hablando`, continúa una frase ya empezada."""
    vol_prev = _volumen_sistema()
    if vol_prev and vol_prev > 0.20:
        VOLUMEN_GUARDADO.write_text(str(vol_prev))
        subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.20"])
    fuente = fuente or pw_record()
    continua = hablando
    bloques, ruido, n = list(previo), [], 0
    calibrar = umbral is None and not continua
    umbral = umbral or 400.0
    estado_vad = _VAD.nuevo_estado() if _VAD.session else None
    try:
        for a in fuente:
            if detener.is_set():
                break
            bloques.append(a)
            n += 1
            t = n * BLOQUE / RATE
            if estado_vad is not None:
                prob, estado_vad = _VAD.probabilidad(a, estado_vad)
                if t < 0.15 and not continua:  # descarta el eco inmediato del sonido de inicio
                    continue
                hay_voz = prob >= 0.5
            else:
                rms = nivel(a)
                if calibrar and t < 0.3:      # calibración del ruido de fondo (fallback sin ONNX)
                    ruido.append(rms)
                    umbral = max(400.0, 3 * float(np.percentile(ruido, 20)))
                    continue
                hay_voz = rms > umbral
            if hay_voz:
                hablando, silencio = True, 0.0
            elif hablando:
                silencio += BLOQUE / RATE
            if hablando and silencio >= SILENCIO_FIN:
                break
            if not hablando and t > espera_inicio:
                return None
            if t > MAX_GRABACION:
                break
    finally:
        fuente.close()
        if vol_prev and vol_prev > 0.20:
            _restaurar_volumen(vol_prev)
    if not hablando and not detener.is_set():
        return None
    audio = np.concatenate(bloques)
    saturado = np.mean(np.abs(audio) >= 32700)
    if saturado > 0.005:
        log(f"audio saturado ({saturado:.1%} de las muestras): baja la ganancia del micrófono "
            f"(wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 0.3)")
    return audio.astype(np.float32) / 32768.0


def guardar_grabacion(audio, conservar=10, carpeta="grabaciones", sufijo=""):
    """Guarda las últimas grabaciones para poder diagnosticar errores de transcripción."""
    d = BASE / carpeta
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)                      # es tu voz: solo para tu usuario
    with wave.open(str(d / time.strftime(f"%Y%m%d-%H%M%S{sufijo}.wav")), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    for viejo in sorted(d.glob("*.wav"))[:-conservar]:
        viejo.unlink()


# --- Catálogo de aplicaciones ---------------------------------------------------------
class App:
    def __init__(self, id, nombre, cmd):
        self.id, self.nombre, self.cmd = id, nombre, cmd


# Cómo pasa cada terminal el comando a ejecutar; las que no aparecen usan -e (konsole, alacritty, xterm…)
# En kitty se desactiva la confirmación al cerrar ventana para que «cierra la ventana» / «cierra htop» funcione directo
ARGS_TERMINAL = {"kitty": ["-o", "confirm_os_window_close=0"], "foot": [], "gnome-terminal": ["--"],
                 "ptyxis": ["--"], "kgx": ["--"], "wezterm": ["start", "--"]}


def cmd_terminal(*args):
    return [TERMINAL, *ARGS_TERMINAL.get(Path(TERMINAL).name, ["-e"]), *args]


def comando_en_terminal(cmd, m):
    """Un comando de config.json (tareas, comando_actualizar, comando_limpiar) en una terminal visible. El comando es de confianza
    (lo escribiste tú); el eco va entre comillas de shell para que unas comillas en él no rompan la línea."""
    return cmd_terminal("bash", "-c", f'echo {shlex.quote("$ " + cmd)}; {cmd}; echo; '
                                      f'read -rp {shlex.quote(m["actualizar_fin"])} _')


def _firma_apps():
    """Cambia al instalar, quitar o editar un .desktop, o al instalar una herramienta de consola."""
    firma = []
    for d in DIRS_DESKTOP:
        if d.is_dir():
            # stat() sigue los enlaces: las apps de Flatpak son enlaces que cambian al actualizarse
            firma.append((d.stat().st_mtime, max((f.stat().st_mtime for f in d.glob("*.desktop")
                                                  if f.exists()), default=0)))
    return tuple(firma), tuple(bool(shutil.which(t)) for t in APPS_TERMINAL)


_REVISION_APPS = 2.0   # segundos entre comprobaciones: resolver_app se llama varias veces por frase
# Apps base de Android en Waydroid que duplican utilidades de KDE (calculadora, ajustes, reloj…)
WAYDROID_IGNORAR = ("Waydroid", "waydroid.app.install", "waydroid.market",
                    "waydroid.com.android.", "waydroid.org.lineageos.", "waydroid.com.google.android.")
ID_PARTES_GENERICAS = {"desktop", "app", "client", "launcher", "hgl"}


def cargar_apps():
    """Apps gráficas desde los .desktop + herramientas de consola instaladas. Devuelve {clave: App}."""
    apps, por_id = {}, {}
    archivos = [f for d in DIRS_DESKTOP if d.is_dir() for f in sorted(d.glob("*.desktop"))]
    # Las apps nativas van primero; las de Waydroid al final para no pisar nombres genéricos
    for f in sorted(archivos, key=lambda p: p.stem.startswith("waydroid.")):
        id = f.stem
        if id in por_id or id == "asistente-voz" or id.startswith(WAYDROID_IGNORAR):
            continue
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            cp.read(f, encoding="utf-8")
            e = cp["Desktop Entry"]
        except Exception:
            continue
        if e.get("Type") != "Application" or e.get("NoDisplay") == "true" or e.get("Hidden") == "true":
            continue
        if e.get("Terminal") == "true":
            continue                      # las de consola se manejan con APPS_TERMINAL
        app = App(id, e.get("Name", id), ["kioclient", "exec", str(f)])
        por_id[id] = app
        for k in ("Name", "Name[es]", "GenericName", "GenericName[es]"):
            if e.get(k):
                ck = clave(e[k])
                apps.setdefault(ck, app)
                if corto := re.sub(r"(launcher|desktop|client|browser|player)+$", "", ck):
                    apps.setdefault(corto, app)
        partes = [clave(p) for p in id.split(".") if clave(p) and clave(p) not in ID_PARTES_GENERICAS]
        if partes:
            apps.setdefault(partes[-1], app)
    for tool in APPS_TERMINAL:
        if shutil.which(tool):
            por_id[tool] = apps[tool] = App(tool, tool, cmd_terminal(tool))
    if "kitty" in por_id:
        apps["kitty"] = por_id["kitty"]
    for alias, ids in ALIAS_APPS.items():
        for id in [ids] if isinstance(ids, str) else ids:
            if id in por_id:
                apps[alias] = por_id[id]
                break
    return apps


APPS = cargar_apps()
_APPS_FIRMA, _APPS_REVISADO = _firma_apps(), time.monotonic()


def resolver_app(nombre):
    global APPS, _APPS_FIRMA, _APPS_REVISADO
    k = clave(nombre)
    if not k:
        return None
    if time.monotonic() - _APPS_REVISADO > _REVISION_APPS:
        _APPS_REVISADO = time.monotonic()
        if (firma := _firma_apps()) != _APPS_FIRMA:
            APPS, _APPS_FIRMA = cargar_apps(), firma
            log(f"apps recargadas: {len({a.id for a in APPS.values()})}")
    if k in APPS:
        return APPS[k]
    # quita artículos/verbos al inicio: "la terminal", "the browser", "app de compass"
    k2 = re.sub(r"^(el|la|los|las|the|app|aplicacion|programa)+", "", k)
    if k2 in APPS:
        return APPS[k2]
    cerca = difflib.get_close_matches(k, APPS.keys(), n=1, cutoff=0.82)
    return APPS[cerca[0]] if cerca else None


# --- Acciones (lista blanca) --------------------------------------------------------
ATAJOS_KWIN = {
    "maximizar": "Window Maximize",
    "minimizar": "Window Minimize",
    "pantalla_completa": "Window Fullscreen",
    "izquierda": "Window Quick Tile Left",
    "derecha": "Window Quick Tile Right",
    "otra_pantalla": "Window to Next Screen",
    "mostrar_escritorio": "Show Desktop",
    "siguiente_escritorio": "Switch to Next Desktop",
    "escritorio_anterior": "Switch to Previous Desktop",
    **{f"escritorio_{i}": f"Switch to Desktop {i}" for i in range(1, 10)},
    **{f"mover_escritorio_{i}": f"Window to Desktop {i}" for i in range(1, 10)},
}


def lanzar(cmd):
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def kwin_atajo(nombre):
    subprocess.run(["busctl", "--user", "call", "org.kde.kglobalaccel", "/component/kwin",
                    "org.kde.kglobalaccel.Component", "invokeShortcut", "s", nombre], capture_output=True)


def cerrar_ventana(app=None):
    """Cierra la ventana activa (si app es None) o las ventanas de la app dada mediante KWin."""
    if app is None:
        kwin_atajo("Window Close")
        return
    if app.id in APPS_TERMINAL:
        # Solo las ventanas de terminal cuyo título es la herramienta: un pkill cerraría también
        # las instancias abiertas en otras terminales o sesiones
        cond = f'w.caption.toLowerCase().split(/\\s+/)[0] === {json.dumps(app.id.lower())}'
    else:
        claves = list({app.id.lower(), app.nombre.lower(),
                       *(p.lower() for p in app.id.split(".") if p.lower() not in ID_PARTES_GENERICAS and len(p) > 2)})
        obj = json.dumps(claves)
        cond = f'({obj}.includes(w.desktopFileName.toLowerCase()) || {obj}.includes(w.resourceClass.toLowerCase()))'
    js = RUNTIME / "asistente-voz-kwin.js"
    js.write_text(f"for (const w of workspace.windowList()) {{ if (w.normalWindow && {cond}) w.closeWindow(); }}\n")
    subprocess.run(["busctl", "--user", "call", "org.kde.KWin", "/Scripting",
                    "org.kde.kwin.Scripting", "unloadScript", "s", "asistente_cerrar"], capture_output=True)
    r = subprocess.run(["busctl", "--user", "call", "org.kde.KWin", "/Scripting",
                        "org.kde.kwin.Scripting", "loadScript", "ss", str(js), "asistente_cerrar"],
                       capture_output=True, text=True)
    if sid := (r.stdout.split()[-1] if r.stdout.split() else ""):
        subprocess.run(["busctl", "--user", "call", "org.kde.KWin", f"/Scripting/Script{sid}",
                        "org.kde.kwin.Script", "run"], capture_output=True)
        subprocess.run(["busctl", "--user", "call", "org.kde.KWin", "/Scripting",
                        "org.kde.kwin.Scripting", "unloadScript", "s", "asistente_cerrar"], capture_output=True)


def palabras(nombre):
    nombre = re.sub(r"([a-z])([A-Z])", r"\1 \2", nombre)
    return set(normalizar(nombre).replace(".", " ").replace("_", " ").replace("-", " ").split())


def resolver_tarea(pedido):
    if not TAREAS or not pedido:
        return None
    objetivo = palabras(pedido) - {"tarea", "la", "el", "de", "del", "en", "task", "the", "run", "ejecuta", "corre"}
    if not objetivo:
        return None
    mejor, puntaje = None, 0.0
    for nombre in TAREAS:
        pal = palabras(nombre)
        p = sum(max((difflib.SequenceMatcher(None, o, w).ratio() for w in pal), default=0)
                for o in objetivo) / len(objetivo)
        if p > puntaje:
            mejor, puntaje = nombre, p
    return mejor if puntaje >= 0.75 else None


def proyectos():
    lista = [d for d in PROYECTOS.iterdir() if (d / ".git").is_dir() and not d.name.startswith("_")] if PROYECTOS.is_dir() else []
    if (BASE / ".git").is_dir() and BASE not in lista:
        lista.append(BASE)
    return lista


def buscar_proyecto(pedido):
    disp = proyectos()
    if not disp:
        return None
    objetivo = palabras(pedido) - {"proyecto", "el", "la", "de", "del", "repo", "repositorio", "project", "the", "en", "in"}
    if not objetivo:
        # Si no se especificó nombre, usar el repositorio modificado más recientemente
        return max(disp, key=lambda d: (d / ".git").stat().st_mtime)
    mejor, puntaje = None, 0.0
    for d in disp:
        nombre = palabras(d.name)
        p = sum(max((difflib.SequenceMatcher(None, o, w).ratio() for w in nombre), default=0)
                for o in objetivo) / max(len(objetivo), 1)
        if p > puntaje:
            mejor, puntaje = d, p
    return mejor if puntaje >= 0.75 else None


def klipper_leer():
    r = subprocess.run(["busctl", "--user", "--json=short", "call", "org.kde.klipper", "/klipper",
                        "org.kde.klipper.klipper", "getClipboardContents"],
                       capture_output=True, text=True, timeout=3)
    if r.returncode != 0 or not r.stdout.strip():
        return ""
    try:
        return (json.loads(r.stdout).get("data") or [""])[0].strip()
    except Exception:
        return ""


def parece_secreto(texto):
    """Una sola «palabra» larga que mezcla letras, números y símbolos: contraseña o token."""
    t = texto.strip()
    if not 8 <= len(t) <= 200 or re.search(r"\s", t) or re.match(r"(https?|file)://", t):
        return False
    clases = sum(bool(re.search(p, t)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^\w]"))
    return clases >= 3 or bool(re.fullmatch(r"(sk|ghp|gho|xox[bp]|AKIA)[\w-]{10,}", t))


def klipper_escribir(texto):
    # «--»: un texto que empiece por «-» (dictado o del LLM) no debe tomarse como opción de busctl
    subprocess.run(["busctl", "--user", "call", "--", "org.kde.klipper", "/klipper",
                    "org.kde.klipper.klipper", "setClipboardContents", "s", texto],
                   capture_output=True, timeout=3)


def controlar_luz_nocturna(modo, m):
    r = subprocess.run(["busctl", "--user", "--json=short", "get-property", "org.kde.KWin.NightLight",
                        "/org/kde/KWin/NightLight", "org.kde.KWin.NightLight", "inhibited"],
                       capture_output=True, text=True, timeout=3)
    try:
        inhibida = bool(json.loads(r.stdout)["data"][0])
    except Exception:
        inhibida = None
    if modo == "luz_nocturna_on":
        if inhibida is True or inhibida is None:
            kwin_atajo("Toggle Night Color")
        return m["luz_nocturna_on"]
    if modo == "luz_nocturna_off":
        if inhibida is False or inhibida is None:
            kwin_atajo("Toggle Night Color")
        return m["luz_nocturna_off"]
    kwin_atajo("Toggle Night Color")
    if inhibida is not None:
        return m["luz_nocturna_on" if inhibida else "luz_nocturna_off"]
    return m["luz_nocturna_toggle"]


def ajustar_brillo(param, m):
    p = normalizar(param)
    if p.startswith("luz_nocturna"):
        return controlar_luz_nocturna(p, m)
    svc = ["busctl", "--user", "--json=short", "call", "org.kde.Solid.PowerManagement",
           "/org/kde/Solid/PowerManagement/Actions/BrightnessControl",
           "org.kde.Solid.PowerManagement.Actions.BrightnessControl"]
    rmax = subprocess.run(svc + ["brightnessMax"], capture_output=True, text=True, timeout=3)
    rcur = subprocess.run(svc + ["brightness"], capture_output=True, text=True, timeout=3)
    try:
        bmax = int(json.loads(rmax.stdout)["data"][0])
        bcur = int(json.loads(rcur.stdout)["data"][0])
    except Exception:
        bmax = 0
    if bmax <= 0:
        return m["brillo_no"]
    if p.isdigit():
        pct = max(0, min(100, int(p)))
    else:
        pct_actual = round(bcur * 100 / bmax)
        pct = max(5, min(100, pct_actual + (15 if p == "subir" else -15)))
    nuevo = round(bmax * pct / 100)
    subprocess.run(svc + ["setBrightness", "i", str(nuevo)], capture_output=True, timeout=3)
    return m["brillo_n"].format(x=pct)


def _formatear_duracion(seg, lang):
    if seg >= 3600:
        h, r = divmod(seg, 3600)
        m = r // 60
        if lang == "en":
            return f"{h} hour{'s' if h != 1 else ''}" + (f" {m} min" if m else "")
        return f"{h} hora{'s' if h != 1 else ''}" + (f" y {m} min" if m else "")
    if seg >= 60:
        m, s = divmod(seg, 60)
        if lang == "en":
            return f"{m} minute{'s' if m != 1 else ''}" + (f" {s} s" if s else "")
        return f"{m} minuto{'s' if m != 1 else ''}" + (f" y {s} s" if s else "")
    return f"{seg} second{'s' if seg != 1 else ''}" if lang == "en" else f"{seg} segundo{'s' if seg != 1 else ''}"


def programar_temporizador(param, lang, m):
    partes = param.split(":", 1)
    if not partes[0].isdigit():
        # El LLM a veces devuelve «5 minutos» en vez de segundos
        convertido = extraer_temporizador("temporizador de " + normalizar(param), param)
        if not convertido:
            return m["timer_no"]
        partes = convertido.split(":", 1)
    seg = int(partes[0])
    if seg <= 0:
        return m["timer_no"]
    nota = partes[1].strip() if len(partes) > 1 and partes[1].strip() else _formatear_duracion(seg, lang)

    def avisar():
        msg = m["timer_fin"].format(x=nota)
        sonido("exito", esperar=True)
        notificar("⏰", msg, "dialog-information", segundos=15)
        if _voz_activa and not _voz_activa.hablando:
            _voz_activa.decir(msg, lang)

    hilo = threading.Timer(seg, avisar)
    hilo.daemon = True
    hilo.start()
    return m["timer_ok"].format(t=_formatear_duracion(seg, lang))


def kdeconnect_dispositivo():
    """Devuelve (id, nombre) del primer dispositivo KDE Connect emparejado y alcanzable."""
    r = subprocess.run(["busctl", "--user", "tree", "--list", "org.kde.kdeconnect"],
                       capture_output=True, text=True, timeout=3)
    for linea in r.stdout.splitlines():
        m_dev = re.fullmatch(r"/modules/kdeconnect/devices/([a-zA-Z0-9_]+)", linea.strip())
        if not m_dev:
            continue
        dev_id = m_dev.group(1)
        ruta = f"/modules/kdeconnect/devices/{dev_id}"
        rp = subprocess.run(["busctl", "--user", "--json=short", "get-property", "org.kde.kdeconnect",
                             ruta, "org.kde.kdeconnect.device", "isPaired", "isReachable", "name"],
                            capture_output=True, text=True, timeout=3)
        try:
            datos = [json.loads(l)["data"][0] for l in rp.stdout.splitlines() if l.strip()]
            if len(datos) == 3 and datos[0] and datos[1]:
                return dev_id, str(datos[2])
        except Exception:
            continue
    return None, None


def kdeconnect_accion(modo, m):
    dev_id, nombre = kdeconnect_dispositivo()
    if not dev_id:
        return m["tel_no"]
    if modo == "sonar":
        subprocess.run(["kdeconnect-cli", "-d", dev_id, "--ring"], capture_output=True, timeout=5)
        return m["tel_sonar"].format(x=nombre)
    if modo == "bateria":
        ruta = f"/modules/kdeconnect/devices/{dev_id}/battery"
        rb = subprocess.run(["busctl", "--user", "--json=short", "get-property", "org.kde.kdeconnect",
                             ruta, "org.kde.kdeconnect.device.battery", "charge", "isCharging"],
                            capture_output=True, text=True, timeout=3)
        try:
            carga, cargando = [json.loads(l)["data"][0] for l in rb.stdout.splitlines() if l.strip()]
            return m["tel_bat"].format(x=nombre, b=carga, c=m["tel_cargando"] if cargando else "")
        except Exception:
            return m["fallo"]
    if modo == "portapapeles":
        texto = klipper_leer()
        if not texto:
            return m["clip_vacio"]
        subprocess.run(["kdeconnect-cli", "-d", dev_id, f"--share-text={texto}"], capture_output=True, timeout=5)
        return m["tel_clip"].format(x=nombre)
    return m["no_entendi"]


def git_info_proyecto(param, m):
    modo, sep, proy = param.partition(":")
    if not sep and modo not in ("estado", "commit"):
        modo, proy = "estado", modo
    if normalizar(proy) in ("main", "master", "head", "git"):
        proy = ""
    d = buscar_proyecto(proy)
    if not d:
        return m["proyecto_no"].format(x=proy or "actual")
    if modo == "commit":
        r = subprocess.run(["git", "-C", str(d), "log", "-1", "--pretty=format:%s (%cr)"],
                           capture_output=True, text=True, timeout=5)
        return m["git_commit"].format(x=d.name, c=r.stdout.strip() or "—")
    rama = subprocess.run(["git", "-C", str(d), "branch", "--show-current"],
                          capture_output=True, text=True, timeout=5).stdout.strip() or "HEAD"
    estado = subprocess.run(["git", "-C", str(d), "status", "--porcelain"],
                            capture_output=True, text=True, timeout=5).stdout.splitlines()
    cambios = m["git_cambios"].format(n=len(estado)) if estado else m["git_limpio"]
    # Según el último fetch (no se conecta a la red)
    r = subprocess.run(["git", "-C", str(d), "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
                       capture_output=True, text=True, timeout=5)
    sync = ""
    if r.returncode == 0 and len(cuenta := r.stdout.split()) == 2:
        subir, bajar = int(cuenta[0]), int(cuenta[1])
        sync = (m["git_subir"].format(n=subir) if subir else "") + (m["git_bajar"].format(n=bajar) if bajar else "")
    return m["git_estado"].format(x=d.name, r=rama, cambios=cambios, sync=sync)


def resolver_contenedor(pedido, aproximado=False):
    """Nombre exacto del contenedor (sin el sufijo -local). Con aproximado=True también acepta nombres
    parecidos, pero nunca una subcadena: «la rapidez» contiene «api» y no debe reiniciar el contenedor api."""
    p = clave(re.sub(r"^((el|la|the)\s+)?(contenedor|container)\s+(de\s+|del\s+)?", "", normalizar(pedido or "")))
    if not p:
        return None
    r = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=5)
    nombres = r.stdout.split()
    base = {n: clave(n.removesuffix("-local")) for n in nombres}
    for n, b in base.items():
        if p in (b, clave(n)):
            return n
    if not aproximado or not nombres:
        return None
    mejor = max(nombres, key=lambda n: difflib.SequenceMatcher(None, p, base[n]).ratio())
    return mejor if difflib.SequenceMatcher(None, p, base[mejor]).ratio() >= 0.8 else None


def pista_whisper():
    """Vocabulario de contexto para Whisper. Lista de términos, no frases: con frases de ejemplo
    Whisper tiende a cortar las transcripciones."""
    nombres = [d.name for d in proyectos()]
    # "Empresa.Modulo.Sub" -> "Modulo Sub" cuando el prefijo se repite en varios repos
    prefijos = [n.split(".")[0] for n in nombres if "." in n]
    nombres = [n.split(".", 1)[1] if "." in n and prefijos.count(n.split(".")[0]) >= 3 else n for n in nombres]
    nombres = [n.replace(".", " ") for n in nombres]
    return (f"{NOMBRE}, VS Code, Visual Studio Code, Firefox, kitty, htop, btop, nvtop, KWrite, lazygit, lazydocker, "
            "MongoDB Compass, Docker, " + ", ".join(n for n in nombres if n))


NAVEGADORES_MPRIS = ("edge", "firefox", "chromium", "chrome", "brave", "plasma-browser-integration")


def mpris(metodo, solo_activo=False):
    r = subprocess.run(["busctl", "--user", "list", "--no-legend"], capture_output=True, text=True)
    reproductores = [l.split()[0] for l in r.stdout.splitlines() if l.startswith("org.mpris.MediaPlayer2.")]
    if not reproductores:
        return False
    # Priorizar reproductores dedicados (Spotify, Elisa, VLC…) antes que navegadores web
    reproductores.sort(key=lambda n: any(b in n.lower() for b in NAVEGADORES_MPRIS))
    for nombre in reproductores:
        subprocess.run(["busctl", "--user", "call", nombre, "/org/mpris/MediaPlayer2",
                        "org.mpris.MediaPlayer2.Player", metodo], capture_output=True)
        if solo_activo:
            break
    return True


def temperatura_cpu():
    for hw in Path("/sys/class/hwmon").iterdir():
        try:
            if (hw / "name").read_text().strip() in ("coretemp", "k10temp", "zenpower"):   # Intel / AMD
                return int((hw / "temp1_input").read_text()) // 1000
        except OSError:
            continue
    return None


def info_gpu_nvidia():
    if not shutil.which("nvidia-smi"):
        return None
    r = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,memory.used",
                        "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
    partes = [p.strip() for p in r.stdout.split(",")]
    if r.returncode == 0 and len(partes) == 3 and all(p.isdigit() for p in partes):
        return int(partes[0]), int(partes[1]), int(partes[2]) / 1024.0
    return None


def bateria_sistema():
    for bat in Path("/sys/class/power_supply").glob("BAT*"):
        try:
            return int((bat / "capacity").read_text().strip())
        except OSError:
            continue
    return None


def red_activa():
    if not shutil.which("nmcli"):
        return None
    r = subprocess.run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
                       capture_output=True, text=True, timeout=3)
    for linea in r.stdout.splitlines():
        nombre, _, tipo = linea.partition(":")
        if nombre and tipo not in ("loopback", "bridge") and not nombre.startswith(("docker", "br-", "veth")):
            return nombre
    return None


def perfil_energia_actual():
    """Perfil de power-profiles-daemon. /sys/firmware/acpi/platform_profile no existe en todos los equipos."""
    r = subprocess.run(["busctl", "--system", "get-property", "org.freedesktop.UPower.PowerProfiles",
                        "/org/freedesktop/UPower/PowerProfiles", "org.freedesktop.UPower.PowerProfiles",
                        "ActiveProfile"], capture_output=True, text=True)
    m = re.search(r'"(.+)"', r.stdout)
    if m:
        return m.group(1)
    f = Path("/sys/firmware/acpi/platform_profile")
    return f.read_text().strip() if f.exists() else "?"


def estado_sistema(m):
    info = dict(l.split(":", 1) for l in Path("/proc/meminfo").read_text().splitlines())
    total = int(info["MemTotal"].split()[0]) / 1048576
    libre = int(info["MemAvailable"].split()[0]) / 1048576
    perfil = perfil_energia_actual()
    gpu = info_gpu_nvidia()
    bat = bateria_sistema()
    red = red_activa()
    return m["estado"].format(
        t=temperatura_cpu() or "?",
        gpu=m["gpu"].format(t=gpu[0], uso=gpu[1], vram=gpu[2]) if gpu else "",
        u=total - libre, m=total, d=shutil.disk_usage("/").free / 1e9,
        bat=m["bat"].format(b=bat) if bat is not None else "",
        red=m["red"].format(r=red) if red else "",
        p=m["perfiles"].get(perfil, perfil),
    )


def diagnostico_sistema(param, m):
    if param in ("cpu", "memoria"):
        llave = "-%cpu" if param == "cpu" else "-%mem"
        r = subprocess.run(["ps", "-eo", "comm,%cpu,%mem", f"--sort={llave}", "--no-headers"],
                           capture_output=True, text=True, timeout=4)
        vistos = []
        for l in r.stdout.splitlines():
            partes = l.split()
            if len(partes) >= 3 and partes[0] not in ("ps", "asistente.py") and partes[0] not in vistos:
                val = partes[1] if param == "cpu" else partes[2]
                vistos.append(f"{partes[0]} ({val}%)")
            if len(vistos) == 3:
                break
        return m["top_proc"].format(x=", ".join(vistos) or "—")

    if param.startswith("puerto:"):
        puerto = param.split(":", 1)[1].strip()
        r = subprocess.run(["ss", "-tulpn", f"sport = :{puerto}"], capture_output=True, text=True, timeout=4)
        lineas = r.stdout.splitlines()[1:]
        if not lineas:
            return m["puerto_libre"].format(p=puerto)
        procs = sorted(set(re.findall(r'"([^"]+)"', r.stdout)))
        return m["puerto_uso"].format(p=puerto, x=", ".join(procs) if procs else "un proceso activo")

    if param == "puertos":
        r = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=4)
        hallados = []
        for l in r.stdout.splitlines()[1:]:
            m_port = re.search(r":(\d+)\s", l)
            m_proc = re.search(r'"([^"]+)"', l)
            if m_port:
                p = int(m_port.group(1))
                etiqueta = f"{p} ({m_proc.group(1)})" if m_proc else str(p)
                if etiqueta not in hallados:
                    hallados.append(etiqueta)
        return m["puertos_lista"].format(x=", ".join(hallados[:6])) if hallados else m["puertos_ninguno"]

    if param == "actualizaciones":
        n = 0
        # Arch: checkupdates usa una copia de la base de datos (pacman -Qu solo ve el último -Sy)
        for cmd in (["checkupdates"], ["pacman", "-Qu"]):
            if shutil.which(cmd[0]):
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                n += len([l for l in r.stdout.splitlines() if l.strip()])
                break
        for aur in ("yay", "paru"):
            if shutil.which(aur):
                r = subprocess.run([aur, "-Qua"], capture_output=True, text=True, timeout=30)
                n += len([l for l in r.stdout.splitlines() if l.strip()])
                break
        if shutil.which("dnf"):   # Fedora: devuelve 100 si hay actualizaciones; una línea "paquete.arq versión repo"
            r = subprocess.run(["dnf", "-q", "check-update"], capture_output=True, text=True, timeout=60)
            n += len([l for l in r.stdout.splitlines() if re.match(r"^\S+\.\S+\s+\S+\s+\S+\s*$", l)])
        if shutil.which("flatpak"):
            r = subprocess.run(["flatpak", "remote-ls", "--updates"], capture_output=True, text=True, timeout=15)
            n += len([l for l in r.stdout.splitlines() if l.strip()])
        return m["updates_n"].format(n=n) if n > 0 else m["updates_0"]

    return m["no_entendi"]


def comando_actualizar():
    if COMANDO_ACTUALIZAR:
        return COMANDO_ACTUALIZAR
    pasos = []
    # yay y paru piden sudo por su cuenta; no deben ejecutarse como root
    for cmd in ("yay -Syu", "paru -Syu", "sudo pacman -Syu", "sudo dnf upgrade --refresh"):
        if shutil.which(cmd.removeprefix("sudo ").split()[0]):
            pasos.append(cmd)
            break
    if shutil.which("flatpak"):
        pasos.append("flatpak update")
    return "; ".join(pasos) or None


def comando_limpiar():
    if COMANDO_LIMPIAR:
        return COMANDO_LIMPIAR
    pasos = []
    # yay/paru -Sc limpian la caché de pacman y la de AUR (preguntan antes); paccache conserva 3 versiones
    for cmd in ("yay -Sc", "paru -Sc", "sudo paccache -r", "sudo pacman -Sc", "sudo dnf clean packages"):
        if shutil.which(cmd.removeprefix("sudo ").split()[0]):
            pasos.append(cmd)
            break
    if shutil.which("flatpak"):
        pasos.append("flatpak uninstall --unused")   # runtimes que ya no usa ninguna app
    return "; ".join(pasos) or None


# --- Ejecutores individuales por acción --------------------------------------------
def _ej_abrir_app(param, _lang, m):
    app = resolver_app(param)
    if not app:
        return m["app_no"].format(x=param)
    lanzar(app.cmd)
    return m["abriendo"].format(x=app.nombre)


def _ej_cerrar_app(param, _lang, m):
    if not param:
        cerrar_ventana(None)
        return ""
    app = resolver_app(param)
    if not app:
        return m["app_no"].format(x=param)
    cerrar_ventana(app)
    return m["cerrando"].format(x=app.nombre)


def _ej_abrir_proyecto(param, _lang, m):
    d = buscar_proyecto(param)
    if not d:
        return m["proyecto_no"].format(x=param)
    lanzar(["code", str(d)])
    return m["proyecto"].format(x=d.name)


def _ej_terminal_proyecto(param, _lang, m):
    modo, _, proy = param.partition(":")
    d = buscar_proyecto(proy)
    if not d:
        return m["proyecto_no"].format(x=proy or "actual")
    if modo == "lazygit" and shutil.which("lazygit"):
        lanzar(cmd_terminal("bash", "-c", f"cd {shlex.quote(str(d))} && exec lazygit"))
        return m["proyecto_term"].format(t="lazygit", x=d.name)
    lanzar(cmd_terminal("bash", "-c", f"cd {shlex.quote(str(d))} && exec ${{SHELL:-bash}}"))
    return m["proyecto_term"].format(t=TERMINAL, x=d.name)


def _ej_ejecutar_tarea(param, _lang, m):
    nombre = resolver_tarea(param)
    if not nombre:
        return m["tarea_no"].format(x=param)
    cmd = TAREAS[nombre]
    lanzar(comando_en_terminal(cmd, m))
    return m["tarea"].format(x=nombre)


def _ej_ventana(param, _lang, m):
    atajo = ATAJOS_KWIN.get(normalizar(param))
    if not atajo:
        return m["no_entendi"]
    kwin_atajo(atajo)
    return ""


def _ej_portapapeles(param, _lang, m):
    contenido = klipper_leer()
    if not contenido:
        return m["clip_vacio"]
    recorte = contenido[:1500]
    if param == "leer":
        return m["clip_secreto"] if parece_secreto(contenido) else recorte[:400]
    clave_prompt = {"resumir": "clip_prompt_resumir", "traducir": "clip_prompt_traducir",
                    "explicar": "clip_prompt_explicar"}.get(param, "clip_prompt_explicar")
    notificar(m["pensando"], recorte[:120], "dialog-question")
    return llm(MODELO_PREGUNTAS, m["responde"], m[clave_prompt].format(x=recorte)).strip()


def _ej_dictar(param, _lang, m):
    if not param:
        return m["no_entendi"]
    klipper_escribir(param)
    return m["dictado_ok"].format(x=param)


def _ej_docker_estado(_param, _lang, m):
    r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True)
    nombres = [n.replace("-local", "").replace("-", " ") for n in r.stdout.split()]
    if not nombres:
        return m["docker_0"]
    return m["docker_n"].format(n=len(nombres), x=", ".join(nombres))


def _ej_docker_compose(up, m):
    if not COMPOSE:
        return m["docker_sin_compose"]
    notificar("Docker", m["docker_up" if up else "docker_stop"])
    r = subprocess.run(["docker", "compose", "-f", str(COMPOSE)] + (["up", "-d"] if up else ["stop"]),
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        log(r.stderr)
        return m["docker_error"]
    return m["docker_up_ok" if up else "docker_stop_ok"]


def _ej_docker_logs(param, _lang, m):
    cont = resolver_contenedor(param, aproximado=True)   # ver logs no cambia nada
    if not cont:
        return m["docker_cont_no"].format(x=param)
    lanzar(cmd_terminal("docker", "logs", "-f", "--tail", "100", cont))
    return m["docker_logs"].format(x=cont)


def _ej_docker_reiniciar(param, _lang, m):
    cont = resolver_contenedor(param)
    if not cont:
        return m["docker_cont_no"].format(x=param)
    r = subprocess.run(["docker", "restart", cont], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        log(r.stderr)
        return m["docker_error"]
    return m["docker_restart"].format(x=cont)


def _ej_volumen(param, _lang, m):
    sink = "@DEFAULT_AUDIO_SINK@"
    p = normalizar(param)
    if p.isdigit():
        subprocess.run(["wpctl", "set-volume", "-l", "1.0", sink, f"{min(int(p), 100)}%"])
        return m["vol_n"].format(x=min(int(p), 100))
    if p == "silenciar":
        subprocess.run(["wpctl", "set-mute", sink, "1"])
        return ""
    if p == "activar":
        subprocess.run(["wpctl", "set-mute", sink, "0"])
        return m["vol_on"]
    subprocess.run(["wpctl", "set-mute", sink, "0"])
    subprocess.run(["wpctl", "set-volume", "-l", "1.0", sink, "10%+" if p == "subir" else "10%-"])
    return ""


def _ej_multimedia(param, _lang, m):
    metodo = {"pausa": "Pause", "reproducir": "Play", "siguiente": "Next", "anterior": "Previous"}.get(param)
    if not metodo:
        return m["media_no"]
    if not mpris(metodo, solo_activo=metodo in ("Play", "Next", "Previous")):
        if metodo == "Play" and (sp := resolver_app("spotify")):
            lanzar(sp.cmd)
            return m["abriendo"].format(x=sp.nombre)
        return m["media_0"]
    return ""


def _ej_perfil_energia(param, _lang, m):
    perfil = {"rendimiento": "performance", "equilibrado": "balanced", "ahorro": "power-saver"}.get(param)
    if not perfil:
        return m["perfil_no"]
    subprocess.run(["busctl", "--system", "set-property", "org.freedesktop.UPower.PowerProfiles",
                    "/org/freedesktop/UPower/PowerProfiles", "org.freedesktop.UPower.PowerProfiles",
                    "ActiveProfile", "s", perfil])
    return m["perfil"].format(x=m["perfiles"][perfil])


def _ej_buscar_web(param, _lang, m):
    consulta = param.lstrip("- ")       # si empezara por «-», Firefox la tomaría como una opción
    if not consulta:
        return m["no_entendi"]
    lanzar(["firefox", "--search", consulta])
    return m["buscando"].format(x=consulta)


def _ej_bloquear_pantalla(_param, _lang, _m):
    subprocess.run(["loginctl", "lock-sessions"])
    return ""


def _ej_actualizar_sistema(_param, _lang, m):
    # En una terminal: sudo necesita tu contraseña y así ves el progreso. El asistente nunca la maneja.
    cmd = comando_actualizar()
    if not cmd:
        return m["actualizar_no"]
    lanzar(comando_en_terminal(cmd, m))
    return m["actualizando"]


def _ej_limpiar_cache(_param, _lang, m):
    cmd = comando_limpiar()
    if not cmd:
        return m["limpiar_no"]
    lanzar(comando_en_terminal(cmd, m))
    return m["limpiando"]


# KDE cierra las apps con orden (y guarda la sesión) antes de reiniciar o apagar; systemctl es el respaldo
ENERGIA_KDE = {"reiniciar": "logoutAndReboot", "apagar": "logoutAndShutdown"}
ENERGIA_SYSTEMCTL = {"suspender": "suspend", "reiniciar": "reboot", "apagar": "poweroff"}


def _ej_energia_equipo(param, _lang, m):
    if param not in ENERGIA_SYSTEMCTL:
        return m["no_entendi"]
    metodo = ENERGIA_KDE.get(param)
    if metodo:
        r = subprocess.run(["busctl", "--user", "call", "org.kde.Shutdown", "/Shutdown", "org.kde.Shutdown", metodo],
                           capture_output=True, timeout=10)
        if r.returncode == 0:
            return m[f"energia_{param}"]
    subprocess.run(["systemctl", ENERGIA_SYSTEMCTL[param]], timeout=30)
    return m[f"energia_{param}"]


def _ej_pregunta(param, _lang, m):
    notificar(m["pensando"], param, "dialog-question")
    return llm(MODELO_PREGUNTAS, m["responde"], param).strip()


def _ej_redactar(param, _lang, m):
    notificar(m["redactando"], param, "document-edit", segundos=30)
    texto = llm(MODELO_REDACTAR, m["redacta"], param).strip()
    if bloque := re.search(r"```[\w+#-]*\n(.*?)```", texto, flags=re.DOTALL):   # algunos modelos lo envuelven igual
        texto = bloque.group(1).strip()
    if not texto:
        return m["no_entendi"]
    klipper_escribir(texto)
    log(f"redactado ({len(texto)} caracteres) con {MODELO_REDACTAR}")
    return m["redactar_ok"]


# --- Imágenes: Fooocus u OpenVINO ---------------------------------------------------
PROMPT_IMAGEN = ("Convierte la petición en un prompt en inglés para Stable Diffusion: una sola línea con el sujeto, "
                 "el estilo, la iluminación y la composición, separados por comas. Responde solo el prompt, sin comillas.")
PEDIDO_IMAGEN = (r"^.*?\b(genera\w*|crea\w*|hazme|haz|dibuja\w*|pinta\w*|ilustra\w*|generate|create|make|draw|paint)\b"
                 r"(\s+(me|us))?\s*((un|una|el|la|a|an|the)\s+)?((imagen\w*|foto\w*|dibujo\w*|ilustracion\w*|image\w*|"
                 r"picture\w*|photo\w*|drawing|illustration|painting)\s+(de|del|con|of|with)\s+)?")
_imagen_en_curso = threading.Lock()


def hay_fooocus():
    return bool(FOOOCUS) and (FOOOCUS / "venv/bin/python").is_file()


def hay_openvino_imagen():
    import importlib.util
    return (IMAGEN_OV / "model_index.json").is_file() and importlib.util.find_spec("openvino_genai") is not None


def motor_imagenes():
    """"fooocus", "openvino" o None si no hay ninguno disponible."""
    if IMAGENES in ("auto", "fooocus") and hay_fooocus():
        return "fooocus"
    if IMAGENES in ("auto", "openvino") and hay_openvino_imagen():
        return "openvino"
    return None


def carpeta_imagenes():
    if CARPETA_IMAGENES:
        return CARPETA_IMAGENES
    try:
        r = subprocess.run(["xdg-user-dir", "PICTURES"], capture_output=True, text=True, timeout=3)
        base = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else HOME / "Pictures"
    except (OSError, subprocess.TimeoutExpired):
        base = HOME / "Pictures"
    return base / NOMBRE


def prompt_imagen(pedido):
    """El LLM describe la imagen en inglés (SDXL entiende poco español). Sin LLM, la descripción tal cual."""
    descripcion = re.sub(PEDIDO_IMAGEN, "", pedido, count=1, flags=re.IGNORECASE).strip(" .?!,") or pedido
    if LLM == "ninguno":
        return descripcion
    try:
        prompt = llm(MODELO_PREGUNTAS, PROMPT_IMAGEN, pedido).strip().splitlines()[0].strip(" \"'")
        return prompt or descripcion
    except Exception as e:
        log(f"prompt de imagen sin LLM ({e}): uso la descripción")
        return descripcion


def liberar_vram_llm():
    """Descarga de la GPU los modelos de Ollama: SDXL necesita casi toda la VRAM de una gráfica de 6 GB."""
    if LLM != "ollama" or not llm_local():
        return
    for modelo in {MODELO_COMANDOS, MODELO_PREGUNTAS, MODELO_REDACTAR}:
        req = urllib.request.Request(f"{LLM_URL}/api/generate", json.dumps({"model": modelo, "keep_alive": 0}).encode(),
                                     {"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15).close()
        except Exception as e:
            log(f"no se pudo descargar {modelo} de la GPU: {e}")


def generar_imagen(prompt, salida, motor="fooocus"):
    """Lanza herramientas/imagen_<motor>.py en un proceso aparte: Fooocus con su propio Python, OpenVINO con el
    del asistente (así la RAM de la difusión se libera al terminar). La petición va por stdin: el texto dictado
    nunca llega a la línea de comandos. Devuelve la ruta del PNG o None."""
    if motor == "openvino":
        cmd = [sys.executable, "-I", str(BASE / "herramientas/imagen_openvino.py"), str(IMAGEN_OV), IMAGEN_DISPOSITIVO]
    else:
        cmd = [str(FOOOCUS / "venv/bin/python"), "-I", str(BASE / "herramientas/imagen_fooocus.py"), str(FOOOCUS)]
    r = subprocess.run(cmd, input=json.dumps({"prompt": prompt, "salida": str(salida)}),
                       capture_output=True, text=True, timeout=900)
    try:   # Fooocus también escribe en stdout: el resultado es la última línea
        resultado = json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        resultado = {}
    if not resultado.get("ok"):
        log(f"error generando la imagen: {resultado.get('error') or r.stderr.strip()[-800:]}")
        return None
    return resultado["ruta"]


def _ej_imagen(param, lang, m):
    motor = motor_imagenes()
    if not motor:
        return m["imagen_no"]
    usa_gpu = motor == "fooocus" or IMAGEN_DISPOSITIVO != "CPU"   # en CPU no hace falta sacar el LLM de la GPU
    if not _imagen_en_curso.acquire(blocking=False):
        return m["imagen_ocupado"]
    try:
        prompt = prompt_imagen(param)        # con el LLM aún cargado
    except BaseException:
        _imagen_en_curso.release()
        raise
    log(f"imagen ({motor}): {prompt!r}")

    def en_segundo_plano():                  # 20 s-1,5 min: mientras, el asistente sigue atendiendo
        try:
            carpeta = carpeta_imagenes()
            carpeta.mkdir(parents=True, exist_ok=True)
            if usa_gpu:
                liberar_vram_llm()
            t = time.time()
            ruta = generar_imagen(prompt, carpeta / time.strftime("%Y%m%d-%H%M%S.png"), motor)
        except Exception as e:
            log(f"error generando la imagen: {e}")
            ruta = None
        finally:
            _imagen_en_curso.release()
        if ruta:
            log(f"imagen lista en {time.time() - t:.0f}s: {ruta}")
            lanzar(["xdg-open", ruta])
        msg = m["imagen_ok" if ruta else "imagen_error"]
        sonido("exito" if ruta else "fallo", esperar=True)
        notificar("🖼", msg if not ruta else f"{msg}\n{ruta}", ruta or "dialog-warning", segundos=10)
        if _voz_activa and not _voz_activa.hablando:
            _voz_activa.decir(msg, lang)
        if usa_gpu and LLM == "ollama":      # vuelve a cargar el modelo de comandos para el siguiente
            try:
                llm(MODELO_COMANDOS, "Responde ok.", "ok")
            except Exception as e:
                log(f"no se pudo recargar {MODELO_COMANDOS}: {e}")

    threading.Thread(target=en_segundo_plano, daemon=True).start()
    return m["imagen_generando"]


# --- Tabla declarativa de acciones e interpretación --------------------------------
PARAM_APP = "<app>"   # en los ejemplos: el parámetro es una app que se busca en la frase
NO_ES_PREGUNTA = r"^(?!\W*(que|what|como|how|por que|why|cual|which)\b)"
# «Genera una imagen de…», «dibújame…». «Crea una imagen de docker» es otra cosa
PIDE_IMAGEN = (NO_ES_PREGUNTA + r"(?!.*\b(docker|contenedor\w*|container\w*|iso|disco|disk|podman)\b)"
               r".*\b(genera\w*|crea\w*|haz|hazme|dibuja\w*|pinta\w*|ilustra\w*|generate|create|make|draw|paint)\b"
               r".*\b(imagen\w*|foto\w*|dibujo\w*|ilustracion\w*|image\w*|picture\w*|photo\w*|drawing\w*|"
               r"illustration\w*|painting\w*)\b")
# «Redacta…», «escríbeme un correo…», «write a function…». «Escribe hola mundo» sigue siendo dictado
DOCUMENTOS = (r"(correo|email|e-?mail|mail|mensaje|carta|parrafo|texto|resumen|poema|cancion|historia|cuento|post|"
              r"publicacion|tuit|tweet|descripcion|respuesta|invitacion|discurso|funcion|script|codigo|programa|"
              r"clase|consulta|query|regex|expresion regular|message|letter|paragraph|text|summary|poem|song|story|"
              r"description|reply|invitation|speech|function|code|program|class)")
PIDE_REDACTAR = (NO_ES_PREGUNTA + r"\W*(redacta\w*|componme|compon|escribeme|escribe|escribir|genera\w*|crea\w*|"
                 r"hazme|haz|draft|write|compose|generate|create)\b(\s+(me|nos|le|les))?\s+"
                 r"(un|una|unos|unas|el|la|a|an|the|some)\s+(\w+\s+)?" + DOCUMENTOS + r"\b")


EQUIPO = r"(el |la |the |my |this )?(equipo|compu\w*|pc|ordenador|maquina|sistema|portatil|laptop|computer|machine|system)\b"
# El parámetro sale de la frase, no del LLM: el verbo dicho decide si se suspende, reinicia o apaga
VERBOS_ENERGIA = {
    "suspender": r"\b(suspend\w*|hiberna\w*|a dormir|to sleep)\b",
    "reiniciar": r"\b(reinicia\w*|restart\w*|reboot\w*)\b",
    "apagar": r"\b(apaga\w*|shut\s*down|power\s*off|turn\s*off)\b",
}


class DefinicionAccion(NamedTuple):
    nombre: str
    prompt: str
    evidencia: str | None = None
    reglas: tuple[tuple[str, str | None], ...] = ()
    ejecutor: object = None
    # Con riesgo: deshacerla molesta (bloquear, cerrar, detener…). Lo que propongan el clasificador o el LLM
    # solo se acepta si la frase cumple `evidencia`. En las demás, la evidencia descartaba órdenes válidas
    # («necesito el navegador», «what's eating my cpu»).
    riesgo: bool = False
    # (frase, parámetro) para el clasificador por embeddings. PARAM_APP = buscar la app en la frase
    ejemplos: tuple[tuple[str, str], ...] = ()


REGISTRO_ACCIONES = [
    DefinicionAccion(
        nombre="abrir_app",
        ejemplos=(("abre firefox", PARAM_APP), ("lanza spotify", PARAM_APP),
                  ("pon el editor de código", PARAM_APP), ("quiero usar la calculadora", PARAM_APP),
                  ("ábreme la consola", PARAM_APP), ("open the file manager", PARAM_APP),
                  ("start chrome", PARAM_APP), ("trae el explorador de archivos", PARAM_APP)),
        prompt="abrir una aplicación (open/launch). parametro = nombre de la app tal como lo dijo (ej: firefox, vscode, kitty, compass, htop, terminal, calculadora)",
        evidencia=r"\b(abr|lanz|inici|ejecut|muestr|mostr|open|launch|start|run|show)\w*",
        ejecutor=_ej_abrir_app,
    ),
    DefinicionAccion(
        nombre="cerrar_app",
        riesgo=True,
        ejemplos=(("cierra spotify", PARAM_APP), ("quita firefox", PARAM_APP), ("close the calculator", PARAM_APP),
                  ("mata el reproductor", PARAM_APP), ("cierra esto", ""), ("cierra la ventana", ""),
                  ("close this window", ""), ("quítame esto de la pantalla", "")),
        prompt='cerrar una aplicación o la ventana actual (close/quit). parametro = nombre de la app tal como lo dijo, o "" si dice "la ventana" / "the window"',
        evidencia=r"\b(cierr|cerr|close|quit)\w*",
        ejecutor=_ej_cerrar_app,
    ),
    DefinicionAccion(
        nombre="abrir_proyecto",
        prompt="abrir un proyecto/repositorio en VS Code. parametro = nombre del proyecto tal como lo dijo",
        evidencia=r"\b(abr|proyecto|repo|open|project)\w*",
        ejecutor=_ej_abrir_proyecto,
    ),
    DefinicionAccion(
        nombre="terminal_proyecto",
        prompt="abrir la terminal o lazygit en un proyecto. parametro = terminal:<proyecto> | lazygit:<proyecto>",
        evidencia=r"\b(terminal|consola|lazygit)\b.*\b(proyecto|project|en|in)\b",
        ejecutor=_ej_terminal_proyecto,
    ),
    DefinicionAccion(
        nombre="git_proyecto",
        prompt="estado de git o último commit de un proyecto. parametro = estado:<proyecto> | commit:<proyecto>",
        evidencia=r"\b(estado|status|commit|comi|git|rama|branch)\b",
        ejecutor=lambda p, _l, m: git_info_proyecto(p, m),
    ),
    DefinicionAccion(
        nombre="ejecutar_tarea",
        riesgo=True,
        prompt="ejecutar una tarea configurada (run task). parametro = nombre de la tarea tal como lo dijo",
        evidencia=r"\b(ejecut|corr|lanz|inici|tarea|run|start|task)\w*",
        ejecutor=_ej_ejecutar_tarea,
    ),
    DefinicionAccion(
        nombre="ventana",
        riesgo=True,
        ejemplos=(("maximiza la ventana", "maximizar"), ("maximize this window", "maximizar"),
                  ("minimiza la ventana", "minimizar"), ("minimize this window", "minimizar"),
                  ("esconde esta ventana", "minimizar"), ("muestra el escritorio", "mostrar_escritorio"),
                  ("show desktop", "mostrar_escritorio")),
        prompt="gestionar la ventana actual o cambiar de escritorio. parametro = maximizar | minimizar | pantalla_completa | izquierda | derecha | otra_pantalla | mostrar_escritorio | siguiente_escritorio | escritorio_anterior | escritorio_1..9 | mover_escritorio_1..9",
        evidencia=(r"maximiz|minimiz|pantalla completa|full\s*screen|ventana|window|escritorio|desktop|otra pantalla"
                   r"|otro monitor|next screen|other screen|izquierda|left|derecha|right"),
        reglas=(
            (r"\b(maximiza\w*|maximize)\b", "maximizar"),
            (r"\b(minimiza\w*|minimize)\b", "minimizar"),
            (r"\b(pantalla completa|full\s*screen)\b", "pantalla_completa"),
            (r"\b(muestra|mostrar)\w* (el )?escritorio|\bshow (the )?desktop\b", "mostrar_escritorio"),
            (r"\b(manda|mueve|pasa|envia|send|move)\w* .*?\b(otra pantalla|otro monitor|next screen|other screen|other monitor)\b", "otra_pantalla"),
            (r"\b(ventana|window|pon\w*|manda\w*|mueve\w*|move\w*|send\w*|tile)\b .*?\b(a la izquierda|to the left|left)\b", "izquierda"),
            (r"\b(ventana|window|pon\w*|manda\w*|mueve\w*|move\w*|send\w*|tile)\b .*?\b(a la derecha|to the right|right)\b", "derecha"),
            (r"\b(siguiente escritorio|escritorio siguiente|next desktop)\b", "siguiente_escritorio"),
            (r"\b(escritorio anterior|previous desktop)\b", "escritorio_anterior"),
        ),
        ejecutor=_ej_ventana,
    ),
    DefinicionAccion(
        nombre="portapapeles",
        ejemplos=(("explica lo que copié", "explicar"), ("explain the clipboard", "explicar"),
                  ("resume lo que copié", "resumir"), ("summarize the clipboard", "resumir"),
                  ("traduce lo que copié", "traducir"), ("translate the clipboard", "traducir")),
        prompt="usar el texto copiado en el portapapeles (clipboard). parametro = leer | resumir | traducir | explicar",
        evidencia=r"portapapeles|clipboard|copi\w*|copied",
        reglas=(
            (r"\b(lee\w*|leame|leer|dime|read)\b.*?\b(copi\w*|portapapeles|clipboard|copied)\b", "leer"),
            (r"\b(resum\w*|summarize)\b.*?\b(copi\w*|portapapeles|clipboard|copied)\b", "resumir"),
            (r"\b(traduc\w*|translate)\b.*?\b(copi\w*|portapapeles|clipboard|copied)\b", "traducir"),
            (r"\b(explic\w*|explain)\b.*?\b(copi\w*|portapapeles|clipboard|copied)\b", "explicar"),
        ),
        ejecutor=_ej_portapapeles,
    ),
    DefinicionAccion(
        nombre="temporizador",
        prompt="poner un temporizador o recordatorio (timer/reminder). parametro = segundos (ej: 600)",
        evidencia=r"temporizador|timer|avisame|recuerdame|remind|alarm",
        ejecutor=programar_temporizador,
    ),
    DefinicionAccion(
        nombre="brillo",
        riesgo=True,
        ejemplos=(("sube el brillo", "subir"), ("más brillo", "subir"), ("brightness up", "subir"),
                  ("pon la pantalla más clara", "subir"), ("la pantalla brilla demasiado", "bajar"),
                  ("baja el brillo", "bajar"), ("menos brillo", "bajar"), ("brightness down", "bajar")),
        prompt="ajustar el brillo de pantalla o la luz nocturna (brightness/night light). parametro = subir | bajar | 0..100 | luz_nocturna | luz_nocturna_on | luz_nocturna_off",
        evidencia=r"brillo|brightness|luz nocturna|modo nocturno|night light|night color|pantalla\b.*\b(oscur|clar|brill)|screen\b.*\b(dark|bright|dim)",
        reglas=(
            (r"\b(sube|subir|aumenta)\w* (el )?brillo|\b(increase|turn up|raise) (the )?brightness|\bbrightness up\b", "subir"),
            (r"\b(baja|bajar|disminu)\w* (el )?brillo|\b(decrease|turn down|lower) (the )?brightness|\bbrightness down\b", "bajar"),
            (r"\b(activa|enciende|pon|enable|turn on)\w* .*?\b(luz nocturna|modo nocturno|night light|night color)\b", "luz_nocturna_on"),
            (r"\b(desactiva|apaga|quita|disable|turn off)\w* .*?\b(luz nocturna|modo nocturno|night light|night color)\b", "luz_nocturna_off"),
            (r"\b(luz nocturna|night light|night color)\b", "luz_nocturna"),
        ),
        ejecutor=lambda p, _l, m: ajustar_brillo(p, m),
    ),
    DefinicionAccion(
        nombre="telefono",
        riesgo=True,
        ejemplos=(("haz sonar el teléfono", "sonar"), ("ring my phone", "sonar"), ("dónde está mi móvil", "sonar"),
                  ("batería del teléfono", "bateria"), ("phone battery", "bateria")),
        prompt="usar KDE Connect con el teléfono (ring phone, phone battery, send clipboard). parametro = sonar | bateria | portapapeles",
        evidencia=r"telefono|celular|movil|phone",
        reglas=(
            (r"\b(donde esta mi (telefono|celular|movil)|haz sonar mi (telefono|celular|movil)|encuentra mi (telefono|celular)|where is my phone|ring my phone|find my phone)\b", "sonar"),
            (r"\b(bateria del (telefono|celular|movil)|cuanta bateria tiene (el|mi) (telefono|celular)|phone battery)\b", "bateria"),
            (r"\b(manda|envia|pasa|comparte|send|share)\w* .*?\b(portapapeles|clipboard|lo que copie)\b.*?\b(telefono|celular|movil|phone)\b", "portapapeles"),
        ),
        ejecutor=lambda p, _l, m: kdeconnect_accion(p, m),
    ),
    DefinicionAccion(
        nombre="dictar",
        prompt="escribir/teclear texto dictado (type/write text). parametro = el texto a escribir",
        evidencia=r"^\W*(escribe|escribir|dicta|dictar|teclea|type)\b",
        ejecutor=_ej_dictar,
    ),
    DefinicionAccion(
        nombre="docker_estado",
        ejemplos=(("qué contenedores están corriendo", ""), ("lista los contenedores", ""),
                  ("which containers are running", ""), ("cómo están mis contenedores de docker", ""),
                  ("estado de docker", "")),
        prompt='qué contenedores están corriendo (which containers are running). parametro = ""',
        evidencia=r"contenedor|container|docker",
        reglas=((r"\b(estado de docker|que contenedores (estan|hay)|which containers are running|docker status)\b", ""),),
        ejecutor=_ej_docker_estado,
    ),
    DefinicionAccion(
        nombre="docker_levantar",
        riesgo=True,
        ejemplos=(("levanta los contenedores", ""), ("inicia docker compose", ""), ("start the containers", ""),
                  ("sube el entorno de docker", "")),
        prompt='levantar/iniciar los contenedores (start containers). parametro = ""',
        evidencia=r"\b(levant|inici|arranc|sub|enciend|corre|start|run|bring|spin)\w*.*(contenedor|container|docker)",
        reglas=((r"\b(levanta|inicia|arranca|sube|start|bring up)\w*\s+(los\s+|the\s+)?(contenedores|containers|docker)\b", ""),),
        ejecutor=lambda _p, _l, m: _ej_docker_compose(True, m),
    ),
    DefinicionAccion(
        nombre="docker_detener",
        riesgo=True,
        ejemplos=(("detén los contenedores", ""), ("para docker compose", ""), ("stop the containers", ""),
                  ("baja el entorno de docker", "")),
        prompt='detener/parar los contenedores (stop containers). parametro = ""',
        evidencia=r"\b(deten|det|par|apag|baj|stop|shut|kill)\w*.*(contenedor|container|docker)",
        reglas=((r"\b(deten|detener|para|parar|apaga|baja|stop)\w*\s+(los\s+|the\s+)?(contenedores|containers|docker)\b", ""),),
        ejecutor=lambda _p, _l, m: _ej_docker_compose(False, m),
    ),
    DefinicionAccion(
        nombre="docker_logs",
        prompt="ver los logs de un contenedor (container logs). parametro = nombre del contenedor",
        evidencia=r"\b(logs|registro\w*)\b",
        ejecutor=_ej_docker_logs,
    ),
    DefinicionAccion(
        nombre="docker_reiniciar",
        riesgo=True,
        prompt="reiniciar un contenedor (restart container). parametro = nombre del contenedor",
        evidencia=r"\b(reinici|restart)\w*.*\b(contenedor|container|docker)",
        ejecutor=_ej_docker_reiniciar,
    ),
    DefinicionAccion(
        nombre="diagnostico",
        ejemplos=(("qué proceso usa más cpu", "cpu"), ("which process uses the most cpu", "cpu"),
                  ("qué está consumiendo procesador", "cpu"), ("qué proceso usa más memoria", "memoria"),
                  ("which app uses the most ram", "memoria"), ("qué consume tanta ram", "memoria"),
                  ("hay actualizaciones pendientes", "actualizaciones"),
                  ("are there updates available", "actualizaciones"), ("qué puertos están abiertos", "puertos"),
                  ("list open ports", "puertos")),
        prompt="ver qué consume CPU/memoria, qué puertos están abiertos o cuántas actualizaciones hay. parametro = cpu | memoria | puertos | puerto:<numero> | actualizaciones",
        evidencia=r"consum|usando|using|puerto|port|actualizacion|update|proceso|process",
        reglas=(
            (r"\b(que (proceso\w* )?(esta )?(consum\w*|usa\w*|gasta\w*)|procesos que mas consumen|what (process )?is using)\b.*?\b(cpu|procesador|processor)\b", "cpu"),
            (r"\b(que (proceso\w* )?(esta )?(consum\w*|usa\w*|gasta\w*)|what (process )?is using)\b.*?\b(ram|memoria|memory)\b", "memoria"),
            (r"\b(que puertos (tengo|estan|hay) abiertos|puertos abiertos|what ports are open|open ports)\b", "puertos"),
            (r"\b(hay actualizaciones|cuantas actualizaciones (hay|tengo)|paquetes (hay )?por actualizar|are there (any )?updates|how many updates)\b", "actualizaciones"),
        ),
        ejecutor=lambda p, _l, m: diagnostico_sistema(p, m),
    ),
    DefinicionAccion(
        nombre="volumen",
        riesgo=True,
        ejemplos=(("sube el volumen", "subir"), ("más volumen", "subir"), ("turn it up", "subir"),
                  ("súbele al sonido", "subir"), ("no se oye nada, sube", "subir"), ("baja el volumen", "bajar"),
                  ("menos volumen", "bajar"), ("turn the volume down", "bajar"),
                  ("está muy fuerte el audio", "bajar"), ("silencia el audio", "silenciar"), ("mute", "silenciar"),
                  ("quita el sonido", "silenciar")),
        prompt="parametro = subir | bajar | silenciar | activar | un número 0-100",
        evidencia=r"volumen|volume|sonido|sound|silenci|mute",
        reglas=(
            (r"\b(sube|subir|aumenta)\w* (el )?volumen|\bturn (the )?volume up|\bturn up the volume|\bvolume up", "subir"),
            (r"\b(baja|bajar|disminu)\w* (el )?volumen|\bturn (the )?volume down|\bturn down the volume|\bvolume down", "bajar"),
            (r"\b(silencia|mutea|quita el sonido)|^mute\b", "silenciar"),
        ),
        ejecutor=_ej_volumen,
    ),
    DefinicionAccion(
        nombre="multimedia",
        ejemplos=(("pausa la música", "pausa"), ("pause", "pausa"), ("detén la canción", "pausa"),
                  ("para el reproductor", "pausa"), ("reanuda la música", "reproducir"), ("play", "reproducir"),
                  ("continúa la canción", "reproducir"), ("siguiente canción", "siguiente"),
                  ("next song", "siguiente"), ("salta esta canción", "siguiente"),
                  ("canción anterior", "anterior"), ("previous track", "anterior")),
        prompt="parametro = pausa | reproducir | siguiente | anterior",
        evidencia=r"musica|music|cancion|song|pausa|pause|reproduc|play|tema|track|siguiente|next|anterior|previous",
        reglas=(
            (r"\b(pausa|pausar|deten la musica|para la musica|pause)\b", "pausa"),
            (r"\b(pon|poner|reproduce|reproducir|escuchar)\b.*\b(musica\w*|cancion\w*)|\b(play|listen to) (some )?music\b|^(reproduce|play)\b[!.?]*$", "reproducir"),
            (r"\b(siguiente cancion|siguiente tema|la siguiente|next song|next track)\b", "siguiente"),
            (r"\b(cancion anterior|tema anterior|la anterior|previous song|previous track)\b", "anterior"),
        ),
        ejecutor=_ej_multimedia,
    ),
    DefinicionAccion(
        nombre="estado_sistema",
        ejemplos=(("cómo está la compu", ""), ("estado del sistema", ""), ("how is the computer doing", ""),
                  ("cuánta temperatura tiene la cpu", ""), ("cuánta batería queda", "")),
        prompt='temperatura, memoria, disco, GPU, batería, cómo está el PC (system status). parametro = ""',
        evidencia=(r"temperatura|temperature|memoria|memory|disco|disk|estado|status|como esta|how is|ram|cpu"
                   r"|procesador|compu|computer|equipo|system|bateria|battery|vram|gpu|grafica"),
        reglas=(
            (r"\b(estado|status|nivel)\s+(de|del|of)\s+(la\s+|el\s+|mi\s+|my\s+|the\s+)?"
             r"(pc|compu\w*|computadora|sistema|equipo|maquina|laptop|portatil|bateria|computer|system|battery)\b", ""),
            (r"\b(estado del sistema|como esta (el|la) (pc|compu|sistema|equipo)|system status|cuanta (ram|memoria|vram)\b|how much ram\b|(temperatura|uso) (del|de la) (cpu|gpu|grafica|pc|compu|procesador))\b", ""),
        ),
        ejecutor=lambda _p, _l, m: estado_sistema(m),
    ),
    DefinicionAccion(
        nombre="perfil_energia",
        ejemplos=(("modo rendimiento", "rendimiento"), ("performance mode", "rendimiento"),
                  ("modo ahorro de energía", "ahorro"), ("power saver mode", "ahorro"),
                  ("ahorra batería", "ahorro"), ("modo equilibrado", "equilibrado"),
                  ("balanced mode", "equilibrado")),
        prompt="parametro = rendimiento | equilibrado | ahorro",
        evidencia=r"rendimiento|performance|equilibrad|balanced|ahorro|saver|perfil|profile|energia|power|modo|mode",
        reglas=(
            (r"\b(modo|perfil)\s+(de\s+)?rendimiento\b|\bperformance\s+(mode|profile)\b", "rendimiento"),
            (r"\b(modo|perfil)\s+equilibrado\b|\bbalanced\s+(mode|profile)\b", "equilibrado"),
            (r"\b(modo|perfil)\s+(de\s+)?ahorro(\s+de\s+energia)?\b|\bpower[- ]saver\s+(mode|profile)\b", "ahorro"),
        ),
        ejecutor=_ej_perfil_energia,
    ),
    DefinicionAccion(
        nombre="buscar_web",
        prompt="buscar en internet (search the web). parametro = lo que hay que buscar, en el idioma original",
        evidencia=r"busca|search|internet|google|web|look up",
        ejecutor=_ej_buscar_web,
    ),
    DefinicionAccion(
        nombre="bloquear_pantalla",
        riesgo=True,
        ejemplos=(("bloquea la pantalla", ""), ("bloquea la sesión", ""), ("lock the screen", ""),
                  ("voy a salir, bloquea el equipo", "")),
        prompt='parametro = ""',
        evidencia=r"bloque|lock",
        reglas=((r"\bbloquea\w* (la )?(pantalla|sesion|compu|pc)|\block (the )?(screen|computer|pc|session)", ""),),
        ejecutor=_ej_bloquear_pantalla,
    ),
    DefinicionAccion(
        nombre="actualizar_sistema",
        riesgo=True,
        prompt='actualizar el sistema, los paquetes o todo (update/upgrade the system). parametro = ""',
        evidencia=r"actualiz|update|upgrade",
        reglas=((r"^(?!\W*(como|how|que|what|por que|why|cual)\b).*\b(actualiza\w*|update|upgrade)\b.*\b(todo|sistema|paquetes|equipo|compu|pc|everything|system|packages|computer)\b", ""),),
        ejecutor=_ej_actualizar_sistema,
    ),
    DefinicionAccion(
        nombre="limpiar_cache",
        riesgo=True,
        ejemplos=(("limpia la caché de paquetes", ""), ("libera espacio de la caché de pacman", ""),
                  ("clean the package cache", "")),
        prompt='borrar la caché de paquetes del sistema (clean package cache). parametro = ""',
        evidencia=r"limpi|borra|libera|clean|clear|cache",
        reglas=((r"\b(limpia\w*|borra\w*|vacia\w*|clean|clear)\b.*\b(cache|cach)\b.*\b(paquetes|pacman|yay|dnf|sistema|"
                 r"packages|system)\b", ""),),
        ejecutor=_ej_limpiar_cache,
    ),
    DefinicionAccion(
        nombre="energia_equipo",
        riesgo=True,   # además se confirma siempre (CONFIRMAR_SIEMPRE)
        ejemplos=(("suspende el equipo", "suspender"), ("reinicia la computadora", "reiniciar"),
                  ("apaga el equipo", "apagar"), ("shut down the computer", "apagar"),
                  ("restart the pc", "reiniciar"), ("put the computer to sleep", "suspender")),
        prompt="suspender, reiniciar o apagar el equipo completo (no una app ni un contenedor). "
               "parametro = suspender | reiniciar | apagar",
        evidencia=r"suspend|hiberna|dormir|sleep|reinici|restart|reboot|apaga|shut\s*down|power\s*off|turn\s*off",
        reglas=tuple((rf"{verbo}\s+{EQUIPO}", p) for p, verbo in VERBOS_ENERGIA.items())
              + ((r"^\W*suspend\w*\W*$", "suspender"), (r"^\W*reboot\W*$", "reiniciar"),
                 (r"^\W*(shut\s*down|power\s*off)\W*$", "apagar")),
        ejecutor=_ej_energia_equipo,
    ),
    DefinicionAccion(
        nombre="imagen",
        riesgo=True,   # descarga el LLM de la GPU y la ocupa más de un minuto
        ejemplos=(("genera una imagen de un perro en la playa", ""), ("dibújame un dragón", ""),
                  ("hazme un dibujo de una casa en el bosque", ""), ("create an image of a mountain at sunset", ""),
                  ("draw a cat wearing a hat", "")),
        prompt="generar una imagen nueva con IA (generate/draw a picture). parametro = la petición completa",
        evidencia=r"imagen|foto|dibuj|ilustra|pinta|image|picture|photo|draw|illustrat|paint",
        reglas=((PIDE_IMAGEN, None), (r"^\W*(dibuja\w*|draw)\b", None)),
        ejecutor=_ej_imagen,
    ),
    DefinicionAccion(
        nombre="redactar",
        riesgo=True,   # sustituye lo que haya en el portapapeles
        ejemplos=(("redacta un correo para mi jefe pidiendo el viernes libre", ""),
                  ("escríbeme un mensaje de cumpleaños para mi hermana", ""),
                  ("escribe una función en python que ordene una lista", ""),
                  ("write an email to the team about the release", ""), ("draft a reply to the client", "")),
        prompt="redactar un texto o código nuevo y dejarlo en el portapapeles (write an email, message, function…). "
               "parametro = la petición completa",
        evidencia=r"redact|escrib|compon|genera|crea|haz|write|draft|compose|generate|create",
        reglas=((PIDE_REDACTAR, None), (r"^\W*(redacta\w*|draft)\b", None)),
        ejecutor=_ej_redactar,
    ),
    DefinicionAccion(
        nombre="pregunta",
        prompt="una pregunta clara de conocimiento general o de programación. parametro = la pregunta completa",
        reglas=(
            (r"^(?!.*\b(busca|internet|google|search|copi\w*|copied|portapapeles|clipboard|puerto|port|consum\w*|gpu|grafica)\b)"
             r"\W*(que es|que son|que significa|"
             # «¿Cómo escribo…?» en primera persona: es una pregunta, no una orden ni el portapapeles
             r"como (funciona|se|hago|puedo|debo|escribo|creo|instalo|configuro|uso|implemento|declaro|defino|"
             r"ordeno|convierto|conecto|despliego|soluciono|arreglo|evito|leo|obtengo|agrego|anado|elimino|borro|"
             r"calculo|recorro|manejo|pruebo|depuro|optimizo|apago|reinicio|suspendo|limpio)|"
             r"cual es la diferencia|por que|explica|diferencia entre|what is|what are|what does|how (does|do|can|to|should|would)|"
             r"why|explain|difference between)\b", None),
        ),
        ejecutor=_ej_pregunta,
    ),
    DefinicionAccion(
        nombre="no_entendido",
        prompt='muletillas, frases incompletas o sin sentido. parametro = ""',
    ),
]

ACCIONES = [a.nombre for a in REGISTRO_ACCIONES]
# Ejemplos para el LLM. Incluyen charla: sin ellos ningún modelo probado respondía no_entendido ante
# frases de una reunión (0 de 16); con ellos, gemma4:e2b rechaza 15 de 16
EJEMPLOS_PROMPT = [
    ("lanza el editor de código", "abrir_app", "vscode"),
    ("cierra spotify", "cerrar_app", "spotify"),
    ("súbele al volumen", "volumen", "subir"),
    ("silencia todo", "volumen", "silenciar"),
    ("ya me voy, bloquea la sesión", "bloquear_pantalla", ""),
    ("¿qué contenedores hay levantados?", "docker_estado", ""),
    ("¿qué está usando tanto procesador?", "diagnostico", "cpu"),
    ("next track", "multimedia", "siguiente"),
    ("make the screen brighter", "brillo", "subir"),
    ("sí, eso lo vemos mañana en la reunión", "no_entendido", ""),
    ("no, pero el cliente dijo que lo quería para el viernes", "no_entendido", ""),
    ("okay, thanks everyone", "no_entendido", ""),
    ("la verdad es que el sistema anda lento hoy", "no_entendido", ""),
]
PROMPT_COMANDOS = (
    "Eres el intérprete de comandos de voz de un PC de desarrollo. La frase viene de un micrófono que puede estar "
    "abierto: muchas veces es una conversación con otras personas, una reunión o ruido mal transcrito. "
    "Solo es una orden si le pide al asistente hacer algo en el equipo o le pregunta por el equipo. "
    "Si no es claramente una orden, responde no_entendido.\n"
    "La frase puede estar en español o en inglés. Convierte la frase en UNA acción. "
    "Los valores fijos de parametro van SIEMPRE en español, tal como aparecen aquí.\n"
    "Acciones y su parámetro:\n"
    + "\n".join(f"- {a.nombre}: {a.prompt}" for a in REGISTRO_ACCIONES)
    + "\n\nEjemplos:\n"
    + "\n".join(f'"{f}" -> {json.dumps({"accion": a, "parametro": p}, ensure_ascii=False)}' for f, a, p in EJEMPLOS_PROMPT)
    + "\n\nResponde solo el JSON."
)
ESQUEMA = {"type": "object",
           "properties": {"accion": {"type": "string", "enum": ACCIONES}, "parametro": {"type": "string"}},
           "required": ["accion", "parametro"], "additionalProperties": False}
REGLAS = [(patron, a.nombre, param) for a in REGISTRO_ACCIONES for patron, param in a.reglas]
EVIDENCIA = {a.nombre: a.evidencia for a in REGISTRO_ACCIONES if a.evidencia and a.riesgo}
EJECUTORES = {a.nombre: a.ejecutor for a in REGISTRO_ACCIONES if a.ejecutor}


# --- Clasificador por embeddings (entre las reglas y el LLM) ---------------------------
# Compara la frase con los `ejemplos` del registro (y con charla) usando multilingual-e5-small (ONNX int8,
# ~113 MB, CPU). Elegir 1 de ~25 acciones y saber decir que no es clasificar, no generar: en las pruebas
# acertó tanto como el mejor LLM en ~3 ms en lugar de 3-6 s. Si el vecino más parecido no llega a
# UMBRAL_CLASIFICADOR, decide el LLM; sin LLM, no_entendido.
CLASIFICADOR_DIR = BASE / "modelos/e5-small"
EJEMPLOS_CHARLA = (  # rechazo explícito: frases de reuniones, ruido y órdenes que el asistente no sabe hacer
    "sí, eso lo vemos mañana en la reunión", "no, pero el cliente dijo que lo quería para el viernes",
    "okay, thanks everyone", "la verdad es que el sistema anda lento hoy", "bueno, pasemos al siguiente punto",
    "¿me escuchan bien?", "perfecto, quedamos así entonces", "eso depende de lo que diga el área de negocio",
    "hay que revisar la historia de usuario", "I think we should merge it after the review",
    "ya te mando el enlace por el chat", "nos vemos luego", "gracias a todos por venir", "mmm", "eh",
    "el despliegue de ayer salió bien", "tenemos que validar con QA", "can you share your screen",
    "manda un correo a juan", "llama a mi mamá", "pide una pizza",
)


class ClasificadorComandos:
    def __init__(self, carpeta):
        import onnxruntime
        from tokenizers import Tokenizer
        self.tok = Tokenizer.from_file(str(carpeta / "tokenizer.json"))
        self.tok.enable_truncation(128)
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.log_severity_level = 4
        self.sesion = onnxruntime.InferenceSession(str(carpeta / "onnx/model_quantized.onnx"), opts,
                                                   providers=["CPUExecutionProvider"])
        self.entradas = {i.name for i in self.sesion.get_inputs()}
        ejemplos = [((a.nombre, p), f) for a in REGISTRO_ACCIONES for f, p in a.ejemplos]
        ejemplos += [(("no_entendido", ""), f) for f in EJEMPLOS_CHARLA]
        self.etiquetas = [e for e, _ in ejemplos]
        self.matriz = self.embeber([f for _, f in ejemplos])

    def embeber(self, textos):
        # e5: prefijo "query: ", media de los tokens y normalización L2
        enc = self.tok.encode_batch([f"query: {t}" for t in textos])
        largo = max(len(e.ids) for e in enc)
        ids = np.array([e.ids + [0] * (largo - len(e.ids)) for e in enc], dtype=np.int64)
        mascara = np.array([e.attention_mask + [0] * (largo - len(e.ids)) for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mascara}
        if "token_type_ids" in self.entradas:
            feed["token_type_ids"] = np.zeros_like(ids)
        salida = self.sesion.run(None, feed)[0]
        m = mascara[..., None].astype(np.float32)
        v = (salida * m).sum(1) / m.sum(1)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def clasificar(self, frase):
        """((acción, parámetro), similitud) del ejemplo más parecido."""
        sim = self.matriz @ self.embeber([frase])[0]
        i = int(sim.argmax())
        return self.etiquetas[i], float(sim[i])


_clasificador = None
_clasificador_lock = threading.Lock()


def clasificador():
    """El clasificador, cargado la primera vez. None si está desactivado o falta el modelo o `tokenizers`."""
    global _clasificador
    with _clasificador_lock:
        if _clasificador is None:
            _clasificador = False
            if USAR_CLASIFICADOR and (CLASIFICADOR_DIR / "onnx/model_quantized.onnx").is_file():
                try:
                    _clasificador = ClasificadorComandos(CLASIFICADOR_DIR)
                except Exception as e:
                    log(f"clasificador no disponible: {e}")
        return _clasificador or None


def extraer_app(n):
    """Busca una app instalada en la frase: el tramo de palabras más largo que resuelve («pon visual studio code»)."""
    palabras = re.sub(r"[^\w\s]", " ", n).split()
    for largo in range(min(4, len(palabras)), 0, -1):
        for i in range(len(palabras) - largo + 1):
            tramo = " ".join(palabras[i:i + largo])
            if len(tramo) >= 3 and resolver_app(tramo):
                return tramo
    return None


SENTIDO_BAJAR = r"\b(baj\w*|menos|down|lower|quieter|reduc\w*|dim\w*)\b|\bmas oscur|\bdarker\b"
SENTIDO_SUBIR = r"\b(sub\w*|aument\w*|up|louder|brighter)\b|\bmas (clar|fuerte|alto)|\bmuy oscur|no se (ve|oye)"
SENTIDO_SILENCIO = r"\b(silenci\w*|mute|mutea\w*)\b|quita\w* el (sonido|audio)"


SENTIDO_PAUSA = r"\b(pausa\w*|pause|deten\w*|para|stop)\b"
SENTIDO_REPRODUCIR = r"\b(pon|ponme|reproduc\w*|play|reanuda\w*|continua\w*|resume)\b"


def corregir_sentido(n, param, accion):
    """Los embeddings captan el tema pero no el sentido: «baja un poco el sonido» queda cerca de «silencia»
    y «pon algo de música», cerca de «pausa la música»."""
    if accion == "multimedia":
        if param in ("pausa", "reproducir"):
            return "pausa" if re.search(SENTIDO_PAUSA, n) else "reproducir" if re.search(SENTIDO_REPRODUCIR, n) else param
        return param
    if accion == "volumen" and re.search(SENTIDO_SILENCIO, n):
        return "silenciar"
    if re.search(SENTIDO_BAJAR, n):
        return "bajar"
    if re.search(SENTIDO_SUBIR, n):
        return "subir"
    return param

# Errores recurrentes de Whisper con esta voz/micrófono:
# "Abre" -> "Ahora", "A ver", "Aurek"…; "Cierra" -> "Sierra", "Cierre"…
ABRE_MAL_OIDO = r"^(ahora(\s+es)?|aurea|aurek|aure|a ver|abra|habre|abri|abrir|ahi|haber|avril|abril|open up)\b,?\s*(a\s+)?"
CIERRA_MAL_OIDO = r"^(sierra|cierre|cierras|serra)\b,?\s*"


def app_en(resto):
    """Nombre de app en lo que sigue a "abre"/"cierra". Whisper a veces repite o mezcla palabras
    («fire, firefox», «farid, firefox»): si la frase completa no es una app, prueba palabra por palabra."""
    resto = resto.strip(" .?!,")
    if resolver_app(resto):
        return resto
    for w in reversed(re.split(r"[\s,]+", resto)):
        if len(w) > 3 and resolver_app(w):
            return w
    return None


def corregir(n):
    if m := re.match(ABRE_MAL_OIDO, n):
        resto = n[m.end():]
        if re.search(r"\bproyecto\b", resto):
            return "abre " + resto
        if app := app_en(resto):
            return "abre " + app
    if m := re.match(CIERRA_MAL_OIDO, n):
        if app := app_en(n[m.end():]):
            return "cierra " + app
    return n


BUSQUEDA_INICIO = (r"^.*?\b(busca(r|me)?|search|look up|google)\b,?\s*((en|on)\s+)?((el|la|the)\s+)?"
                   r"(internet|google|web)?\s*(for|por|sobre)?\s*")
BUSQUEDA_FIN = r"\s+((en|on)\s+)?((el|la|the)\s+)?(internet|google|web|online)\s*[.?!]*$"


def extraer_busqueda(texto):
    """Toma lo que hay que buscar directamente de la frase (el modelo tiende a traducirlo)."""
    q = re.sub(BUSQUEDA_INICIO, "", texto, flags=re.IGNORECASE)
    q = re.sub(BUSQUEDA_FIN, "", q, flags=re.IGNORECASE)
    return q.strip(" .?!,")


class SinLLM(Exception):
    """llm = "ninguno": la acción necesita un modelo de lenguaje y no hay."""


def _peticion_llm(modelo, sistema, usuario, formato=None, stream=False):
    """Abre la petición al proveedor configurado y devuelve la respuesta HTTP."""
    mensajes = [{"role": "system", "content": sistema}, {"role": "user", "content": usuario}]
    if LLM == "ollama":
        url = f"{LLM_URL}/api/chat"
        # think: los modelos con razonamiento (Qwen 3.5, Gemma 4…) piensan minutos antes de contestar;
        # para elegir una acción o responder en voz alta no compensa. Los demás modelos lo ignoran.
        cuerpo = {"model": modelo, "stream": stream, "keep_alive": KEEP_ALIVE, "think": False,
                  "options": {"temperature": 0, "num_ctx": NUM_CTX}, "messages": mensajes}
        if formato:
            cuerpo["format"] = formato
    elif LLM == "openai":
        url = f"{LLM_URL}/chat/completions"
        cuerpo = {"model": modelo, "stream": stream, "temperature": 0, "messages": mensajes}
        if formato:
            cuerpo["response_format"] = {"type": "json_schema",
                                         "json_schema": {"name": "respuesta", "schema": formato, "strict": True}}
    else:
        raise SinLLM()
    cabeceras = {"Content-Type": "application/json"}
    if LLM_CLAVE:
        cabeceras["Authorization"] = f"Bearer {LLM_CLAVE}"
    req = urllib.request.Request(url, json.dumps(cuerpo).encode(), cabeceras)
    return urllib.request.urlopen(req, timeout=120)


def llm(modelo, sistema, usuario, formato=None):
    with _peticion_llm(modelo, sistema, usuario, formato) as r:
        datos = json.load(r)
    return datos["message"]["content"] if LLM == "ollama" else datos["choices"][0]["message"]["content"]


def _fragmentos_stream(r):
    """Texto que va llegando: líneas JSON en Ollama, eventos SSE («data: …») en la API de OpenAI."""
    for linea in r:
        linea = linea.decode().strip()
        if not linea:
            continue
        if LLM == "ollama":
            item = json.loads(linea)
            yield item.get("message", {}).get("content", "")
            if item.get("done"):
                return
        elif linea.startswith("data:"):
            dato = linea[5:].strip()
            if dato == "[DONE]":
                return
            opciones = json.loads(dato).get("choices") or [{}]
            yield (opciones[0].get("delta") or {}).get("content") or ""


def llm_stream(modelo, sistema, usuario):
    """Genera la respuesta del LLM oración por oración para poder hablar sin esperar al final."""
    buf = ""
    with _peticion_llm(modelo, sistema, usuario, stream=True) as r:
        for fragmento in _fragmentos_stream(r):
            buf += fragmento
            while m := re.search(r"([.!?]+(?=\s)|\n+)", buf):
                fin = m.end()
                oracion, buf = buf[:fin].strip(), buf[fin:].lstrip()
                if oracion:
                    yield oracion
    if buf.strip():
        yield buf.strip()


NUMEROS = {
    "un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiun": 21,
    "veintiuno": 21, "veintiuna": 21, "veintidos": 22, "veintitres": 23, "veinticuatro": 24, "veinticinco": 25,
    "veintiseis": 26, "veintisiete": 27, "veintiocho": 28, "veintinueve": 29, "treinta": 30, "cuarenta": 40,
    "cincuenta": 50, "sesenta": 60, "noventa": 90,
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "ninety": 90,
}
_UNIDAD_TIEMPO = r"(?=\s*(segundo|second|sec|minuto|minute|min|hora|hour))"


def numeros_en_cifras(n):
    """«cinco minutos» -> «5 minutos», «media hora» -> «30 minutos»: Whisper escribe con letras los
    números pequeños. Solo cambia números seguidos de una unidad de tiempo."""
    for frase, cifra in ((r"\b(una )?hora y media\b", "90 minutos"), (r"\b(media hora|half an hour)\b", "30 minutos"),
                         (r"\b(un )?cuarto de hora\b", "15 minutos"), (r"\b(a )?quarter of an hour\b", "15 minutes")):
        n = re.sub(frase, cifra, n)
    palabras_num = "|".join(sorted(NUMEROS, key=len, reverse=True))
    # «treinta y cinco», «forty five», «forty-five»
    n = re.sub(rf"\b({palabras_num})(?:\s+y\s+|[\s-]+)({palabras_num}){_UNIDAD_TIEMPO}",
               lambda m: str(NUMEROS[m[1]] + NUMEROS[m[2]]) if NUMEROS[m[1]] >= 20 and NUMEROS[m[2]] < 10
               else m[0], n)
    return re.sub(rf"\b({palabras_num}){_UNIDAD_TIEMPO}", lambda m: str(NUMEROS[m[1]]), n)


def extraer_temporizador(n, texto):
    """Extrae segundos y nota opcional de frases como «avísame en 10 minutos» o «set a timer for 30 seconds»."""
    n = numeros_en_cifras(n)
    m = re.search(
        r"\b(avisame (?:en|dentro de)|recuerdame (?:en|dentro de)|(?:pon(?:me)? )?(?:un |una )?(?:temporizador|alarma)"
        r" (?:de|para|por|en)|set a timer for|timer for|remind me in)\s+"
        r"(\d+)\s*(segundo\w*|second\w*|sec\w*|minuto\w*|minute\w*|min\w*|hora\w*|hour\w*)\s*(.*)",
        n,
    )
    if not m:
        return None
    cantidad = int(m.group(2))
    unidad = m.group(3)
    factor = 3600 if unidad.startswith(("h",)) else (60 if unidad.startswith(("m",)) else 1)
    segundos = max(1, cantidad * factor)
    nota = re.sub(r"^(que|para|de|to|for|about)\s+", "", m.group(4).strip(" .?!,"))
    return f"{segundos}:{nota}" if nota else str(segundos)


def interpretar(texto):
    n = corregir(normalizar(texto))
    if n != normalizar(texto):
        log(f"corregido: {n!r}")
        texto = n
    # Dictado directo: "escribe ..." / "dicta ..." / "type ..." (con o sin coma tras el verbo).
    # «Escribe un correo a…» no es dictado: lo resuelve la regla de redactar
    m = re.match(r"^\W*(escribe|dicta|teclea|type)[,:;]?\s+(.+)$", texto.strip(), flags=re.IGNORECASE)
    if m and not re.search(PIDE_REDACTAR, n):
        contenido = m.group(2).strip()
        if not re.search(r"\b(tu contrasena|your password)\b", normalizar(contenido)):
            return "dictar", contenido
    # Temporizadores: "avísame en 10 minutos" / "pon un temporizador de 5 minutos"
    if temp := extraer_temporizador(n, texto):
        return "temporizador", temp
    # Diagnóstico de un puerto concreto: "¿qué corre en el puerto 5000?" / "what is running on port 5000?"
    if m := re.search(r"\b(puerto|port)\s+(\d{2,5})\b", n):
        return "diagnostico", f"puerto:{m.group(2)}"
    # Git por voz: "estado del proyecto X", "estado git, X", "último commit de X"
    # Si la frase no dice «git»/«proyecto», solo es git cuando lo que sigue es un proyecto que existe:
    # «estado de la compu» o «estado de la batería» son estado_sistema
    git_explicito = re.search(r"\b(git|stargit|proyecto|project|repo\w*)\b", n)
    if m := re.search(r"\b(estado\s+git|status\s+git|stargit|(?:estado|status)\s+(?:de\s+git\s*)?(?:del\s+proyecto|de|en|in|of))\s*(?:del\s+proyecto\s+|de\s+|en\s+|in\s+|of\s+|,\s*|\s+)(.+?)[.!?]*$", n):
        if git_explicito or buscar_proyecto(m.group(2)):
            return "git_proyecto", f"estado:{m.group(2).strip()}"
    if m := re.search(r"\b(?:(?:ultimo|last)\s+)?(?:commit|comi)\s+(?:de\s+git\s+)?(?:del\s+proyecto\s+|de\s+|en\s+|of\s+project\s+|of\s+|in\s+)(.+?)[.!?]*$", n):
        return "git_proyecto", f"commit:{m.group(1).strip()}"   # «commit» ya es explícito
    # Abrir terminal o lazygit en un proyecto: "abre lazygit en validaciones", "abre la terminal en el proyecto X"
    if m := re.match(r"^(abre|abrir|open|lanza|launch)\s+(lazygit|(?:la\s+|una\s+|a\s+)?(?:terminal|consola))\s+(en|in)\s+(el\s+proyecto\s+|the\s+project\s+|project\s+)?(.+?)[.!?]*$", n):
        herr = "lazygit" if "lazygit" in m.group(2) else "terminal"
        return "terminal_proyecto", f"{herr}:{m.group(5).strip()}"
    # Docker con más detalle: "logs de X", "reinicia el contenedor X"
    if m := re.match(r"^(abre\s+|muestra\s+|ver\s+|show\s+|open\s+)?(los\s+|the\s+)?(logs|registros)\s+(del\s+contenedor\s+|de\s+|of\s+container\s+|of\s+|for\s+)(.+?)[.!?]*$", n):
        return "docker_logs", m.group(5).strip()
    # «reinicia X» solo es un contenedor si X se llama exactamente así; con «el contenedor X» se aceptan
    # nombres parecidos. «reinicia la computadora / spotify» no debe acabar en docker.
    if m := re.match(r"^(reinicia|reiniciar|restart)\s+(el\s+contenedor\s+|the\s+container\s+|container\s+)?(.+?)[.!?]*$", n):
        if cont := resolver_contenedor(m.group(3), aproximado=bool(m.group(2))):
            return "docker_reiniciar", cont
        if m.group(2):
            return "docker_reiniciar", m.group(3).strip()   # responderá que no existe
    for patron, accion, param in REGLAS:
        if re.search(patron, n):
            return accion, texto if param is None else param
    if m := re.search(r"\b(volumen|volume)\b.*?\b(\d{1,3})\b", n):
        return "volumen", m.group(2)
    if m := re.search(r"\b(brillo|brightness)\b.*?\b(\d{1,3})\b", n):
        return "brillo", m.group(2)
    # «escritorio dos» / «desktop two»: el número también en palabras
    n_cifras = re.sub(r"\b(escritorio|desktop)\s+(\w+)\b",
                      lambda m: f"{m.group(1)} {NUMEROS.get(m.group(2), m.group(2))}", n)
    if m := re.search(r"\b(manda|mueve|pasa|envia|move|send)\w*\b.*\b(escritorio|desktop)\s*(\d)\b", n_cifras):
        return "ventana", f"mover_escritorio_{m.group(3)}"
    if m := re.search(r"\b(escritorio|desktop)\s*(\d)\b", n_cifras):
        return "ventana", f"escritorio_{m.group(2)}"
    if re.match(r"^\W*(busca|buscar|buscame|search|look up|google)\b", n) and extraer_busqueda(texto):
        return "buscar_web", extraer_busqueda(texto)
    # Tareas configuradas en config.json: antes de abrir_app para que "ejecuta X" priorice tareas
    if TAREAS and (m := re.match(r"^(ejecuta|corre|lanza|inicia|run|start)\s+(la tarea\s+|the task\s+|task\s+)?(.+?)[.!?]*$", n)):
        if tarea := resolver_tarea(m.group(3)):
            return "ejecutar_tarea", tarea
    # "abre el proyecto X" / "open project X"
    if m := re.match(r"^(abre|abrir|open)\s+(el\s+|the\s+)?(proyecto|repositorio|repo|project)\s+(.+?)[.!?]*$", n):
        return "abrir_proyecto", m.group(4).strip()
    # "abre X" / "open X" / "muestra X" donde X es una app conocida: sin pasar por el modelo
    m = re.match(r"^((tambien\s+|also\s+)?(abre|abrir|muestra|mostrar|open|launch|lanza|inicia|ejecuta|show))\s+(.+?)[.!?]*$", n)
    if m and "proyecto" not in m.group(4) and "project" not in m.group(4) and (app := app_en(m.group(4))):
        return "abrir_app", app
    # "cierra X" / "close X" donde X es la ventana actual o una app conocida
    m = re.match(r"^(cierra|cierro|cerrar|close|quit)\s+(.+?)[.!?]*$", n)
    if m:
        if re.search(r"\b(ventana\w*|window\w*|esto|this)\b", m.group(2)):
            return "cerrar_app", ""
        if app := app_en(m.group(2)):
            return "cerrar_app", app
    # Clasificador por embeddings: milisegundos y sin LLM. Si no está seguro, decide el LLM (si lo hay)
    if clf := clasificador():
        (accion, param), sim = clf.clasificar(n)
        seguro = sim >= UMBRAL_CLASIFICADOR
        log(f"clasificador: {accion}({param!r}) {sim:.2f}{'' if seguro else ' -> dudoso'}")
        if seguro:
            if param == PARAM_APP:
                param = extraer_app(n)
                if not param:               # «cierra X» con X desconocida nunca debe cerrar la ventana actual
                    return "no_entendido", ""
            elif accion in ("volumen", "brillo", "multimedia"):
                param = corregir_sentido(n, param, accion)
            return validar_interpretacion(accion, param, n, texto)
    try:
        r = json.loads(llm(MODELO_COMANDOS, PROMPT_COMANDOS, texto, ESQUEMA))
        accion, param = r.get("accion", "no_entendido"), r.get("parametro", "")
    except SinLLM:
        return "no_entendido", ""
    except Exception as e:
        log(f"error del LLM ({LLM}): {e}")
        return "error_llm", ""
    return validar_interpretacion(accion, param, n, texto)


def validar_interpretacion(accion, param, n, texto):
    """Salvaguardas para lo que propone el clasificador o el LLM (las reglas ya son exactas)."""
    # En las acciones con riesgo, solo se acepta si la frase contiene palabras que la respalden:
    # una frase de una reunión no debe bloquear la pantalla ni detener contenedores
    evidencia = EVIDENCIA.get(accion)
    if evidencia and not re.search(evidencia, n):
        log(f"descartado {accion}({param!r}): la frase no lo respalda")
        return "no_entendido", ""
    # «Cierra y» (frase cortada) no debe cerrar la ventana actual: sin app, hay que decir «la ventana» / «esto»
    if accion == "cerrar_app" and not param and not re.search(r"\b(ventana\w*|window\w*|esto|this)\b", n):
        log(f"descartado cerrar_app(''): no dice qué cerrar")
        return "no_entendido", ""
    numero = re.search(r"\b(\d{1,3})\b", n)
    if accion in ("volumen", "brillo") and numero:
        param = numero.group(1)
    if accion == "diagnostico" and normalizar(param) in ("gpu", "grafica", "vram"):
        accion, param = "estado_sistema", ""
    if accion == "pregunta" and len(n.split()) < 3:
        accion, param = "no_entendido", ""
    if accion == "buscar_web":
        param = extraer_busqueda(texto) or param
    if accion == "energia_equipo":
        # Hay que nombrar el equipo («apaga la luz nocturna» no es apagar) salvo con «suspende» / «reboot» a secas
        verbos = [p for p, v in VERBOS_ENERGIA.items() if re.search(v, n)]
        if len(verbos) != 1 or not (re.search(EQUIPO, n) or re.match(r"^\W*(suspend\w*|reboot|shut\s*down|power\s*off)\W*$", n)):
            log(f"descartado energia_equipo({param!r}): no dice qué hacer con el equipo")
            return "no_entendido", ""
        param = verbos[0]
    # «Limpia la caché del navegador» no es la de paquetes: o lo dice, o no dice de qué
    if accion == "limpiar_cache" and not re.search(r"\b(paquet\w*|pacman|yay|paru|dnf|flatpak|sistema|package\w*|system)\b", n) \
            and not re.match(r"^\W*(limpia\w*|clean|clear)\s+(la |the )?(cache|cach)\W*$", n):
        log(f"descartado limpiar_cache: no es la caché de paquetes")
        return "no_entendido", ""
    if accion in ("imagen", "redactar"):     # la petición completa, no el resumen del LLM ni el del ejemplo
        param = texto
    # "¿Cómo actualizo un paquete de npm?" es una pregunta, no una orden de actualizar el sistema
    if accion == "actualizar_sistema" and re.match(r"^\W*(como|how|que|what|por que|why|cual)\b", n):
        accion, param = "pregunta", texto
    if accion == "abrir_proyecto" and not re.search(r"\b(proyecto|repositorio|repo|project)\b", n) and resolver_app(param):
        accion = "abrir_app"
    if accion in ("abrir_app", "cerrar_app") and param:
        # La app tiene que existir y mencionarse en la frase: así «necesito el navegador» abre Firefox, pero
        # si el LLM adivina la terminal ante «pon algo de música», se descarta
        app, mencionada = resolver_app(param), extraer_app(n)
        if not app or not mencionada or resolver_app(mencionada).id != app.id:
            log(f"descartado {accion}({param!r}): la app no se menciona en la frase")
            return "no_entendido", ""
    if accion == "abrir_app" and not param:
        return "no_entendido", ""
    if accion == "ventana" and param not in ATAJOS_KWIN:
        return "no_entendido", ""
    if accion == "docker_reiniciar":
        cont = resolver_contenedor(param, aproximado=True)
        if not cont:
            return "no_entendido", ""
        param = cont
    if accion == "ejecutar_tarea":
        tarea = resolver_tarea(param)
        if not tarea:
            return "no_entendido", ""
        param = tarea
    return accion, param


def ejecutar(accion, param, lang):
    """Devuelve el texto de respuesta en el idioma indicado."""
    m = MSG[lang]
    if accion == "error_llm":
        return m["llm_error"]
    fn = EJECUTORES.get(accion)
    return fn(param, lang, m) if fn else m["no_entendi"]


# --- Voz a texto ---------------------------------------------------------------------
def motor_whisper():
    if WHISPER_MOTOR != "auto":
        return WHISPER_MOTOR
    try:
        import faster_whisper  # noqa: F401  (solo se instala en equipos con NVIDIA)
        return "cuda"
    except ImportError:
        return "openvino"


def preparar_cuda():
    """CTranslate2 busca libcublas.so.12 y libcudnn*.so.9, que vienen en los paquetes nvidia-*-cu12 del
    venv, fuera de la ruta del enlazador. LD_LIBRARY_PATH solo se lee al arrancar el proceso, así que
    se define y el script se vuelve a ejecutar a sí mismo."""
    libs = sorted(str(d) for d in (Path(sysconfig.get_paths()["purelib"]) / "nvidia").glob("*/lib"))
    actual = os.environ.get("LD_LIBRARY_PATH", "")
    if not libs or all(l in actual.split(":") for l in libs):
        return
    os.environ["LD_LIBRARY_PATH"] = ":".join(libs + ([actual] if actual else []))
    os.execv(sys.executable, sys.orig_argv)


class WhisperOpenVINO:
    def __init__(self):
        import openvino_genai
        for dispositivo in WHISPER_DISPOSITIVOS:
            try:
                # Sin CACHE_DIR: con la GPU, el modelo leído de la caché transcribe basura (fallo de OpenVINO)
                self.pipe = openvino_genai.WhisperPipeline(str(WHISPER), dispositivo)
                self.descripcion = f"{WHISPER.name} en {dispositivo}"
                return
            except Exception as e:
                log(f"Whisper no disponible en {dispositivo}: {str(e).strip().splitlines()[-1][:120]}")
        raise RuntimeError("Whisper (OpenVINO) no pudo cargarse en ningún dispositivo")

    def transcribir(self, audio, pista):
        return generar_openvino(self.pipe, audio, initial_prompt=pista), None


def generar_openvino(pipe, audio, **kw):
    """Whisper de OpenVINO con detección automática de idioma. A veces falla al detectarlo
    ("Language token id -1"); entonces se reintenta en español."""
    try:
        return pipe.generate(audio.tolist(), task="transcribe", **kw).texts[0].strip()
    except RuntimeError as e:
        if "Language token" not in str(e):
            raise
        return pipe.generate(audio.tolist(), task="transcribe", language="<|es|>", **kw).texts[0].strip()


class WhisperCUDA:
    """faster-whisper (CTranslate2) en la GPU NVIDIA; si CUDA falla, en la CPU con int8."""
    def __init__(self):
        from faster_whisper import WhisperModel
        local = BASE / f"modelos/faster-whisper-{WHISPER_CUDA}"
        modelo = str(local) if local.is_dir() else WHISPER_CUDA
        for dispositivo, tipo in (("cuda", WHISPER_CUDA_TIPO), ("cpu", "int8")):
            try:
                self.modelo = WhisperModel(modelo, device=dispositivo, compute_type=tipo)
                self.descripcion = f"faster-whisper {WHISPER_CUDA} en {dispositivo} ({tipo})"
                break
            except Exception as e:
                log(f"Whisper no disponible en {dispositivo}: {str(e).strip().splitlines()[-1][:120]}")
        else:
            raise RuntimeError("Whisper (faster-whisper) no pudo cargarse")
        # La primera transcripción en CUDA tarda varios segundos (inicialización de cuDNN): se hace ahora
        self.transcribir(np.zeros(RATE, np.float32), "")

    def transcribir(self, audio, pista):
        _, _, probabilidades = self.modelo.detect_language(audio)
        lang = max(((l, p) for l, p in probabilidades if l in IDIOMAS), key=lambda x: x[1], default=(None, 0))[0]
        segmentos, _ = self.modelo.transcribe(audio, task="transcribe", language=lang, initial_prompt=pista or None,
                                              beam_size=5, condition_on_previous_text=False)
        return "".join(s.text for s in segmentos).strip(), lang


class WhisperActivacion:
    """Whisper pequeño en la CPU que busca la palabra de activación al inicio de cada frase."""
    def __init__(self, motor):
        self.pipe = self.modelo = None
        if motor == "cuda":
            from faster_whisper import WhisperModel
            local = BASE / "modelos/faster-whisper-base"
            self.modelo = WhisperModel(str(local) if local.is_dir() else "base", device="cpu", compute_type="int8")
        else:
            import openvino_genai
            self.pipe = openvino_genai.WhisperPipeline(str(WHISPER_ACTIVACION), "CPU")

    def transcribir(self, audio):
        if self.pipe:
            return generar_openvino(self.pipe, audio)
        segmentos, _ = self.modelo.transcribe(audio, task="transcribe", beam_size=1, condition_on_previous_text=False)
        return "".join(s.text for s in segmentos).strip()


AFIRMATIVO = r"^\W*(si|sii|confirma\w*|dale|adelante|claro|ok|okay|yes|yep|yeah|confirm\w*|sure|go ahead)\b"
NEGATIVO = r"^\W*(no|cancela\w*|olvida\w*|nada|cancel\w*|never\s*mind|stop)\b"


# --- Bucle principal ---------------------------------------------------------------
class Asistente:
    def __init__(self):
        t = time.time()
        validar_config()
        motor = motor_whisper()
        self.whisper = WhisperCUDA() if motor == "cuda" else WhisperOpenVINO()
        self.voz = Voz()
        self.activar = threading.Event()
        self.detener = threading.Event()
        self.ocupado = threading.Lock()   # atajo y palabra de activación no atienden a la vez
        self.escuchando = False
        self.reescuchar = False  # el atajo interrumpió la voz: escuchar en cuanto termine atender()
        self.encolado = False    # el atajo llegó mientras procesaba: escuchar en cuanto quede libre
        self.pendiente = None   # (accion, param, lang, expira) para confirmación en 1 paso
        self.mic = None
        if PALABRA_ACTIVACION:
            self.whisper_activacion = WhisperActivacion(motor)
            self.mic = Microfono()
        _restaurar_volumen()
        signal.signal(signal.SIGUSR1, self._senal)
        if _PULSACION_AL_ARRANCAR.is_set():   # se pulsó el atajo mientras cargaban los modelos
            log("atajo pulsado durante el arranque: escuchando")
            self.activar.set()
        activacion = ", ".join([PALABRA_ACTIVACION] + VARIANTES_ACTIVACION) if PALABRA_ACTIVACION else "solo atajo"
        log(f"listo en {time.time() - t:.1f}s (Whisper {self.whisper.descripcion}, "
            f"VAD: {'Silero' if _VAD.session else 'RMS'}, "
            f"{len({a.id for a in APPS.values()})} apps, voces: {', '.join(self.voz.voces)}, "
            f"nombre: {NOMBRE}, activación: {activacion}, intérprete: reglas"
            f"{' + clasificador' if clasificador() else ''}{'' if LLM == 'ninguno' else f' + {LLM} {MODELO_COMANDOS}'})")

    def _senal(self, *_):
        if self.voz.hablando:
            self.voz.callar()
            self.reescuchar = True
        elif self.escuchando:
            self.detener.set()
        elif self.ocupado.locked():
            # Procesando (transcribiendo o pensando): la pulsación no se pierde, escucha al terminar
            if not self.encolado:
                self.encolado = True
                log("atajo mientras procesa: escuchará al terminar")
                threading.Thread(target=self._avisar_ocupado, daemon=True).start()
        else:
            self.activar.set()

    def _avisar_ocupado(self):
        sonido("ocupado")
        notificar("Un momento… / One moment…", "Te escucho al terminar / I'll listen when done",
                  segundos=4, transitoria=True)

    def precargar_llm(self):
        if LLM == "ninguno":
            return
        try:
            llm(MODELO_COMANDOS, "Responde ok.", "ok")
        except Exception as e:
            log(f"no se pudo precargar {MODELO_COMANDOS}: {e}")

    def _grabar_y_transcribir(self, espera_inicio=ESPERA_INICIO, vaciar=True):
        """Con la palabra de activación graba del micrófono compartido. `vaciar` descarta lo que se oyó
        antes (p. ej. la propia voz del asistente); tras "Oye Nova" no se vacía: puede que ya estés
        diciendo el comando."""
        if self.mic and vaciar:
            self.mic.vaciar()
        self.escuchando = True
        self.detener.clear()
        audio = grabar(self.detener, espera_inicio=espera_inicio, fuente=self.mic.bloques() if self.mic else None)
        self.escuchando = False
        if audio is None:
            return None, None, 0.0
        sonido("procesando")
        notificar("Procesando… / Processing…", "", segundos=15, transitoria=True)
        t = time.time()
        guardar_grabacion(audio)
        texto, lang_stt = self.whisper.transcribir(audio, pista_whisper())
        if PALABRA_ACTIVACION:                 # "Oye Nova, abre kitty" también vale tras el atajo
            puntaje, n = puntaje_activacion(texto)
            if puntaje >= UMBRAL_ACTIVACION:
                texto = quitar_palabras(texto, n)
        return texto, lang_stt, time.time() - t

    def _continuar_activacion(self, previo, umbral, silencio, filtro):
        """Termina la frase que empezó con la posible palabra de activación y la confirma con el Whisper
        principal. Devuelve (texto, idioma, segundos) del comando, o None si no era la palabra.
        Mientras no está confirmado solo hay avisos visuales y discretos: en una reunión, cada falso
        candidato sonaría."""
        notificar(f"¿{NOMBRE}? Te escucho… / Listening…", "", segundos=15, transitoria=True)
        self.escuchando = True
        self.detener.clear()
        audio = grabar(self.detener, fuente=self.mic.bloques(), previo=previo, hablando=True, silencio=silencio,
                       umbral=umbral)
        self.escuchando = False
        if audio is None:
            cerrar_notificacion()
            return None
        notificar(f"¿{NOMBRE}? Procesando… / Processing…", "", segundos=15, transitoria=True)
        t = time.time()
        texto, lang_stt = self.whisper.transcribir(audio, pista_whisper())
        puntaje, n = puntaje_activacion(texto)
        ok = puntaje >= UMBRAL_ACTIVACION or (filtro >= FILTRO_SEGURO and puntaje >= FILTRO_ACTIVACION)
        log(f"activación: «{texto}» filtro {filtro:.2f}, confirmación {puntaje:.2f} -> "
            f"{'ACTIVA' if ok else 'rechazada'}")
        guardar_grabacion(audio, conservar=20, carpeta="grabaciones/activacion",
                          sufijo=f"-{'si' if ok else 'no'}-{filtro:.2f}-{puntaje:.2f}")
        if not ok:
            cerrar_notificacion()
            return None
        guardar_grabacion(audio)
        texto = quitar_palabras(texto, n)
        if len(clave(texto)) >= 2:
            sonido("procesando")               # confirmado: ahora sí suena «te oí»
            return texto, lang_stt, time.time() - t
        # Solo dijo "Oye Nova": aviso y escucha el comando
        notificar("Escuchando… / Listening…", "", segundos=15, transitoria=True)
        sonido("escuchando")
        return self._grabar_y_transcribir(vaciar=False)

    def _ejecutar_y_responder(self, texto, accion, param, lang):
        if accion == "pregunta":
            m = MSG[lang]
            notificar(m["pensando"], param, "dialog-question", segundos=15)
            try:
                oraciones = llm_stream(MODELO_PREGUNTAS, m["responde"], param)
                primera = [True]

                def avance(parcial):
                    if primera:            # el sonido de éxito, solo cuando ya hay respuesta
                        primera.clear()
                        sonido("exito")
                    notificar(f"«{texto}»", parcial, "dialog-information", segundos=15)

                respuesta = self.voz.decir_stream(oraciones, lang, al_avanzar=avance)
                if not respuesta:
                    notificar(f"«{texto}»", m["no_entendi"], "dialog-warning", segundos=5)
            except Exception as e:
                clave_msg = "sin_llm" if isinstance(e, SinLLM) else "llm_error"
                log(f"pregunta sin respuesta: {'llm = ninguno' if isinstance(e, SinLLM) else e}")
                sonido("fallo")
                notificar(f"«{texto}»", m[clave_msg], "dialog-warning", segundos=5)
                self.voz.decir(m[clave_msg], lang)
            return

        try:
            respuesta = ejecutar(accion, param, lang)
            ok = accion not in ("no_entendido", "error_llm")
        except SinLLM:
            respuesta, ok = MSG[lang]["sin_llm"], False
        except Exception as e:
            log(f"error ejecutando {accion}: {e}")
            respuesta, ok = MSG[lang]["fallo"], False

        sonido("exito" if ok else "fallo")
        notificar(f"«{texto}»", respuesta or "✓", "dialog-information" if ok else "dialog-warning", segundos=5)
        if respuesta:
            self.voz.decir(respuesta, lang)

    def atender(self, activacion=None):
        """Atiende un comando. `activacion` = (previo, umbral, silencio, filtro) cuando viene de la
        palabra de activación y la frase ya empezó."""
        if not self.ocupado.acquire(blocking=False):
            if activacion is None:             # atajo con la palabra de activación procesando
                self.encolado = True
            return
        try:
            self._atender(activacion)
        finally:
            self.escuchando = False
            self.ocupado.release()

    def _atender(self, activacion):
        if activacion is None:
            notificar("Escuchando… / Listening…", "", segundos=15, transitoria=True)
            sonido("escuchando", esperar=True)
            texto, lang_stt, t_stt = self._grabar_y_transcribir()
        else:
            r = self._continuar_activacion(*activacion)
            if r is None:
                return
            texto, lang_stt, t_stt = r
        # Toda interacción termina con un sonido y una notificación, y queda en el log
        if texto is None:
            log("no escuché nada (no se detectó voz)")
            sonido("no_escuche")
            notificar("No escuché nada / Didn't hear anything", "Habla justo después del pitido / Speak after the beep",
                      "dialog-warning", segundos=4)
            return
        if len(texto) < 2 or any(a in normalizar(texto) for a in ALUCINACIONES) or recita_pista(texto, pista_whisper()):
            log(f"descartado (alucinación de Whisper): «{texto[:80]}»")
            sonido("fallo")
            notificar("No te entendí / Didn't catch that", "Inténtalo de nuevo / Please try again",
                      "dialog-warning", segundos=4)
            return

        n = normalizar(texto)
        lang = idioma(texto, lang_stt)
        # Si había una confirmación pendiente vigente y el usuario respondió con el atajo
        if self.pendiente and time.time() < self.pendiente[3]:
            p_accion, p_param, p_lang, _ = self.pendiente
            self.pendiente = None
            if re.search(AFIRMATIVO, n):
                self._ejecutar_y_responder(texto, p_accion, p_param, p_lang)
                return
            if re.search(NEGATIVO, n):
                msg = MSG[p_lang]["cancelado"]
                notificar(f"«{texto}»", msg, "dialog-information", segundos=4)
                self.voz.decir(msg, p_lang)
                return
        self.pendiente = None

        notificar(f"«{texto}»", "…")
        t = time.time()
        accion, param = interpretar(texto)
        t_llm = time.time() - t
        log(f"«{texto}» [{lang}] -> {accion}({param!r})  [voz {t_stt:.1f}s, intención {t_llm:.1f}s]")

        pregunta_conf = (MSG[lang].get(f"confirmar_{accion}_{param}") or MSG[lang].get(f"confirmar_{accion}")
                         if accion in CONFIRMAR | CONFIRMAR_SIEMPRE else None)
        if pregunta_conf:
            self.pendiente = (accion, param, lang, time.time() + 15.0)
            notificar(f"«{texto}»", pregunta_conf, "dialog-question", segundos=8)
            self.voz.decir(pregunta_conf, lang)
            sonido("escuchando", esperar=True)
            resp, _, _ = self._grabar_y_transcribir(espera_inicio=4.0)
            if resp and re.search(AFIRMATIVO, normalizar(resp)):
                self.pendiente = None
                self._ejecutar_y_responder(texto, accion, param, lang)
            elif resp is not None:
                self.pendiente = None
                msg = MSG[lang]["cancelado"]
                notificar(f"«{resp}»", msg, "dialog-information", segundos=4)
                self.voz.decir(msg, lang)
            else:                              # sigue pendiente 15 s: se puede responder con el atajo
                log(f"confirmación de {accion} sin respuesta")
                sonido("no_escuche")
                notificar(pregunta_conf, "Pulsa el atajo y di «sí» / Press the shortcut and say «yes»",
                          "dialog-question", segundos=12)
            return

        self._ejecutar_y_responder(texto, accion, param, lang)

    def escuchar_activacion(self):
        """Hilo: busca la palabra de activación al inicio de cada frase que se escucha."""
        previo = collections.deque(maxlen=10)          # 0,3 s antes de que empiece la voz
        seg, silencio, piso, revisado = [], 0.0, 100.0, False
        estado_vad = _VAD.nuevo_estado() if _VAD.session else None
        vaciar = False
        ventana = int(VENTANA_ACTIVACION * RATE / BLOQUE)
        while True:
            # Atendiendo un comando o hablando (p. ej. un temporizador): no toca el micrófono
            if self.ocupado.locked() or self.voz.hablando:
                time.sleep(0.05)
                seg, silencio, revisado = [], 0.0, False
                previo.clear()
                vaciar = True
                continue
            if vaciar:                                 # descarta lo grabado mientras hablaba el asistente
                self.mic.vaciar()
                estado_vad = _VAD.nuevo_estado() if _VAD.session else None
                vaciar = False
            try:
                a = self.mic.cola.get(timeout=0.5)
            except queue.Empty:
                continue
            rms = nivel(a)
            umbral = max(400.0, 3 * piso)
            if estado_vad is not None:                 # Silero no confunde música o ruido con voz
                prob, estado_vad = _VAD.probabilidad(a, estado_vad)
                hay_voz = prob >= 0.5
            else:
                hay_voz = rms > umbral
            if hay_voz:
                if not seg:
                    seg = list(previo)
                seg.append(a)
                silencio = 0.0
            elif seg:
                seg.append(a)
                silencio += BLOQUE / RATE
            else:
                previo.append(a)
                piso = 0.95 * piso + 0.05 * rms
                continue
            dur = len(seg) * BLOQUE / RATE
            fin = silencio >= 0.5
            if not revisado and (fin or dur >= VENTANA_ACTIVACION):
                revisado = True
                if dur >= 0.4:
                    audio = np.concatenate(seg[:ventana]).astype(np.float32) / 32768.0
                    texto = self.whisper_activacion.transcribir(audio)
                    filtro = puntaje_activacion(texto)[0]
                    if filtro >= FILTRO_ACTIVACION:
                        log(f"¿activación? «{texto.strip()}» ({filtro:.2f})")
                        try:
                            self.atender(activacion=(seg, umbral, silencio, filtro))
                        except Exception as e:
                            log(f"error: {e}")
                        fin = True
            if fin or dur > MAX_GRABACION:
                seg, silencio, revisado = [], 0.0, False
                previo.clear()

    def correr(self):
        threading.Thread(target=self.precargar_llm, daemon=True).start()
        if self.mic:
            threading.Thread(target=self.escuchar_activacion, daemon=True).start()
        while True:
            # Pulsación pendiente (llegó mientras procesaba): en cuanto se libera, a escuchar
            if self.encolado and not self.ocupado.locked():
                self.encolado = False
                self.activar.set()
            if self.activar.wait(timeout=0.2):
                self.activar.clear()
                try:
                    self.atender()
                except Exception as e:
                    log(f"error: {e}")
                    sonido("fallo")
                    notificar("Error del asistente", str(e), "dialog-error")
                self.activar.clear()
                if self.reescuchar:            # el atajo interrumpió la voz
                    self.reescuchar = False
                    self.activar.set()


if __name__ == "__main__":
    if motor_whisper() == "cuda":
        preparar_cuda()
    Asistente().correr()
