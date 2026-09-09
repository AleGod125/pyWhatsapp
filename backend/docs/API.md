# API

Base: `http://127.0.0.1:5000/api/v1`. CORS abierto solo a `FRONTEND_ORIGIN`
(`http://localhost:4200` por defecto).

## Producto

| Método | Ruta | Para qué |
|---|---|---|
| GET | `/health` | Estado del servicio y modo (`whatsapp_enabled`). |
| GET | `/session` | Estado de la sesión + `pairing_phase`. Ver [SESSION_LIFECYCLE](SESSION_LIFECYCLE.md). |
| POST | `/session/pair` | Reintento manual. Idempotente: si ya hay vinculación en curso devuelve **esa**. |
| GET | `/session/qr` | Metadatos del QR. **Nunca el payload.** |
| GET | `/session/qr/image` | PNG. `no-store`. 404 si no hay, 410 si expiró. |
| GET | `/chats` | Sidebar, ordenado por último mensaje. |
| GET | `/chats/<id>` | Detalle + estado histórico. |
| GET | `/chats/<id>/messages` | Paginación por keyset (`before_*` / `after_*`). |
| GET | `/events/stream` | SSE. Una sola conexión. |
| GET | `/sync/status` | Conexión, historial, multimedia, backfill, desglose por estado. |
| POST | `/sync/run` | Ciclo manual. Single-flight: 409 con el trabajo activo. |
| POST | `/history/recheck-pending` | Revisa los pendientes. `?auto=1` respeta la espera. |
| GET | `/history/recheck-pending/status/<job_id>` | Progreso (respaldo de SSE). |
| POST | `/chats/<id>/history/recheck` | Lo mismo para un chat. Síncrono. |
| GET | `/media/<id>` · `/file` · `/thumbnail` | Multimedia. `file` soporta Range/206. |
| POST | `/media/<id>/retry` | Reintenta una descarga. |
| POST | `/messages/<id>/media/recover` | Recupera el multimedia de un mensaje. |

## Experimental — apagado

`/history/web-bootstrap/*` (5 rutas) responden **404 `WEB_BOOTSTRAP_DISABLED`**
salvo con `WEB_BOOTSTRAP_ENABLED=true`.

Siguen registradas a propósito: una ruta inexistente devuelve 404 **sin
cabeceras CORS**, y el navegador lo reporta como error de CORS, que manda a
diagnosticar el sitio equivocado. Registrada, el frontend recibe un motivo
legible.

## SSE

```
session.state · session.qr
chat.updated · message.created · message.updated · media.updated
history.progress · backfill.progress · sync.status
history.recheck.started|progress|completed
history.backfill.started|completed
```

Los nombres se traducen en un solo sitio: `EVENT_NAMES` en `app/api/routes.py`.
Los eventos internos que no estén ahí no salen al frontend.

## Errores

`{"error": {"code": "...", "message": "..."}}` más campos de contexto.

| Código | HTTP |
|---|---|
| `SYNC_ALREADY_RUNNING` | 409 (+ `sync`) |
| `RECHECK_BUSY` | 409 (+ `job`) |
| `WEB_BOOTSTRAP_DISABLED` | 404 |
| `SESSION_ALREADY_CONNECTED` | 409 |
| `WHATSAPP_DISABLED` | 409 |
| `QR_NOT_AVAILABLE` / `QR_EXPIRED` | 404 / 410 |

## Nunca sale por la API

Payload del QR, claves privadas, estado Signal, `DATABASE_URL`, tokens, JIDs
completos (van enmascarados).
