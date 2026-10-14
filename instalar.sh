#!/usr/bin/bash
# Instala el asistente de voz para el usuario actual (sin sudo).
# Se puede volver a ejecutar: omite lo que ya existe.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3.13}"

echo "==> Entorno de Python ($PY)"
[ -d "$DIR/venv" ] || "$PY" -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "==> Modelos (Whisper medium ~800 MB + voces ~200 MB)"
"$DIR/venv/bin/python" -I - "$DIR/modelos" <<'EOF'
import sys
from pathlib import Path
from huggingface_hub import hf_hub_download, snapshot_download
dst = Path(sys.argv[1])
if not (dst / "whisper-medium-int8-ov").exists():
    snapshot_download("OpenVINO/whisper-medium-int8-ov", local_dir=dst / "whisper-medium-int8-ov")
for voz in ["es/es_MX/claude/high/es_MX-claude-high.onnx", "en/en_US/lessac/medium/en_US-lessac-medium.onnx"]:
    for ext in ["", ".json"]:
        if not (dst / "voces" / (voz + ext)).exists():
            hf_hub_download("rhasspy/piper-voices", voz + ext, local_dir=dst / "voces")
EOF

echo "==> Configuración"
[ -f "$DIR/config.json" ] || { cp "$DIR/config.example.json" "$DIR/config.json"; echo "    Edita $DIR/config.json"; }

echo "==> Servicio de systemd y entrada del menú"
mkdir -p ~/.config/systemd/user ~/.local/share/applications
sed "s|@DIR@|$DIR|g" "$DIR/systemd/asistente-voz.service" > ~/.config/systemd/user/asistente-voz.service
cp "$DIR/systemd/asistente-voz.desktop" ~/.local/share/applications/
systemctl --user daemon-reload
systemctl --user enable --now asistente-voz.service
systemctl --user restart asistente-voz.service

echo
echo "Listo. Asigna un atajo en Configuración del sistema → Teclado → Atajos →"
echo "Añadir nuevo → Aplicación → \"Asistente de voz\"."
echo "Logs: journalctl --user -u asistente-voz -f"
