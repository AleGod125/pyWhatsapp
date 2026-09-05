# Copias de lo que yo mismo escribo

## Dos casos que parecen uno

Un mensaje que el usuario escribe llega a este companion como una copia cuyo
remitente es **su propia cuenta**. Pero no todas vienen del mismo sitio, y eso
decide por qué sesión Signal llegan:

| Origen | Dispositivo | Clasificación |
|---|---|---|
| el teléfono | `0` | `primary_phone` |
| WhatsApp Web u otro vinculado | ≠ `0` | `linked_web` |
| sin dispositivo en la stanza | — | `linked_unknown` |

El identificador de cuenta es **el mismo** en los dos primeros. Distinguirlos
por el JID a secas los confunde, y confundirlos manda a buscar el fallo al
sitio equivocado.

`linked_unknown` existe a propósito: que sea de un vinculado no dice **cuál**,
y afirmar "Web" sin saberlo sería inventárselo.

## Lo que se midió

En el Signal Store real de la cuenta:

```
573002***:0@s.whatsapp.net    ← el teléfono, por número
865311***:0@lid               ← EL TELÉFONO OTRA VEZ, por LID
865311***:92@lid              ← el dispositivo vinculado
```

`PN:0` y `LID:0` son **el mismo aparato con dos estados de Double Ratchet**.
El vinculado tiene uno solo.

Cómo se llega ahí: `migrate_pn_session_to_lid` mueve la sesión del número al
LID y **borra la del número**; después, cuando el companion le pide historial
a su propio teléfono —que va dirigido al número, dispositivo 0— ya no
encuentra sesión y establece una nueva. A partir de ahí existen las dos.

**Esto es una correlación medida, no una causa demostrada.** Es coherente con
que las copias del teléfono fallen y las del navegador no, pero para
afirmarlo haría falta ver el fallo y el estado de las dos sesiones en el mismo
instante. La auditoría lo avisa al arrancar para poder comprobarlo.

## Qué se hace cuando una copia no cuadra

Nada distinto de lo que ya se hacía, y a propósito:

```
no cuadra → NO se entrega, NO se persiste, NO siembra ancla
          → acuse de reintento
          → el emisor reenvía como pkmsg (X3DH completo)
          → esa sesión nueva sustituye a la que estaba mal
```

**No se copia ninguna sesión, no se borra ninguna, no se toca ningún ratchet y
no se salta ninguna verificación.** Arreglar un fallo de autenticación
desactivando la comprobación convertiría el backup en algo que acepta
mensajes que nadie ha autenticado.

## El recorrido de un mensaje que falló

```
original_failed → retry_sent → retry_received → retry_success
                                              ↘ retry_failed
```

Antes solo se contaba *"N fallos de descifrado"*. Con eso no se puede
responder a la única pregunta que importa: **¿ese mensaje acabó llegando?**

`retry_failed` es un reenvío que tampoco cuadró. Que no llegue nada no es un
estado sino la ausencia de uno, y se mide como `sin_respuesta`.

Un mensaje recuperado entra por el camino normal **una sola vez**, y su
procedencia se anota como `retry_resend` — es justo el que puede ser la
primera ancla de su conversación.

## Una limitación medida y no tocada

`Receiver._send_retry_receipt` escribe `count="1"` fijo. Un mensaje que falla
tres veces pide tres reenvíos que dicen los tres "es la primera vez". Baileys
y whatsmeow envían el contador real, y el emisor lo usa para decidir si rehace
la sesión entera.

**No se ha cambiado.** Reescribir esa stanza es reimplementar protocolo que
hoy funciona, y no hay evidencia de que ese campo sea lo que impide el
reenvío. El contador real está en el seguimiento para el día que se quiera
comprobar.

## Un solo proceso

`app/core/lock.py` ya lo impide desde antes de construir nada: dos
`service.py` sobre la misma sesión avanzarían los mismos ratchets y
producirían fallos de autenticación idénticos a estos. Detecta cerrojos
huérfanos por latido, no solo por PID —Windows reutiliza los PID—.

## Registros

```
[LIVE] recibidos=20 propios=8 entrantes=12 descifrados_ok=18 reintentos=2 recuperados=1
```

Una línea periódica, no una por mensaje. El detalle de la resolución de
dirección —JID, dispositivo, PN y LID resueltos, dirección Signal usada, si
hay sesión— va a `DEBUG` y vuelve entero con `PROTOCOL_DEBUG=true`.

Los avisos repetidos se agrupan **por motivo**: cien fallos iguales son un
problema que ocurre cien veces.
