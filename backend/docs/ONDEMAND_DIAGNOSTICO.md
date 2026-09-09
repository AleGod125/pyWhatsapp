# Por qué ON_DEMAND deja de responder

Una petición `HISTORY_SYNC_ON_DEMAND` que sale, recibe ACK y nunca obtiene
respuesta tiene ocho explicaciones posibles, y el log no distinguía entre
ellas. Este documento recoge lo que se midió y qué se corrigió.

## La cadena, tramo a tramo

```
build_on_demand_message      construye el Message (campo 16, chat dentro)
        │
peer_mode + Sender           cifra para el teléfono propio, device 0
        │                    stanza: <message category=peer><enc/></message>
        ▼
ACK del servidor             "la stanza llegó". NO es una respuesta.
        │
        ▼
HISTORY_SYNC_NOTIFICATION    el teléfono avisa de que hay un blob
        │                    campos 8 y 12: originalMessageID, sessionID
        ▼
descarga + inflate + parse   pywhats HistorySyncer.handle
        │
        ▼
notify_history               despierta la espera que corresponda
        │
        ▼
ingesta -> PostgreSQL
```

**ACK ≠ respuesta.** El ACK lo emite el servidor al aceptar la stanza; la
respuesta la genera el teléfono, después, y llega por otro camino.

## Lo que se midió (2026-09-03 y 2026-09-04)

| Fecha | Peticiones | `enc.type` | Resultado |
|---|---|---|---|
| 09-03 22:49–22:54 | 73 | `msg` | respondieron todas, latencia ~1 s |
| 09-04 14:45–14:49 | 4 | `pkmsg` | ninguna respondió, timeout de 45 s |

Correlación del 100 % sobre 77 peticiones. El transporte estaba sano en los
dos casos: `sender: sent` → `receiver: ack->ok` → `sender: ack`.

`pkmsg` es un `PreKeySignalMessage`: significa que **no había sesión Signal**
con el teléfono y la petición abre una nueva. La sesión desaparece porque, al
llegar un mensaje propio desde el LID, pywhats migra la sesión con
`migrate_pn_session_to_lid`, que termina con `sessions.delete(pn_key)` — y esa
es exactamente la dirección a la que va el ON_DEMAND.

## Las tres correcciones

### 1. `<device-identity>` en los `pkmsg`

`app/compat/peer_message.py` reestructuraba la stanza con `node.content =
[enc]`, quitando **siempre** el `<device-identity>` que pywhats había puesto.

Con un `msg` da igual: la sesión ya está establecida. Con un `pkmsg` no: ese
nodo lleva la `ADVSignedDeviceIdentity`, la firma con la que el teléfono
comprueba que la clave de identidad que viaja dentro del prekey es de un
companion suyo. Sin ella no puede aceptar la sesión nueva, y el resultado es
indistinguible de "el teléfono no contestó".

Ahora el nodo se conserva **solo** cuando el `enc` es `pkmsg`. La forma que
funcionó 73 veces (`msg` desnudo) no cambia.

### 2. Una petición cada vez, de verdad

`_busy` protegía `run()` y `run_canary()`, pero `SeedBackfillQueue` llama a
`_process_chat` directamente y se saltaba esa bandera. Medido:

```
14:45:26  119627…@lid desperto: se le pide historial ahora   (cola)
14:45:28  candidatos=27 con_cursor=2 → REQUEST 250482…@lid   (ciclo)
14:46:11  TIMEOUT 119627…@lid
14:46:14  TIMEOUT 250482…@lid
```

Dos peticiones en vuelo con dos segundos de diferencia. El teléfono atiende de
una en una, y dos respuestas cruzadas no se pueden atribuir. Ahora hay un
`asyncio.Lock` alrededor de envío + espera, por el que pasan **todos** los
caminos.

### 3. `SUSPECT` dejó de ser una puerta de un solo sentido

`_confirm_capability()` solo escribía cuando no había registro para la sesión.
Una vez guardado `{"confirmed": true, "state": "SUSPECT"}`, un canary que
funcionara después no borraba el `state`: la capacidad seguía `SUSPECT` hasta
desvincular. Ahora una respuesta buena la devuelve a `CONFIRMED` y limpia la
racha de timeouts.

## Correlación de la respuesta

Dos reglas nuevas en `notify_history`:

- **Solo un `ON_DEMAND` resuelve una espera de ON_DEMAND.** Un
  `INITIAL_BOOTSTRAP` puede traer el mismo chat mientras hay una petición en
  vuelo; contarlo como respuesta convierte historial que venía solo en "el
  protocolo funciona".
- **Primero por `peerDataRequestSessionID`** (campo 12 del aviso, donde el
  teléfono devuelve el identificador de nuestra stanza), y solo después por
  JID de chat. El identificador es exacto; el JID es lo único que queda cuando
  el teléfono no lo manda.

Una respuesta ON_DEMAND que no encuentra a quien despertar ya no se descarta
en silencio:

```
[BACKFILL] HistorySync ON_DEMAND recibido sin waiter correlacionable session=… 
```

El waiter se registra **antes** de enviar, y hay una prueba que lo comprueba
sobre el AST: las respuestas que funcionaron llegaron en ~1 s, y registrarlo
después abre una ventana en la que la respuesta no encuentra a nadie.

## El aviso, antes del parser

`HistorySyncer.handle` captura cualquier excepción de descarga y vuelve sin
más. Un aviso cuyo blob no se pudiera descargar no dejaba ni una línea en el
log de la aplicación, y el síntoma era idéntico a un timeout.

Ahora cada aviso deja constancia **antes** de la descarga:

```
HISTORY_SYNC_NOTIFICATION type=ON_DEMAND chunk=0 bytes=1234 session=A1B2C3D4...
```

Con `PROTOCOL_DEBUG=true` se ve además el tramo completo:

```
[ON_DEMAND] waiter_registered chat=…
[ON_DEMAND] request_sent chat=… enc=msg
[ON_DEMAND] ack_received ack_ms=122
[ON_DEMAND] correlation_ok chat=… via=chat_jid latencia=1.20s
```

## La herramienta

```
py tools/diagnose_ondemand.py
```

Solo lectura: no envía nada, no escribe en la base y no toca la sesión.
Informa de la huella de sesión, el estado de la capacidad, el destino
resuelto, los dispositivos propios registrados, si hay sesión Signal con el
teléfono (y por tanto si la próxima petición saldrá como `msg` o `pkmsg`), el
cursor elegido con su unidad de tiempo, y valida la forma de la petición campo
a campo sin emitirla.

Para mandar **una** petición real hacen falta las dos banderas:

```
py tools/diagnose_ondemand.py --send-one --si-quiero-enviar-de-verdad
```

Usa la sesión que ya tiene `service.py` en marcha (por
`POST /api/v1/diagnostics/ondemand/canary`); no abre una segunda.

## Lo que el Web Companion NO cambió

Vincularlo añadió el dispositivo 94 a la cuenta:

```
dispositivos propios   [0, 92, 94]
```

El destino del ON_DEMAND no se resuelve por usync: se construye desde nuestro
propio JID poniendo `device=0`, y `_target_devices` está parcheado para
devolver ese destino tal cual, sin fanout. Que existan el 92 y el 94 no puede
desviarlo, y hay pruebas que lo fijan.

## Privacidad

Ni el log ni la herramienta imprimen texto de mensajes, nombres, teléfonos
completos, identificadores de mensaje completos, QR, cookies, tokens ni
material criptográfico.

## El experimento known-good

Cuando todo lo comprobable en local está bien y la petición sigue sin
responder, lo único que queda es medir. `history_requests` guarda **88
peticiones que WhatsApp respondió de verdad**, con su ancla completa. Repetir
una de ellas sobre el mismo chat, y compararla con el ancla que el motor
usaría hoy, separa las dos hipótesis que quedan.

```bash
py tools/diagnose_ondemand_known_good.py                    # solo lectura
py tools/diagnose_ondemand_known_good.py --send-known-good  # ancla histórica
py tools/diagnose_ondemand_known_good.py --send-current     # ancla de hoy
```

Las banderas son mutuamente excluyentes: una ejecución, una petición.

El camino de diagnóstico (`BackfillService.request_diagnostico`) usa el mismo
constructor, el mismo destino, el mismo waiter y la misma correlación que la
excavación normal — un camino especial podría funcionar ahí y fallar en
producción, y entonces no habría medido nada. Lo que no hace: escribir cursor,
cambiar `history_status`, incrementar intentos, añadir fila a
`history_requests` ni persistir mensajes. Mientras corre toma `_busy`, así que
ni el ciclo automático ni la cola de despertados ni un canary pueden emitir
otra petición.

Los blobs `ON_DEMAND` que lleguen durante la prueba se observan y **no** se
persisten; quedan archivados en `data/history/` y se pueden ingerir después.
Un `INITIAL_BOOTSTRAP` que llegue a la vez se guarda como siempre.

### Cómo leer el resultado

| current | known-good | Lectura |
|---|---|---|
| timeout | responde | el problema son los **cursores**, no el protocolo |
| responde | responde | ON_DEMAND **está sano**; los timeouts fueron transitorios |
| timeout | timeout | cambió la **sesión o el servidor**: una petición históricamente válida ya no obtiene respuesta |

Sólo una `HISTORY_SYNC_NOTIFICATION` correlacionada devuelve la capacidad a
`CONFIRMED`. Un ACK no.
