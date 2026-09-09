# HISTORY_SYNC_ON_DEMAND

Es la única forma protocolar de pedir historial anterior. **Va anclado por
definición**: hay que decirle desde qué mensaje seguir hacia atrás.

En `PeerDataOperationRequestType` no existe nada parecido a "dame los últimos N
de este chat":

```
0 UPLOAD_STICKER   1 SEND_RECENT_STICKER_BOOTSTRAP   2 GENERATE_LINK_PREVIEW
3 HISTORY_SYNC_ON_DEMAND   4 PLACEHOLDER_MESSAGE_RESEND
```

## La forma exacta de la petición

Todo esto está verificado contra la cuenta real. **No cambiar sin una prueba
que garantice paridad.**

| Elemento | Valor | Por qué |
|---|---|---|
| `oldestMsgID` | WAMID **real** | Un id inventado recibe ACK y después nada. |
| `oldestMsgTimestampMS` | **segundos**, no ms | Pese al nombre del campo. |
| `count` | 50 | |
| destino | PN propio, `device=0` | |
| forma del mensaje | bare (sin envoltorio) | |
| `category` | `peer` | Sin esto el servidor confirma la stanza pero no la encamina, y nunca responde. |
| concurrencia | 1 (`MAX_ON_DEMAND_CONCURRENCY`) | Secuencial por chat. |

## Ciclo

```
REQUEST_SENT → ACK_RECEIVED → HISTORY_SYNC_NOTIFICATION → mensajes
     ↓
recalcular cursor desde lo persistido, no desde lo recibido
     ↓
COMPLETE_BUT_MORE_MESSAGES_REMAIN_ON_PRIMARY → continuar
COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY → exhausted
```

Se persiste **primero** y se recalcula el cursor después. La deduplicación por
`whatsapp_message_id` (índice parcial único `uq_messages_chat_wamid`) hace que
reconsultar historial ya conocido no pueda duplicar nada.

## Nunca

- Fabricar un WAMID.
- Usar un timestamp como sustituto del id.
- Inventar un cursor cuando no hay ancla — para eso está
  [`waiting_seed`](WAITING_SEED.md).

Un ON_DEMAND anclado en un id inventado responde ACK y luego silencio. Fue el
fallo que más tiempo costó diagnosticar.

## Capacidad por sesión

`capability_confirmed()` recuerda que **esta** vinculación demostró que
ON_DEMAND funciona, atado a la huella de sesión. Tras dos timeouts reales pasa
a `SUSPECT` y se vuelve a comprobar con un canary en vez de darla por buena.

## Tipos de History Sync

```
0 INITIAL_BOOTSTRAP   1 INITIAL_STATUS_V3   2 FULL   3 RECENT
4 PUSH_NAME   5 NON_BLOCKING_DATA   6 ON_DEMAND
```

`FULL` y `RECENT` no se han recibido nunca en esta cuenta.
