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
main.py                 punto de entrada
alembic.ini             migraciones (sin secretos: la URL sale de .env)
app/
  config.py             carga y valida .env; nunca expone la password
  logging_setup.py      logs [APP] [DB] [WA] ... + filtro anti-secretos
  database.py           motor y transacciones de PostgreSQL
  models.py             esquema (7 tablas)
  repository.py         upserts en lote, paginacion, cursor historico
  whatsapp_client.py    pywhats en su propio hilo + event loop
  qr_render.py          imagen del QR
  gui.py                Tkinter (hilo principal, una sola ventana)
  compat/               adaptaciones sobre pywhats 0.2.0
migrations/             Alembic
session/                estado del companion (NO versionado)
data/                   media, cache, blobs de history (NO versionado)
diagnostics/            logs y sesiones archivadas (NO versionado)
tests/
```

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

Este proyecto no promete un "backup 100% completo": el objetivo es recuperar y
preservar **todo lo que el protocolo permita obtener**, que no es lo mismo.
