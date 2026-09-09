"""Los descriptores protobuf del protocolo de WhatsApp.

DE DONDE SALEN
--------------
Son los ficheros generados de ``pywhats`` 0.2.0 (Apache-2.0), copiados tal
cual al soltar esa dependencia:

    pywhats/proto/e2e_pb2.py          -> e2e_pb2.py
    pywhats/proto/history_sync_pb2.py -> history_sync_pb2.py
    pywhats/proto/sync_action_pb2.py  -> sync_action_pb2.py

POR QUE COPIARLOS Y NO REESCRIBIRLOS
------------------------------------
Un ``*_pb2.py`` no es logica: es la traduccion mecanica de un esquema. No
lleva criptografia, ni parseo de historial, ni nada de lo que hacia falta
rodear con parches. Es la unica parte de la libreria que funcionaba sin
reservas, y regenerarla pedia una cadena de ``protoc`` para obtener byte a
byte lo mismo.

Copiarlos deja el comportamiento IDENTICO --el mismo descriptor, los mismos
campos, los mismos numeros-- y quita la dependencia. Ni una linea del
normalizador de mensajes cambia por esto.

EL NOMBRE DE PAQUETE SIGUE DICIENDO ``pywhats.proto``
-----------------------------------------------------
Va dentro del descriptor serializado y cambiarlo obligaria a regenerar. No
molesta: es una etiqueta interna de protobuf, no un import de Python. Y de
paso deja dicho de donde vienen, que es lo correcto con codigo de otro.

LO QUE NO ESTA AQUI
-------------------
Los otros siete modulos de ``pywhats/proto`` --handshake, client_payload,
companion_reg, server_sync, sender_key, whisper-- describen el emparejamiento
y el transporte, y de eso ya se encarga Baileys. Solo se copio lo que este
proyecto lee de verdad.
"""

from app.wa.proto.e2e_pb2 import (  # noqa: F401
    ContextInfo,
    DeviceSentMessage,
    HistorySyncNotification,
    Message,
    ProtocolMessage,
)
from app.wa.proto.history_sync_pb2 import HistorySync  # noqa: F401
from app.wa.proto.sync_action_pb2 import SyncActionValue  # noqa: F401

__all__ = [
    "ContextInfo",
    "DeviceSentMessage",
    "HistorySync",
    "HistorySyncNotification",
    "Message",
    "ProtocolMessage",
    "SyncActionValue",
]
