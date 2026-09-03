# History Sync

## Qué llega y cuándo

Al emparejar, el servidor envía el historial en **tandas**, cada una con su
`syncType`:

```
0 INITIAL_BOOTSTRAP   1 INITIAL_STATUS_V3   2 FULL   3 RECENT
4 PUSH_NAME   5 NON_BLOCKING_DATA   6 ON_DEMAND
```

`FULL` y `RECENT` no se han recibido nunca en esta cuenta. El grueso llega
como `INITIAL_BOOTSTRAP`, y es **de una sola vez**: reconectar no lo repite.

Lo posterior se pide con [ON_DEMAND](ON_DEMAND.md).

## Espera del bootstrap

`InitialHistoryGate` espera la primera tanda antes de dejar excavar: pedir
ON_DEMAND sin bootstrap no tiene desde dónde anclarse.

La confirmación se guarda **por huella de sesión**, no como un booleano
global. Una vinculación nueva cambia la huella y vuelve a esperar, porque de
verdad va a llegar otro bootstrap.

## Los blobs se guardan

Cada blob descifrado se archiva en `data/history/*.pb` **antes** de
interpretarlo. Dos razones:

1. un fallo de normalización no puede costar historial;
2. el normalizador mejora, y reinterpretar lo ya entregado saca mensajes que
   la primera pasada no entendió — es lo que hace el recheck de
   [waiting_seed](WAITING_SEED.md).

`COMPAT_HISTORY_MESSAGES` es lo que expone los `WebMessageInfo` individuales:
pywhats 0.2.0 los descarga y descifra, pero solo los cuenta.

## raw_proto

Cada mensaje guarda su protobuf original. **No se elimina.** Es lo que permite
reinterpretar sin volver a pedir nada, diagnosticar formas nuevas y recuperar
contenido que el parser de hoy todavía no entiende.

## Fidelidad

- Un mensaje **no** se descarta porque su tipo no se reconozca: se guarda con
  lo que se pudo determinar y su `raw_proto`.
- Deduplicación por `whatsapp_message_id` (índice parcial único
  `uq_messages_chat_wamid`): reingerir no duplica.
- El cursor histórico **no** es el mensaje más antiguo de la tabla, sino el más
  antiguo con un WAMID válido para anclar.
