# Plan J: ¿un motor propio con una sola identidad? Investigación

Fecha: 5 de septiembre de 2026.
Base: backend `c3fc37e`, frontend `28e3ece`.
**Nada productivo se ha cambiado. No se ha limpiado, desvinculado ni
re-vinculado nada.**

---

## A. Resumen ejecutivo

**`VIABLE_CON_TRABAJO`**, y el trabajo que falta está ahora localizado en una
función concreta.

De las 48 conversaciones que hoy tienen ancla, **sólo 8 (17%) la tendrían con
lo que llega al vincular**. El resto viene del segundo dispositivo: 32 de su
almacén y **23 de `fetchMessages({limit:1})`**. Ese `fetchMessages` no es magia
del navegador — llama a `WAWebChatLoadMessages.loadEarlierMsgs({chat})`, y
consigue mensajes de conversaciones **cuyo almacén local está vacío**. Es decir:
WhatsApp Web sabe pedir «dame lo reciente de este chat» **sin ancla previa**, y
nosotros hoy no.

Si esa operación resulta ser una petición de protocolo que la identidad
principal puede emitir, un solo QR pasa a ser viable. Si resulta depender de
estado exclusivo del navegador, no. **No lo he podido determinar** sin
instrumentar la capa de red del cliente Web, y eso es lo primero que haría a
continuación.

---

## B. Qué tiene hoy la sesión principal

Medido sobre el bootstrap real, con `app/discovery/primary_inventory.py`:

```
primary_chats=41  groups=6  individuals=35
with_name=14  with_last_activity=41  with_pn_lid_pair=34
with_seed=0
```

`pywhats` modela **5 de los 31 campos** que trae una `Conversation`. Los que
descarta y sí vienen:

| campo | qué es | en cuántas |
|---:|---|---:|
| 12 | marca de actividad | **41 de 41** |
| 13 | asunto del grupo | 7 |
| 21 | opaco (11 bytes) | 32 |
| 23 | opaco (32 bytes) | 33 |
| 38 / 43 | push name | 7 |
| **39** | **JID de teléfono** | 34 |
| **49** | **LID** | 34 |

Los campos 39 y 49 juntos son el par PN↔LID, que costó fases resolver por
`usync`. Estaba en el primer blob.

**Configuración de emparejamiento descartada como causa.** `full_sync=True`,
`days=3650`, cuota 100 GB llevan activos desde el 2 de septiembre —antes del
emparejamiento de esta sesión— y el bootstrap siguió siendo **un solo trozo**,
`chunk=0 progress=0`, 41 conversaciones y 103 mensajes. Pedir más historial en
el registro **no** trae más inventario.

---

## C. De dónde salen de verdad las anclas

Medido con `tools/trace_seed_sources.py` sobre los 122 blobs archivados y la
base:

```
origen del ancla        chats distintos
  web_store                          32
  web_fetch1                         23
  on_demand                          16
  live                                9
  initial_bootstrap                   8

chats con ancla hoy                  48
chats con ancla EN EL BOOTSTRAP       8   → 17%
```

`on_demand` y `live` son **corriente abajo**: el primero necesita un ancla
previa para poder pedirse, el segundo necesita que alguien escriba. Ninguno
cuenta como cobertura inicial.

**La respuesta a «¿las anclas de Web ya están en nuestros blobs?» es NO.**
Están en 8 de 48.

---

## D. El hueco, con nombre y apellidos

`fetchMessages({limit:1})` en `whatsapp-web.js`:

```js
const loadedMessages = await window
  .require('WAWebChatLoadMessages')
  .loadEarlierMsgs({ chat });
```

Se ejecuta **sólo** cuando el almacén local del chat está vacío — así lo
ordena nuestro propio índice— y aun así consiguió mensaje en 23 conversaciones.

Eso es exactamente lo que no sabemos hacer. Nuestro `HISTORY_SYNC_ON_DEMAND`
exige `oldestMsgID` y `oldestMsgTimestampMS`: sin ancla, no hay petición.

Los tipos de operación que modelamos:

```
0 UPLOAD_STICKER
1 SEND_RECENT_STICKER_BOOTSTRAP
2 GENERATE_LINK_PREVIEW
3 HISTORY_SYNC_ON_DEMAND      ← el que usamos
4 PLACEHOLDER_MESSAGE_RESEND
```

Ninguno es «dame lo reciente sin ancla». Pero **este enum es nuestro
subconjunto**, no el del protocolo: lo escribimos nosotros en
`app/models/proto/whatsapp_backup.proto`. Que no esté aquí no significa que no
exista — es exactamente la lección del Plan I con los campos 39 y 49.

---

## E. Lo que haría a continuación, en orden

1. **Instrumentar `loadEarlierMsgs`** en nuestro Web Companion, en modo
   diagnóstico, para capturar qué manda por el cable cuando el chat está
   vacío. Es una función nombrada y localizada; no hay que adivinar dónde
   mirar.
2. **Comparar el enum completo** de `PeerDataOperationRequestType` en Baileys y
   whatsmeow contra el nuestro. Si hay un tipo para «mensajes recientes», el
   camino queda abierto.
3. **Clasificar los 9 chats** que Web ve y la principal no, para saber si la
   diferencia 41/50 es real o si algunos no son conversaciones de usuario
   (difusiones, newsletters, entidades de sistema). La cobertura real podría
   ser mejor que el 82% nominal.

---

## F. Arquitectura propuesta, si resultara viable

No reescribir criptografía. `pywhats` se queda debajo como dueño del
protocolo — Noise, Signal, emparejamiento, transporte — y encima va nuestro
modelo:

```
                    UN QR → UNA identidad companion
                              │
                     pywhats (transporte + Signal)
                              │
                    ┌─────────┴─────────┐
                    │   NUESTRO STORE   │
                    └─────────┬─────────┘
        inventario · identidad PN/LID · anclas · actividad
                              │
              PostgreSQL (verdad del producto) → Drive
```

**Un solo dueño del protocolo.** Nada de compartir Signal entre motores: eso
sigue descartado sin experimentar, porque son dos ratchets sobre una identidad
y ya sabemos cómo acaba.

Migración por capas, si se llega a hacer:

| fase | qué | riesgo |
|---|---|---|
| J1 | Nuestro índice sobre el bootstrap (**ya hecho y probado**) | bajo |
| J2 | Instrumentar y entender `loadEarlierMsgs` | bajo |
| J3 | Petición equivalente desde la principal | **alto** |
| J4 | Quitar el segundo dispositivo | medio |

J3 es la que decide, y es la única con riesgo real.

---

## G. Complejidad

| componente | complejidad | nota |
|---|---|---|
| Índice desde el bootstrap | **BAJA** | hecho |
| Par PN↔LID desde el cable | **BAJA** | hecho |
| Instrumentar `loadEarlierMsgs` | **BAJA** | función localizada |
| Petición sin ancla desde la principal | **ALTA** | depende de que exista |
| Quitar el segundo dispositivo | MEDIA | sólo tras lo anterior |
| Motor propio completo | **MUY ALTA** | y no hace falta |

**No hace falta un motor propio.** Lo que falta es una operación concreta.

---

## H. Licencias

| proyecto | licencia | qué se puede hacer |
|---|---|---|
| `pywhats` | ver el paquete instalado | dependencia; hoy se envuelve, no se edita |
| `whatsapp-web.js` | Apache-2.0 | usar y adaptar conservando aviso |
| Baileys | MIT | estudiar y adaptar |
| whatsmeow | MPL-2.0 | estudiar; adaptar obliga a publicar cambios del archivo |

Para lo que hace falta —entender una forma de mensaje— basta con **estudiar**.
No propongo copiar código de ninguno.

---

## I. Qué haría falta capturar en el próximo emparejamiento

Si se hace un clean-run algún día, esto es lo que hoy no se puede observar:

1. **Todos los trozos del bootstrap**, con su `progress`. Sólo se vio
   `chunk=0 progress=0`, y no se sabe si hubo más y se perdieron.
2. **Los nodos crudos** de los primeros 60 segundos, con las claves redactadas.
3. **El momento exacto** en que nacen las sesiones PN y LID del propio
   dispositivo.
4. **El bootstrap del segundo dispositivo**, para comparar los dos de la misma
   cuenta.

---

## J. Estados

| | estado |
|---|---|
| `PLAN_J` | **VIABLE_CON_TRABAJO** |
| `ONE_QR` | **NO_RESUELTO** — falta la operación sin ancla |
| `DISCOVERY` | **OK** — y ahora también desde la principal |
| `DEEP_HISTORY` | **OK** — intacto |
| `OWN_LIVE` | **PARCIAL** — investigación aparte |

---

## Herramientas de esta fase, todas de solo lectura

| herramienta | qué contesta |
|---|---|
| `tools/trace_seed_sources.py` | de dónde sale cada ancla, y cuántas vendrían del bootstrap |
| `app/discovery/primary_inventory.py` | qué inventario trae el bootstrap que se descartaba |
