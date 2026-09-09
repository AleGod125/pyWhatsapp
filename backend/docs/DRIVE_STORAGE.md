# Almacenamiento en Google Drive

## El reparto

**PostgreSQL** guarda el índice: quién, cuándo, de qué chat, en qué segmento y
en qué línea. **Drive** guarda el contenido: el texto de los mensajes y los
archivos.

Así la base se mantiene manejable aunque el backup llegue a cientos de
gigabytes, y sigue sirviendo para listar, paginar y buscar sin tocar la red.

## Estructura en Drive

```
WhatsApp Backup/                     ← carpeta raíz, en el Drive del usuario
  accounts/
    <whatsapp_account_uuid>/
      chats/
        <chat_id>/
          messages/
            segment-000001.jsonl.gz.enc
            segment-000002.jsonl.gz.enc
          media/
            media-000000042.bin.enc
```

Nombres con UUID e identificadores internos. **Ningún número de teléfono, ni
nombre de contacto, ni nada legible** en los nombres de carpeta: quien vea el
Drive por encima del hombro no aprende con quién habla el usuario.

## Por qué segmentos y no un archivo por mensaje

Un mensaje por archivo daría millones de archivos. Listar una carpeta así es
lento, recorrerla es caro y los límites de peticiones de la API se agotan
enseguida. Un segmento de mil mensajes convierte mil subidas en una.

Un segmento se cierra cuando llega a **cualquiera** de los tres límites:

| Límite | Por defecto | Por qué |
|---|---|---|
| mensajes | 1000 | tamaño manejable |
| bytes | 5 MB | antes de comprimir |
| **edad** | 60 s | **sin esto, un chat con poco tráfico no subiría nunca** |

Cerrado es **inmutable**. No se vuelve a descargar ni reescribir: los mensajes
nuevos van al siguiente. Reescribir un archivo de 100 MB por cada mensaje
significaría bajarlo, modificarlo y volver a subirlo entero.

## El camino de un mensaje

```
WhatsApp
   ↓
normalizar → deduplicar
   ↓
PostgreSQL: índice + storage_job   ← MISMA transacción
   ↓                                  (patrón outbox)
   commit
   ↓
DriveStorageWorker (hilo aparte)
   ↓
JSONL → gzip → AES-256-GCM
   ↓
Drive
   ↓
DB: storage_status = ready
```

**El trabajo se crea en la misma transacción que el mensaje.** Si el proceso
muere entre las dos cosas, no puede quedar un mensaje sin subir del que nadie
se acuerde.

**Nunca se sube desde el hilo que recibe de WhatsApp.** Una subida puede tardar
segundos; bloquear ese hilo haría que llegaran mensajes que nadie atiende y el
socket acabaría cayéndose.

## Multimedia

```
descarga temporal → SHA-256 → cifrado por trozos → subida reanudable → Drive
                                                                        ↓
                                             cache local (LRU + TTL) ←──┘
```

La copia local es **caché**, no el original. Se desaloja por tamaño y por edad
— pero **solo** lo que cumple las tres condiciones: subido, con identificador
guardado y con tamaño comprobado. Con dos de tres, un archivo truncado en Drive
se convertiría en contenido perdido.

Archivos grandes van por **subida reanudable**, leyendo del disco por trozos:
`file.read()` sobre un vídeo de 5 GB reservaría 5 GB de RAM.

## Servir multimedia

`GET /api/v1/media/<id>/file` no cambia. Angular no sabe que existe Drive.

1. ¿Hay caché local? Se sirve de ahí — más rápido y no gasta cupo de Google.
2. Si no, se lee de Drive respetando `Range`.

Como AES-GCM no se descifra por la mitad, la multimedia se cifra en **trozos de
1 MiB independientes**. Un rango de bytes se traduce a un rango de trozos y solo
se descargan esos: servir diez segundos de un vídeo no baja los 2 GB. Hay un
test que lo mide.

## Cola de trabajos

Estados: `pending → processing → complete`, con `failed` y `paused`.

| Situación | Qué pasa |
|---|---|
| 500 / timeout | reintento con espera creciente: 5s, 15s, 45s, 2m, 5m, 15m + dispersión |
| 429 | se respeta `Retry-After` — adelantarse solo consigue otro rechazo |
| Sin espacio | se reintenta y se avisa; **no** es un fallo de WhatsApp |
| 401 revocado | **todo en pausa**, se pide reconectar; reintentar no arregla un acceso revocado |
| 12 intentos | `failed`; el contenido **sigue** en PostgreSQL y se puede reencolar |

`UNIQUE(job_type, entity_id)`: un solo trabajo vivo por entidad. Sin eso, cada
reintento crearía un trabajo nuevo y acabarían subiéndose copias del mismo
segmento.

## Recuperación tras una caída

Al arrancar, los trabajos que quedaron en `processing` más de 5 minutos vuelven
a `pending`: el trabajador que los tenía ya no existe. El margen evita robarle
un trabajo a uno que sigue vivo y solo va lento.

El contenido del segmento se **rehace desde PostgreSQL**: el segmento en
memoria desaparece, pero sus mensajes siguen en la base y el trabajo dice
cuáles eran.

## Contrapresión

Si Drive lleva días caído, lo pendiente no puede crecer sin límite.
`MAX_PENDING_STORAGE_BYTES` (10 GB) marca el tope; al llegar, estado
`STORAGE_BLOCKED` y aviso. **No se borra ningún mensaje.**

## Reconstruir sin PostgreSQL

Cada archivo lleva `appProperties` con `app`, `entity`, `account_id`,
`chat_id`, `segment_id` y `sequence`. Cada segmento guarda además su
`chat_jid`. Con eso se puede rehacer el índice.

Un índice se reconstruye; un contenido perdido no. Por eso la redundancia está
del lado de Drive.
