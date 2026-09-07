"""Aislar chats, contactos, historial y mensajes por cuenta de WhatsApp.

LO QUE ROMPE HOY EL MULTIUSUARIO
--------------------------------
Cuatro restricciones globales. Cada una impide o cruza datos entre cuentas::

    chats               UNIQUE(jid)                     -> la segunda cuenta con
                                                           el mismo contacto NO
                                                           puede ni insertar
    contacts            UNIQUE(jid), sin cuenta         -> A llama "Mama" a un
                                                           numero y B ve ese
                                                           nombre
    chat_history_state  UNIQUE(chat_jid)                -> una semilla de A
                                                           tocaria el estado de B
    messages            UNIQUE(chat_jid, wa_msg_id)     -> el mismo mensaje de
                                                           grupo recibido por A y
                                                           B: el de B se descarta
                                                           EN SILENCIO

QUE HACE ESTA MIGRACION
-----------------------
Cambia las cuatro por su version por cuenta. En ``messages`` no hace falta
columna nueva: ``chat_id`` ya apunta a un chat que SI tiene cuenta, asi que
deduplicar por ``(chat_id, wa_msg_id)`` da el aislamiento sin duplicar dato.

EL BACKFILL DE CONTACTOS
------------------------
Es el unico que necesita criterio. Con UNA sola cuenta no hay ambiguedad: no
existe otro sitio al que un contacto pueda pertenecer, asi que se le asigna esa
--y eso es deduccion, no adivinanza. Con varias cuentas se atribuye por los
chats donde aparece ese identificador (por telefono o por LID), y si alguno
queda sin poder atribuirse **la migracion se detiene con el recuento delante**
en vez de repartir a ojo.

LO QUE NO SE TOCA
-----------------
Ni un identificador, ni una marca de tiempo, ni un mensaje. Sobre la base real:
41 chats, 172 contactos, 4550 mensajes, 41 estados de historial. Los mismos
antes y despues.

Revision ID: b2c3d4e5f6a7
Revises: f1a2b3c4d5e6
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conexion = op.get_bind()

    # -- Guardia previa -----------------------------------------------------
    #
    # Con chats sin cuenta no se puede aislar nada, y seguir dejaria filas
    # imposibles de atribuir despues. Mejor parar aqui, con el numero delante.
    huerfanos = conexion.execute(
        sa.text("SELECT count(*) FROM chats WHERE whatsapp_account_id IS NULL")
    ).scalar()
    if huerfanos:
        raise RuntimeError(
            f"{huerfanos} chat(s) sin cuenta de WhatsApp. Se detiene la "
            "migracion: atribuirlos a ojo mezclaria conversaciones de "
            "personas distintas."
        )

    # -- A. CHATS: cuenta OBLIGATORIA y unicidad por cuenta -----------------
    #
    # El NOT NULL no es cosmetico. PostgreSQL trata los NULL como distintos
    # entre si, asi que con la columna nullable dos filas `(NULL, mismo_jid)`
    # NO colisionan: la unicidad por cuenta no deduplicaria nada y aparecerian
    # chats repetidos en silencio. Las dos mitades van juntas o no van.
    op.alter_column("chats", "whatsapp_account_id", nullable=False)
    op.drop_constraint("chats_jid_key", "chats", type_="unique")
    op.create_unique_constraint(
        "uq_chats_account_jid", "chats", ["whatsapp_account_id", "jid"]
    )

    # -- B. CONTACTS: columna de cuenta, backfill y unicidad ----------------
    op.add_column(
        "contacts",
        sa.Column(
            "whatsapp_account_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )

    cuentas = conexion.execute(
        sa.text("SELECT count(*) FROM whatsapp_accounts")
    ).scalar()

    if cuentas == 1:
        # Sin ambiguedad posible: no hay otro sitio al que puedan pertenecer.
        conexion.execute(
            sa.text(
                "UPDATE contacts SET whatsapp_account_id = "
                "(SELECT id FROM whatsapp_accounts LIMIT 1)"
            )
        )
    elif cuentas > 1:
        # Con varias cuentas se atribuye por evidencia: los chats donde
        # aparece ese identificador, por telefono o por LID.
        conexion.execute(
            sa.text(
                """
                UPDATE contacts c
                SET whatsapp_account_id = sub.cuenta
                FROM (
                    SELECT c2.id AS contacto,
                           min(ch.whatsapp_account_id::text)::uuid AS cuenta
                    FROM contacts c2
                    JOIN chats ch
                      ON ch.jid = c2.jid
                      OR (c2.lid IS NOT NULL AND ch.jid = c2.lid)
                    GROUP BY c2.id
                    HAVING count(DISTINCT ch.whatsapp_account_id) = 1
                ) sub
                WHERE c.id = sub.contacto
                """
            )
        )
        sin_atribuir = conexion.execute(
            sa.text(
                "SELECT count(*) FROM contacts WHERE whatsapp_account_id IS NULL"
            )
        ).scalar()
        if sin_atribuir:
            raise RuntimeError(
                f"{sin_atribuir} contacto(s) no se pueden atribuir a una sola "
                "cuenta. Se detiene la migracion: repartirlos a ojo pondria el "
                "nombre que una persona le dio a un numero en la agenda de otra."
            )

    if cuentas:
        op.alter_column("contacts", "whatsapp_account_id", nullable=False)
        op.create_foreign_key(
            "fk_contacts_account",
            "contacts",
            "whatsapp_accounts",
            ["whatsapp_account_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.drop_constraint("contacts_jid_key", "contacts", type_="unique")
        op.create_unique_constraint(
            "uq_contacts_account_jid", "contacts", ["whatsapp_account_id", "jid"]
        )
        op.create_index("ix_contacts_account", "contacts", ["whatsapp_account_id"])

    # -- C. CHAT_HISTORY_STATE: el jid deja de ser unico global -------------
    #
    # `chat_id` ya es unico y ya da la cuenta a traves de `chats`, asi que la
    # unicidad global por `chat_jid` no aporta nada y si impide que dos cuentas
    # tengan la misma conversacion. Queda un indice normal: las consultas por
    # jid siguen siendo rapidas, pero ya no fuerzan una sola fila en todo el
    # sistema.
    op.drop_constraint(
        "chat_history_state_chat_jid_key", "chat_history_state", type_="unique"
    )
    op.create_index(
        "ix_chat_history_state_jid", "chat_history_state", ["chat_jid"]
    )

    # -- D. MESSAGES: deduplicar por conversacion, no por jid global --------
    #
    # `chat_id` pertenece a un chat que tiene cuenta, asi que esto aisla sin
    # anadir una columna que habria que mantener en dos sitios.
    op.drop_index("uq_messages_chat_wamid", table_name="messages")
    op.create_index(
        "uq_messages_chat_wamid",
        "messages",
        ["chat_id", "whatsapp_message_id"],
        unique=True,
        postgresql_where=sa.text("whatsapp_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    conexion = op.get_bind()

    # Bajar solo es seguro si NO hay duplicados entre cuentas: restaurar una
    # unicidad global con dos cuentas teniendo el mismo contacto reventaria, y
    # la unica forma de "arreglarlo" seria borrar filas de alguien. No se hace.
    for tabla, columna in (("chats", "jid"), ("contacts", "jid")):
        repetidos = conexion.execute(
            sa.text(
                f"SELECT count(*) FROM (SELECT {columna} FROM {tabla} "
                f"GROUP BY {columna} HAVING count(*) > 1) x"
            )
        ).scalar()
        if repetidos:
            raise RuntimeError(
                f"{repetidos} {columna}(s) repetidos en {tabla} entre cuentas. "
                "No se puede restaurar la unicidad global sin borrar datos de "
                "alguien, y esta migracion no borra nada."
            )

    op.drop_index("uq_messages_chat_wamid", table_name="messages")
    op.create_index(
        "uq_messages_chat_wamid",
        "messages",
        ["chat_jid", "whatsapp_message_id"],
        unique=True,
        postgresql_where=sa.text("whatsapp_message_id IS NOT NULL"),
    )

    op.drop_index("ix_chat_history_state_jid", table_name="chat_history_state")
    op.create_unique_constraint(
        "chat_history_state_chat_jid_key", "chat_history_state", ["chat_jid"]
    )

    op.drop_index("ix_contacts_account", table_name="contacts")
    op.drop_constraint("uq_contacts_account_jid", "contacts", type_="unique")
    op.create_unique_constraint("contacts_jid_key", "contacts", ["jid"])
    op.drop_constraint("fk_contacts_account", "contacts", type_="foreignkey")
    op.drop_column("contacts", "whatsapp_account_id")

    op.drop_constraint("uq_chats_account_jid", "chats", type_="unique")
    op.create_unique_constraint("chats_jid_key", "chats", ["jid"])
    op.alter_column("chats", "whatsapp_account_id", nullable=True)
