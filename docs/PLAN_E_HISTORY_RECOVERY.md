# Recuperar historial: anclas

## El problema

`HISTORY_SYNC_ON_DEMAND` va **anclado**: hay que decirle desde qué mensaje
seguir hacia atrás. Una conversación sin ninguna referencia queda en
`waiting_seed` — existe, se ve, y no se le puede pedir nada.

`waiting_seed` **no** significa "chat vacío". Significa: *WhatsApp todavía no
ha entregado un punto desde el que pedir*.

## Qué recibimos de verdad

Medido sobre 318 blobs archivados, 4 emparejamientos:

| Tipo | Recibidos |
|---|---|
| `ON_DEMAND` | 302 |
| `INITIAL_BOOTSTRAP` | 4 |
| `INITIAL_STATUS_V3` | 4 |
| `PUSH_NAME` | 4 |
| `NON_BLOCKING_DATA` | 4 |
| **`RECENT`** | **0** |
| **`FULL`** | **0** |

El enum tiene 7 valores (`0 INITIAL_BOOTSTRAP` … `6 ON_DEMAND`). **`RECENT` y
`FULL` no han llegado nunca.** El colector los acepta si algún día llegan, pero
no son una fuente de la que dependa nada hoy.

## Fuentes

Todo mensaje descifrado y autenticado pasa por el mismo sitio:

```
initial_bootstrap · recent_history · full_history · on_demand
live · offline · retry_resend · blob_scan
        ↓
   RecentSeedCollector.observe()
        ↓
   validar → guardar → despertar
```

Un mensaje que **no** se pudo descifrar no es fuente de nada. No se baja la
seguridad de Signal para conseguir anclas.

## Validación

Todo lo que pase se usará como ancla de una petición real al teléfono del
usuario, así que es preferible rechazar de más:

- identificador presente y con forma de WAMID (16–32 hex)
- el **mismo** filtro que usa el motor de excavación
- marca de tiempo > 0 y **en segundos** — una en milisegundos se **rechaza**,
  no se convierte: dividir por mil es adivinar la unidad
- el chat se resuelve a uno existente
- no es `broadcast` ni `newsletter`
- no es señalización de protocolo

Un mensaje con imagen, vídeo o audio **sí** sirve. Lo que descarta un ancla es
ser señalización, no llevar contenido.

## PN/LID y grupos

Un contacto aparece por teléfono y por LID: son la **misma** conversación. Se
usa `canonical_chat_jid`, el resolutor de siempre, para no acabar con dos chats
y la mitad del historial en cada uno.

En grupos el ancla pertenece al **grupo**, no a quien escribió: el participante
no es una conversación.

## Cursor elegido

El **más antiguo** conocido. Se excava hacia atrás, así que empezar por ahí
alcanza lo que queda antes; partir del más reciente obligaría a recorrer otra
vez lo que ya se tiene.

Lo decide **una sola función**, `get_valid_history_cursor`, que usan también
el canary, la excavación, la cola y la reconciliación. Antes había tres
definiciones distintas de "tiene cursor" y discrepaban entre sí — ver
[HISTORY_CURSOR.md](HISTORY_CURSOR.md).

## Despertar

```
waiting_seed → (ancla válida) → pending → cola del motor de siempre
```

Automático, al observar. No hace falta pulsar nada, ni recargar, ni reiniciar.

El motor de ON_DEMAND **no se toca**: destino, `count=50`, forma de la
petición, `category=peer`, semántica del cursor y single-flight siguen igual.
Aquí solo se le entrega trabajo.

## Un timeout no cuesta el ancla

Que el teléfono no conteste no dice nada malo de la referencia. El chat pasa a
`timeout` **conservándola**, se anota el intento y se calcula cuándo volver a
probar (1 min, 5 min, 15 min, 1 h). Sobrevive al reinicio.

## Blobs: una lectura, no una por chat

Antes, revisar los pendientes descomprimía los mismos archivos **una vez por
chat**: con 28 pendientes y 4 blobs, 112 lecturas para descubrir lo mismo que
la primera.

Ahora cada archivo se lee una vez y se anota su **SHA-256** en `scanned_blobs`.
La huella identifica el contenido, no el nombre. `hay_blobs_nuevos()` compara
huellas sin abrir nada, para que una revisión automática no haga trabajo pesado
cuando no ha cambiado nada.

El escaneo **no reingiere mensajes**: solo saca referencias.

## Resultado medido en esta cuenta

```
318 blobs → 11 638 referencias utilizables
            → pertenecen a 11 conversaciones
            → las 11 que YA tienen historial (10 exhausted + 1 pending)

28 conversaciones esperando → 0 tienen ancla en los blobs
```

**Escanear lo guardado no despierta ninguna de las 28.** WhatsApp nunca envió
un solo mensaje para ellas: no hay nada que recolectar de lo que ya tenemos.

La infraestructura queda lista y funcionará cuando lleguen anclas por los
caminos vivos —un mensaje nuevo, un pendiente al reconectar, un reenvío—. Pero
prometer una cifra menor sería inventarla.

## Límites

WhatsApp decide qué historial entrega a un dispositivo vinculado. Si para una
conversación nunca da una referencia, esa conversación se queda esperando.
**Eso es correcto**, y es preferible a fabricar un cursor: uno inventado recibe
confirmación del servidor y después silencio, que es el fallo más caro de
diagnosticar de este proyecto.

## Hacia Drive

Lo que se recupere sigue el camino de siempre:

```
normalizar → deduplicar → índice en PostgreSQL → trabajo de subida
→ segmento → gzip → AES-256-GCM → Drive
```

Sin almacenamiento paralelo.

## Herramientas

```bash
py tools/inspect_history_seeds.py    # qué anclas hay (solo lectura)
py tools/plan_e_scan.py              # simula: qué se podría despertar
py tools/plan_e_scan.py --aplicar    # anota anclas y despierta
```
