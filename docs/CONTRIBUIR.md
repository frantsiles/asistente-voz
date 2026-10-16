# Cómo añadir o cambiar acciones

Guía paso a paso para extender el asistente sin romper lo que ya funciona. Para entender el flujo completo, lee antes [ARQUITECTURA.md](ARQUITECTURA.md).

## Reglas que no se rompen

- **Nada de comandos arbitrarios.** Una acción hace una cosa concreta con parámetros validados. El LLM nunca escribe comandos ni rutas que se ejecuten.
- **Toda acción que pueda elegir el LLM lleva `evidencia`**: una regex que la frase tiene que cumplir.
- **Los parámetros se validan contra lo que existe** (apps, proyectos, contenedores, atajos, tareas) antes de ejecutar.
- **Lo que no se puede deshacer pide confirmación** (lista `CONFIRMAR`).
- **Sin `shell=True` con texto dictado.** Los comandos son listas de argumentos; si hace falta `bash -c`, las rutas van con `shlex.quote()`.

## Ejemplo: «Captura la pantalla»

### 1. El ejecutor

Una función que recibe `(param, lang, m)` y devuelve el texto que se dirá ("" = solo el sonido de éxito). `m` son los textos del idioma detectado.

```python
def _ej_captura(param, _lang, m):
    if not shutil.which("spectacle"):
        return m["captura_no"]
    modo = "-a" if param == "ventana" else "-f"      # ventana activa o pantalla completa
    lanzar(["spectacle", "-b", "-n", modo])
    return m["captura_ok"]
```

Ponla junto a los demás `_ej_*` en `asistente.py`. El ejemplo de esta guía está probado: con estas reglas, las tres frases del paso 6 dan el resultado esperado.

### 2. Los textos, en los dos idiomas

En `MSG["es"]` y `MSG["en"]`, con las mismas claves:

```python
"captura_ok": "Captura guardada en Imágenes.", "captura_no": "Spectacle no está instalado.",
```

```python
"captura_ok": "Screenshot saved to Pictures.", "captura_no": "Spectacle is not installed.",
```

### 3. La definición en `REGISTRO_ACCIONES`

```python
DefinicionAccion(
    nombre="captura",
    prompt="hacer una captura de pantalla (screenshot). parametro = pantalla | ventana",
    evidencia=r"captura|pantallazo|screenshot|screen\s*shot",
    reglas=(
        (r"^(?!\W*(que|what|como|how|por que|why)\b).*"
         r"(\b(captura|pantallazo)\b.*\bventana\b|\bscreenshot of (the|this) window\b)", "ventana"),
        (r"^(?!\W*(que|what|como|how|por que|why)\b).*\b(captura|pantallazo|take a screenshot|screenshot)\b",
         "pantalla"),
    ),
    ejecutor=_ej_captura,
),
```

| Campo | Para qué sirve |
|---|---|
| `nombre` | Identificador. Aparece en el esquema JSON del LLM, en `CONFIRMAR` y en las pruebas |
| `prompt` | Una línea para `PROMPT_COMANDOS`: qué hace y qué valores admite `parametro`. Los valores fijos van en español |
| `evidencia` | Regex sobre la frase **normalizada** (minúsculas, sin tildes). Solo se exige si `riesgo=True` |
| `riesgo` | `True` si ejecutarla por error molesta (bloquear, cerrar, detener, volumen…): el clasificador y el LLM solo la eligen si la frase cumple `evidencia` |
| `ejemplos` | `(frase, parámetro)` para el clasificador por embeddings, en español e inglés y con distintas formas de decirlo. `PARAM_APP` = buscar la app en la frase. Sin ejemplos, la acción solo llega por reglas o por el LLM |
| `reglas` | `(regex, parámetro)` que resuelven la frase sin pasar por el LLM. `None` como parámetro = la frase completa |
| `ejecutor` | La función del paso 1 |

**Cuidado con las preguntas.** La regla de `pregunta` está al final del registro, así que tu regla gana antes: sin el `^(?!\W*(que|what|como|how|…)\b)` del inicio, «¿Qué es una captura de pantalla?» sacaría una captura en vez de responder. Lo mismo hace `actualizar_sistema` con «¿Cómo actualizo…?».

**El orden importa.** Las `reglas` de todas las acciones se prueban en el orden del registro y gana la primera que coincide. Pon las reglas específicas antes que las generales (como «captura… ventana» antes que «captura»). Si tu regla puede chocar con la de otra acción, colócala antes o después a propósito y añade una prueba de cada caso.

Las regex se aplican al texto normalizado: escribe `cancion`, no `canción`.

### 4. Validar el parámetro que venga del LLM

Si el parámetro tiene que estar en una lista, compruébalo al final de `interpretar()`, junto a las demás validaciones:

```python
if accion == "captura" and param not in ("pantalla", "ventana"):
    param = "pantalla"
```

Si la frase necesita extraer partes que una regex con parámetro fijo no puede devolver (un número, un nombre), añade una regla especial en `interpretar()`, antes del bucle `for patron, accion, param in REGLAS`, como las de temporizador o puerto.

### 5. ¿Pide confirmación?

Si la acción no se puede deshacer (apagar, borrar, detener servicios), añádela a la lista por defecto de `CONFIRMAR` y escribe la pregunta en los dos idiomas con la clave `confirmar_<nombre>`:

```python
"confirmar_apagar": "¿Confirmas apagar el equipo?",
```

### 6. Las pruebas

En `pruebas/frases.json`, al menos una frase en español, una en inglés y una que **no** debe activar la acción:

```json
{"frase": "Haz una captura de la ventana", "accion": "captura", "param": "ventana", "idioma": "es"},
{"frase": "Take a screenshot", "accion": "captura", "param": "pantalla", "idioma": "en"},
{"frase": "¿Qué es una captura de pantalla?", "accion": "pregunta", "param": "¿Qué es una captura de pantalla?"}
```

Campos opcionales: `idioma`, `app_id` (id o lista de ids que debe resolver `resolver_app`) y `tarea`.

Para comprobar el comportamiento de un ejecutor o una salvaguarda (no solo la interpretación), añade un `verificar(...)` en `probar_salvaguardas()` de `pruebas/test_frases.py`. Sustituye lo que tenga efectos (`subprocess.run`, `threading.Timer`, `ollama`) por una versión falsa y restaura el original en un `finally`.

Ejecuta las dos variantes:

```bash
venv/bin/python pruebas/test_frases.py            # reglas y salvaguardas, sin LLM (rápido)
venv/bin/python pruebas/test_frases.py --con-llm  # también por el LLM configurado: el LLM real no debe saltarse la evidencia
```

Las dos tienen que terminar en `0 fallos`.

### 7. Probarlo de verdad

```bash
systemctl --user restart asistente-voz
journalctl --user -u asistente-voz -f
```

Cada comando aparece como `«texto» [idioma] -> accion(parámetro)`. Si Whisper transcribe tu frase de otra forma, copia esa transcripción a `frases.json`: las pruebas con errores reales son las más útiles.

### 8. Documentarlo

- Una fila en la tabla de **Comandos** del `README.md`.
- Si añadiste una clave de configuración: en `CLAVES_CFG`, en `config.example.json` y en la tabla de **Configuración** del README.
- Una línea en `CHANGELOG.md`, sección «Sin publicar».

## Otros cambios frecuentes

| Quiero… | Dónde |
|---|---|
| Que reconozca otro nombre para una app | `ALIAS_APPS`, o `alias_apps` en tu `config.json` si es solo tuyo |
| Corregir una palabra que Whisper oye mal | `ABRE_MAL_OIDO` / `CIERRA_MAL_OIDO`, y añadir la frase a `frases.json` |
| Que Whisper conozca un término | `pista_whisper()`: solo términos, nunca frases |
| Que el idioma se detecte mejor | `MARCAS_ES` / `MARCAS_EN` |
| Una herramienta de consola nueva | `apps_terminal` en `config.json` |
| Un comando de desarrollo por voz | `tareas` en `config.json`: no hace falta tocar código |

## Flujo de git

`main` está protegida en GitHub: se puede hacer push directo, pero **no se permite `git push --force` ni borrar la rama**. Si un push se rechaza porque hay commits nuevos en GitHub, integra los cambios en vez de forzar:

```bash
git pull --rebase
venv/bin/python pruebas/test_frases.py
git push
```

El escaneo de secretos bloquea los pushes que contengan tokens o contraseñas. Si te bloquea, quita el secreto del commit (`git commit --amend` si es el último) en vez de saltarte la protección, y revoca ese token.
