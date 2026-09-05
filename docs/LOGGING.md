# Registros

El objetivo: mirar la terminal y entender en segundos si el sistema va bien.

## Niveles

| Nivel | Qué va aquí |
|---|---|
| `INFO` | Eventos de producto: conectado, resultado de sincronización, anclas encontradas, resumen de Drive |
| `DEBUG` | Detalle de protocolo: cursores, ACKs, forma de las peticiones, cada mensaje, cada contacto |
| `WARNING` | Fallos recuperables que importan |
| `ERROR` | Fallos terminales |

**Nada se ha borrado.** Lo que antes estaba en `INFO` y era detalle de protocolo
bajó a `DEBUG`. Con `PROTOCOL_DEBUG=true` vuelve a verse todo.

## Etiquetas

```
[APP] [CONFIG] [DB] [WA] [PAIRING] [QR] [SIGNAL] [SYNC]
[BACKFILL] [MEDIA] [API] [SSE] [COMPAT] [LIVE] [AUTH]
[DRIVE] [STORAGE] [PLAN_E]
```

## Interruptores

```ini
LOG_LEVEL=INFO
HTTP_ACCESS_LOG=false   # peticiones HTTP correctas
PROTOCOL_DEBUG=false    # detalle de protocolo
```

### `HTTP_ACCESS_LOG`

Werkzeug escribe una línea por petición. Con el panel abierto son decenas por
minuto —multimedia, estado, SSE— y entierran lo único que hay que ver.

Con `false` se callan **200, 204, 206, 301, 302, 304**. Los **4xx y 5xx siempre
se ven**: un fallo silencioso es peor que ruido.

Ponlo en `true` para diagnosticar el frontend.

### `PROTOCOL_DEBUG`

Sube a `DEBUG` las etiquetas `SIGNAL`, `BACKFILL`, `COMPAT` y `PLAN_E`.
Devuelve cursores, identificadores truncados, formas de petición, tipos de
History Sync y reutilización de ratchet.

## Avisos repetidos

`RateLimitedLogger` agrupa avisos idénticos. Cien fallos iguales de Signal no
son cien problemas: son uno que ocurre cien veces.

Los fallos de descifrado se agrupan **por motivo**, y se distingue qué clase
son: `sin_sesion`, `mac_fallido` u otro. No es lo mismo — uno se resuelve con
la sesión y el otro con el ratchet.

- el **primero** se ve siempre — hay que saber que pasa
- después se cuenta, y se resume cada ventana
- avisos **distintos nunca se agrupan** entre sí

## Privacidad

En `INFO` nunca aparecen: nombres de contactos, texto de mensajes, teléfonos
completos, WAMIDs completos, tokens, identificadores de Drive ni claves.

Los identificadores internos y los JIDs truncados sí, cuando hacen falta para
entender qué pasó.

## Arranque esperado

```
[APP] Iniciando...
[DB]  PostgreSQL disponible (server_version=18.6, base=whatsapp_backup)
[API] Migraciones verificadas (revision=...)
[API] API escuchando en http://127.0.0.1:5000/api/v1
[API] Desde el navegador, usa: http://localhost:5000/api/v1
[WA]  Estado: STARTING -> CHECKING_SESSION
[WA]  Estado: CHECKING_SESSION -> CONNECTED
[AUTH] Cuenta de WhatsApp marcada como vinculada
[COMPAT] Adaptaciones activas: full_history, own_lid_map, ...
[PLAN_E] waiting=27 exhausted=10 timeout=2 pending=1 anclas=0 conversaciones_con_ancla=0
[STORAGE] Drive: N mensajes / M segmentos listos
```

Cada adaptación ya no anuncia la suya: la línea que importa es el resumen.

## Resumen del receptor

```
[LIVE] recibidos=20 propios=8 entrantes=12 descifrados_ok=18 reintentos=2 recuperados=1
```

Periodico, no una linea por mensaje. `recuperados` es el dato util: dice
cuantos de los que fallaron acabaron entrando. Ver
[MENSAJES_PROPIOS.md](MENSAJES_PROPIOS.md).

## Lo que se ve mientras se excava

```
[PLAN_E] chat=13 ancla real detectada (live): se pide su historial
[BACKFILL] chat=5730...@s timeout intento=1; se reintenta en 19:20 (ancla conservada)
[RECHECK] waiting=27 blobs_nuevos=0 semillas_nuevas=0 despertados=0
[SYNC] complete chats=40 con_ancla=13 esperando=27 reintentos=2 recuperados=0 anclas_nuevas=0 drive_pendiente=0
```

Antes, cada ronda de cada chat escribía seis líneas de cursor y nueve de
petición, y la revisión escribía una línea *por conversación* diciendo lo
mismo. Todo eso sigue disponible con `PROTOCOL_DEBUG=true`.
