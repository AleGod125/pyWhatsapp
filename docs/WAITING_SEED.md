# waiting_seed

Un chat está en `waiting_seed` cuando llegó del emparejamiento como **pura
metadata** —nombre, identificador, contadores— sin un solo identificador de
mensaje.

Como [ON_DEMAND](ON_DEMAND.md) va anclado, sin esa primera referencia no se
puede pedir nada.

## Lo que NO significa

- **No** significa "chat vacío".
- **No** es un error.
- **No** se convierte a `exhausted` ni a `complete`.

Significa: *WhatsApp todavía no ha entregado un punto desde el que pedir el
historial*. Es reintentable.

Texto para la interfaz:

> WhatsApp todavía no proporcionó una referencia válida para recuperar mensajes
> anteriores.

En ámbar o neutro. **Nunca en rojo**: si la UI lo pinta como fallo, el usuario
cree que se rompió algo que no se rompió.

## Cómo despierta

**Solo, sin ningún botón.** Si el chat recibe o envía un mensaje real, ese
mensaje sirve de ancla: pasa a `pending` y se encola para ON_DEMAND.

## Revisión manual

`POST /history/recheck-pending` (todos) o
`POST /chats/<id>/history/recheck` (uno). Todo local:

1. resuelve los alias del contacto (el ancla puede estar bajo su *otro*
   identificador — teléfono vs LID);
2. busca un mensaje con ID real de WhatsApp entre ellos;
3. si no lo hay, reinterpreta los blobs de History Sync ya guardados en disco
   (el normalizador ha mejorado desde que se guardaron);
4. si aparece un ancla, el chat vuelve a `pending` y se excava.

No pide nada al servidor y no vincula ningún dispositivo.

## Fuentes agotadas, con medidas

| Fuente | Resultado |
|---|---|
| INITIAL_BOOTSTRAP Conversation | 29 campos auditados, ningún id en los 33 chats |
| blobs de History Sync | ninguno |
| PostgreSQL | ninguno |
| alias PN/LID | ninguno |
| app-state incremental | 0 claves / 61 mutaciones |
| app-state snapshot completo | 0 claves / 93 mutaciones |
| sesión auxiliar Baileys | 0 eventos de historial al reconectar |

La entrega de historial parece ser de una sola vez, en el emparejamiento. Por
eso `no_seed` es el resultado habitual y **no** un fallo.
