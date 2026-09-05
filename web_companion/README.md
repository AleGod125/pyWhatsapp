# Web Companion

Un dispositivo vinculado **más** de la misma cuenta, con su propia sesión y su
propio QR, que existe para responder a **una** pregunta:

> De las conversaciones que esperan una referencia, ¿cuántas ve WhatsApp Web y
> de cuántas puede dar un mensaje real?

Nada más. Esta fase **mide**; no recupera.

## Lo que no es

No es un segundo backup. No guarda mensajes, no descarga multimedia, no
escribe en PostgreSQL, no habla con Drive y **no toca la sesión de pywhats**.

Su sesión vive en `session/web_companion/` y es una vinculación
independiente: otro aparato en la lista del teléfono. No se copia ni una clave
entre las dos — son sesiones Signal distintas y mezclarlas corrompe ambas.

## Cómo se arranca

No se arranca. Lo levanta `service.py` cuando `WEB_COMPANION_ENABLED=true`,
y lo cierra al salir.

```bash
py tools/setup_web_companion.py   # una vez: baja Node deps + Chromium
# WEB_COMPANION_ENABLED=true en .env
py service.py                     # el entrypoint sigue siendo este
```

La primera vez pide **su propio QR**. No sustituye al emparejamiento
principal, y el panel lo marca como experimental.

## El canal

JSON Lines por las tuberías del proceso. Sin puerto, sin servidor.

```
Python → Node        {"cmd":"status","id":1}
                     {"cmd":"inventory","id":2,"python_chat_jids":[...]}
                     {"cmd":"probe_waiting_seeds","id":3,"chat_jids":[...]}

Node → Python        {"event":"starting","pid":1234}
                     {"event":"state","state":"qr_required","qr":"..."}
                     {"event":"inventory_result","metrics":{...}}
                     {"event":"seed_probe_result","summary":{...}}
```

**`stdout` es solo protocolo.** Una línea, un evento. Lo legible para humanos
va a `stderr`, y el worker redefine `console.log` al arrancar porque
whatsapp-web.js y puppeteer escriben cuando les apetece.

## Quién decide

Node **propone**; Python **valida**. El worker no sabe de quién es la cuenta,
ni de alias PN/LID, ni de qué conversaciones existen en esta base. Sus
candidatos pasan por `validar()`, el mismo filtro que usa el colector de
anclas — si aceptara algo que aquel rechaza, lo medido no valdría de nada.

## Los archivos

| | |
|---|---|
| `worker.js` | ciclo de vida, canal, comandos |
| `protocol.js` | JSON Lines: troceo de líneas y parseo. Lógica pura |
| `candidates.js` | qué se puede proponer y qué no. Lógica pura |
| `store_adapter.js` | lo que corre **dentro** del navegador |

`protocol.js` y `candidates.js` no importan puppeteer a propósito: sus
pruebas corren sin Chromium. Una prueba que exigiera un navegador no la
correría nadie.

```bash
cd web_companion && npm test
```

## No se suponen APIs

WhatsApp Web no tiene API pública y renombra las cosas sin avisar. El proyecto
anterior llamaba en cadena a cuatro cargadores envolviendo cada uno en un
`catch` vacío: funciona, pero no deja saber cuál existe — si los cuatro han
desaparecido, el resultado es idéntico a que ninguno haya encontrado nada.

Aquí primero se **descubre** qué existe (`store_adapter.descubrir`), se
informa en `capabilities`, y sólo después se usa. Un método que no está se
reporta como ausente; no se simula.

## Niveles

| Nivel | Interruptor | Qué hace |
|---|---|---|
| 1 | `WEB_COMPANION_ENABLED` | inventario + sondeo de lo que ya está cargado |
| 2 | `WEB_STORE_LOAD_EARLIER` | pedir al Store mensajes anteriores |
| 3 | *no implementado* | `chat.syncHistory()` |
| 4 | *no implementado* | `Store.HistorySync.sendPeerDataOperationRequest` |

Los niveles 3 y 4 están **documentados y no activados** — ver
[PLAN_F_WEB_COMPANION.md](../docs/PLAN_F_WEB_COMPANION.md). Primero se mide el
nivel 1.

## Privacidad

Por el canal viajan metadatos, nunca contenido: a qué conversación pertenece,
qué identificador tiene y de cuándo es. Los nombres se recortan a 40
caracteres y los identificadores completos no salen en los registros — un JID
completo es un número de teléfono.

El QR viaja para poder pintarlo, pero no se guarda en disco ni se registra.
