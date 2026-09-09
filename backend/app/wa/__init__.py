"""La capa de WhatsApp.

QUE HAY AQUI
------------
Todo lo que habla el protocolo, detras de un contrato (``port.py``) que el
resto del proyecto no ve por dentro. La implementacion es
``baileys_client.py``: un proceso hijo de Node que traduce a los mismos
``ClientEvent`` sobre la misma cola.

SOLO LEE
--------
El contrato no expone NADA de envio. Lo unico que sale hacia WhatsApp son
peticiones del historial propio. No hay ``send_message``, ni acuses de
lectura, ni presencia: el producto es una copia de seguridad, y lo que no
esta implementado no se puede llamar por error.

DE DONDE VIENE
--------------
Aqui hubo un interruptor ``WA_PROVIDER`` para convivir con ``pywhats``
mientras se median las dos. Esa etapa termino: pywhats se retiro entero
--libreria, cliente y los 13 parches de ``app/compat/``-- y con el se fueron
sus carencias, que eran las que obligaban a parchear. Lo que quedo util de
aquella epoca vive aqui: el lector de blobs (``historial.py``), los
descriptores protobuf (``proto/``) y el resolutor de version (``version.py``),
que sigue encontrando revisiones mas nuevas que la propia libreria.
"""

from app.wa.factory import crear_cliente_de_whatsapp
from app.wa.port import ClienteDeWhatsApp

__all__ = ["ClienteDeWhatsApp", "crear_cliente_de_whatsapp"]
