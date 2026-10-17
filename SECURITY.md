# Seguridad

El asistente escucha el micrófono, ejecuta acciones en tu escritorio y le pasa texto a un modelo de lenguaje. Este documento explica qué puede hacer, qué lo protege y cómo informar de un problema.

## Cómo informar de un problema

**No abras un issue público** si el problema permite ejecutar algo que no debería. Usa el aviso privado de GitHub: pestaña **Security → Report a vulnerability** del repositorio. Indica la frase o el texto que lo provoca, qué pasó y qué esperabas. Lo reviso en cuanto pueda; si se confirma, se corrige, se añade una prueba y se menciona en el `CHANGELOG.md`.

Para fallos que no tienen que ver con la seguridad (no entiende una frase, abre la app equivocada), un issue normal está bien.

## Modelo de amenazas

Corre como **tu usuario**, sin `sudo`, en tu sesión gráfica. Puede hacer lo mismo que tú desde el escritorio, así que la pregunta no es qué permisos tiene, sino **quién decide qué hace**. Las entradas que no controlas son:

| Entrada | Riesgo | Qué lo impide |
|---|---|---|
| Lo que oye el micrófono (reuniones, vídeos, otra persona) | Que una frase ajena dispare una acción | Solo escucha al pulsar el atajo (la palabra de activación es opcional y está desactivada). Las acciones con `riesgo` exigen palabras que las respalden; las que no se pueden deshacer piden confirmación |
| La transcripción de Whisper | Texto que empiece por `-` y se tome como opción de un comando | Nunca pasa por un shell. `notify-send` y `busctl` reciben `--` antes del texto, `kdeconnect-cli` lo recibe como `--share-text=…` y a las búsquedas se les quitan los guiones iniciales |
| La respuesta del LLM | Que «invente» un comando o una ruta | Solo elige un nombre de la lista blanca de acciones (esquema JSON). Sus parámetros se validan contra lo que existe: apps instaladas, proyectos, contenedores, tareas, atajos de KWin |
| El portapapeles (resumir, traducir, explicar) | Texto copiado con instrucciones («ignora lo anterior y…») | Lo que responde el LLM solo se lee en voz alta o se copia al portapapeles: nunca se interpreta como orden |
| La petición de una imagen | Inyección en la línea de comandos de Fooocus u OpenVINO | El texto va por stdin en JSON, nunca como argumento |

**Fuera del alcance**: alguien que ya puede ejecutar código como tu usuario (podría editar `config.json` o el propio `asistente.py`), y los modelos o paquetes comprometidos en su origen, más allá de lo que fijan los hashes y commits de abajo.

## Capas de protección

1. **Lista blanca de acciones.** Nada de comandos arbitrarios: cada acción hace una cosa concreta. El LLM elige una acción y un parámetro; nunca escribe un comando.
2. **Evidencia** para las acciones con `riesgo` (bloquear, cerrar, volumen, Docker, imagen, redactar…): lo que propongan el clasificador o el LLM solo se acepta si la frase contiene palabras que lo respalden.
3. **Confirmación por voz** para lo que no se puede deshacer (`confirmar` en `config.json`; por defecto detener y reiniciar contenedores). Suspender, reiniciar y apagar el equipo se confirman siempre.
4. **Sin shell con texto dictado.** Los comandos son listas de argumentos. Los únicos `bash -c` llevan rutas con `shlex.quote()` o comandos que escribiste tú en `config.json` (`tareas`, `comando_actualizar`, `comando_limpiar`), en una terminal visible.
5. **Contraseñas fuera.** `sudo` pide la contraseña en una terminal visible; el asistente nunca la ve. «Lee lo que copié» no lee nada que parezca una contraseña o un token (`parece_secreto()`). La clave de un LLM remoto se lee de una variable de entorno, nunca de `config.json`, y si viajaría sin cifrar (`http://` fuera del equipo) se avisa al arrancar.
6. **Todo local.** Whisper, el LLM, Piper, el clasificador y el generador de imágenes (Fooocus u OpenVINO) corren en tu equipo. Si `llm_url` apunta fuera, se avisa al arrancar.
7. **Archivos privados.** Los temporales (WAV, script de KWin) van en `XDG_RUNTIME_DIR`, que solo es tuyo; sin esa variable, en una carpeta nueva creada con `mkdtemp` (0700), nunca en `/tmp` a secas. Las grabaciones de tu voz (las 10 últimas, para diagnosticar) quedan en `grabaciones/` con permisos 0700 y nunca se suben al repositorio.
8. **Dependencias fijadas.** `instalar.sh` instala desde `requirements-*.lock` con `pip --require-hashes`: si un paquete de PyPI cambia, no se instala. Los modelos de Hugging Face se descargan de un commit fijo y Silero VAD se comprueba con su SHA-256.
9. **Repositorio protegido.** `main` no admite force-push ni borrado, el escaneo de secretos bloquea los pushes con tokens, Dependabot avisa de dependencias vulnerables y cada push pasa las pruebas en GitHub Actions (acciones fijadas por commit, sin credenciales).

Las pruebas de `pruebas/test_frases.py` cubren estas salvaguardas: acciones sin evidencia, apps que no se mencionan, textos que empiezan por `-`, el portapapeles con instrucciones, los temporales y los permisos de las grabaciones.

## Si cambias el código

Sigue las reglas de [docs/CONTRIBUIR.md](docs/CONTRIBUIR.md#reglas-que-no-se-rompen). Al revisar un pull request, mira con más cuidado los cambios en `asistente.py`, `instalar.sh`, `herramientas/`, `requirements*` y `.github/`: es lo que se ejecuta en tu equipo o en la integración continua.
