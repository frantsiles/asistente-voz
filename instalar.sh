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

faltan=()
for c in pw-record pw-play wpctl notify-send kioclient busctl ollama; do
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
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements-$MOTOR.txt"

echo "==> Modelos (Whisper ~800 MB-1,6 GB + voces ~200 MB)"
"$DIR/venv/bin/python" -I - "$DIR/modelos" "$MOTOR" "${WHISPER_MODELO:-large-v3-turbo}" <<'EOF'
import sys
from pathlib import Path
from huggingface_hub import hf_hub_download, snapshot_download
dst, motor, modelo = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
if motor == "nvidia":
    from faster_whisper import download_model
    if not (dst / f"faster-whisper-{modelo}").exists():
        download_model(modelo, output_dir=str(dst / f"faster-whisper-{modelo}"))
    if not (dst / "faster-whisper-base").exists():      # palabra de activación
        download_model("base", output_dir=str(dst / "faster-whisper-base"))
else:
    for m in ["whisper-medium-int8-ov", "whisper-base-int8-ov"]:   # comandos / palabra de activación
        if not (dst / m).exists():
            snapshot_download("OpenVINO/" + m, local_dir=dst / m)
for voz in ["es/es_MX/claude/high/es_MX-claude-high.onnx", "en/en_US/lessac/medium/en_US-lessac-medium.onnx"]:
    for ext in ["", ".json"]:
        if not (dst / "voces" / (voz + ext)).exists():
            hf_hub_download("rhasspy/piper-voices", voz + ext, local_dir=dst / "voces")
vad_dst = dst / "silero_vad_v6.onnx"
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

if ! curl -s -m 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
    echo
    echo "AVISO: Ollama no responde. Arráncalo con: sudo systemctl enable --now ollama"
fi
echo
echo "Listo. Asigna un atajo en Configuración del sistema → Teclado → Atajos →"
echo "Añadir nuevo → Aplicación → \"Asistente de voz\"."
echo "Logs: journalctl --user -u asistente-voz -f"
