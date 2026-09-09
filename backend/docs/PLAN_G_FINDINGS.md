# Plan G: qué aprendimos, medido

Este documento es el registro de lo que **se midió**, no de lo que se supuso.
Cada hallazgo trae los números que lo sostienen y, cuando existe, la línea de
registro que lo produjo. Sirve para dos cosas: no volver a diagnosticar lo
mismo, y no volver a romper algo que costó entender.

Última actualización: 5 de septiembre de 2026.

---

## A1. La arquitectura, ya validada

```
whatsapp-web.js
      │  DISCOVERY / INDEX ENGINE
      │  qué conversaciones existen y cuál es el último mensaje real
      ▼
  chat inventory
  jid · nombre · WAMID · timestamp · fromMe
      ▼
RecentSeedCollector          ← valida, deduplica, promueve
      ▼
SeedBackfillQueue
      ▼
   pywhats
      │  DEEP HISTORY ENGINE
      │  ON_DEMAND: 50 → MORE → 50 → … → FINAL
      ▼
PostgreSQL  →  media  →  Drive
```

**La fuente de verdad es PostgreSQL + Drive.** WhatsApp Web no es una segunda
base de datos: propone, y Python decide. El reparto funciona porque cada mitad
hace lo que hace bien — Web ve el universo de conversaciones, pywhats excava
hondo — y ninguna intenta hacer la otra mitad.

---

## A2. pywhats no basta como descubridor

Medido en el `INITIAL_BOOTSTRAP` real de un clean-run:

| dato | valor |
|---|---|
| conversaciones en el blob | 41 |
| con mensajes dentro | 8 |
| mensajes reales | 107 (todos con WAMID válido) |
| **registros completamente vacíos** | **33** |

La sesión principal extrae el 100% de lo que recibe. El problema no es la
extracción: es que lo que WhatsApp entrega al vincular no es el universo de
conversaciones.

**Conclusión:** pywhats no puede ser la fuente completa del inventario.

---

## A3. Conversaciones que sólo ve Web

En el mismo clean-run:

| origen | conversaciones |
|---|---|
| pywhats | 41 |
| Web Inventory | 50 |
| base tras reconciliar | 51 |

Unas 10 conversaciones existían **sólo** para Web. Sin el índice de Web no
habrían aparecido nunca. La inversión de roles del Plan G fue correcta.

---

## A4. `getChats()` puede devolver 0

Hallazgo que costó una fase entera de confusión.

`client.getChats()` devolvió **cero** en la sesión real, mientras el Store
tenía las ~50 conversaciones. La causa probable es cómo whatsapp-web.js
construye esa lista: mapea todos los modelos, y basta que uno falle para
quedarse sin lista.

El fallo derivado era peor que el original: existía un camino de respaldo por
el Store, pero **devolvía la lista y se acababa ahí** — `candidate: null` para
las 50, sin mirar `lastMessage`, sin mirar el Store y sin pedir nada.

```
[WEB_INDEX] chats=50 nuevos=10 seeds=0 cobertura=0% sin_referencia=50
```

...mientras el sondeo antiguo, en el mismo segundo, encontraba 14:

```
probe waiting=42 visibles=42 mensajes=14 seeds=14
```

**Arreglo:** el inventario es la **unión** de `getChats()` ∪ Store, por JID, y
los tres intentos se hacen siempre venga la conversación de donde venga. Nunca
se elige una vía y se descarta la otra.

---

## A5. Cobertura real de Web, después del arreglo

Medido en vivo, solo lectura:

```
origen                         store          ← getChats() seguía dando 0
conversaciones vistas          50
  del ultimo mensaje           0
  ya materializado en memoria  45
  pidiendo UNO a la red        0
  sin ninguna                  5

referencias validas            45 de 50  (90%)
resolverian una que espera     22 de 28
```

El Store materializado cubrió la gran mayoría. `fetch1` se ejecutó en las 5
restantes y devolvió vacío en todas — no es un fallo del código: esas
conversaciones no tienen nada materializable ahora mismo.

---

## A6. Las reglas de `fetch1`

Lo que **no** se hace, y por qué:

- **no** `fetchMessages({limit: Infinity})` — sería un segundo extractor
  profundo compitiendo con el que ya funciona;
- **no** `loadEarlier` agresivo;
- **no** `syncHistory` hackeado.

Lo que sí, y sólo cuando `lastMessage` falla **y** el Store no tiene nada:

```js
await chat.fetchMessages({ limit: 1 })
```

Tope de **60 conversaciones con red por pasada**. El orden de esa cuota es:
primero las que esperan referencia, después las que sólo ve Web, y dentro de
cada grupo por actividad reciente. Las que ya tienen cursor válido se omiten:
gastar una petición ahí se la quita a otra que sí la necesita.

---

## A7. La sesión principal manda

**Bug medido:** `service.py` arrancó con `STARTING → NO_SESSION` —sin
`device.json`, sin identidad, sin Signal— y aun así salió:

```
[WEB] worker iniciado
[WEB] QR requerido para el Web Companion
```

El usuario acabó escaneando el código del segundo dispositivo cuando el que
hacía falta era el principal.

**Causa raíz:** `service.py` levantaba el worker en un hilo nada más arrancar,
sin mirar la sesión. Y no podía salir bien: al arrancar, la conexión principal
nunca está lista todavía — se abre después y en otro hilo.

**Causa secundaria:** el supervisor se reiniciaba solo con espera creciente sin
mirar nada, y cada arranque sin sesión guardada publica un código nuevo.

**Definición canónica de `primary_ready`** (`app/core/primary.py`) — las cuatro
a la vez, porque cada una se cumple sin las otras:

1. estado `CONNECTED` o `WAITING_INITIAL_HISTORY`;
2. identidad propia del dispositivo **vivo** (no de un archivo);
3. Signal Store presente;
4. cuenta de WhatsApp reconciliada.

`RECONNECTING` es un caso aparte: la sesión sigue valiendo y mandar al usuario
al QR sería hacerle rehacer algo que no está roto.

**Orden operativo:**

```
PRIMARY PAIRING → PRIMARY READY → WEB → INVENTORY → SEEDS → BACKFILL
```

Nunca `NO_SESSION → WEB QR`.

---

## A8. El canary salía demasiado pronto

Medido al reiniciar sobre una sesión guardada y buena (51 chats, 3382
mensajes, 89 peticiones respondidas):

```
00:32:38.443  CONNECTED (<success>)
00:32:38.630  ib: dirty type=account_sync     ← el servidor aún colocándose
00:32:40.489  peticion de prueba              ← +2,0 s
00:33:25.646  TIMEOUT → capability SUSPECT
```

**Dos causas.** Una: una vinculación nueva espera su `INITIAL_BOOTSTRAP`, y esa
espera hacía de asentamiento sin que nadie la pensara; una sesión recuperada no
espera nada. Dos: `pick_canary` eligió un chat que ya había agotado dos esperas
(`intento=3`) — el peor objetivo posible para un diagnóstico.

**Arreglos:**

- margen de ~20 s tras recuperar sesión (no se aplica a una vinculación nueva);
- la prueba elige a quien **ya contestó** en esta base, y entre los demás al que
  menos ha fallado;
- **un ACK no confirma nada**;
- cualquier `HISTORY_SYNC ON_DEMAND` válida y correlacionada confirma, **aunque
  traiga 0 mensajes nuevos** (antes hacía falta que además trajera historial);
- reintento `30 s → 1 min → 5 min`;
- `SUSPECT` es reversible y no borra nada.

---

## A9. ON_DEMAND funciona

Validado repetidamente, con miles de mensajes recuperados:

```
50 → MORE
50 → MORE
 …
     FINAL
```

`endOfHistoryTransferType`: **0 y 2 = MORE**, **1 y 3 = FINAL**. El FINAL real
se respeta y no se reabre sin evidencia nueva.

El fallo histórico de ON_DEMAND (73 peticiones con ACK y silencio) tenía causa
medida: `peer_message.py` quitaba `<device-identity>` incondicionalmente, y un
`pkmsg` abre sesión Signal nueva y el teléfono necesita la firma ADV para
validarla. Correlación de 77 peticiones: 73 con `enc_type=msg` respondieron
todas (~1 s); 4 con `pkmsg` se agotaron todas.

---

## A10. El realtime congelado

**Causa exacta:** `EVENT_NAMES` en el frontend era una lista fija de
`addEventListener`, y le faltaban `chat.status`, `chat.inventory`,
`chat.created` y `heartbeat`.

`EventSource` entrega un evento **con nombre** sólo a quien se registró con ese
nombre exacto; `onmessage` recoge únicamente los que no lo llevan — y todos los
de este backend lo llevan.

| capa | estado |
|---|---|
| EventBus backend | correcto |
| endpoint SSE | correcto |
| navegador | **descartaba los eventos con nombre no suscrito** |

El panel tenía manejadores para `chat.status` y `chat.inventory` que no podían
ejecutarse nunca. Desde fuera se veía como una aplicación congelada.

**Arreglos:** lista completa, estado `LIVE / RECONNECTING / OFFLINE`, latido
suscrito, watchdog de 90 s comprobado cada 15 s, y una sola resincronización al
reconectar.

Un segundo hallazgo del mismo frente: `WebInventoryService` creaba
conversaciones en PostgreSQL y **no avisaba a nadie** — el resumen sólo lo
publicaba la ruta HTTP, y esa no la llama nadie: el índice lo lanza el vigilante
automático.

---

## A11. Nombres y metadata

`chats.name` está **NULL en las 51 conversaciones**. Los nombres viven en
`contacts.display_name` (172 filas) y el serializador los resuelve por join
contra el JID o el LID.

Consecuencia práctica: buscar por nombre en `chats` no encuentra nada. La
herramienta de diagnóstico busca también en la agenda por eso.

La pantalla actualiza metadata por `chat.updated`, que ahora lleva la fila
entera dentro y no obliga a pedir la lista.

---

## A12. Los contadores engañaban

Ejemplo real de la interfaz:

```
37 en curso
6 sin referencia
43 chats pendientes
```

«43 pendientes» es una suma de cosas que no son lo mismo: algo que se está
procesando ahora mismo no es un fallo. Ver 43 daba la impresión de 43 problemas.

Las categorías reales son seis, y no se suman entre sí: recuperados,
recuperándose, reintento pendiente, esperando referencia, sin mensajes
disponibles, y error.

---

## A13. Isaac — falta el borde reciente, no la historia vieja

`Isaac Virtual Tec`, chat 29910.

| dato | valor |
|---|---|
| mensajes guardados | 254 |
| el más antiguo | 12 ago 2026 |
| el más nuevo | **24 ago 2026** |
| peticiones | 7 |
| respuestas | **7** |
| timeouts | **0** |
| estado | `exhausted` |
| último error | `COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY` |
| lo que ve Web | mensaje del **4 sep 2026** |

**Por abajo está de verdad completo**: el servidor lo dijo, y las 7 peticiones
respondieron. Lo que falta son once días **por arriba**.

```
    ┌──────────────── DB ────────────────┐
    12 ago ......................... 24 ago
                                        │
                                     [ HUECO ]
                                        │
                                     4 sep  ← lo que ve Web
```

`ON_DEMAND` excava **hacia atrás** desde el ancla, así que nunca alcanza lo que
está por encima del mensaje más nuevo guardado. Por eso pulsar «Recuperar
historial completo» no lo arreglaba: repetía la única operación que no puede
cerrar ese hueco.

Marcas: `RECENT_GAP_DETECTED` + `EXHAUSTED_PERO_INCOMPLETO`.

---

## A14. Tía Nore — es otro problema

`Tia Nore`, chat 29915. JID `820255…@lid`.

| dato | valor |
|---|---|
| mensajes guardados | 2 |
| cursor | 11 ago 2026, origen `web_store` |
| peticiones | 4 |
| respuestas | **0** |
| timeouts | **4** |

Marca: `OLD_HISTORY_PENDING`. **No tiene hueco reciente.** Su problema es que
ese chat concreto no contesta a `ON_DEMAND`, con el mismo protocolo con el que
Isaac contestó 7 de 7.

Dato relevante: es el chat que el canary eligió como objetivo, y de sus
timeouts salió el `SUSPECT` que dejó parada la recuperación entera. Ver A8.

**No se mezcla con Isaac.** Su política es la de siempre: reintento con espera
creciente, tope de intentos, y `SUSPECT`/canary si corresponde.

---

## A15. Mensajes de negocio: el volumen es mínimo

De 3700 mensajes guardados:

| tipo | cantidad |
|---|---|
| `buttonsMessage` | 5 |
| `buttonsResponseMessage` | 4 |
| `templateMessage` | 3 |
| `listResponseMessage` | 2 |
| `listMessage` | 2 |
| `unknown` | 10 |
| **total «especiales»** | **26 (0,7%)** |

Aparte, `system` 155 y `poll` 10, que ya se tratan.

**Conclusión:** no abrir ese frente todavía. No se fabrica texto para ningún
blob desconocido: un mensaje inventado es peor que un hueco declarado.

---

## A16. Media y Drive

Pipeline probado y estable: **DB primero → segmentos → Drive**, con AES-256-GCM.
El descargador es independiente del historial: `404` es `unavailable`, `410` es
`expired`, y ninguno de los dos es un fallo del backfill.

Medido: 617 adjuntos, 487 descargados, 125 caducados, 5 no disponibles.

**No tocar.**

---

## A17. Los principios que no se rompen

Estos no son preferencias de estilo. Cada uno viene de un incidente medido.

1. **No MAC bypass, no hacks de Signal.**
2. **No fabricar un cursor.** Una referencia inventada recibe confirmación del
   servidor y después silencio, y eso es lo más caro de diagnosticar que tiene
   este proyecto.
3. **No fabricar un WAMID.** Ni una marca de tiempo. Ni un `fromMe`.
4. **No adivinar la unidad de un timestamp.** Dividir por mil produce un cursor
   que el servidor confirma y nunca responde. Lo que no encaja se descarta.
5. **No copiar estado Signal entre identidades.** Un `device.json` nuevo sobre
   un Signal Store viejo produce «unknown one-time pre-key id» en cada mensaje.
6. **Un ACK no confirma nada.** Confirma la entrega de la stanza, no que vaya a
   llegar el historial.
7. **Una respuesta tardía sin waiter no confirma la sesión actual.** El
   aislamiento por huella es lo que impide que una respuesta de otra
   vinculación valide ésta.
8. **DB primero → EventBus → SSE.** Nunca al revés: emitir antes de guardar
   enseña algo que no existe.
9. **PostgreSQL y Drive son la fuente de verdad.** El bus vive en memoria y no
   guarda nada.
10. **Web propone, Python decide.** Web no sabe de quién es la cuenta, ni de
    alias entre teléfono y LID.
11. **Discovery y Deep History son cosas separadas**, y ninguna intenta hacer
    la otra.
12. **Una sola definición de «tiene cursor»**: `get_valid_history_cursor`. Dos
    definiciones fue exactamente el bug que produjo una oscilación infinita.
13. **La sesión principal manda; el segundo dispositivo espera.**
14. **Un timeout aislado no es una incapacidad**, y nunca justifica borrar una
    sesión válida. Hacen falta tres rechazos seguidos de la misma sesión.

---

## Apéndice: herramientas de diagnóstico, todas de solo lectura

| herramienta | qué contesta |
|---|---|
| `tools/diagnose_chat_recovery.py` | por qué una conversación se quedó a medias |
| `tools/diagnose_web_inventory.py` | qué ve Web y cuántas referencias da |
| `tools/diagnose_ondemand.py` | por qué ON_DEMAND no responde |
| `tools/diagnose_ondemand_known_good.py` | repetir una petición que sí funcionó |
| `tools/capture_baseline.py` | foto del estado, para comparar después |
| `tools/compare_baselines.py` | qué cambió entre dos fotos |
