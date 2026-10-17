#!/usr/bin/env bash
# Instala el asistente de voz para el usuario actual (sin sudo).
# Se puede volver a ejecutar: omite lo que ya existe.
#
# Variables opcionales:
#   PYTHON=/ruta/python3.13   intérprete a usar
#   MOTOR=nvidia|intel        fuerza el motor de Whisper (por defecto se detecta la GPU)
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
. /etc/os-release 2>/dev/null || true
DISTRO="${ID:-desconocida}"
case " $DISTRO ${ID_LIKE:-} " in    # derivadas (EndeavourOS, CachyOS, Nobara…)
    *" arch "*) DISTRO=arch ;;
    *" fedora "*) DISTRO=fedora ;;
esac

# Nombre del paquete que aporta cada comando, por distribución
paquete() {
    case "$DISTRO:$1" in
        fedora:pw-record|fedora:pw-play) echo pipewire-utils ;;
        fedora:wpctl)       echo wireplumber ;;
        fedora:notify-send) echo libnotify ;;
        fedora:kioclient)   echo kf6-kio-core ;;
        fedora:powerprofilesctl|arch:powerprofilesctl) echo power-profiles-daemon ;;
        arch:pw-record|arch:pw-play) echo pipewire ;;
        arch:wpctl)         echo wireplumber ;;
        arch:notify-send)   echo libnotify ;;
        arch:kioclient)     echo kio ;;
        arch:ollama)        if [ "$MOTOR" = nvidia ]; then echo ollama-cuda; else echo ollama; fi ;;
        *) echo "$1" ;;
    esac
}
instalar_cmd() {
    case "$DISTRO" in
        fedora) echo "sudo dnf install $*" ;;
        arch) echo "sudo pacman -S --needed $*" ;;
        *) echo "instala: $*" ;;
    esac
}

echo "==> Sistema: ${PRETTY_NAME:-$DISTRO}"
if [ -z "${MOTOR:-}" ]; then
    if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then MOTOR=nvidia; else MOTOR=intel; fi
fi
echo "    Whisper: $([ "$MOTOR" = nvidia ] && echo 'faster-whisper en GPU NVIDIA' || echo 'OpenVINO (GPU Intel / CPU)')"

# Proveedor del LLM en config.json (por defecto ollama): solo con Ollama se exige el comando
LLM_CFG="$(grep -o '"llm" *: *"[a-z]*"' "$DIR/config.json" 2>/dev/null | grep -o '[a-z]*"$' | tr -d '"' || true)"
LLM_CFG="${LLM_CFG:-ollama}"
faltan=()
for c in pw-record pw-play wpctl notify-send kioclient busctl $([ "$LLM_CFG" = ollama ] && echo ollama); do
    command -v "$c" >/dev/null || faltan+=("$(paquete "$c")")
done
if [ ${#faltan[@]} -gt 0 ]; then
    echo "    Faltan comandos. Instálalos con: $(instalar_cmd "${faltan[@]}")"
fi

echo "==> Entorno de Python"
# OpenVINO y CTranslate2 no siempre tienen ruedas para el Python más reciente (Arch ya usa 3.14)
if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
elif command -v python3.13 >/dev/null; then
    PY=python3.13
elif command -v uv >/dev/null; then
    uv python install 3.13
    PY="$(uv python find 3.13)"
else
    echo "    Necesito Python 3.13. Instálalo con:"
    [ "$DISTRO" = arch ] && echo "      sudo pacman -S uv   (y vuelve a ejecutar este script)" \
                         || echo "      $(instalar_cmd python3.13)"
    exit 1
fi
echo "    $PY ($("$PY" --version))"
[ -d "$DIR/venv" ] || "$PY" -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
# Versiones y hashes fijos (requirements-*.lock): si un paquete de PyPI cambia, pip se niega a instalarlo
"$DIR/venv/bin/pip" install -q --require-hashes -r "$DIR/requirements-$MOTOR.lock"

echo "==> Modelos (Whisper ~800 MB-1,6 GB + voces ~200 MB + clasificador ~113 MB)"
"$DIR/venv/bin/python" -I - "$DIR/modelos" "$MOTOR" "${WHISPER_MODELO:-large-v3-turbo}" <<'EOF'
import sys
from pathlib import Path
from huggingface_hub import hf_hub_download, snapshot_download
dst, motor, modelo = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
# Commit fijo de cada repositorio de Hugging Face: si alguien cambia los archivos, no se descargan.
# Para actualizar un modelo, cambia aquí su commit (huggingface.co/api/models/<repo> -> "sha").
REVISION = {
    "large-v3-turbo": "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",     # dropbox-dash/faster-whisper-large-v3-turbo
    "base": "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",               # Systran/faster-whisper-base
    "OpenVINO/whisper-medium-int8-ov": "8d43cce846729381f56bd45a1c70925cee2222ff",
    "OpenVINO/whisper-base-int8-ov": "0606293f0511136ada21755a265492f623a934b8",
    "rhasspy/piper-voices": "c10ece1aade47bb51c153c893d14e5bf8e5b7117",
    "Xenova/multilingual-e5-small": "761b726dd34fb83930e26aab4e9ac3899aa1fa78",
}
if motor == "nvidia":
    from faster_whisper import download_model
    if not (dst / f"faster-whisper-{modelo}").exists():   # otro WHISPER_MODELO: sin commit fijo
        download_model(modelo, output_dir=str(dst / f"faster-whisper-{modelo}"), revision=REVISION.get(modelo))
    if not (dst / "faster-whisper-base").exists():      # palabra de activación
        download_model("base", output_dir=str(dst / "faster-whisper-base"), revision=REVISION["base"])
else:
    for m in ["whisper-medium-int8-ov", "whisper-base-int8-ov"]:   # comandos / palabra de activación
        if not (dst / m).exists():
            snapshot_download("OpenVINO/" + m, local_dir=dst / m, revision=REVISION["OpenVINO/" + m])
for voz in ["es/es_MX/claude/high/es_MX-claude-high.onnx", "en/en_US/lessac/medium/en_US-lessac-medium.onnx"]:
    for ext in ["", ".json"]:
        if not (dst / "voces" / (voz + ext)).exists():
            hf_hub_download("rhasspy/piper-voices", voz + ext, local_dir=dst / "voces",
                            revision=REVISION["rhasspy/piper-voices"])
# Clasificador de comandos: multilingual-e5-small en ONNX int8 (~113 MB)
for f in ["onnx/model_quantized.onnx", "tokenizer.json"]:
    if not (dst / "e5-small" / f).exists():
        hf_hub_download("Xenova/multilingual-e5-small", f, local_dir=dst / "e5-small",
                        revision=REVISION["Xenova/multilingual-e5-small"])
vad_dst = dst / "silero_vad_v6.onnx"
VAD_SHA256 = "4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2"
if not vad_dst.exists():
    try:
        import shutil, faster_whisper.vad as v
        shutil.copy2(Path(v.get_assets_path()) / "silero_vad_v6.onnx", vad_dst)
    except Exception:
        import urllib.request
        urllib.request.urlretrieve(
            "https://raw.githubusercontent.com/SYSTRAN/faster-whisper/v1.2.1/faster_whisper/assets/silero_vad_v6.onnx",
            vad_dst,
        )
import hashlib
if hashlib.sha256(vad_dst.read_bytes()).hexdigest() != VAD_SHA256:
    vad_dst.unlink()
    sys.exit("silero_vad_v6.onnx no coincide con el hash esperado: borrado")
EOF

echo "==> Configuración"
if [ ! -f "$DIR/config.json" ]; then
    cp "$DIR/config.example.json" "$DIR/config.json"
    # La carpeta de proyectos por defecto es ~/Proyectos; si solo existe ~/Projects, usar esa
    [ ! -d ~/Proyectos ] && [ -d ~/Projects ] && sed -i 's|~/Proyectos|~/Projects|g' "$DIR/config.json"
    echo "    Edita $DIR/config.json"
fi

echo "==> Servicio de systemd y entrada del menú"
mkdir -p ~/.config/systemd/user ~/.local/share/applications
sed "s|@DIR@|$DIR|g" "$DIR/systemd/asistente-voz.service" > ~/.config/systemd/user/asistente-voz.service
cp "$DIR/systemd/asistente-voz.desktop" ~/.local/share/applications/
systemctl --user daemon-reload
systemctl --user enable --now asistente-voz.service
systemctl --user restart asistente-voz.service

if [ "$LLM_CFG" = ollama ] && ! curl -s -m 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
    echo
    echo "AVISO: Ollama no responde. Arráncalo con: sudo systemctl enable --now ollama"
fi
echo
echo "Listo. Asigna un atajo en Configuración del sistema → Teclado → Atajos →"
echo "Añadir nuevo → Aplicación → \"Asistente de voz\"."
echo "Logs: journalctl --user -u asistente-voz -f"
