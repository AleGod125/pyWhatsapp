# Desarrollo

## Poner en marcha

```bash
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        # y rellenar las credenciales
python -m alembic upgrade head
py service.py --check         # verifica entorno, PostgreSQL y migraciones
```

`.env.example` es una plantilla versionada: **nunca** poner ahí una contraseña
real.

## Ejecutar

```bash
py service.py     # backend, único entrypoint
ng serve          # frontend, en el repo WhatsappBackup
```

Solo un proceso puede tener la sesión abierta. Si arrancas dos, el segundo
avisa de quién la tiene.

## Tests

```bash
pytest                        # backend, contra el PostgreSQL real
```

Corren dentro de una transacción que **siempre** se revierte: se prueba el
motor de verdad (índices parciales, `ON CONFLICT`, JSONB, BYTEA, CHECK) sin
dejar residuos.

En el frontend:

```bash
ng test --watch=false
ng build
```

## Análisis estático

```bash
python -m ruff check app/ tests/ tools/ scripts/ service.py --select F
```

`F` cubre imports muertos, nombres indefinidos y variables sin usar. Encontró
un `NameError` real en la ruta de apagado.

## Convenciones

**Comentarios.** Se comenta lo que no es obvio: reglas de protocolo,
invariantes, comportamiento raro de WhatsApp, y **por qué** una decisión es la
que es. Cuando algo salió de una medición, se dice ("se midió: ..."), porque
distingue un hecho de una suposición. No se comentan cosas que el código ya
dice, ni se deja narrativa histórica ni código comentado.

**Documentación.** Lo que explica un subsistema entero va a `docs/`, no a un
docstring de 60 líneas.

**Pruebas de frontera.** Las reglas de dependencia se comprueban mirando el
AST, no el texto: los comentarios que las explican mencionan justamente lo que
prohíben, y buscar cadenas da falsos positivos.

## Herramientas

```bash
py tools/reset_product_test.py             # simula
py tools/reset_product_test.py --aplicar   # pide escribir BORRAR
```

Deja el sistema como recién instalado para una prueba desde cero. Se niega a
ejecutarse con `service.py` vivo: borrar con el Signal Store abierto se salta
el archivo en silencio.

`scripts/` son diagnósticos puntuales, no parte del arranque.

## Lo que no se toca

- El comportamiento de [ON_DEMAND](ON_DEMAND.md).
- La lógica criptográfica ([SIGNAL_COMPAT.md](SIGNAL_COMPAT.md)).
- `raw_proto`: es lo que permite reinterpretar historial cuando el
  normalizador mejora.
- `site-packages`.
