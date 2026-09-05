# Plan F: qué ve WhatsApp Web

## La pregunta

`HISTORY_SYNC_ON_DEMAND` va anclado: sin el identificador y la marca de tiempo
de un mensaje real, no se puede pedir nada. Las conversaciones que llegaron del
emparejamiento como pura metadata se quedan esperando.

La hipótesis es que **WhatsApp Web puede conocer conversaciones o mensajes que
los blobs de History Sync no entregaron**. El proyecto anterior usaba
`Store.Chat.getModelsArray()` justo para eso.

Esta fase no recupera nada. Responde:

```
de las conversaciones que esperan,
  ¿cuántas ve WhatsApp Web?
  ¿de cuántas puede dar un mensaje real?
```

## Qué se reutilizó del proyecto anterior

| Idea | Dónde está ahora |
|---|---|
| dos vías de descubrimiento: `getChats()` **y** `Store.Chat.getModelsArray()` | `worker.js → inventario()` |
| leer `chat.msgs.getModelsArray()` para ver qué hay ya cargado | `store_adapter.mensajesEnMemoria` |
| la cadena de cargadores (`loadEarlierMsgs`, `loadEarlier`, `loadOlderMsgs`) | `store_adapter.cargarAnteriores`, tras un interruptor |
| `Store.WidFactory.createWid` para localizar un chat por JID | `store_adapter` |
| opciones de arranque de Chromium headless | `worker.js` |

## Qué se descartó, y por qué

| Descartado | Motivo |
|---|---|
| `server.js` entero (8 457 líneas) | tiene su propia base, auth, extracción, media, API, WebSocket y ciclo de vida de sesiones. Todo eso ya vive en Python |
| `db.js` y el pool de `pg` | el worker **no** habla con PostgreSQL |
| Express + WebSocket | un puerto más que asegurar. El canal son las tuberías del proceso |
| `puppeteer-extra` + plugin *stealth* | dos dependencias más para un uso que no lo pide |
| 80 desplazamientos del panel por defecto | agresivo y lento. Queda tras `WEB_STORE_DISCOVERY_SCROLL`, apagado |
| `getContacts()` como fuente de chats | un contacto no es una conversación. Crear chats así llenaría la base de cosas que no existen |
| descarga de mensajes y multimedia | esta fase mide; el extractor es Python |

## Los cargadores: descubrir, no suponer

El proyecto anterior hacía esto:

```js
const fns = [
  () => chat?.loadEarlierMsgs?.(),
  () => chat?.msgs?.loadEarlierMsgs?.(),
  () => chat?.msgs?.loadEarlier?.(),
  () => chat?.loadOlderMsgs?.(),
];
for (const run of fns) { try { await run(); } catch (e) { } }
```

Funciona, pero **no deja saber cuál existe**: si los cuatro han desaparecido en
una versión nueva, el resultado es idéntico a que ninguno haya encontrado nada.

Ahora se inspecciona el `Store` vivo y se informa en `capabilities`:

```
Store.Chat.getModelsArray
Store.Chat.get · Store.Chat.find
Store.WidFactory.createWid
chat.loadEarlierMsgs · chat.msgs.loadEarlierMsgs
chat.msgs.loadEarlier · chat.loadOlderMsgs
Store.ConversationMsgs.loadEarlierMsgs · Store.ConversationMsgs.fetchPage
chat.syncHistory · Store.HistorySync.sendPeerDataOperationRequest
```

Un método que no está se reporta como ausente. **No se simula.**

## Los hacks: documentados, no activados

No se descartan. Se dejan escritos aquí para la fase siguiente, si el nivel 1
da poco.

### `chat.syncHistory()`

Método nativo del Store. El proyecto anterior lo usaba como alternativa cuando
`HistorySync` no estaba.

### `Store.HistorySync.sendPeerDataOperationRequest(3, {chatId})`

El equivalente en el navegador de lo que hace nuestro `ON_DEMAND`. El proyecto
anterior dejó anotado un aviso que conviene conservar:

> ⚠️ NO usar el tipo 5 (`FULL_HISTORY`): activa el anti-bot de WhatsApp y
> corta la sesión.

### `storeChat.endOfHistoryTransferType = 0`

Reescribe la marca que WhatsApp Web usa para no volver a pedir historial de un
chat que ya dio por terminado. Sin esto, las peticiones repetidas se ignoran.

**Es escribir en el estado interno del cliente**, y por eso no se activa a la
ligera: no se sabe qué más mira WhatsApp Web ese campo.

### Por qué no se activan todavía

Las tres piden historial al teléfono, que es exactamente lo que ya hace el
motor de Python — y ese funciona, está medido y tiene control de una petición
a la vez. Meter una segunda vía pidiendo lo mismo por otro camino, en
paralelo, y sin ese control, es la forma de acabar sin saber quién trajo qué.

Primero el nivel 1. Después se decide con números.

## Qué se mide

**Inventario** — las dos vías de descubrimiento y su unión:

```
python_chats · web_get_chats · web_store_chats · union_chats
extra_vs_python · missing_vs_python · individual · group
```

`extra_vs_python` es el número que dice si esto aporta algo.

**Sondeo** — por cada conversación que espera:

```
waiting · visible_store · with_messages
candidates    ← lo que propuso Node
seed_usable   ← lo que pasa NUESTRAS reglas
sin_seed · rejections · by_source
```

Los dos últimos importan: `candidates` dice cuánto encontró el navegador;
`seed_usable` dice cuánto sirve de verdad. Si divergen, `rejections` dice por
qué.

## Solo lectura

No se anota ni un ancla, no se cambia ni un estado, no se pide ni un
`ON_DEMAND`. La respuesta lo declara (`read_only`, `mutations`,
`on_demand_requests`) y hay una prueba que cuenta filas antes y después con un
candidato perfectamente válido.

`wakeable_chats` dice cuántos **se podrían** despertar. Ninguno se despierta.

## La decisión de después

```
seed_usable > 0   →  Web candidate → validación Python → history_seeds
                     → cursor → pending → ON_DEMAND (el motor de siempre)

seed_usable ≈ 0   →  evaluar nivel 2, y después los hacks de arriba
```

Y puede salir cero. WhatsApp decide qué entrega a un dispositivo vinculado, y
un segundo dispositivo no cambia esa decisión por existir.
