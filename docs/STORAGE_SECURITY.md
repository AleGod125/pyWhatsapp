# Seguridad del almacenamiento

## Por qué se cifra, si Google ya cifra

Google cifra el transporte y su disco — pero **Google tiene esa llave**. Estos
archivos son conversaciones privadas. Cifrando aquí, lo que llega a Drive es
opaco para Google, para quien acceda a esa cuenta de Google y para quien
consiga el enlace de un archivo.

## Dos claves, no una

| Clave | Qué es | Dónde vive |
|---|---|---|
| **DEK** | 256 bits, **una por usuario**. Cifra el contenido. | `user_storage_keys`, **envuelta** |
| **KEK** | Del servidor. Solo envuelve DEKs. | `.env` |

Con una sola clave para todos, un descuido abriría el contenido de todo el
mundo a la vez.

**La KEK no es `APP_ENCRYPTION_KEY`.** Esa protege los tokens de Google. Si la
misma clave cifrara además el contenido de todos los usuarios, comprometerla
abriría las dos cosas. Por defecto la KEK se **deriva** de ella con HKDF y una
etiqueta distinta — derivada no es la misma clave — y `STORAGE_KEK` permite
separarlas del todo.

> Limitación conocida: por defecto ambas cuelgan de la misma raíz. Para
> separación real, define `STORAGE_KEK`. La migración futura natural es un KMS
> o una clave derivada de contraseña del usuario.

## Formato

```
[ MAGIC "WABK" | longitud | cabecera JSON ]  ← en claro, AUTENTICADA
[ nonce | ciphertext | tag ]                 ← AES-256-GCM
```

La cabecera lleva `format_version`, `encryption_version`, `compression`,
`entity`, `chunked` y `plaintext_size`. **Nada sensible**: solo cómo leer el
resto. Va como AAD, así que manipularla hace **fallar** el descifrado en vez de
cambiar cómo se interpreta el archivo.

AES-GCM es cifrado **autenticado**: si un byte cambia, el descifrado falla en
vez de devolver basura.

### Troceado para multimedia

Cada trozo de 1 MiB lleva su propio nonce, y **su índice va en el AAD**. Sin
eso, reordenar o repetir trozos pasaría desapercibido, porque cada uno se
autentica por separado.

## Lo que NUNCA sube a Drive

- `device.json`, `device.json.signal.db`, `compat_prekey.db`
- Claves privadas, estado Signal, sesiones, prekeys
- Tokens de Google (ni access ni refresh)
- `APP_ENCRYPTION_KEY`, `STORAGE_KEK`, ninguna DEK
- Contraseñas ni sus hashes

Una copia con conversaciones no puede llevar además con qué descifrarlas o con
qué suplantar el dispositivo: una sola filtración lo daría todo.

El constructor de líneas copia **campo a campo**, sin volcar el objeto entero:
añadir una columna a la tabla no puede filtrar algo nuevo sin que alguien lo
decida. Además hay una lista de campos prohibidos como red de seguridad.

## Propiedad

Cada trabajo lleva `user_id`. El cliente de Drive se construye **por usuario**
con **su** token; no existe un cliente global capaz de escribir en el Drive de
cualquiera.

La cadena es `chat → whatsapp_account → user`. Un usuario no puede leer
segmentos, multimedia ni estado de otro; se responde **404**, no 403, porque un
403 sobre un identificador ajeno confirma que existe.

## Hashes: cuál verifica qué

| Campo | De qué | Para qué |
|---|---|---|
| `sha256` (segmento) | contenido en claro | comprobar que lo recuperado es lo guardado |
| `ciphertext_sha256` | archivo tal cual en Drive | comprobar la subida sin descifrar |
| `plaintext_sha256` (media) | archivo en disco | comprobar la caché local |
| `file_sha256` (media) | bytes crudos de WhatsApp | comprobar la **descarga** — es otra cosa |

Las dos últimas van en columnas distintas a propósito: son dos verificaciones
diferentes y meterlas juntas haría que una pisara a la otra.

## Que Drive responda 200 no basta

Tras cada subida se contrasta el tamaño declarado con el enviado. Un archivo
truncado que se da por bueno es una copia que falla el día que se necesita, y
hasta entonces nadie lo sabe.

Y **nunca** se borra la copia local antes de: subido + identificador guardado +
tamaño comprobado. Las tres.

## Rotación de claves

`user_storage_keys.key_version` existe para poder rotar la KEK sin perder
acceso a lo anterior. **Todavía no está implementada la rotación**: hoy,
cambiar `APP_ENCRYPTION_KEY` o `STORAGE_KEK` deja ilegible todo lo ya subido.
Está avisado en `.env.example`.

## Registros

Nunca se registra: tokens, contenido de mensajes, nombres de archivo, claves,
ni el cuerpo de las respuestas de Google. Solo códigos y tamaños.
