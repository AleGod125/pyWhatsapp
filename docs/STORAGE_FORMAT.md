# Formato de los archivos

Todo archivo empieza igual:

```
"WABK" | uint16 longitud | cabecera JSON | contenido
```

## Cabecera

```json
{
  "format_version": 1,
  "encryption_version": 1,
  "compression": "gzip",
  "entity": "message_segment",
  "chunked": false,
  "plaintext_size": 128400,
  "chunk_size": null
}
```

Va **en claro** —hay que poder leerla para saber cómo leer el resto— pero
**autenticada**: entra como AAD del cifrado, así que manipularla hace fallar el
descifrado.

Un `format_version` desconocido da un error claro en vez de intentar leerlo:
*"lo escribió una versión posterior de la aplicación"*.

## Segmentos de mensajes

`segment-000001.jsonl.gz.enc` — el relleno de ceros hace que el orden
alfabético en Drive coincida con el cronológico.

```
JSONL → gzip (mtime=0) → AES-256-GCM (una pieza)
```

`mtime=0` a propósito: con la marca de tiempo por defecto, comprimir dos veces
lo mismo da archivos distintos y la huella deja de servir para comparar.

Una línea por mensaje:

```json
{"v":1,"id":8421,"wamid":"3A1F...","timestamp":1788460000,
 "from_me":false,"sender":"573...@s.whatsapp.net","type":"text",
 "text":"...","media_ref":42,"raw_proto_b64":"CgQI...","metadata":{...}}
```

`raw_proto` viaja **aquí**, no en PostgreSQL: es lo que permite reinterpretar
un mensaje cuando el parser mejore, sin que la base cargue con ese peso.

`media_ref` apunta al adjunto. Cuando la multimedia termina de subir **no se
reescribe el segmento**: la relación vive en la base.

## Multimedia

`media-000000042.bin.enc`

```
[ cabecera ]
[ nonce | trozo 1 cifrado | tag ]   ← 1 MiB en claro
[ nonce | trozo 2 cifrado | tag ]
...
```

Troceado porque AES-GCM no se descifra por la mitad. Un rango de bytes se
traduce a un rango de trozos:

```
trozo_primero = inicio // 1MiB
byte_inicial  = len(cabecera) + trozo_primero * (12 + 1MiB + 16)
```

El tamaño cifrado se puede **predecir** antes de subir, que es lo que exige la
subida reanudable de Drive: hay que declarar el total por adelantado.

## appProperties

```
app=whatsapp_backup   entity=message_segment|media
account_id=<uuid>     chat_id=<id>
segment_id=<uuid>     sequence=<n>        media_id=<id>
```

Existen para poder reconstruir el índice sin PostgreSQL: un directorio de
archivos cifrados no dice a qué conversación pertenece cada uno. **Nunca**
llevan contenido, teléfonos ni nada sensible.

## Versionado

Cada archivo dice qué versión es. No se asume formato eterno: `format_version`
y `encryption_version` permiten leer lo antiguo mientras se escribe lo nuevo.

`encrypted` se guarda **por segmento**, no por configuración: si el cifrado se
apaga o se enciende, hay que saber cómo leer **cada** archivo, no suponerlo por
los ajustes de hoy.
