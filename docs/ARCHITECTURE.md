# Arquitectura

Un solo proceso, un solo entrypoint: `py service.py`.

```
service.py
  │  carga config, verifica PostgreSQL y migraciones
  │  toma session/runtime.lock
  ▼
AppRuntime  (app/core/runtime.py)         coordina, no implementa
  ├── Database            PostgreSQL vía SQLAlchemy
  ├── WhatsAppClient      pywhats en su propio hilo + event loop
  ├── Orchestrator        post-connect: historial → backfill → media → live
  ├── EventBus            eventos internos → SSE
  └── servicios           ingesta, backfill, multimedia, mantenimiento
       ▲
Flask (app/api)  ──────► Angular (repo WhatsappBackup, :4200)
```

## Capas y su regla

| Capa | Responsabilidad | No puede |
|---|---|---|
| `app/api` | HTTP, serialización, SSE | importar capas de interfaz ni `app.experimental` en cabecera |
| `app/core` | runtime, estado, config, cerrojo, identidad | importar Flask ni `app.experimental` |
| `app/services` | ingesta, backfill, multimedia, repositorio | importar Flask ni `app.experimental` |
| `app/compat` | parches sobre pywhats 0.2.0 | modificar `site-packages` |
| `app/experimental` | fuera del producto, tras flag | ser importado por nadie del producto |

Cada regla tiene una prueba que mira el AST, no el texto: los comentarios que
las explican mencionan justamente lo que prohíben.

## Dos almacenes distintos

**PostgreSQL** es la copia: chats, mensajes, multimedia, cursores. Sobrevive a
desvincular el teléfono.

**`session/`** es el estado del companion: `device.json` (identidad) y
`device.json.signal.db` (estado Signal). Van **siempre juntos** — ver
[SESSION_LIFECYCLE.md](SESSION_LIFECYCLE.md).

Borrar `session/` no pierde ni un mensaje. Borrar PostgreSQL sí.

## Solo lectura

El backup no envía mensajes, no marca nada como leído y no altera ningún chat
en el teléfono. Es un dispositivo vinculado que escucha y persiste.
