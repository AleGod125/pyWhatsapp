# El cursor de historial

## El bug

En la misma ejecución aparecían estas dos cosas:

```
CANARY: no hay ningun chat con cursor valido
... y a continuación la excavación encontraba uno, lo enviaba y hubo ACK.
```

No era un fallo de datos. Eran **dos definiciones distintas de "tiene
cursor"**:

| Quién | Qué exigía |
|---|---|
| la excavación | un mensaje con ID real de WhatsApp |
| el canary | eso **y además** chat individual **y** ≥2 mensajes |
| el Plan E | un ancla en `history_seeds` |

Y cuando el canary no encontraba nada, lo informaba como *"no hay ningún chat
con cursor válido"* — que es otra cosa.

Medido sobre la base real: **3 conversaciones con cursor, 1 sola elegible para
el canary.**

```
id=13  1 mensaje   individual  -> canary ANTES: no    (por "<2 mensajes")
id=16  3 mensajes  grupo       -> canary ANTES: no    (por ser grupo)
id=7  18 mensajes  individual  -> canary ANTES: sí
```

## La función única

```python
from app.history.cursor import get_valid_history_cursor

cursor = get_valid_history_cursor(session, chat_id=..., chat_jid=...)
# -> CursorInfo(wa_msg_id, timestamp, from_me, source, valid) | None
```

La usan **todos**: canary, excavación, cola de semillas, revisión de
pendientes, reconciliación y el colector del Plan E. Discrepar ya no es
posible.

## Quién manda

```
history_seeds        catálogo de TODAS las anclas conocidas
messages             los mensajes reales guardados
chat_history_state   el CURSOR ACTIVO — lo que se persiste y sobrevive
```

La función mira las tres y devuelve la **más antigua** que sea real. Se excava
hacia atrás: si el chat tiene anclas de las 17:00, 18:20 y 18:28, la que sirve
es la de las **17:00**, porque lo que queda por recuperar está antes.

Los alias cuentan: teléfono y LID son el mismo contacto y la misma
conversación.

## Máquina de estados

```
waiting_seed   no hay ancla
pending        hay ancla
fetching       hay ancla y una petición en vuelo
timeout        hay ancla y el último intento venció
exhausted      el teléfono dijo que no queda más
```

Al arrancar se reconcilia, porque un estado que promete un ancla tiene que
tenerla:

| Situación | Queda en |
|---|---|
| `pending` sin ancla | `waiting_seed` |
| `timeout` sin ancla | `waiting_seed` |
| `fetching` colgado, con ancla | `pending` |
| `fetching` colgado, sin ancla | `waiting_seed` |

## Un timeout no toca el ancla

Un timeout dice que **el teléfono no contestó**, no que el ancla sea mala.
Borrarla convertiría un chat recuperable en uno que vuelve a esperar una
semilla que ya tiene.

Lo único que cambia:

```
history_status   -> timeout
attempt_count    +1
last_attempt_at  ahora
next_retry_at    ahora + espera
```

## Reintentos

```
intento 1  ->  1 min
intento 2  ->  5 min
intento 3  -> 15 min
intento 4+ ->  1 h   (se estabiliza)
```

Insistir cada minuto no hace que el teléfono conteste, y sí consume la única
ranura de peticiones, que es de una en una. Un chat en espera no entra en las
candidatas hasta que le toca.

Una respuesta válida pone el contador a cero.

## Orden de persistencia

```
1. anotar la semilla
2. persistir el cursor        <- primero
3. waiting_seed -> pending    <- después
4. encolar
```

Si el proceso muere entre 2 y 3, queda un chat que **sigue esperando con su
ancla ya guardada**, que es recuperable. Al revés quedaría uno marcado como
listo para excavar sin nada con que hacerlo.

## Lo que no se toca

La **forma** de la petición `HISTORY_SYNC_ON_DEMAND` es la única que funciona
y no se ha modificado: destino el propio teléfono (dispositivo 0),
`category=peer`, mensaje desnudo, `count=50`, marca en **segundos**, y
`single-flight` global y por chat.

Lo único que cambió es de dónde sale el cursor y qué pasa después de un
timeout.

## `oldest_from_me`

Viaja en la petición. Antes se volvía a consultar el mensaje justo antes de
enviar, y si ese mensaje no estaba —un ancla que viene del catálogo de
semillas, por ejemplo— salía un `False` por defecto sin que nada lo dijera.
Ahora se guarda **con** el cursor.

## El canary

Sigue prefiriendo un chat individual con varios mensajes, que es el más fácil
de verificar a mano. Pero eso es una **preferencia, no un requisito**.

Y si no encuentra objetivo, **no bloquea la excavación**: no ha probado nada,
así que no puede concluir que ON_DEMAND no funcione. Lo único que la bloquea
es una prueba que se hizo y no obtuvo respuesta.
