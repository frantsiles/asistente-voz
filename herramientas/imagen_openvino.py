"""Genera una imagen con OpenVINO GenAI (LCM Dreamshaper v7, 4 pasos), sin torch ni Pillow.

Alternativa a Fooocus para equipos sin GPU NVIDIA: ~20 s por imagen de 512×512 en la CPU de un
Core Ultra 7 255U. Lo lanza el asistente con el Python de su propio entorno, en un proceso aparte
para que los ~7 GB de RAM que usa al generar se liberen al terminar:

    venv/bin/python -I herramientas/imagen_openvino.py modelos/LCM_Dreamshaper_v7-int8-ov CPU < peticion.json

Mismo protocolo que imagen_fooocus.py. La petición llega por stdin:
    {"prompt": "...", "salida": "/ruta/imagen.png", "ancho": 512, "alto": 512, "pasos": 4, "semilla": 123}
Escribe el PNG en `salida` y, como última línea, {"ok": true, "ruta": "..."} o {"ok": false, "error": "..."}.

En la GPU integrada Intel (i915, Arc 255U) la difusión colgó la GPU (CL_OUT_OF_RESOURCES y reinicio del
contexto), por eso el dispositivo por defecto es la CPU.
"""
import json
import random
import struct
import sys
import zlib


def guardar_png(pixeles, ruta):
    """PNG RGB de 8 bits con la biblioteca estándar: evita añadir Pillow al entorno."""
    alto, ancho, _ = pixeles.shape
    def bloque(tipo, datos):
        return struct.pack(">I", len(datos)) + tipo + datos + struct.pack(">I", zlib.crc32(tipo + datos))
    filas = b"".join(b"\0" + pixeles[y].tobytes() for y in range(alto))
    with open(ruta, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + bloque(b"IHDR", struct.pack(">IIBBBBB", ancho, alto, 8, 2, 0, 0, 0))
                + bloque(b"IDAT", zlib.compress(filas, 6)) + bloque(b"IEND", b""))


def main():
    modelo, dispositivo = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "CPU"
    peticion = json.load(sys.stdin)

    import numpy as np
    import openvino_genai

    pipeline = openvino_genai.Text2ImagePipeline(modelo, dispositivo)
    imagen = pipeline.generate(
        peticion["prompt"].strip(),
        width=int(peticion.get("ancho", 512)), height=int(peticion.get("alto", 512)),
        num_inference_steps=int(peticion.get("pasos", 4)),
        rng_seed=int(peticion.get("semilla") or random.randint(0, 2**31 - 1)),
    )
    salida = peticion["salida"]
    guardar_png(np.ascontiguousarray(imagen.data[0], dtype=np.uint8), salida)
    return salida


if __name__ == "__main__":
    try:
        ruta = main()
        print(json.dumps({"ok": True, "ruta": ruta}), flush=True)
    except Exception as e:  # el asistente solo necesita saber que falló; el detalle queda en stderr
        import traceback
        traceback.print_exc()
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}), flush=True)
        sys.exit(1)
