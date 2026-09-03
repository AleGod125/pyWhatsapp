# Historiales pendientes — contrato de API

Base: `http://127.0.0.1:5000/api/v1`. Frontend: `WhatsappBackup` (Angular, `http://localhost:4200`).

> **Cambio importante.** La recuperación con sesión auxiliar (Web Bootstrap /
> Baileys) está **apagada** por defecto. El producto usa un solo QR y una sola
> sesión. Los endpoints `/history/web-bootstrap/*` siguen registrados pero
> devuelven `404 WEB_BOOTSTRAP_DISABLED`.

---

## 1. Qué es un chat pendiente

Algunas conversaciones llegaron del emparejamiento como **pura metadata**:
nombre, identificador, contadores… y ni un solo identificador de mensaje.
`HISTORY_SYNC_ON_DEMAND` va **anclado por definición** —hay que decirle *desde
qué mensaje* seguir hacia atrás—, así que sin esa referencia no se puede pedir
nada. Esos chats quedan en `waiting_seed`.

`waiting_seed` **NO significa "chat vacío"** y **no es un error**. Significa:
*WhatsApp todavía no ha entregado un punto desde el que pedir el historial*. Es
reintentable, y el chat **despierta solo** si llega un mensaje real.

Texto recomendado para la UI:

> WhatsApp todavía no proporcionó una referencia válida para recuperar mensajes
> anteriores.

En ámbar o neutro. **Nunca en rojo.**

---

## 2. Un solo QR

El único código que se muestra es el de la sesión principal:

```
GET /api/v1/session/qr          → { available, generation, ... }
GET /api/v1/session/qr/image    → image/png
```

No hay QR auxiliar en el flujo normal. Nada arranca Baileys al iniciar — hay
un test que lo comprueba leyendo `runtime.py`, `orchestrator.py` y `service.py`.

---

## 3. Los dos botones

### Global — "Revisar historiales pendientes"

```
POST /api/v1/history/recheck-pending
```

Sin cuerpo. Responde **`202`** enseguida:

```json
{
  "job_id": "a3f81c92",
  "state": "starting",
  "total": 29,
  "processed": 0,
  "recovered": 0,
  "still_waiting": 0,
  "errors": 0,
  "messages_recovered": 0,
  "current_chat": null,
  "chats": [{ "id": 13, "name": "ubernel", "state": "waiting_seed" }],
  "error": null,
  "elapsed_seconds": 0
}
```

### Por conversación — "Volver a comprobar"

```
POST /api/v1/chats/<chat_id>/history/recheck
```

Este es **síncrono** (es un solo chat) y devuelve otra forma:

```json
{
  "chat_jid": "...",
  "previous_status": "waiting_seed",
  "status": "waiting_seed",
  "aliases": ["...", "..."],
  "blobs_reviewed": 12,
  "messages_recovered": 0,
  "seed_found": false,
  "can_dig": false,
  "history": { }
}
```

`normalizeChatRecheck()` en el frontend lo traduce a la misma forma de trabajo
para que el panel sea uno solo.

### Qué hacen exactamente

Todo local, sin salir a la red:

1. resuelven los alias del contacto (el ancla puede estar guardada bajo su
   *otro* identificador — teléfono vs LID);
2. buscan un mensaje con ID real de WhatsApp entre ellos;
3. si no lo hay, **reinterpretan los blobs de historial ya guardados en disco**
   (el normalizador ha mejorado desde que se guardaron);
4. si aparece un ancla, el chat vuelve a `pending` y se encola para que pywhats
   pida su historial con ON_DEMAND.

Si no aparece nada, el chat **se queda en `waiting_seed`**. No es un error.

---

## 4. Progreso

### SSE — el mecanismo principal

```
GET /api/v1/events/stream        (text/event-stream)
```

| Evento | Cuándo | Datos |
|---|---|---|
| `history.recheck.started` | arranca | el trabajo entero |
| `history.recheck.progress` | cada chat | el trabajo entero |
| `history.recheck.completed` | termina | el trabajo entero |
| `history.backfill.started` | lo despertado entra en la cola | `{job_id, chats}` |
| `history.backfill.completed` | el backfill automático terminó | el resumen medido |

Los tres `recheck.*` traen el trabajo completo: puedes redibujar el panel con
cada uno sin llevar contabilidad propia.

Se conservan los de siempre: `session.state`, `session.qr`, `chat.updated`,
`message.created`, `message.updated`, `media.updated`, `sync.status`.

### Sondeo — respaldo

```
GET /api/v1/history/recheck-pending/status/<job_id>
```

---

## 5. Estados

### Del trabajo — `state`

| Valor | UI |
|---|---|
| `starting` | spinner |
| `running` | barra `processed / total` |
| `completed` | resumen |
| `failed` | mensaje de `error` |

### De cada chat — `chats[].state`, `current_chat.state`

| Valor | Significado | Color |
|---|---|---|
| `waiting_seed` | en cola, o sigue sin ancla | neutro / ámbar |
| `rechecking` | revisándose ahora | spinner |
| `seed_found` | apareció un ancla | verde |
| `fetching_history` | ya está descargando | verde + progreso |
| `error` | falló de verdad | rojo |

Los tres que el usuario debe distinguir de un vistazo: **recuperado**,
**sigue pendiente**, **error**.

---

## 6. Errores

`{"error": {"code": "...", "message": "..."}}` más campos extra.

| Código | HTTP | Qué hacer |
|---|---|---|
| `RECHECK_BUSY` | 409 | Ya hay una en marcha. Trae `job`: engánchate a ese. |
| `WEB_BOOTSTRAP_DISABLED` | 404 | Estás llamando a la ruta apartada. Usa `/history/recheck-pending`. |
| — | 503 | La base de datos no está disponible. |
| — | 404 | `job_id` o `chat_id` inexistente. |

---

## 7. Flujo de arranque desde cero

```
py service.py
    ↓  PostgreSQL, cerrojo de sesión, comprobar device.json
    ↓  sin sesión → PAIRING, se genera QR
ng serve  +  http://localhost:4200
    ↓  "No hay una cuenta de WhatsApp vinculada"  →  [Conectar WhatsApp]
    ↓  se escanea UN código
    ↓  "Conectando…"          session.state = CONNECTING
    ↓  "Sincronizando…"       history.progress
    ↓  "Recuperando historial…"  backfill automático (ON_DEMAND, count=50)
    ↓  media worker + live
    ↓  history.backfill.completed  +  [PRODUCT_TEST] en el log
```

Al terminar el backfill el backend registra el recuento medido:

```
[PRODUCT_TEST] RESULTADO FINAL
[PRODUCT_TEST]   chats_total=40
[PRODUCT_TEST]   messages_total=3617
[PRODUCT_TEST]   media_total=607 (descargados=...)
[PRODUCT_TEST]   exhausted=9 waiting_seed=29 timeout=1 errors=0
```

El mismo objeto viaja en `history.backfill.completed`, así que el frontend
puede mostrarlo sin volver a preguntar.

---

## 8. Despertar automático

Se conserva y **no requiere ningún botón**: si un chat en `waiting_seed` recibe
o envía un mensaje real, ese mensaje sirve de ancla, el chat pasa a `pending` y
se encola solo para ON_DEMAND.

---

## 9. Endpoints del producto normal

| Endpoint | Para qué |
|---|---|
| `GET /health` | modo del backend (`whatsapp_enabled`) |
| `GET /session` · `GET /session/qr` · `GET /session/qr/image` | la única vinculación |
| `GET /chats` · `GET /chats/<id>` · `GET /chats/<id>/messages` | datos |
| `GET /sync/status` · `POST /sync/run` | sincronización |
| `GET /events/stream` | SSE |
| `POST /history/recheck-pending` | botón global |
| `GET /history/recheck-pending/status/<job_id>` | sondeo |
| `POST /chats/<id>/history/recheck` | botón por chat |
| `POST /media/<id>/retry` · `POST /messages/<id>/media/recover` | multimedia |

**No usar desde el frontend normal:** `/history/web-bootstrap/*`.

---

## 10. Reactivar la sesión auxiliar (futuro)

En `.env` del backend:

```
WEB_BOOTSTRAP_ENABLED=true
```

Entonces vuelven a responder los seis endpoints `/history/web-bootstrap/*` y
los eventos `history.recovery.*` / `history.seed.*`. En el frontend siguen
`WebBootstrapService` y el panel `recovery/`, sin usar.

Antes de encenderla conviene saber lo que ya se midió: las fuentes nativas se
agotaron (bootstrap, blobs, PostgreSQL, alias, app-state incremental y snapshot
completo: cero claves) y la sesión auxiliar tampoco recibió historial al
reconectar. **`no_seed` fue el resultado habitual.** No es una función a la que
volver esperando mucho.

---

## 11. Lo que nada de esto hace

- No escribe mensajes ni multimedia: el único efecto en la base es el cursor.
- No fabrica cursores ni WAMIDs. Un ON_DEMAND anclado en un id inventado recibe
  un ACK y después nada — es el fallo que más costó diagnosticar.
- No toca Signal ni la criptografía.
- No marca nada como leído ni altera ningún chat en el teléfono.
