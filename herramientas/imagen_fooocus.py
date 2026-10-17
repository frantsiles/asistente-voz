"""Genera una imagen con el motor de Fooocus, sin abrir su interfaz web.

Lo lanza el asistente con el Python del entorno de Fooocus (torch y sus modelos no se instalan en el
del asistente):

    ~/Fooocus/venv/bin/python -I herramientas/imagen_fooocus.py ~/Fooocus < peticion.json

La petición llega por stdin para que el texto dictado nunca pase por la línea de comandos:
    {"prompt": "...", "salida": "/ruta/imagen.png", "ancho": 1024, "alto": 1024, "pasos": 30, "semilla": 123}
Escribe el PNG en `salida` y, como última línea, {"ok": true, "ruta": "..."} o {"ok": false, "error": "..."}.

Usa el modelo base, el sampler y la expansión de prompt («Fooocus V2») por defecto de Fooocus. Su gestor
de memoria carga y descarga los modelos según la VRAM libre, así que funciona con 4-6 GB.
"""
import json
import os
import random
import sys

NEGATIVO = ("(worst quality, low quality, lowres:1.4), watermark, signature, text, logo, blurry, jpeg artifacts, "
            "deformed, mutated, disfigured, bad anatomy, bad hands, extra fingers, cropped, out of frame")


def main():
    fooocus = os.path.abspath(os.path.expanduser(sys.argv[1]))
    peticion = json.load(sys.stdin)
    # Fooocus lee su config.txt y sus modelos relativos a su carpeta, y analiza sys.argv al importarse
    os.chdir(fooocus)
    sys.path.insert(0, fooocus)
    sys.argv = [sys.argv[0], "--disable-image-log", "--disable-analytics"]

    import numpy as np
    from PIL import Image
    import modules.async_worker  # noqa: F401  (aplica los parches de Fooocus a ldm_patched)
    import modules.patch
    import modules.default_pipeline as pipeline   # carga el modelo base por defecto

    semilla = int(peticion.get("semilla") or random.randint(0, 2**31 - 1))
    pasos = int(peticion.get("pasos", 30))
    modules.patch.patch_settings[os.getpid()] = modules.patch.PatchSettings()   # nitidez, ADM… por defecto

    prompt = peticion["prompt"].strip()
    expansion = pipeline.final_expansion(prompt, semilla)
    positivo = pipeline.clip_encode(texts=[prompt, expansion], pool_top_k=1)
    negativo = pipeline.clip_encode(texts=[peticion.get("negativo") or NEGATIVO], pool_top_k=1)

    imagenes = pipeline.process_diffusion(
        positive_cond=positivo, negative_cond=negativo, steps=pasos, switch=pasos,
        width=int(peticion.get("ancho", 1024)), height=int(peticion.get("alto", 1024)), image_seed=semilla,
        callback=None, sampler_name="dpmpp_2m_sde_gpu", scheduler_name="karras", cfg_scale=4.0,
        disable_preview=True,
    )
    salida = peticion["salida"]
    Image.fromarray(np.asarray(imagenes[0])).save(salida)
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
