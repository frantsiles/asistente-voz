"""Genera una imagen con OpenVINO GenAI (LCM Dreamshaper v7, 4 pasos) y la escala x4 (512×512 -> 2048×2048).

Alternativa a Fooocus para equipos sin GPU NVIDIA: ~20 s por imagen de 512×512 en la CPU de un
Core Ultra 7 255U + superresolución x4 hasta 2048×2048 con uno de tres motores (`"escalar"` en la petición):
  - "realesrgan" (por defecto): Real-ESRGAN Compact (realesr-general-x4v3.onnx, 4,6 MB, ~1,5 s)
  - "openvino": Intel Open Model Zoo (single-image-super-resolution-1032.xml, 150 KB, ~0,8 s)
  - "realesrgan_x4plus": Real-ESRGAN x4plus RRDB completo (RealESRGAN_x4plus.fp16.onnx, 33,6 MB, ~26 s)
  - null / "ninguno": sin superresolución (512×512)
Sin torch ni Pillow. Lo lanza el asistente con el Python de su propio entorno, en un proceso aparte
para que los ~7 GB de RAM que usa al generar se liberen al terminar:

    venv/bin/python -I herramientas/imagen_openvino.py modelos/LCM_Dreamshaper_v7-int8-ov CPU < peticion.json

Mismo protocolo que imagen_fooocus.py. La petición llega por stdin:
    {"prompt": "...", "salida": "/ruta/imagen.png", "escalar": "realesrgan", "ancho": 512, "alto": 512, "pasos": 4}
Escribe el PNG en `salida` y, como última línea, {"ok": true, "ruta": "..."} o {"ok": false, "error": "..."}.

En la GPU integrada Intel (i915, Arc 255U) la difusión colgó la GPU (CL_OUT_OF_RESOURCES y reinicio del
contexto), por eso el dispositivo por defecto es la CPU.
"""
import json
import random
import struct
import sys
import zlib
from pathlib import Path

MODELOS_SR = {
    "realesrgan": "realesr-general-x4v3.onnx",
    "openvino": "sr-omz/single-image-super-resolution-1032.xml",
    "realesrgan_x4plus": "RealESRGAN_x4plus.fp16.onnx",
}


def guardar_png(pixeles, ruta):
    """PNG RGB de 8 bits con la biblioteca estándar: evita añadir Pillow al entorno."""
    alto, ancho, _ = pixeles.shape
    def bloque(tipo, datos):
        return struct.pack(">I", len(datos)) + tipo + datos + struct.pack(">I", zlib.crc32(tipo + datos))
    filas = b"".join(b"\0" + pixeles[y].tobytes() for y in range(alto))
    with open(ruta, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + bloque(b"IHDR", struct.pack(">IIBBBBB", ancho, alto, 8, 2, 0, 0, 0))
                + bloque(b"IDAT", zlib.compress(filas, 6)) + bloque(b"IEND", b""))


def leer_imagen(ruta):
    """Decodifica una imagen (JPG, PNG, WebP, BMP…) a un array RGB uint8 (H, W, 3) sin Pillow en el venv."""
    import shutil
    import subprocess
    import numpy as np

    ruta = str(ruta)
    for cmd in (
        ["magick", "--", ruta, "-alpha", "off", "-depth", "8", "ppm:-"] if shutil.which("magick") else None,
        ["ffmpeg", "-v", "error", "-i", ruta, "-pix_fmt", "rgb24", "-f", "image2pipe", "-vcodec", "ppm", "-"]
        if shutil.which("ffmpeg") else None,
    ):
        if not cmd:
            continue
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode == 0 and r.stdout.startswith(b"P6"):
            datos = r.stdout
            pos, tokens = 2, []
            while len(tokens) < 3:
                while pos < len(datos) and datos[pos:pos + 1] in (b" ", b"\t", b"\r", b"\n"):
                    pos += 1
                if datos[pos:pos + 1] == b"#":
                    pos = datos.find(b"\n", pos) + 1
                    continue
                fin = pos
                while fin < len(datos) and datos[fin:fin + 1] not in (b" ", b"\t", b"\r", b"\n"):
                    fin += 1
                tokens.append(int(datos[pos:fin]))
                pos = fin
            pos += 1  # un único byte de espacio/salto tras el valor máximo (255)
            w, h, _ = tokens
            return np.frombuffer(datos, dtype=np.uint8, offset=pos, count=h * w * 3).reshape(h, w, 3).copy()

    # Respaldo con la biblioteca estándar para PNG RGB de 8 bits (p. ej. en contenedores de CI sin magick/ffmpeg)
    datos = Path(ruta).read_bytes()
    if datos.startswith(b"\x89PNG\r\n\x1a\n"):
        pos, w, h, idat = 8, 0, 0, b""
        while pos < len(datos):
            l = struct.unpack(">I", datos[pos:pos + 4])[0]
            t, c = datos[pos + 4:pos + 8], datos[pos + 8:pos + 8 + l]
            if t == b"IHDR":
                w, h = struct.unpack(">II", c[:8])
            elif t == b"IDAT":
                idat += c
            pos += 12 + l
        raw = zlib.decompress(idat)
        return np.frombuffer(raw, dtype=np.uint8).reshape(h, 1 + w * 3)[:, 1:].reshape(h, w, 3).copy()
    raise RuntimeError(f"No pude decodificar la imagen {ruta} (instala ImageMagick o ffmpeg)")


def _interpolar_x4(x):
    """Interpolación bilineal separable x4 en NCHW (entrada base que pide single-image-super-resolution-1032)."""
    import numpy as np
    _, _, h, w = x.shape
    gh = (np.arange(h * 4, dtype=np.float32) + 0.5) / 4.0 - 0.5
    i0 = np.clip(np.floor(gh).astype(np.int32), 0, h - 1)
    i1 = np.clip(i0 + 1, 0, h - 1)
    wh = (gh - np.floor(gh)).reshape(1, 1, -1, 1)
    xh = x[:, :, i0, :] * (1.0 - wh) + x[:, :, i1, :] * wh
    gw = (np.arange(w * 4, dtype=np.float32) + 0.5) / 4.0 - 0.5
    j0 = np.clip(np.floor(gw).astype(np.int32), 0, w - 1)
    j1 = np.clip(j0 + 1, 0, w - 1)
    ww = (gw - np.floor(gw)).reshape(1, 1, 1, -1)
    return xh[:, :, :, j0] * (1.0 - ww) + xh[:, :, :, j1] * ww


def escalar(pixeles, ruta_sr, dispositivo="CPU"):
    """Superresolución x4 (512×512 -> 2048×2048) con Real-ESRGAN Compact (.onnx) u OpenVINO OMZ (.xml)."""
    import numpy as np

    ruta_sr = Path(ruta_sr)
    if ruta_sr.suffix == ".xml":
        # single-image-super-resolution-1032 (Intel Open Model Zoo): recibe [0..255] + base bilineal x4
        import openvino as ov
        x = pixeles.astype(np.float32).transpose(2, 0, 1)[None]
        b = _interpolar_x4(x)
        core = ov.Core()
        red = core.read_model(str(ruta_sr))
        red.reshape({"0": list(x.shape), "1": list(b.shape)})
        salida = core.compile_model(red, dispositivo)({"0": x, "1": b})[0][0]
        return np.clip(salida.transpose(1, 2, 0) * 255.0 + 0.5, 0, 255).astype(np.uint8)

    entrada = (pixeles.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
    try:
        import openvino as ov
        core = ov.Core()
        red = core.read_model(str(ruta_sr))
        red.reshape(list(entrada.shape))
        salida = core.compile_model(red, dispositivo)([entrada])[0][0]
    except ImportError:
        import onnxruntime
        sesion = onnxruntime.InferenceSession(str(ruta_sr), providers=["CPUExecutionProvider"])
        salida = sesion.run(None, {sesion.get_inputs()[0].name: entrada})[0][0]
    return np.clip(salida.transpose(1, 2, 0) * 255.0 + 0.5, 0, 255).astype(np.uint8)


def resolver_modelo_sr(carpeta_modelos, modo="realesrgan"):
    """Devuelve la ruta del modelo de superresolución elegido, o None si está desactivado o no existe."""
    if not modo or modo in ("ninguno", "no", "false"):
        return None
    if modo is True or modo == "auto":
        modo = "realesrgan"
    rel = MODELOS_SR.get(str(modo), str(modo))
    ruta = Path(carpeta_modelos) / rel
    return ruta if ruta.is_file() else None


def main():
    modelo, dispositivo = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "CPU"
    peticion = json.load(sys.stdin)

    import numpy as np

    p_modelo = Path(modelo).resolve()
    carpeta_modelos = p_modelo if (p_modelo.is_dir() and not (p_modelo / "model_index.json").is_file()) else p_modelo.parent

    if peticion.get("entrada"):
        pixeles = leer_imagen(peticion["entrada"])
        modo_sr = peticion.get("escalar") or "realesrgan"
        if modo_sr in ("ninguno", "no", "false"):
            modo_sr = "realesrgan"
        ruta_sr = resolver_modelo_sr(carpeta_modelos, modo_sr)
        if not ruta_sr:
            raise FileNotFoundError(f"No encontré el modelo de superresolución ({modo_sr!r}) en {carpeta_modelos}")
        pixeles = np.ascontiguousarray(escalar(pixeles, ruta_sr, dispositivo))
    else:
        import openvino_genai
        pipeline = openvino_genai.Text2ImagePipeline(modelo, dispositivo)
        imagen = pipeline.generate(
            peticion["prompt"].strip(),
            width=int(peticion.get("ancho", 512)), height=int(peticion.get("alto", 512)),
            num_inference_steps=int(peticion.get("pasos", 4)),
            rng_seed=int(peticion.get("semilla") or random.randint(0, 2**31 - 1)),
        )
        pixeles = np.ascontiguousarray(imagen.data[0], dtype=np.uint8)
        ruta_sr = resolver_modelo_sr(carpeta_modelos, peticion.get("escalar", "realesrgan"))
        if ruta_sr:
            pixeles = np.ascontiguousarray(escalar(pixeles, ruta_sr, dispositivo))

    salida = peticion["salida"]
    guardar_png(pixeles, salida)
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
