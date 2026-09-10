"""Saca de PostgreSQL el contenido que ya esta a salvo en Drive.

EL REPARTO QUE ESTO COMPLETA
----------------------------
``app/storage/reader.py`` ya lo dice: *PostgreSQL dice QUE mensajes hay y
DONDE estan; Drive tiene el contenido*. Faltaba la otra mitad -- el contenido
se subia a Drive y se quedaba TAMBIEN en la base, asi que el reparto era una
intencion, no un hecho.

Aqui se hace hecho: cuando un mensaje esta confirmado en Drive, su texto y su
protobuf se vacian de PostgreSQL. Lo que queda es el indice: de que chat es,
cuando fue, su identificador de WhatsApp, y en que segmento y linea vive su
contenido. Con eso el panel sigue listando, ordenando, paginando y
deduplicando sin tocar la red, y al abrir la conversacion el lector baja el
segmento y lo descifra.

LA GARANTIA, Y ES LA UNICA QUE IMPORTA
--------------------------------------
NO se vacia nada que no este confirmado. Tres condiciones a la vez, y las
tres se comprueban en el mismo ``WHERE``:

1. el mensaje se dio por subido (``storage_status = 'ready'``);
2. su segmento existe y esta cerrado (``status = 'ready'``);
3. ese segmento tiene ``drive_file_id`` -- o sea, Google contesto con un
   identificador de archivo.

Sin las tres, la fila se queda como esta. Vaciar contenido que no llego a
Drive no es optimizar: es perderlo, y sin nada que lo delate hasta que
alguien abra esa conversacion meses despues.

LO QUE NO SE TOCA
-----------------
Ni una fila se borra. Ni un identificador, ni una fecha, ni un ancla, ni el
estado del historial. Solo se ponen a nulo tres columnas de contenido, y solo
cuando su copia remota esta confirmada.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import null, select, update

from app.core.logging_setup import get_logger
from app.models import Message
from app.models.storage import MessageSegment

log = get_logger("STORAGE")

#: Cuantos mensajes se vacian por pasada. Ni uno --seria un barrido eterno--
#: ni todos: una sola sentencia sobre cien mil filas bloquea la tabla mientras
#: dura, y por ahi entran los mensajes en vivo.
LOTE = 2000


def purgar_contenido_subido(database: Any, *, limite: int = LOTE) -> int:
    """Vacia el contenido local de los mensajes confirmados en Drive.

    Devuelve cuantos se vaciaron. Nunca lanza: es una tarea de fondo y un
    fallo suyo no puede tumbar al trabajador ni perder una subida.
    """
    try:
        with database.transaction() as sesion:
            # Se eligen PRIMERO los identificadores y despues se actualiza por
            # id. Un `UPDATE ... WHERE id IN (SELECT ...)` con un `JOIN` dentro
            # deja a PostgreSQL decidiendo el plan sobre una tabla que se esta
            # escribiendo a la vez; con la lista ya resuelta, el bloqueo dura
            # lo que tarda en escribir esas filas y nada mas.
            ids = (
                sesion.execute(
                    select(Message.id)
                    .join(MessageSegment, MessageSegment.id == Message.segment_id)
                    .where(
                        Message.storage_status == "ready",
                        MessageSegment.status == "ready",
                        MessageSegment.drive_file_id.is_not(None),
                        # Ya vaciado: no se vuelve a tocar.
                        (Message.text.is_not(None))
                        | (Message.raw_proto.is_not(None))
                        | (Message.raw_metadata.is_not(None)),
                    )
                    .limit(limite)
                )
                .scalars()
                .all()
            )
            if not ids:
                return 0
            sesion.execute(
                update(Message)
                .where(Message.id.in_(ids))
                # `raw_metadata` es JSONB, y ahi `None` NO es NULL: SQLAlchemy
                # lo escribe como el valor JSON `null`, que es un dato. La
                # columna seguia siendo `IS NOT NULL`, asi que la misma fila
                # se elegia una y otra vez en cada barrido -- trabajo infinito
                # sin liberar nada. `null()` pide el NULL de SQL.
                .values(text=None, raw_proto=None, raw_metadata=null())
            )
        log.info(
            "[STORAGE] %d mensaje(s) liberados de PostgreSQL: su contenido "
            "esta confirmado en Drive",
            len(ids),
        )
        return len(ids)
    except Exception:  # noqa: BLE001 - una purga fallida no puede tumbar nada
        log.exception("No se pudo purgar el contenido ya subido")
        return 0


def cuanto_queda_por_purgar(database: Any) -> int:
    """Mensajes con contenido local que ya podrian vaciarse. Para el diagnostico."""
    from sqlalchemy import func

    try:
        with database.transaction() as sesion:
            return int(
                sesion.execute(
                    select(func.count())
                    .select_from(Message)
                    .join(MessageSegment, MessageSegment.id == Message.segment_id)
                    .where(
                        Message.storage_status == "ready",
                        MessageSegment.status == "ready",
                        MessageSegment.drive_file_id.is_not(None),
                        (Message.text.is_not(None))
                        | (Message.raw_proto.is_not(None))
                        | (Message.raw_metadata.is_not(None)),
                    )
                ).scalar()
                or 0
            )
    except Exception:  # noqa: BLE001
        return 0
