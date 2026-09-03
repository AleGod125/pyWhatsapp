# Recuperar historiales pendientes — contrato para el frontend

Para el repo de Angular (`WhatsappBackup`). Base: `http://127.0.0.1:5000/api/v1`.

---

## 1. El problema que resuelve, en una frase

Algunas conversaciones llegaron del emparejamiento como **pura metadata**:
nombre, identificador, contadores... y ni un solo identificador de mensaje.
`HISTORY_SYNC_ON_DEMAND` va **anclado por definición** — hay que decirle *desde
qué mensaje* seguir hacia atrás — así que sin esa primera referencia no se
puede pedir nada. Esos chats quedan en `waiting_seed`.

`waiting_seed` **NO significa "chat vacío"**. Significa: *WhatsApp todavía no
ha entregado un punto desde el que pedir el historial*. Es reintentable, y el
chat despierta solo si llega un mensaje real.

Esta función abre una **sesión auxiliar independiente** (su propio QR, su
propia vinculación) cuyo único trabajo es conseguir esa referencia. Si la
consigue, se la entrega al motor de siempre. Su único efecto sobre la base de
datos es dejar el cursor del chat: no escribe mensajes ni multimedia.

### Expectativa realista

**`no_seed` va a ser el resultado habitual.** Las fuentes nativas se agotaron
con medidas (bootstrap, blobs de historial, PostgreSQL, alias PN/LID, app-state
incremental y snapshot completo: cero claves), y en la prueba la sesión
auxiliar tampoco recibió eventos de historial al reconectar — la entrega
parece ser de una sola vez, en el emparejamiento.

Diséñalo para que **`no_seed` se vea como un estado normal y reintentable**, no
como un fallo. Si la UI lo pinta en rojo, el usuario va a creer que algo se
rompió cuando no se rompió nada.

---

## 2. Los dos botones

### A. Global — "Recuperar historiales pendientes"

```
POST /api/v1/history/web-bootstrap/recover-pending
```

Sin cuerpo. Responde **`202`** enseguida (el proceso tarda minutos y puede
pedir un QR, así que no bloquea):

```json
{
  "job_id": "a3f81c92",
  "state": "starting",
  "total": 30,
  "processed": 0,
  "recovered": 0,
  "no_seed": 0,
  "errors": 0,
  "qr_required": false,
  "current_chat": null,
  "chats": [{ "id": 13, "name": "ubernel", "state": "waiting_seed" }],
  "error": null,
  "elapsed_seconds": 0
}
```

### B. Por conversación — dentro de la vista del chat

```
POST /api/v1/chats/<chat_id>/history/recover
```

Mismo cuerpo de respuesta, más `"chat_id": <id>`.

Muestra el botón **solo** cuando el chat esté en `waiting_seed`. Si no, la API
lo rechaza con `409 CHAT_NOT_WAITING_SEED`.

---

## 3. Seguir el progreso

Dos vías. **Usa SSE**, y el sondeo solo como respaldo.

### SSE (recomendado)

```
GET /api/v1/events/stream        (text/event-stream)
```

| Evento | Cuándo | Datos |
|---|---|---|
| `history.recovery.started` | arranca | el trabajo entero |
| `history.recovery.progress` | cada chat procesado, y al pedir QR | el trabajo entero |
| `history.recovery.completed` | termina (también si no había nada) | el trabajo entero |
| `history.seed.found` | aparece una referencia | `{chat_id, job_id}` |
| `history.backfill.started` | esa referencia ya alimenta al motor | `{chat_id, job_id}` |
| `history.seed.not_found` | ese chat sigue esperando | `{chat_id, job_id}` |

Los tres `recovery.*` traen el objeto completo del trabajo: puedes redibujar
todo el panel con cada uno sin guardar estado propio.

### Sondeo (respaldo)

```
GET /api/v1/history/web-bootstrap/recover-pending/status/<job_id>
```

Mismo objeto. `404` si el `job_id` no existe.

---

## 4. Estados

### Del trabajo — `state`

| Valor | Significado | UI |
|---|---|---|
| `starting` | arrancando | spinner |
| `running` | buscando referencias | barra `processed / total` |
| `qr_required` | **hace falta escanear el QR auxiliar** | mostrar el QR |
| `completed` | terminó | resumen |
| `failed` | no pudo arrancar; mirar `error` | mensaje de error |

### De cada chat — `chats[].state` y `current_chat.state`

| Valor | Significado | Sugerencia |
|---|---|---|
| `waiting_seed` | en cola | neutro |
| `recovering_seed` | buscándose ahora | spinner |
| `seed_found` | apareció una referencia | ✔ |
| `fetching_history` | ya está descargando historial | ✔ + progreso |
| `no_seed` | **sigue esperando. No es error. Reintentable** | neutro / ámbar |
| `error` | algo falló de verdad | ✖ |
| `complete`, `timeout` | reservados | neutro |

Los tres que el usuario tiene que poder distinguir de un vistazo:
**recuperado** (`seed_found` / `fetching_history`), **sin referencia**
(`no_seed`) y **error** (`error`).

---

## 5. El QR auxiliar

Es una **vinculación distinta** de la principal. Que pywhats esté conectado no
implica que la auxiliar lo esté, y viceversa. **No reutilices el componente del
QR principal sin cambiar el texto**, o el usuario va a creer que se le cayó la
sesión.

Antes de ofrecer el botón, consulta:

```
GET /api/v1/history/web-bootstrap/session
→ {"available": true, "reason": null, "linked": false}
```

- `available: false` → la función no está instalada; oculta el botón y muestra
  `reason` (suele ser: falta `npm install` en `web_bootstrap/`).
- `linked: false` → habrá QR. Avisa antes: *"se vinculará un dispositivo
  adicional a tu WhatsApp"*.

Cuando el trabajo pase a `qr_required` (por SSE o sondeo):

```
GET /api/v1/history/web-bootstrap/qr        → image/png, Cache-Control: no-store
```

`404 NO_AUXILIARY_QR` si no hay ninguno pendiente. **El payload del QR nunca
sale en JSON** — es una credencial de vinculación y solo se sirve como imagen.
No lo caches, no lo registres, no lo pongas en la URL.

Para desvincular solo la auxiliar (la principal, el Signal Store y PostgreSQL
quedan intactos):

```
DELETE /api/v1/history/web-bootstrap/session   → {"removed": true}
```

Ofrécelo visible. Es lo que permite quitar toda esta función sin consecuencias.

---

## 6. Errores

Todos traen `{"error": {"code": "...", "message": "..."}}` y algún campo extra.

| Código | HTTP | Qué hacer |
|---|---|---|
| `RECOVERY_UNAVAILABLE` | 409 | Falta el componente auxiliar. Oculta el botón, muestra `message`. |
| `RECOVERY_BUSY` | 409 | Ya hay uno en marcha. **La respuesta trae `job` con el trabajo activo**: engánchate a ese en vez de reintentar a ciegas. |
| `CHAT_NOT_WAITING_SEED` | 409 | Ese chat ya tiene referencia. Trae `history_status`. Oculta el botón. |
| — | 503 | La base de datos no está disponible. |
| — | 404 | `job_id` o `chat_id` inexistente. |

Solo **un** trabajo a la vez, global o por chat: deshabilita ambos botones
mientras haya uno activo.

---

## 7. Flujo completo

```
GET  /history/web-bootstrap/session
      available:false ─────────────────► ocultar botón, mostrar reason
      available:true
        │  linked:false → avisar: "se vinculará un dispositivo adicional"
        ▼
POST /history/web-bootstrap/recover-pending          → 202 {job_id}
        │                                            → 409 RECOVERY_BUSY {job}
        ▼
GET  /events/stream  (o sondear status/<job_id>)
        │
        ├─ state=qr_required ──► GET /history/web-bootstrap/qr  (PNG)
        │                        el usuario escanea → sigue solo
        │
        ├─ history.seed.found       → chat en verde
        ├─ history.backfill.started → ese chat ya descarga historial
        ├─ history.seed.not_found   → chat en ámbar, reintentable
        │
        ▼
   state=completed → "N recuperadas, M siguen esperando, K errores"
```

Para el resumen final, evita decir "vacías" o "completado al 100%". Lo honesto
es: *"M conversaciones siguen sin una referencia desde la que pedir historial.
No están vacías; se puede reintentar más tarde."*

---

## 8. Endpoints relacionados que ya existían

| Endpoint | Para qué |
|---|---|
| `POST /chats/<id>/history/recheck` | Reevalúa el estado de un chat sin sesión auxiliar. Más barato: pruébalo primero. |
| `GET /session/qr/image` | QR de la sesión **principal** (pywhats). No confundir. |
| `POST /media/<id>/retry` | Reintenta una descarga de multimedia. |
| `POST /messages/<id>/media/recover` | Recupera el multimedia de un mensaje. |

---

## 9. Lo que esta función NO hace

Vale la pena tenerlo presente al construir la UI, para no prometer de más:

- No escribe mensajes ni multimedia. Su único efecto en la base es el cursor.
- No comparte criptografía con la sesión principal.
- No marca nada como leído ni altera ningún chat en el teléfono.
- No es un segundo backup: consigue una referencia y se apaga.
