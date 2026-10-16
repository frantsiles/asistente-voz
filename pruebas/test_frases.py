#!/usr/bin/env python3
"""Pruebas automáticas de interpretación de frases, resolución de apps y tareas."""
import json
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import asistente  # noqa: E402

# Los clientes reales del LLM, antes de que preparar_entorno() los sustituya
LLM_REAL, LLM_STREAM_REAL = asistente.llm, asistente.llm_stream


APPS_BASE = {
    "firefox": asistente.App("firefox", "Firefox", ["firefox"]),
    "spotify": asistente.App("com.spotify.Client", "Spotify", ["spotify"]),
    "dolphin": asistente.App("org.kde.dolphin", "Dolphin", ["dolphin"]),
    "kwrite": asistente.App("org.kde.kwrite", "KWrite", ["kwrite"]),
    "grite": asistente.App("org.kde.kwrite", "KWrite", ["kwrite"]),
    "heroicgameslauncher": asistente.App("com.heroicgameslauncher.hgl", "Heroic Games Launcher", ["heroic"]),
    "heroicgames": asistente.App("com.heroicgameslauncher.hgl", "Heroic Games Launcher", ["heroic"]),
    "legionanimefull": asistente.App("waydroid.aplicaciones.paleta.legionanimefull", "Legión Anime Full", ["waydroid"]),
    "btop": asistente.App("btop", "btop", ["kitty", "-e", "btop"]),
    "pittop": asistente.App("btop", "btop", ["kitty", "-e", "btop"]),
    "htop": asistente.App("htop", "htop", ["kitty", "-e", "htop"]),
}

TAREAS_BASE = {
    "tests de validaciones": "dotnet test ~/Proyectos/validaciones",
    "build backend": "npm run build",
}


def preparar_entorno(usar_llm: bool = False):
    for clave, valor in APPS_BASE.items():
        asistente.APPS.setdefault(clave, valor)
    asistente.TAREAS.update(TAREAS_BASE)
    if not usar_llm:
        asistente.llm = lambda *_a, **_kw: json.dumps({"accion": "no_entendido", "parametro": ""})


def probar_frases(ruta: Path) -> tuple[int, int]:
    casos = json.loads(ruta.read_text())
    ok = 0
    fallos = 0

    for i, caso in enumerate(casos, 1):
        frase = caso["frase"]
        esp_accion = caso["accion"]
        esp_param = caso["param"]

        accion, param = asistente.interpretar(frase)
        errores = []

        if accion != esp_accion or param != esp_param:
            errores.append(
                f"interpretar() -> ({accion!r}, {param!r}), esperaba ({esp_accion!r}, {esp_param!r})"
            )

        if "app_id" in caso:
            resuelto = asistente.resolver_app(param)
            resuelto_id = resuelto.id if resuelto else None
            esperados = caso["app_id"] if isinstance(caso["app_id"], list) else [caso["app_id"]]
            if resuelto_id not in esperados:
                errores.append(
                    f"resolver_app({param!r}) -> {resuelto_id!r}, esperaba {caso['app_id']!r}"
                )

        if "tarea" in caso:
            resuelta = asistente.resolver_tarea(param)
            if resuelta != caso["tarea"]:
                errores.append(
                    f"resolver_tarea({param!r}) -> {resuelta!r}, esperaba {caso['tarea']!r}"
                )

        if "idioma" in caso:
            lang = asistente.idioma(frase)
            if lang != caso["idioma"]:
                errores.append(
                    f"idioma() -> {lang!r}, esperaba {caso['idioma']!r}"
                )

        if errores:
            fallos += 1
            print(f"✗ [{i:02d}] {frase!r}")
            for err in errores:
                print(f"      {err}")
        else:
            ok += 1
            print(f"✓ [{i:02d}] {frase!r} -> ({accion}, {param!r})")

    return ok, fallos


def probar_salvaguardas() -> tuple[int, int]:
    ok = 0
    fallos = 0

    def verificar(nombre: str, condicion: bool, detalle: str = ""):
        nonlocal ok, fallos
        if condicion:
            ok += 1
            print(f"✓ [guarda] {nombre}")
        else:
            fallos += 1
            print(f"✗ [guarda] {nombre}: {detalle}")

    # 1. Segmentos genéricos como "desktop" no deben mapearse a una app arbitraria
    verificar(
        "Segmento genérico 'desktop' no está en APPS",
        "desktop" not in asistente.APPS,
        f"APPS['desktop'] = {asistente.APPS.get('desktop')!r}",
    )

    # 2. 'pon' no debe dar evidencia de abrir_app
    verificar(
        "'Pon algo de música' no tiene evidencia de abrir_app",
        not re.search(asistente.EVIDENCIA["abrir_app"], asistente.normalizar("Pon algo de música")),
    )

    # 3. Si el LLM alucina valores fuera de lista blanca, interpretar() los rechaza
    orig_llm = asistente.llm
    try:
        asistente.llm = lambda *_a, **_kw: json.dumps({"accion": "abrir_app", "parametro": "app_inventada_xyz"})
        acc, prm = asistente.interpretar("Por favor ejecuta app_inventada_xyz")
        verificar(
            "Rechaza abrir_app del LLM si resolver_app() no encuentra la app",
            acc == "no_entendido" and prm == "",
            f"devolvió ({acc!r}, {prm!r})",
        )

        asistente.llm = lambda *_a, **_kw: json.dumps({"accion": "ventana", "parametro": "atajo_inventado"})
        acc, prm = asistente.interpretar("Mueve la ventana de forma inventada")
        verificar(
            "Rechaza ventana del LLM si el atajo no está en ATAJOS_KWIN",
            acc == "no_entendido" and prm == "",
            f"devolvió ({acc!r}, {prm!r})",
        )

        asistente.llm = lambda *_a, **_kw: json.dumps({"accion": "ejecutar_tarea", "parametro": "rm -rf /"})
        acc, prm = asistente.interpretar("Corre la tarea borrar todo")
        verificar(
            "Rechaza ejecutar_tarea del LLM si no está en la lista blanca TAREAS",
            acc == "no_entendido" and prm == "",
            f"devolvió ({acc!r}, {prm!r})",
        )
    finally:
        asistente.llm = orig_llm

    # 4. Silero VAD cargado y distinguiendo silencio
    verificar(
        "Silero VAD cargado y operativo",
        asistente._VAD.session is not None,
    )
    if asistente._VAD.session is not None:
        prob, _ = asistente._VAD.probabilidad(
            asistente.np.zeros(asistente.BLOQUE, dtype=asistente.np.int16),
            asistente._VAD.nuevo_estado(),
        )
        verificar(
            "Silero VAD devuelve probabilidad < 0.05 en silencio",
            prob < 0.05,
            f"prob = {prob:.4f}",
        )

    # 5. Nuevas funciones de sistema, diagnóstico y git responden sin errores
    m_es = asistente.MSG["es"]
    est = asistente.estado_sistema(m_es)
    verificar(
        "estado_sistema() incluye procesador, memoria y red/GPU",
        "grados" in est and "memoria" in est,
        est,
    )
    diag_cpu = asistente.diagnostico_sistema("cpu", m_es)
    verificar(
        "diagnostico_sistema('cpu') lista procesos",
        diag_cpu.startswith("Los procesos que más consumen son:"),
        diag_cpu,
    )
    diag_puerto = asistente.diagnostico_sistema("puerto:59999", m_es)
    verificar(
        "diagnostico_sistema('puerto:59999') responde estado del puerto",
        "59999" in diag_puerto,
        diag_puerto,
    )
    git_est = asistente.git_info_proyecto("estado:asistente-voz", m_es)
    verificar(
        "git_info_proyecto('estado:asistente-voz') reporta la rama actual",
        "asistente-voz" in git_est and "rama" in git_est,
        git_est,
    )
    git_com = asistente.git_info_proyecto("commit:asistente-voz", m_es)
    verificar(
        "git_info_proyecto('commit:asistente-voz') reporta el último commit",
        git_com.startswith("Último commit en asistente-voz:"),
        git_com,
    )

    # 6. «reinicia X» no acepta subcadenas de nombres de contenedor («la rapidez» contiene «api»)
    orig_run = asistente.subprocess.run

    class _Docker:
        stdout, returncode = "api-local\nmongo-local\nredis\n", 0

    asistente.subprocess.run = lambda cmd, **kw: _Docker() if cmd[:2] == ["docker", "ps"] else orig_run(cmd, **kw)
    try:
        acc, prm = asistente.interpretar("Reinicia la rapidez")
        verificar("«Reinicia la rapidez» no reinicia el contenedor api", acc != "docker_reiniciar",
                  f"devolvió ({acc!r}, {prm!r})")
        acc, prm = asistente.interpretar("Reinicia redis")
        verificar("«Reinicia redis» reinicia el contenedor de nombre exacto", (acc, prm) == ("docker_reiniciar", "redis"),
                  f"devolvió ({acc!r}, {prm!r})")
        acc, prm = asistente.interpretar("Reinicia el contenedor mongo")
        verificar("«Reinicia el contenedor mongo» acepta el sufijo -local",
                  (acc, prm) == ("docker_reiniciar", "mongo-local"), f"devolvió ({acc!r}, {prm!r})")
    finally:
        asistente.subprocess.run = orig_run
    verificar("docker_reiniciar pide confirmación por defecto", "docker_reiniciar" in asistente.CONFIRMAR)

    # 7. Temporizador: una duración no numérica se convierte o se rechaza, nunca 60 s por defecto
    orig_timer = asistente.threading.Timer
    asistente.threading.Timer = lambda seg, fn: type("T", (), {"daemon": True, "start": lambda self: None})()
    try:
        r = asistente.programar_temporizador("5 minutos", "es", m_es)
        verificar("programar_temporizador('5 minutos') -> 5 minutos", "5 minutos" in r, r)
        r = asistente.programar_temporizador("pronto", "es", m_es)
        verificar("programar_temporizador('pronto') -> no entendí la duración", r == m_es["timer_no"], r)
    finally:
        asistente.threading.Timer = orig_timer

    # 8. Streaming: ningún WAV se sobrescribe mientras suena, y los errores llegan a quien llama
    import hashlib
    import threading
    import time

    class _VozFalsa:
        def synthesize_wav(self, texto, w):
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(texto.encode() * 50)

    voz = asistente.Voz.__new__(asistente.Voz)
    voz._proc, voz._activa, voz._lock = None, False, threading.Lock()
    voz._interrumpido, voz._cancelar_stream = threading.Event(), threading.Event()
    voz.voces = {"es": _VozFalsa()}
    pisados = []

    def _reproducir_lento(wav):
        h = hashlib.md5(wav.read_bytes()).hexdigest()
        time.sleep(0.05)                      # reproducir es más lento que sintetizar
        if hashlib.md5(wav.read_bytes()).hexdigest() != h:
            pisados.append(wav.name)

    voz._reproducir_wav = _reproducir_lento
    dicho = voz.decir_stream(iter(["Uno.", "", "Dos.", "", "", "Tres.", "Cuatro.", "Cinco.", "Seis."]), "es")
    verificar("decir_stream no pisa el WAV que está sonando", not pisados, f"pisados: {pisados}")
    verificar("decir_stream devuelve todas las oraciones", dicho == "Uno. Dos. Tres. Cuatro. Cinco. Seis.", dicho)

    def _ollama_caido():
        raise ConnectionRefusedError("Ollama caído")
        yield  # noqa: unreachable — lo convierte en generador

    try:
        voz.decir_stream(_ollama_caido(), "es")
        verificar("decir_stream relanza el error de Ollama", False, "no lanzó excepción")
    except ConnectionRefusedError:
        verificar("decir_stream relanza el error de Ollama", True)

    # 9. Interrumpir la voz con el atajo vuelve a escuchar (no se pierde la pulsación)
    a = asistente.Asistente.__new__(asistente.Asistente)
    a.voz, a.escuchando, a.reescuchar = voz, False, False
    a.activar, a.detener = threading.Event(), threading.Event()
    voz._activa = True
    a._senal()
    voz._activa = False
    verificar("El atajo durante la voz la corta y marca reescuchar", a.reescuchar and voz._cancelar_stream.is_set())

    # 9b. El atajo nunca se pierde en silencio: mientras procesa, se encola y avisa
    a.ocupado, a.encolado, a.reescuchar = threading.Lock(), False, False
    avisos = []
    a._avisar_ocupado = lambda: avisos.append(1)
    a._senal()
    verificar("El atajo en reposo activa la escucha", a.activar.is_set() and not a.encolado)
    a.activar.clear()
    with a.ocupado:
        a._senal()
        a._senal()
    time.sleep(0.05)
    verificar("El atajo mientras procesa se encola, no activa ni se pierde",
              a.encolado and not a.activar.is_set(), f"encolado={a.encolado}")
    verificar("El atajo mientras procesa avisa una sola vez", len(avisos) == 1, f"avisos={len(avisos)}")
    a.encolado = False
    with a.ocupado:
        a.atender()
    verificar("atender() con la palabra de activación ocupada encola la pulsación", a.encolado)

    # 9c. Proveedores de LLM: la API nativa de Ollama y la compatible con OpenAI, contra un servidor falso
    import http.server

    peticiones = []

    class ServidorFalso(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            cuerpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            peticiones.append((self.path, cuerpo, self.headers.get("Authorization")))
            ollama = self.path == "/api/chat"
            if cuerpo["stream"]:
                trozos = ["Hola", " mundo.", " Adiós."]
                if ollama:
                    lineas = [json.dumps({"message": {"content": t}, "done": False}) for t in trozos]
                    lineas.append(json.dumps({"message": {"content": ""}, "done": True}))
                    datos = "\n".join(lineas) + "\n"
                else:
                    datos = "".join(f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}\n\n" for t in trozos)
                    datos += "data: [DONE]\n\n"
            else:
                contenido = json.dumps({"accion": "abrir_app", "parametro": "firefox"})
                datos = json.dumps({"message": {"content": contenido}} if ollama else
                                   {"choices": [{"message": {"content": contenido}}]})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(datos.encode())

    servidor = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ServidorFalso)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{servidor.server_port}"
    previo = (asistente.LLM, asistente.LLM_URL, asistente.LLM_CLAVE)
    try:
        for proveedor, url, ruta in (("ollama", base, "/api/chat"), ("openai", base + "/v1", "/v1/chat/completions")):
            asistente.LLM, asistente.LLM_URL, asistente.LLM_CLAVE = proveedor, url, "clave-xyz"
            peticiones.clear()
            r = json.loads(LLM_REAL("modelo-x", "sistema", "abre firefox", asistente.ESQUEMA))
            ruta_ok, cuerpo, auth = peticiones[0]
            esquema_ok = ("format" in cuerpo) if proveedor == "ollama" else \
                cuerpo.get("response_format", {}).get("type") == "json_schema"
            verificar(f"LLM {proveedor}: petición, esquema JSON y respuesta",
                      ruta_ok == ruta and esquema_ok and auth == "Bearer clave-xyz" and r["parametro"] == "firefox",
                      f"{ruta_ok} {cuerpo} {auth} {r}")
            oraciones = list(LLM_STREAM_REAL("modelo-x", "sistema", "hola"))
            verificar(f"LLM {proveedor}: streaming por oraciones", oraciones == ["Hola mundo.", "Adiós."], oraciones)

        # Sin LLM: las frases que necesitan el modelo no se adivinan y las preguntas avisan, sin tocar la red
        asistente.LLM = "ninguno"
        peticiones.clear()
        anterior, asistente.llm = asistente.llm, LLM_REAL
        try:
            verificar("llm = ninguno: frase ambigua -> no_entendido sin red",
                      asistente.interpretar("pon algo tranquilo para concentrarme") == ("no_entendido", "") and not peticiones)
            respuesta = asistente.ejecutar("pregunta", "qué es docker", "es")
            verificar("llm = ninguno: no hay pregunta posible", False, f"respondió {respuesta!r}")
        except asistente.SinLLM:
            verificar("llm = ninguno: una pregunta lanza SinLLM (se avisa al usuario)", not peticiones)
        finally:
            asistente.llm = anterior
    finally:
        asistente.LLM, asistente.LLM_URL, asistente.LLM_CLAVE = previo
        servidor.shutdown()

    # 10. No leer contraseñas del portapapeles en voz alta
    for texto, esperado in (("Xk9#mP2$vLq", True), ("ghp_1234567890abcdefXYZ", True), ("hola mundo", False),
                            ("https://github.com/frantsiles/asistente-voz/pull/12", False), ("Firefox", False)):
        verificar(f"parece_secreto({texto!r}) = {esperado}", asistente.parece_secreto(texto) == esperado)

    return ok, fallos


def main() -> int:
    usar_llm = "--con-llm" in sys.argv
    preparar_entorno(usar_llm=usar_llm)

    ruta_frases = Path(__file__).resolve().parent / "frases.json"
    ok1, f1 = probar_frases(ruta_frases)
    ok2, f2 = probar_salvaguardas()

    total_ok = ok1 + ok2
    total_fallos = f1 + f2
    print(f"\nResultado: {total_ok} correctas, {total_fallos} fallos.")
    return 0 if total_fallos == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
