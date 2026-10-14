"""Asistente de voz local para KDE (español e inglés).

Flujo: atajo de teclado -> graba hasta que dejas de hablar -> Whisper (OpenVINO, detecta
el idioma) -> reglas rápidas o llama3.2 (Ollama, GPU) -> acción de una lista blanca ->
respuesta hablada con Piper en el mismo idioma + notificación.

Se activa enviando SIGUSR1 al servicio (eso hace el atajo):
    systemctl --user kill -s USR1 --kill-whom=main asistente-voz
Una segunda pulsación mientras escucha deja de grabar en ese momento.

Logs: journalctl --user -u asistente-voz -f
"""
import configparser
import difflib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import unicodedata
import urllib.request
import wave
from pathlib import Path

import numpy as np
import openvino_genai
from piper import PiperVoice

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

WHISPER = BASE / "modelos/whisper-medium-int8-ov"
# Se prueban en orden. GPU requiere el paquete intel-compute-runtime;
# NPU, que Fedora incluya el compilador del NPU en intel-npu-driver.
WHISPER_DISPOSITIVOS = ["GPU", "CPU"]
VOCES = {
    "es": BASE / "modelos/voces/es/es_MX/claude/high/es_MX-claude-high.onnx",
    "en": BASE / "modelos/voces/en/en_US/lessac/medium/en_US-lessac-medium.onnx",
}
HABLAR = CFG.get("hablar", True)   # False = solo notificaciones

OLLAMA = "http://127.0.0.1:11434/api/chat"
MODELO_COMANDOS = CFG.get("modelo_comandos", "llama3.2")
MODELO_PREGUNTAS = CFG.get("modelo_preguntas", "llama3.2")  # uno más grande responde mejor, pero carga más lento
NUM_CTX = 4096                 # igual para ambos usos, así Ollama no recarga el modelo
KEEP_ALIVE = "60m"

PROYECTOS = Path(CFG.get("proyectos", "~/Proyectos")).expanduser()   # carpeta con tus repos git
COMPOSE = Path(CFG["compose"]).expanduser() if CFG.get("compose") else None  # docker-compose para levantar/detener

# Terminal para las herramientas de consola y herramientas que se abren dentro de ella
TERMINAL = CFG.get("terminal", "kitty")
APPS_TERMINAL = CFG.get("apps_terminal", ["htop", "btop", "top", "nvtop", "lazygit", "lazydocker", "k9s",
                                          "ranger", "yazi", "ncdu"])

# Apps gráficas: se leen de los .desktop instalados (como el menú de KDE).
# Estos alias (clave normalizada sin espacios -> id del .desktop o herramienta de consola)
# cubren nombres genéricos y errores típicos de transcripción.
ALIAS_APPS = {
    **dict.fromkeys(["vscode", "vs", "code", "visualstudiocode", "visualcode", "visualstudio",
                     "visualestudiocode", "vscodigo", "codigo"], "com.microsoft.VSCode"),
    **dict.fromkeys(["navegador", "browser", "webbrowser", "elnavegador"], "org.mozilla.firefox"),
    **dict.fromkeys(["terminal", "consola", "laterminal", "kiti", "kity", "kitti"], "kitty"),
    **dict.fromkeys(["archivos", "explorador", "exploradordearchivos", "files", "filemanager"], "org.kde.dolphin"),
    **dict.fromkeys(["configuracion", "ajustes", "settings", "configuraciondelsistema"], "systemsettings"),
    **dict.fromkeys(["compass", "mongo", "mongodb", "mongocompass", "compas"], "com.mongodb.Compass"),
    **dict.fromkeys(["monitordelsistema", "monitor", "systemmonitor"], "org.kde.plasma-systemmonitor"),
    **dict.fromkeys(["calculadora", "calculator"], "org.kde.kcalc"),
    **dict.fromkeys(["edge", "microsoftedge"], "microsoft-edge"),
    **dict.fromkeys(["hetop", "hitop", "achetop"], "htop"),
    **CFG.get("alias_apps", {}),
}
DIRS_DESKTOP = [HOME / ".local/share/applications", HOME / ".local/share/flatpak/exports/share/applications",
                Path("/var/lib/flatpak/exports/share/applications"), Path("/usr/share/applications")]

RATE = 16000
BLOQUE = 480                  # 30 ms
ESPERA_INICIO = 5.0           # segundos esperando a que empieces a hablar
SILENCIO_FIN = 1.0            # segundos de silencio que cierran la frase
MAX_GRABACION = 15.0

SONIDOS = Path("/usr/share/sounds/ocean/stereo")
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
os.environ["PATH"] = f"{HOME}/.local/bin:{os.environ.get('PATH', '/usr/bin')}"

# Textos que Whisper "alucina" cuando solo hay silencio o ruido
ALUCINACIONES = ("amara.org", "subtitulos", "gracias por ver", "suscribete", "gracias por su atencion",
                 "thanks for watching", "thank you for watching", "subscribe")

# --- Textos de respuesta -----------------------------------------------------------
MSG = {
    "es": {
        "app_no": "No encontré la aplicación {x}.", "abriendo": "Abriendo {x}.",
        "proyecto_no": "No encontré un proyecto parecido a {x}.", "proyecto": "Abriendo {x} en Visual Studio Code.",
        "docker_0": "No hay contenedores corriendo.", "docker_n": "Hay {n} contenedores corriendo: {x}.",
        "docker_up": "Levantando contenedores…", "docker_stop": "Deteniendo contenedores…",
        "docker_up_ok": "Contenedores levantados.", "docker_stop_ok": "Contenedores detenidos.",
        "docker_error": "Docker compose falló, revisa el registro.",
        "docker_sin_compose": "No hay un archivo docker compose configurado.",
        "vol_n": "Volumen al {x} por ciento.", "vol_on": "Sonido activado.",
        "media_no": "No entendí qué hacer con la música.", "media_0": "No hay ningún reproductor abierto.",
        "perfil_no": "Los perfiles son rendimiento, equilibrado o ahorro.",
        "perfil": "Perfil {x} activado durante una hora.", "buscando": "Buscando {x}.",
        "ollama_error": "No pude conectar con Ollama.", "no_entendi": "No te entendí.",
        "fallo": "Algo falló al ejecutar el comando.", "pensando": "Pensando…",
        "estado": "El procesador está a {t} grados, usas {u:.0f} de {m:.0f} gigas de memoria, "
                  "el disco tiene {d:.0f} gigas libres, y el perfil de energía es {p}.",
        "perfiles": {"performance": "rendimiento", "balanced": "equilibrado", "quiet": "silencioso", "cool": "fresco",
                     "power-saver": "ahorro"},
        "responde": "Responde en español, en máximo 3 frases cortas. El texto se leerá en voz alta: "
                    "sin markdown, sin listas, sin bloques de código.",
    },
    "en": {
        "app_no": "I couldn't find the application {x}.", "abriendo": "Opening {x}.",
        "proyecto_no": "I couldn't find a project like {x}.", "proyecto": "Opening {x} in Visual Studio Code.",
        "docker_0": "No containers are running.", "docker_n": "There are {n} containers running: {x}.",
        "docker_up": "Starting containers…", "docker_stop": "Stopping containers…",
        "docker_up_ok": "Containers started.", "docker_stop_ok": "Containers stopped.",
        "docker_error": "Docker compose failed, check the log.",
        "docker_sin_compose": "No docker compose file is configured.",
        "vol_n": "Volume at {x} percent.", "vol_on": "Sound on.",
        "media_no": "I didn't understand what to do with the music.", "media_0": "No media player is open.",
        "perfil_no": "The profiles are performance, balanced or power saver.",
        "perfil": "{x} profile enabled for one hour.", "buscando": "Searching for {x}.",
        "ollama_error": "I couldn't connect to Ollama.", "no_entendi": "Sorry, I didn't understand.",
        "fallo": "Something went wrong running the command.", "pensando": "Thinking…",
        "estado": "The CPU is at {t} degrees, you're using {u:.0f} of {m:.0f} gigabytes of memory, "
                  "the disk has {d:.0f} gigabytes free, and the power profile is {p}.",
        "perfiles": {"performance": "performance", "balanced": "balanced", "quiet": "quiet", "cool": "cool",
                     "power-saver": "power saver"},
        "responde": "Answer in English, in at most 3 short sentences. The text will be read aloud: "
                    "no markdown, no lists, no code blocks.",
    },
}


def log(msg):
    print(msg, flush=True)


def normalizar(texto):
    t = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def clave(texto):
    return re.sub(r"[^a-z0-9]", "", normalizar(texto))


MARCAS_EN = {"open", "launch", "start", "run", "the", "search", "for", "volume", "turn", "up", "down", "mute",
             "unmute", "what", "how", "is", "are", "running", "containers", "lock", "screen", "play", "pause",
             "next", "previous", "song", "music", "stop", "show", "status", "mode", "power", "project", "my",
             "please", "explain", "why", "who", "can", "you", "set", "to", "internet", "computer", "doing"}
MARCAS_ES = {"abre", "abrir", "el", "la", "los", "las", "busca", "buscar", "volumen", "sube", "baja", "que",
             "como", "contenedores", "proyecto", "pantalla", "musica", "cancion", "pausa", "modo", "en", "de",
             "del", "por", "esta", "estan", "pon", "levanta", "deten", "explica", "cual", "compu", "una", "un"}


def idioma(texto):
    p = normalizar(texto).replace("?", " ").replace(",", " ").replace(".", " ").split()
    en = sum(w in MARCAS_EN for w in p)
    es = sum(w in MARCAS_ES for w in p)
    return "en" if en > es else "es"


# --- Interfaz: sonidos, notificaciones, voz --------------------------------------
_notif_id = None


def sonido(nombre):
    f = SONIDOS / f"{nombre}.oga"
    if f.exists():
        subprocess.Popen(["pw-play", str(f)], stderr=subprocess.DEVNULL)


def notificar(titulo, cuerpo="", icono="audio-input-microphone", segundos=6):
    global _notif_id
    cmd = ["notify-send", "-p", "-a", "Asistente de voz", "-i", icono, "-t", str(segundos * 1000)]
    if _notif_id:
        cmd += ["-r", _notif_id]
    r = subprocess.run(cmd + [titulo, cuerpo], capture_output=True, text=True)
    _notif_id = r.stdout.strip() or None


class Voz:
    def __init__(self):
        self.voces = {k: PiperVoice.load(str(v)) for k, v in VOCES.items() if v.exists()} if HABLAR else {}

    def decir(self, texto, lang):
        voz = self.voces.get(lang) or self.voces.get("es")
        if not voz:
            return
        archivo = RUNTIME / "asistente-voz.wav"
        with wave.open(str(archivo), "wb") as w:
            voz.synthesize_wav(texto, w)
        subprocess.run(["pw-play", str(archivo)], stderr=subprocess.DEVNULL)


# --- Audio -----------------------------------------------------------------------
def grabar(detener):
    """Graba del micrófono por defecto hasta detectar el fin de la frase."""
    p = subprocess.Popen(["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    bloques, ruido = [], []
    hablando, silencio, umbral = False, 0.0, 400.0
    try:
        while not detener.is_set():
            data = p.stdout.read(BLOQUE * 2)
            if not data:
                break
            a = np.frombuffer(data, np.int16)
            bloques.append(a)
            rms = float(np.sqrt(np.mean(a.astype(np.float32) ** 2)))
            t = len(bloques) * BLOQUE / RATE
            if t < 0.3:                       # calibración del ruido de fondo
                ruido.append(rms)
                # percentil bajo: el sonido de inicio se cuela en los primeros bloques
                umbral = max(400.0, 3 * float(np.percentile(ruido, 20)))
                continue
            if rms > umbral:
                hablando, silencio = True, 0.0
            elif hablando:
                silencio += BLOQUE / RATE
            if hablando and silencio >= SILENCIO_FIN:
                break
            if not hablando and t > ESPERA_INICIO:
                return None
            if t > MAX_GRABACION:
                break
    finally:
        p.terminate()
        p.wait()
    if not hablando and not detener.is_set():
        return None
    return np.concatenate(bloques).astype(np.float32) / 32768.0


def guardar_grabacion(audio, conservar=10):
    """Guarda las últimas grabaciones para poder diagnosticar errores de transcripción."""
    d = BASE / "grabaciones"
    d.mkdir(exist_ok=True)
    with wave.open(str(d / time.strftime("%Y%m%d-%H%M%S.wav")), "wb") as w:
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


def cargar_apps():
    """Apps gráficas desde los .desktop + herramientas de consola instaladas. Devuelve {clave: App}."""
    apps, por_id = {}, {}
    for d in DIRS_DESKTOP:
        for f in sorted(d.glob("*.desktop")):
            id = f.stem
            if id in por_id or id.startswith("waydroid.") or id == "asistente-voz":
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
                    apps.setdefault(clave(e[k]), app)
            apps.setdefault(clave(id.split(".")[-1]), app)
    for tool in APPS_TERMINAL:
        if shutil.which(tool):
            por_id[tool] = apps[tool] = App(tool, tool, [TERMINAL, tool])
    if "kitty" in por_id:
        apps["kitty"] = por_id["kitty"]
    for alias, id in ALIAS_APPS.items():
        if id in por_id:
            apps[alias] = por_id[id]
    return apps


APPS = cargar_apps()


def resolver_app(nombre):
    k = clave(nombre)
    if not k:
        return None
    if k in APPS:
        return APPS[k]
    # quita artículos/verbos al inicio: "la terminal", "the browser", "app de compass"
    k2 = re.sub(r"^(el|la|los|las|the|app|aplicacion|programa)+", "", k)
    if k2 in APPS:
        return APPS[k2]
    cerca = difflib.get_close_matches(k, APPS.keys(), n=1, cutoff=0.82)
    return APPS[cerca[0]] if cerca else None


# --- Interpretación ----------------------------------------------------------------
ACCIONES = ["abrir_app", "abrir_proyecto", "docker_estado", "docker_levantar", "docker_detener",
            "volumen", "multimedia", "estado_sistema", "perfil_energia", "buscar_web",
            "bloquear_pantalla", "pregunta", "no_entendido"]

PROMPT_COMANDOS = """Eres el intérprete de comandos de voz de un PC de desarrollo. La frase puede estar en español o en inglés.
Convierte la frase en UNA acción. Los valores fijos de parametro van SIEMPRE en español, tal como aparecen aquí.
Acciones y su parámetro:
- abrir_app: abrir una aplicación (open/launch). parametro = nombre de la app tal como lo dijo (ej: firefox, vscode, kitty, compass, htop, terminal, calculadora)
- abrir_proyecto: abrir un proyecto/repositorio en VS Code. parametro = nombre del proyecto tal como lo dijo
- docker_estado: qué contenedores están corriendo (which containers are running). parametro = ""
- docker_levantar: levantar/iniciar los contenedores (start containers). parametro = ""
- docker_detener: detener/parar los contenedores (stop containers). parametro = ""
- volumen: parametro = subir | bajar | silenciar | activar | un número 0-100
- multimedia: parametro = pausa | reproducir | siguiente | anterior
- estado_sistema: temperatura, memoria, disco, cómo está el PC (system status). parametro = ""
- perfil_energia: parametro = rendimiento | equilibrado | ahorro
- buscar_web: buscar en internet (search the web). parametro = lo que hay que buscar, en el idioma original
- bloquear_pantalla: parametro = ""
- pregunta: una pregunta clara de conocimiento general o de programación. parametro = la pregunta completa
- no_entendido: muletillas, frases incompletas o sin sentido. parametro = ""
Responde solo el JSON."""

ESQUEMA = {"type": "object",
           "properties": {"accion": {"type": "string", "enum": ACCIONES}, "parametro": {"type": "string"}},
           "required": ["accion", "parametro"]}

REGLAS = [  # (regex sobre texto normalizado, acción, parámetro; None = la frase completa)
    (r"\b(sube|subir|aumenta)\w* (el )?volumen|\bturn (the )?volume up|\bturn up the volume|\bvolume up",
     "volumen", "subir"),
    (r"\b(baja|bajar|disminu)\w* (el )?volumen|\bturn (the )?volume down|\bturn down the volume|\bvolume down",
     "volumen", "bajar"),
    (r"\b(silencia|mutea|quita el sonido)|^mute\b", "volumen", "silenciar"),
    (r"\b(pausa|pausar|deten la musica|para la musica|pause)\b", "multimedia", "pausa"),
    (r"\b(siguiente cancion|siguiente tema|la siguiente|next song|next track)\b", "multimedia", "siguiente"),
    (r"\b(cancion anterior|tema anterior|la anterior|previous song|previous track)\b", "multimedia", "anterior"),
    (r"\bbloquea\w* (la )?(pantalla|sesion|compu|pc)|\block (the )?(screen|computer|pc|session)", "bloquear_pantalla", ""),
    # Preguntas directas, salvo que pida buscar en internet
    (r"^(?!.*\b(busca|internet|google|search)\b)\W*(que es|que son|que significa|como (funciona|se|hago|puedo)|"
     r"cual es la diferencia|por que|explica|diferencia entre|what is|what are|what does|how (does|do|can)|"
     r"why|explain|difference between)\b", "pregunta", None),
]

EVIDENCIA = {  # la acción del modelo solo se acepta si la frase normalizada cumple esta regex
    "abrir_app": r"\b(abr|lanz|inici|ejecut|pon|open|launch|start|run)\w*",
    "abrir_proyecto": r"\b(abr|proyecto|repo|open|project)\w*",
    "docker_estado": r"contenedor|container|docker",
    "docker_levantar": r"\b(levant|inici|arranc|sub|enciend|corre|start|run|bring|spin)\w*.*(contenedor|container|docker)",
    "docker_detener": r"\b(deten|det|par|apag|baj|stop|shut|kill)\w*.*(contenedor|container|docker)",
    "volumen": r"volumen|volume|sonido|sound|silenci|mute",
    "multimedia": r"musica|music|cancion|song|pausa|pause|reproduc|play|tema|track|siguiente|next|anterior|previous",
    "estado_sistema": r"temperatura|temperature|memoria|memory|disco|disk|estado|status|como esta|how is|ram|cpu"
                      r"|procesador|compu|computer|equipo|system",
    "perfil_energia": r"rendimiento|performance|equilibrad|balanced|ahorro|saver|perfil|profile|energia|power|modo|mode",
    "buscar_web": r"busca|search|internet|google|web|look up",
    "bloquear_pantalla": r"bloque|lock",
}

# Errores recurrentes de Whisper con esta voz/micrófono: "Abre" se transcribe como "Ahora", "A ver", "Aurek"…
ABRE_MAL_OIDO = r"^(ahora|aurea|aurek|aure|a ver|abra|habre|abri|abrir|ahi|haber|open up)\b,?\s*(a\s+)?"


def corregir(n):
    m = re.match(ABRE_MAL_OIDO, n)
    if m:
        resto = n[m.end():]
        if resolver_app(resto.strip(" .?!")) or re.search(r"\bproyecto\b", resto):
            return "abre " + resto
    return n


BUSQUEDA_INICIO = (r"^.*?\b(busca(r|me)?|search|look up|google)\b,?\s*((en|on)\s+)?((el|la|the)\s+)?"
                   r"(internet|google|web)?\s*(for|por|sobre)?\s*")
BUSQUEDA_FIN = r"\s+((en|on)\s+)?((el|la|the)\s+)?(internet|google|web|online)\s*[.?!]*$"


def extraer_busqueda(texto):
    """Toma lo que hay que buscar directamente de la frase (el modelo tiende a traducirlo)."""
    q = re.sub(BUSQUEDA_INICIO, "", texto, flags=re.IGNORECASE)
    q = re.sub(BUSQUEDA_FIN, "", q, flags=re.IGNORECASE)
    return q.strip(" .?!,")


def ollama(modelo, sistema, usuario, formato=None):
    cuerpo = {"model": modelo, "stream": False, "keep_alive": KEEP_ALIVE,
              "options": {"temperature": 0, "num_ctx": NUM_CTX},
              "messages": [{"role": "system", "content": sistema}, {"role": "user", "content": usuario}]}
    if formato:
        cuerpo["format"] = formato
    req = urllib.request.Request(OLLAMA, json.dumps(cuerpo).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["message"]["content"]


def interpretar(texto):
    n = corregir(normalizar(texto))
    if n != normalizar(texto):
        log(f"corregido: {n!r}")
        texto = n
    for patron, accion, param in REGLAS:
        if re.search(patron, n):
            return accion, texto if param is None else param
    if re.match(r"^\W*(busca|buscar|buscame|search|look up|google)\b", n) and extraer_busqueda(texto):
        return "buscar_web", extraer_busqueda(texto)
    # "abre X" / "open X" donde X es una app conocida: sin pasar por el modelo
    m = re.match(r"^(abre|abrir|open|launch|lanza|inicia|ejecuta)\s+(.+?)[.!?]*$", n)
    if m and resolver_app(m.group(2)) and "proyecto" not in m.group(2) and "project" not in m.group(2):
        return "abrir_app", m.group(2)
    try:
        r = json.loads(ollama(MODELO_COMANDOS, PROMPT_COMANDOS, texto, ESQUEMA))
        accion, param = r.get("accion", "no_entendido"), r.get("parametro", "")
    except Exception as e:
        log(f"error de Ollama: {e}")
        return "error_ollama", ""
    # El modelo tiende a adivinar: solo se acepta la acción si la frase contiene palabras que la respalden
    evidencia = EVIDENCIA.get(accion)
    if evidencia and not re.search(evidencia, n):
        log(f"descartado {accion}({param!r}): la frase no lo respalda")
        return "no_entendido", ""
    numero = re.search(r"\b(\d{1,3})\b", n)
    if accion == "volumen" and numero:
        param = numero.group(1)
    if accion == "pregunta" and len(n.split()) < 3:
        accion = "no_entendido"
    if accion == "buscar_web":
        param = extraer_busqueda(texto) or param
    if accion == "abrir_proyecto" and resolver_app(param):
        accion = "abrir_app"
    return accion, param


# --- Acciones (lista blanca) --------------------------------------------------------
def lanzar(cmd):
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def palabras(nombre):
    nombre = re.sub(r"([a-z])([A-Z])", r"\1 \2", nombre)
    return set(normalizar(nombre).replace(".", " ").replace("_", " ").replace("-", " ").split())


def proyectos():
    if not PROYECTOS.is_dir():
        return []
    return [d for d in PROYECTOS.iterdir() if (d / ".git").is_dir() and not d.name.startswith("_")]


def buscar_proyecto(pedido):
    objetivo = palabras(pedido) - {"proyecto", "el", "la", "de", "del", "repo", "repositorio", "project", "the"}
    mejor, puntaje = None, 0.0
    for d in proyectos():
        nombre = palabras(d.name)
        p = sum(max((difflib.SequenceMatcher(None, o, w).ratio() for w in nombre), default=0)
                for o in objetivo) / max(len(objetivo), 1)
        if p > puntaje:
            mejor, puntaje = d, p
    return mejor if puntaje >= 0.75 else None


def pista_whisper():
    """Vocabulario de contexto para Whisper. Lista de términos, no frases: con frases de ejemplo
    Whisper tiende a cortar las transcripciones."""
    nombres = [d.name for d in proyectos()]
    # "Empresa.Modulo.Sub" -> "Modulo Sub" cuando el prefijo se repite en varios repos
    prefijos = [n.split(".")[0] for n in nombres if "." in n]
    nombres = [n.split(".", 1)[1] if "." in n and prefijos.count(n.split(".")[0]) >= 3 else n for n in nombres]
    nombres = [n.replace(".", " ") for n in nombres]
    return ("VS Code, Visual Studio Code, Firefox, kitty, htop, lazygit, lazydocker, MongoDB Compass, Docker, "
            + ", ".join(n for n in nombres if n))


def mpris(metodo, solo_activo=False):
    r = subprocess.run(["busctl", "--user", "list", "--no-legend"], capture_output=True, text=True)
    reproductores = [l.split()[0] for l in r.stdout.splitlines() if l.startswith("org.mpris.MediaPlayer2.")]
    if not reproductores:
        return False
    for nombre in reproductores:
        subprocess.run(["busctl", "--user", "call", nombre, "/org/mpris/MediaPlayer2",
                        "org.mpris.MediaPlayer2.Player", metodo], capture_output=True)
        if solo_activo:
            break
    return True


def temperatura_cpu():
    for hw in Path("/sys/class/hwmon").iterdir():
        if (hw / "name").read_text().strip() == "coretemp":
            return int((hw / "temp1_input").read_text()) // 1000
    return None


def estado_sistema(m):
    info = dict(l.split(":", 1) for l in Path("/proc/meminfo").read_text().splitlines())
    total = int(info["MemTotal"].split()[0]) / 1048576
    libre = int(info["MemAvailable"].split()[0]) / 1048576
    perfil = Path("/sys/firmware/acpi/platform_profile").read_text().strip()
    return m["estado"].format(t=temperatura_cpu() or "?", u=total - libre, m=total,
                              d=shutil.disk_usage("/").free / 1e9, p=m["perfiles"].get(perfil, perfil))


def ejecutar(accion, param, lang):
    """Devuelve el texto de respuesta en el idioma indicado."""
    m = MSG[lang]

    if accion == "abrir_app":
        app = resolver_app(param)
        if not app:
            return m["app_no"].format(x=param)
        lanzar(app.cmd)
        return m["abriendo"].format(x=app.nombre)

    if accion == "abrir_proyecto":
        d = buscar_proyecto(param)
        if not d:
            return m["proyecto_no"].format(x=param)
        lanzar(["code", str(d)])
        return m["proyecto"].format(x=d.name)

    if accion == "docker_estado":
        r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True)
        nombres = [n.replace("-local", "").replace("-", " ") for n in r.stdout.split()]
        if not nombres:
            return m["docker_0"]
        return m["docker_n"].format(n=len(nombres), x=", ".join(nombres))

    if accion in ("docker_levantar", "docker_detener"):
        up = accion == "docker_levantar"
        if not COMPOSE:
            return m["docker_sin_compose"]
        notificar("Docker", m["docker_up" if up else "docker_stop"])
        r = subprocess.run(["docker", "compose", "-f", str(COMPOSE)] + (["up", "-d"] if up else ["stop"]),
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            log(r.stderr)
            return m["docker_error"]
        return m["docker_up_ok" if up else "docker_stop_ok"]

    if accion == "volumen":
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

    if accion == "multimedia":
        metodo = {"pausa": "Pause", "reproducir": "Play", "siguiente": "Next", "anterior": "Previous"}.get(param)
        if not metodo:
            return m["media_no"]
        if not mpris(metodo, solo_activo=metodo in ("Play", "Next", "Previous")):
            return m["media_0"]
        return ""

    if accion == "estado_sistema":
        return estado_sistema(m)

    if accion == "perfil_energia":
        perfil = {"rendimiento": "performance", "equilibrado": "balanced", "ahorro": "power-saver"}.get(param)
        if not perfil:
            return m["perfil_no"]
        subprocess.run(["busctl", "--system", "set-property", "org.freedesktop.UPower.PowerProfiles",
                        "/org/freedesktop/UPower/PowerProfiles", "org.freedesktop.UPower.PowerProfiles",
                        "ActiveProfile", "s", perfil])
        return m["perfil"].format(x=m["perfiles"][perfil])

    if accion == "buscar_web":
        lanzar(["firefox", "--search", param])
        return m["buscando"].format(x=param)

    if accion == "bloquear_pantalla":
        subprocess.run(["loginctl", "lock-sessions"])
        return ""

    if accion == "pregunta":
        notificar(m["pensando"], param, "dialog-question")
        return ollama(MODELO_PREGUNTAS, m["responde"], param).strip()

    if accion == "error_ollama":
        return m["ollama_error"]

    return m["no_entendi"]


# --- Bucle principal ---------------------------------------------------------------
class Asistente:
    def __init__(self):
        t = time.time()
        for dispositivo in WHISPER_DISPOSITIVOS:
            try:
                # Sin CACHE_DIR: con la GPU, el modelo leído de la caché transcribe basura (fallo de OpenVINO)
                self.whisper = openvino_genai.WhisperPipeline(str(WHISPER), dispositivo)
                self.dispositivo = dispositivo
                break
            except Exception as e:
                log(f"Whisper no disponible en {dispositivo}: {str(e).strip().splitlines()[-1][:120]}")
        self.voz = Voz()
        self.pista = pista_whisper()
        self.activar = threading.Event()
        self.detener = threading.Event()
        self.escuchando = False
        signal.signal(signal.SIGUSR1, self._senal)
        log(f"listo en {time.time() - t:.1f}s (Whisper {WHISPER.name} en {self.dispositivo}, "
            f"{len({a.id for a in APPS.values()})} apps, voces: {', '.join(self.voz.voces)})")

    def _senal(self, *_):
        if self.escuchando:
            self.detener.set()
        else:
            self.activar.set()

    def precargar_llm(self):
        try:
            ollama(MODELO_COMANDOS, "Responde ok.", "ok")
        except Exception as e:
            log(f"no se pudo precargar {MODELO_COMANDOS}: {e}")

    def atender(self):
        self.escuchando = True
        self.detener.clear()
        sonido("button-pressed")
        notificar("Escuchando… / Listening…", "", segundos=15)
        audio = grabar(self.detener)
        self.escuchando = False
        if audio is None:
            notificar("No escuché nada / Didn't hear anything", "", segundos=2)
            return

        t = time.time()
        guardar_grabacion(audio)
        # Sin language: Whisper detecta si es español o inglés
        texto = self.whisper.generate(audio.tolist(), task="transcribe",
                                      initial_prompt=self.pista).texts[0].strip()
        t_stt = time.time() - t
        if len(texto) < 2 or any(a in normalizar(texto) for a in ALUCINACIONES):
            notificar("No te entendí / Didn't catch that", "", segundos=2)
            return
        lang = idioma(texto)
        notificar(f"«{texto}»", "…")

        t = time.time()
        accion, param = interpretar(texto)
        t_llm = time.time() - t
        log(f"«{texto}» [{lang}] -> {accion}({param!r})  [voz {t_stt:.1f}s, intención {t_llm:.1f}s]")

        try:
            respuesta = ejecutar(accion, param, lang)
            ok = accion not in ("no_entendido", "error_ollama")
        except Exception as e:
            log(f"error ejecutando {accion}: {e}")
            respuesta, ok = MSG[lang]["fallo"], False

        sonido("completion-success" if ok else "completion-fail")
        notificar(f"«{texto}»", respuesta or "✓", "dialog-information" if ok else "dialog-warning",
                  segundos=15 if accion == "pregunta" else 5)
        if respuesta:
            self.voz.decir(respuesta, lang)

    def correr(self):
        threading.Thread(target=self.precargar_llm, daemon=True).start()
        while True:
            if self.activar.wait(timeout=1):
                self.activar.clear()
                try:
                    self.atender()
                except Exception as e:
                    log(f"error: {e}")
                    notificar("Error del asistente", str(e), "dialog-error")
                self.activar.clear()


if __name__ == "__main__":
    Asistente().correr()
