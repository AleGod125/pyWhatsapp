# whatsapp_backup

Backup local de una cuenta propia de WhatsApp mediante un **dispositivo
vinculado (companion)** real, hablando el protocolo multi-device nativo.

Sin navegador, sin Selenium/Playwright, sin WhatsApp Web, sin Node.js. El
cliente de protocolo es [`pywhats`](https://github.com/sanjay3290/pywhats);
la base de datos del backup es **PostgreSQL**.

> Proyecto experimental, para uso con la cuenta propia.

---

## Requisitos

| Componente | Version verificada |
|---|---|
| Python | 3.11.9 |
| PostgreSQL | 18.6 |
| Sistema | Windows 11 (el codigo no asume Windows) |

## Instalacion (Windows)

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Base de datos

Crear la base (una vez):

```sql
CREATE DATABASE whatsapp_backup;
```

Copiar la plantilla de configuracion y completarla:

```powershell
copy .env.example .env
```

En `.env` hay que rellenar al menos `POSTGRES_PASSWORD`. `DATABASE_URL` tiene
prioridad si se define; si se deja vacia se construye desde las variables
`POSTGRES_*`.

Aplicar las migraciones:

```powershell
python -m alembic upgrade head
```

Comprobar que todo responde antes de tocar WhatsApp:

```powershell
python main.py --check
```

## Ejecucion

Hay DOS entrypoints. Los dos usan el MISMO `.env` y los MISMOS servicios.

```powershell
py main.py       # ventana Tkinter
py service.py    # API HTTP en http://127.0.0.1:5000/api/v1
```

```
                       +-- main.py     -> Tkinter   (app/gui)
    AppRuntime/Services +
                       +-- service.py  -> Flask     (app/api)
```

`AppRuntime` (`app/core/runtime.py`) monta configuracion, base de datos, sesion
de WhatsApp, ingesta, backfill, multimedia y mantenimiento. Los dos entrypoints
solo le ponen una interfaz delante; el cableado NO esta duplicado, y hay una
prueba que lo verifica.

### Un solo dueno de la sesion

`main.py` y `service.py` no pueden abrir el companion a la vez: dos procesos
escribiendo en `session/device.json` y en la base Signal producen una sesion
corrupta y obligan a volver a vincular. Lo impide `session/runtime.lock`.

El segundo proceso sale con codigo 5 y un mensaje que dice QUIEN tiene la
sesion y que hacer. Si un proceso murio de mala manera, su cerrojo se detecta
como huerfano (se comprueba si el PID sigue vivo) y se retira solo ESE archivo:
`device.json` y la base Signal no se tocan nunca.

Leer la copia local NO pide el cerrojo. Con la sesion abierta en otro proceso:

```powershell
py main.py --viewer      # ventana, solo lectura
py service.py --local    # API, solo lectura
```

### main.py

```powershell
python main.py
```

- **Primera vez**: se abre una ventana con un codigo QR. En el telefono:
  WhatsApp -> Dispositivos vinculados -> Vincular un dispositivo -> escanear.
- **Siguientes veces**: detecta `session/device.json` y hace login directo,
  sin QR.

Opciones:

| Flag | Efecto |
|---|---|
| `--check` | Verifica entorno, PostgreSQL, migraciones y compatibilidades. No conecta a WhatsApp. |
| `--no-gui` | Sin ventana. Util para ver el flujo en logs; el QR no se puede escanear. |
| `--fresh` | Archiva la sesion actual en `diagnostics/` y vuelve a vincular. **No** borra PostgreSQL, ni multimedia, ni diagnosticos. |
| `--viewer` | Solo el visor sobre PostgreSQL. No conecta ni pide el cerrojo. |

### service.py

```powershell
py service.py
```

| Flag | Efecto |
|---|---|
| `--check` | Verifica entorno, PostgreSQL y migraciones. No conecta ni escucha. |
| `--local` | Sirve la copia local sin abrir la sesion de WhatsApp. |
| `--host` / `--port` | Sobrescriben `API_HOST` / `API_PORT` solo en esa ejecucion. |

El servidor HTTP **no espera a WhatsApp**: la sesion se abre en un hilo aparte
y el puerto queda escuchando de inmediato. `GET /api/v1/session` responde
`CONNECTING` mientras tanto.

#### Endpoints

| Metodo | Ruta | Que hace |
|---|---|---|
| GET | `/api/v1/health` | Comprobacion ligera. No recorre la tabla de mensajes. |
| GET | `/api/v1/session` | Estado real de la sesion. |
| POST | `/api/v1/session/pair` | Arranca la vinculacion. |
| GET | `/api/v1/session/qr` | Si hay QR vigente. **No** devuelve el payload. |
| GET | `/api/v1/session/qr/image` | El QR como PNG. |
| GET | `/api/v1/chats` | Sidebar, por `last_message_timestamp` DESC. |
| GET | `/api/v1/chats/<id>` | Un chat con sus cifras. |
| GET | `/api/v1/chats/<id>/messages` | Ultimos N, o anteriores a un cursor. |
| GET | `/api/v1/media/<id>` | Metadatos del adjunto. |
| GET | `/api/v1/media/<id>/file` | El archivo. `410` si ya no esta en WhatsApp. |
| GET | `/api/v1/media/<id>/thumbnail` | Miniatura cacheada. |
| GET | `/api/v1/sync/status` | Progreso del trabajo de fondo y del ciclo manual. |
| POST | `/api/v1/sync/run` | Lanza un ciclo manual. `202`, o `409` si ya hay uno. |
| GET | `/api/v1/events/stream` | Server-Sent Events. |

Paginacion por **keyset**, nunca OFFSET:

```
GET /api/v1/chats/12/messages?limit=200
    -> next_cursor: {before_timestamp, before_id}
GET /api/v1/chats/12/messages?limit=200&before_timestamp=...&before_id=...
```

Las respuestas NUNCA llevan una ruta del disco: el frontend recibe URLs
(`/api/v1/media/123/file`), no rutas de Windows. Tampoco sale `raw_proto`, ni
`media_key`, ni el payload del QR en texto. Hay pruebas que lo comprueban.

Eventos SSE: `session.state`, `session.qr`, `chat.updated`, `message.created`,
`media.updated`, `history.progress`, `backfill.progress`, `sync.status`.

### Mensajes en tiempo real

Mientras `py service.py` este conectado, los mensajes nuevos entran SOLOS. No
dependen del boton de sincronizar:

```
receptor WhatsApp -> live_service -> clasificador -> PostgreSQL
                  -> bus de eventos -> SSE -> frontend
```

El orden no es casual: primero se persiste, luego se avisa. Al reves, el
frontend podria pedir un mensaje que todavia no existe.

Un mensaje nuevo produce DOS eventos SSE:

```
event: message.created      {"chat_id": 123, "message": { ...burbuja completa... }}
event: chat.updated         {"chat_id": 123, "preview": "...",
                             "last_message_at": "...", "message_count": 453}
```

`message.created` lleva la burbuja entera, para poder anadirla sin volver a
preguntar. Y un DUPLICADO no emite nada: History Sync y el receptor en vivo se
solapan a proposito, la base deduplica por wamid, y dejar pasar el aviso haria
aparecer la burbuja dos veces.

Cuando un adjunto termina de descargarse:

```
event: media.updated        {"media_id": 9, "message_id": 5, "status": "downloaded",
                             "file_url": "...", "thumbnail_url": "..."}
```

El bus entrega el MISMO objeto a todos los suscriptores, asi que la traduccion
se memoriza en el evento: cinco pestanas abiertas hacen UNA consulta, no cinco.

### Sincronizacion manual

`POST /api/v1/sync/run` lanza el ciclo que NO es automatico: reconciliar,
revalidar cursores, pedir historial pendiente y recoger multimedia.

```
202  {"started": true, "job_id": "a1b2c3", "state": "running"}
409  SYNC_ALREADY_RUNNING     ya hay un ciclo en curso
409  WHATSAPP_DISABLED        el backend esta en modo --local
409  SESSION_NOT_CONNECTED    WhatsApp no esta conectado
```

Responde y vuelve: el ciclo puede durar minutos y corre en el event loop del
cliente. `GET /sync/status` cuenta como va (`state`, `phase`, `chats_total`,
`chats_processed`, `messages_new`, `media_pending`, `last_error`), y lo mismo
llega por SSE como `sync.status`.

No ejecuta NINGUN script: llama a `MaintenanceService`, `BackfillService` y
`MediaService` directamente. Lanzar `probe_chat.py` como subproceso abriria un
segundo proceso que pelearia por el cerrojo de la sesion con este. Hay una
prueba que lo verifica sobre el AST.

### Vinculacion automatica

Si el backend arranca sin sesion valida, el QR se genera SOLO. Nadie tiene que
pulsar nada:

```
[WA]      Estado: STARTING -> NO_SESSION (no hay device.json)
[APP]     Se requiere vinculacion: iniciando automaticamente
[WA]      Estado: NO_SESSION -> PAIRING
[PAIRING] Vigilante de vinculacion en marcha (TTL=300s)
[PAIRING] QR generation=1 expires_in=300s
[WA]      Estado: PAIRING -> QR_READY
```

`GET /session/qr` devuelve `generated_at`, `expires_at`, `expires_in_seconds`,
`generation` y `ttl_seconds`. **Nunca el payload**: es una credencial de
vinculacion, vive solo en memoria, no se escribe en disco y no se registra.

`GET /session/qr/image` sirve el PNG con `no-store` y `Pragma: no-cache`, y
responde `410 QR_EXPIRED` si caduco (distinto de `404 QR_NOT_AVAILABLE`, que
es "no hubo ninguno").

`PAIRING_QR_TTL` (300 s por defecto) es una ventana de EXPERIENCIA, no la vida
del `ref`. pywhats lo rota antes —el primero a los 60 s, luego cada 20 s— y
cada rotacion publica un QR nuevo, sube `generation` y emite `session.qr`. El
TTL es el tope: pasado ese plazo sin ninguno nuevo, se pide otra vinculacion
sola. Reportar 300 s y no renovar seria mentir; lo que hace cierto el numero es
la renovacion.

`POST /session/pair` sigue existiendo como reintento manual y es idempotente:
con un QR vigente devuelve ese, sin generar otro. Un cerrojo interno impide dos
vinculaciones simultaneas, que produirian dos QR de los que solo uno serviria.

Al conectar, el QR se invalida y el vigilante se para.

Estados de sesion: `STARTING`, `CHECKING_SESSION`, `NO_SESSION`,
`PAIRING_REQUIRED`, `PAIRING`, `QR_READY`, `CONNECTING`, `CONNECTED`,
`WAITING_INITIAL_HISTORY`, `SESSION_INVALID`, `DISCONNECTED`, `ERROR`.

Perder la conexion da `DISCONNECTED`, no `SESSION_INVALID`: al reconectar se
vuelve a procesar todo sin pasar por vincular.

El frontend vivira en un repositorio aparte (`WhatsappBackup`). Este proyecto
es el backend; aqui no hay nada de Angular.

`--fresh` separa a proposito el *reset de sesion* del *reset de base de datos*.
No existe ninguna opcion que borre PostgreSQL por accidente.

---

## PostgreSQL vs. Signal Store: no son lo mismo

Es la distincion mas importante del proyecto.

| | Signal Store / Companion | PostgreSQL |
|---|---|---|
| Donde | `session/device.json`, `session/device.json.signal.db` | base `whatsapp_backup` |
| De quien | de **pywhats** / del protocolo | de **esta aplicacion** |
| Que guarda | credenciales del dispositivo, sesiones Signal, prekeys, identidades | chats, mensajes, contactos, multimedia, estado del backfill |
| Se puede borrar | solo con `--fresh`, y se archiva | solo a mano y a proposito |

`session/device.json.signal.db` es SQLite **porque asi lo implementa pywhats**.
No es la base de datos del backup y no se migra a PostgreSQL. La aplicacion no
usa SQLite para chats ni mensajes en ningun punto.

`session/compat_prekey.db` es un tercer archivo, propio de nuestra capa de
compatibilidad (ver mas abajo). Tampoco es el Signal Store.

---

## Estructura

```
main.py                 entrypoint Tkinter
service.py              entrypoint Flask (API HTTP)
alembic.ini             migraciones (sin secretos: la URL sale de .env)
.env                    UNA sola configuracion para los dos entrypoints

app/
  core/                 lo que la aplicacion ES, sin interfaz
    config.py           carga y valida .env; nunca expone la password
    logging_setup.py    logs [APP] [DB] [WA] [API] ... + filtro anti-secretos
    database.py         motor y transacciones de PostgreSQL
    runtime.py          AppRuntime: la capa que comparten ambos entrypoints
    orchestrator.py     arranque, trabajo de fondo y cierre
    lock.py             cerrojo de sesion entre procesos
    identity.py         identidad propia y enmascarado de identificadores
    session_state.py    maquina de estados de la sesion
    message_parser.py   normalizacion de WebMessageInfo
    message_classifier.py  visible vs. control del protocolo
    system_message.py   que significa un evento de sistema
    previews.py         vista previa de un mensaje
    avatars.py          iniciales y color por contacto
    qr_render.py        imagen del QR
  services/             logica de negocio; no conoce ninguna interfaz
    repository.py       upserts en lote, paginacion, cursor historico
    history_service.py  ingesta de History Sync
    history_gate.py     barrera del historial inicial
    backfill_service.py recuperacion ON_DEMAND
    media_service.py    descarga de adjuntos
    live_service.py     mensajes en vivo
    contacts_service.py nombres de la agenda
    maintenance_service.py  reconciliacion automatica NO destructiva
    thumbnails.py       miniaturas cacheadas
  models/               esquema PostgreSQL + protobuf propio
    schema.py           las 7 tablas
    proto/              descriptores que pywhats no trae
  events/               bus de eventos con reparto a varios consumidores
  gui/                  adaptador Tkinter
    app_window.py, chat_view.py, ui_theme.py
  api/                  adaptador HTTP (Flask + SSE)
    app_factory.py, routes.py, serializers.py
  compat/               adaptaciones sobre pywhats 0.2.0
  whatsapp_client.py    pywhats en su propio hilo + event loop

scripts/                herramientas de diagnostico, se ejecutan a mano
  inspect_db.py  inspect_history_gaps.py  probe_chat.py
  repair_db.py   ingest_blobs.py

migrations/             Alembic
session/                estado del companion + runtime.lock (NO versionado)
data/                   media, cache, blobs de history (NO versionado)
diagnostics/            logs y sesiones archivadas (NO versionado)
tests/
```

Las dependencias van en un solo sentido:

```
    api  ---+
            +--> services --> models
    gui  ---+        |
                     +--> core
```

`app/api` no importa `tkinter` ni `app.gui`. `app/gui` no importa `flask` ni
`app.api`. `app/services` no importa ninguno de los dos. `app/core` no importa
`tkinter`. Hay una prueba por cada una de esas reglas, y miran el AST, no el
texto: los comentarios que explican la regla mencionan la palabra prohibida.

---

## Compatibilidades sobre pywhats 0.2.0

`pywhats 0.2.0` (2026-07-10) es la ultima version publicada. Los cinco puntos
siguientes se verificaron **sobre el paquete instalado**, no sobre su
documentacion. Cada uno se corrige en `app/compat/`, en memoria y al arrancar.
Nunca se edita `.venv/Lib/site-packages/`.

Cada adaptacion tiene su flag en `.env` y puede desactivarse para diagnostico.

### 1. `COMPAT_WA_VERSION` — version de WhatsApp Web

`pywhats/version.py:19` lleva fija `WA_WEB_VERSION = (2, 3000, 1035194821)`.
El servidor la rechaza con `PairingFailed reason=405`. La revision real se
obtiene de `https://web.whatsapp.com/sw.js` (campo `client_revision`).

Dos detalles que hacen falta para que funcione:

- `sw.js` responde **HTTP 400** a una peticion normal. Requiere las cabeceras
  con que el navegador registra un service worker (`Sec-Fetch-Dest:
  serviceworker`, `Service-Worker: script`, `Referer`).
- `pywhats/pairing.py:83-88` importa `WA_WEB_VERSION` **a nivel de modulo**,
  o sea por copia. Parchear solo `pywhats.version` no cambia nada: hay que
  parchear tambien `pywhats.pairing`, y antes de construir el `Client`.

Si no hay revision en vivo ni cache valida, el pairing **se aborta**. No se
continua en silencio con una revision obsoleta.

### 2. `COMPAT_WINDOWS_STORE` — persistencia en Windows

`pywhats/store.py:359` usa `os.fchmod()`, que no existe en Windows. El pairing
llega a `pair-success verified` y revienta al guardar: el telefono queda
vinculado pero la PC no conserva credenciales.

Se reimplementa `save_device_store` conservando la escritura atomica
(`mkstemp` -> permisos -> write -> flush -> fsync -> `os.replace` -> fsync del
directorio) y omitiendo `fchmod` solo donde no existe. En POSIX se sigue
aplicando `0600`. El camino de lectura no necesita parche: ya es Windows-aware.

### 3. `COMPAT_PAIRING_515` — restart tras el pairing

`Pairer.run` retorna nada mas enviar `pair-device-sign`, y `Client._run_pairing`
cierra el socket en su `finally`, sin esperar el `<stream:error code="515"/>`
que el propio codigo de pywhats documenta como la senal de reconectar.

Se mantiene el socket vivo hasta recibir el restart o agotar un plazo corto.
El 515 posterior al pairing **no es un error fatal**: es el restart esperado.
Un timeout tampoco aborta nada; se registra y se continua.

### 4. `COMPAT_PREKEY_REPLAY` — PreKeySignalMessage repetido

`pywhats/messaging/receiver.py:767` ejecuta X3DH en **todos** los `pkmsg`, sin
comprobar si esa base key ya establecio la sesion. Cuando WhatsApp reenvia
mensajes del mismo establecimiento, pide de nuevo una OPK que ya se consumio
correctamente y lanza `unknown one-time pre-key id`.

Se aplica la semantica de libsignal: si el mensaje pertenece a un
establecimiento ya registrado para esa `(session_id, base_key, identity_key)` y
la sesion sigue viva, se reutiliza el ratchet sin rehacer X3DH ni tocar la OPK.
**Si la base key es distinta, es un rekey y se ejecuta X3DH normalmente.**

No se aplica el atajo inseguro "si hay cualquier sesion, ignora la OPK". El MAC
se verifica exactamente igual que en el camino original. Hay tests que lo
comprueban, incluido uno que corrompe el MAC y exige que siga fallando.

El registro `(session_id -> base_key)` vive en `session/compat_prekey.db`, un
SQLite **nuestro**. El Signal Store de pywhats no se toca ni se duplica.

### 5. `COMPAT_HISTORY_MESSAGES` — mensajes del History Sync

`pywhats/history.py` descarga y descifra el blob pero solo lo cuenta; su propio
docstring dice *"kept opaque for now and only counted"*. El evento
`history_sync` lleva un resumen sin los mensajes.

Se lee el protobuf **ya descifrado por pywhats** para obtener conversaciones y
mensajes. No se interceptan claves ni se reimplementa nada de History Sync.
Cada blob inflado se archiva ademas en `data/history/` antes de interpretarlo,
para que un fallo de normalizacion no cueste historial.

`HistorySyncMsg.message` esta declarado como `bytes` porque pywhats **no define
`WebMessageInfo`** en ninguna parte. Esos bytes son el `WebMessageInfo`
serializado y se conservan en `messages.raw_proto`.

---

## Mantenimiento automatico, y la linea que no cruza

`main.py` se mantiene solo. Al arrancar, y luego cada
`MAINTENANCE_INTERVAL_SECONDS`, `MaintenanceService` reconcilia lo que se
deriva de los datos:

- contadores por chat y estado historico que falte (permite reanudar el
  backfill donde se quedo);
- el cursor ON_DEMAND de cada chat;
- mensajes que quedaron `unknown` y que ahora si se saben interpretar, a
  partir del `raw_proto` ya guardado;
- adjuntos marcados como descargados cuyo archivo ya no esta (se reintentan) y
  adjuntos sin `direct_path` ni `media_key`, que se marcan terminales;
- correspondencia telefono <-> LID, leida de los blobs de History Sync ya
  archivados (no se pide nada al servidor);
- vistas previas del sidebar.

Todo eso es **recalcular**. Lo que NO hace, nunca y por diseno: `DELETE`,
merges destructivos, borrar mensajes `unknown`, borrar `raw_proto` o tocar la
sesion. El modulo no importa `delete` de SQLAlchemy y hay una prueba que lo
verifica, igual que hay otra que comprueba que ninguna ruta automatica invoca
`repair_db.py`.

`repair_db.py` sigue siendo la unica via para operaciones destructivas, se
ejecuta a mano y pide autorizacion explicita.

### El worker de multimedia no para

Un adjunto que aparece despues del arranque ya no espera a un reinicio:

```
mensaje -> media_files(pending) -> worker -> descarga -> aviso a la GUI
```

El worker vive en su propia tarea con concurrencia
`MEDIA_DOWNLOAD_CONCURRENCY`. La interfaz nunca espera por el.

### El arranque ya no pierde tres minutos

Una sesion ya sincronizada no vuelve a recibir el History Sync inicial, asi
que la barrera agotaba sus 180 segundos en cada arranque para acabar
escribiendo "sesion ya sincronizada". Ahora la confirmacion se persiste **por
huella de sesion** (SHA-256 truncado del JID y el device_id, nunca el
identificador en claro) y el arranque es inmediato:

```
[SYNC] Initial history ya confirmado para esta sesion; no se espera bootstrap
```

Un pairing nuevo cambia la huella y la espera vuelve, porque entonces si va a
llegar algo.

---

## Fidelidad de los datos

- `messages.raw_proto` (BYTEA) guarda el `WebMessageInfo` serializado. Un
  mensaje se puede reinterpretar en el futuro sin volver a pedirlo.
- `messages.timestamp` es el epoch en segundos **tal como lo entrega
  WhatsApp**.
- Deduplicacion: indice UNIQUE **parcial** sobre
  `(chat_jid, whatsapp_message_id) WHERE whatsapp_message_id IS NOT NULL`.
  Los mensajes sin ID real no colisionan entre si; su identidad es la clave
  primaria de PostgreSQL.
- `whatsapp_message_id` contiene **solo** IDs reales de WhatsApp. Un
  identificador generado localmente va en `synthetic_identifier`, nunca ahi.
  Un CHECK impide ademas guardar la cadena vacia.

### El cursor historico no es el mensaje mas antiguo

Para `HISTORY_SYNC_ON_DEMAND` hay que anclar la peticion en un ID que el
servidor **conozca**. Un ID sintetico produce un ACK y despues silencio.

Por eso se distinguen dos cosas:

| | Que es | Se usa para |
|---|---|---|
| `get_oldest_stored_timestamp()` | mensaje mas antiguo almacenado, tenga ID o no | estadisticas |
| `get_oldest_valid_history_cursor()` | mensaje mas antiguo **con ID real** | peticiones ON_DEMAND |

Pueden ser mensajes distintos. Si el del 13 de agosto no tiene ID real y el del
14 si, las estadisticas siguen diciendo "historial desde el 13" pero la
peticion se ancla en el del 14. Un chat sin ningun ID utilizable queda como
`no_valid_cursor`, que es un estado, no un error.

---

## Tests

```powershell
python -m pytest tests/ -q
```

Los tests de base de datos corren contra el **PostgreSQL real** del `.env`,
dentro de una transaccion que siempre se revierte. No se usa SQLite como
sustituto porque el comportamiento que importa (indice parcial, `ON CONFLICT`,
JSONB, BYTEA, CHECK) es especifico de PostgreSQL.

Los tests de la compatibilidad Signal construyen una sesion **real** con las
primitivas de pywhats y la hacen pasar por el `Receiver._decrypt_enc`
autentico: si el parche debilitara una verificacion, se veria.

---

## Seguridad

Nunca se escriben en el log: passwords, `DATABASE_URL` completa, claves
privadas, Noise keys, estado Signal, el payload del QR, tokens, ciphertext,
media keys ni blobs de protobuf. `app/logging_setup.py` incluye ademas un
filtro de redaccion como ultima red de seguridad.

`.gitignore` excluye `.env`, `session/`, `data/` y `diagnostics/`.
`.env.example` **si** se versiona, y no lleva secretos.

---

## Estado

Verificado automaticamente: configuracion, PostgreSQL, migraciones, esquema,
compatibilidades, generacion del QR, arranque de la GUI.

El pairing requiere escanear el QR fisicamente, asi que no puede darse por
validado sin observar los eventos reales `pair-success` / `paired` /
`connected`. Lo mismo aplica a History Sync, a `ON_DEMAND` y a la descarga de
multimedia: se dan por funcionando cuando se observan, no antes.

Verificado con datos reales de esta copia:

- scroll automatico hasta el final de lo almacenado en VirtualTec Marco
  (452/452), Tia Nore (355/355) y Mi Madre (246/246), sin pulsar el boton;
- 412 adjuntos descargados, todos presentes en disco y abribles;
- eventos de sistema: 67 de 93 interpretados desde su `messageStubType`; los
  26 restantes (stubs 177 y 193) se muestran como "Evento del sistema"
  conservando su numero y su `raw_proto`, porque no se ha podido confirmar que
  significan y no se van a inventar.

Sobre llamadas: en esta copia **no hay ni un solo registro de llamada**. Los
blobs de History Sync archivados no traen el campo `callLogRecords` y ningun
mensaje contiene `call` ni `callLogMesssage`. El soporte esta implementado
segun la definicion publica del protocolo y se activara cuando WhatsApp
entregue esos registros, pero hoy no hay nada que mostrar y decir lo contrario
seria falso.

Este proyecto no promete un "backup 100% completo": el objetivo es recuperar y
preservar **todo lo que el protocolo permita obtener**, que no es lo mismo.
