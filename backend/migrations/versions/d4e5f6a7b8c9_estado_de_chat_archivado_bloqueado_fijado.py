"""Estado de la conversacion: archivada, restringida, fijada, silenciada.

DE DONDE SALEN
--------------
No hay que pedirle nada a nadie: ``proto.Conversation`` --lo que llega en cada
``messaging-history.set``-- ya trae los cuatro::

    archived      bool
    locked        bool     <- los "chats restringidos" de WhatsApp
    pinned        uint     <- marca de tiempo, no booleano
    muteEndTime   uint

El traductor los descartaba: ``conversacion()`` solo copiaba jid, nombre,
marca de tiempo y contador de no leidos. El dato estaba llegando y se tiraba
en cada lote.

Baileys ademas mantiene ``archived``, ``pinned``, ``muteEndTime`` y
``starred`` al vuelo desde el app-state (``archiveChatAction`` y compania), y
eso ya se emite. Lo unico que NO procesa es ``lockChatAction``, asi que el
estado de restringido solo puede venir del historial.

POR QUE COLUMNAS Y NO ``raw_metadata``
--------------------------------------
Porque se filtra por ellas. El panel pide "los archivados de esta cuenta" y
eso tiene que ser un ``WHERE``, no leer y descartar en Python cientos de
filas. ``pinned`` ademas ordena.

LO QUE NO SE TOCA
-----------------
Ni un mensaje, ni un cursor, ni un estado de historial. Cuatro columnas
nuevas con valor por defecto: una conversacion que nunca reciba el dato se
comporta exactamente como hoy.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chats",
        sa.Column(
            "archived", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    # Los "chats restringidos". Se guarda aunque hoy no podamos abrirlos: sin
    # el dato no hay forma de decirle al usuario cuantos tiene ni de sacarlos
    # del listado normal, que es donde WhatsApp NO los ensena.
    op.add_column(
        "chats",
        sa.Column(
            "locked", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    # WhatsApp guarda una MARCA DE TIEMPO, no un booleano: el orden entre
    # varios fijados es el de cuando se fijaron. Nulo significa "no fijado".
    op.add_column("chats", sa.Column("pinned_at", sa.BigInteger(), nullable=True))
    # Hasta cuando esta silenciado. Nulo es "no silenciado"; WhatsApp usa 0
    # para "para siempre", asi que no se puede comparar con la hora actual sin
    # mirar antes ese caso.
    op.add_column("chats", sa.Column("mute_until", sa.BigInteger(), nullable=True))

    # El panel pide "los archivados de ESTA cuenta" y "los restringidos de
    # ESTA cuenta". Sin el indice son dos recorridos completos por cada
    # cambio de seccion.
    op.create_index(
        "ix_chats_cuenta_archivado",
        "chats",
        ["whatsapp_account_id", "archived"],
    )
    op.create_index(
        "ix_chats_cuenta_restringido",
        "chats",
        ["whatsapp_account_id", "locked"],
    )


def downgrade() -> None:
    op.drop_index("ix_chats_cuenta_restringido", table_name="chats")
    op.drop_index("ix_chats_cuenta_archivado", table_name="chats")
    op.drop_column("chats", "mute_until")
    op.drop_column("chats", "pinned_at")
    op.drop_column("chats", "locked")
    op.drop_column("chats", "archived")
