"""Aislar contactos por cuenta TAMBIEN en una base recien creada.

EL FALLO, Y POR QUE SOLO SE VE AL DESPLEGAR
-------------------------------------------
``b2c3d4e5f6a7`` aisla los contactos por cuenta, pero envuelve las
restricciones en la misma condicion que el relleno de datos::

    if cuentas:                       # <- cuantas filas hay en whatsapp_accounts
        op.alter_column("contacts", "whatsapp_account_id", nullable=False)
        op.drop_constraint("contacts_jid_key", ...)
        op.create_unique_constraint("uq_contacts_account_jid", ...)

El relleno SI necesita esa guarda: sin cuentas no hay a quien atribuir los
contactos que ya existen. Las restricciones no -- son estructura, y una tabla
vacia se puede apretar sin mirar nada.

El resultado es que una base **recien creada** --el caso exacto de un
despliegue-- se queda con::

    contacts.whatsapp_account_id   NULLABLE
    UNIQUE (jid)                   global, sin cuenta

Y con esa unicidad, dos cuentas NO PUEDEN tener el mismo contacto: la segunda
choca con la fila de la primera, o la pisa. El nombre que una persona le puso
a un numero en su agenda acabaria en la agenda de otra. Es el mismo fallo que
la migracion venia a cerrar, intacto en toda instalacion nueva.

Se comprobo comparando los modelos con el esquema que dejan las migraciones
sobre una base vacia: once diferencias, y estas cuatro eran las que importan.

POR QUE UNA MIGRACION NUEVA Y NO EDITAR AQUELLA
-----------------------------------------------
Porque ya esta aplicada. Cambiarla dejaria a las instalaciones existentes
diciendo que hicieron algo que no hicieron. Esta se puede aplicar encima de
las dos situaciones --la que ya apreto y la que no-- porque cada paso mira
antes si hace falta.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def _restricciones(conexion, tabla: str) -> set[str]:
    return {
        fila[0]
        for fila in conexion.execute(
            sa.text(
                # `CAST(... AS regclass)` y no `:t::regclass`: los dos
                # puntos seguidos chocan con el marcador de parametro.
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = CAST(:t AS regclass)"
            ),
            {"t": tabla},
        )
    }


def _indices(conexion, tabla: str) -> set[str]:
    return {
        fila[0]
        for fila in conexion.execute(
            sa.text("SELECT indexname FROM pg_indexes WHERE tablename = :t"),
            {"t": tabla},
        )
    }


def upgrade() -> None:
    conexion = op.get_bind()

    # Un contacto sin cuenta no se puede aislar de nada. Si quedara alguno
    # --instalacion a medio migrar-- se para: repartirlos a ojo pondria el
    # nombre que una persona le dio a un numero en la agenda de otra.
    huerfanos = conexion.execute(
        sa.text("SELECT count(*) FROM contacts WHERE whatsapp_account_id IS NULL")
    ).scalar()
    if huerfanos:
        raise RuntimeError(
            f"{huerfanos} contacto(s) sin cuenta. No se aprieta la restriccion "
            "con filas que no se pueden atribuir: ejecuta antes la migracion "
            "b2c3d4e5f6a7 sobre una base con cuentas, o borra esas filas."
        )

    restricciones = _restricciones(conexion, "contacts")
    indices = _indices(conexion, "contacts")

    # 1. La columna es obligatoria: un contacto siempre es de alguien.
    op.alter_column(
        "contacts",
        "whatsapp_account_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    # 2. Y apunta a una cuenta que existe. Sin la clave ajena, borrar una
    #    cuenta dejaria sus contactos sueltos y visibles para el siguiente.
    if "fk_contacts_account" not in restricciones:
        op.create_foreign_key(
            "fk_contacts_account",
            "contacts",
            "whatsapp_accounts",
            ["whatsapp_account_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # 3. LA UNICIDAD, que es el corazon del asunto.
    #
    #    `UNIQUE (jid)` dice "este numero solo puede estar una vez en toda la
    #    instalacion". Con dos cuentas eso es falso y peligroso: el mismo
    #    contacto esta en las dos, con nombres distintos.
    if "contacts_jid_key" in restricciones:
        op.drop_constraint("contacts_jid_key", "contacts", type_="unique")
    if "uq_contacts_account_jid" not in restricciones:
        op.create_unique_constraint(
            "uq_contacts_account_jid", "contacts", ["whatsapp_account_id", "jid"]
        )

    # 4. Y el indice por cuenta: los listados filtran siempre por ella.
    if "ix_contacts_account" not in indices:
        op.create_index("ix_contacts_account", "contacts", ["whatsapp_account_id"])


def downgrade() -> None:
    # Volver a `UNIQUE (jid)` solo es posible si no hay dos cuentas con el
    # mismo contacto. Si las hay, deshacer perderia una de las dos agendas, y
    # eso no lo hace una migracion en silencio.
    conexion = op.get_bind()
    repetidos = conexion.execute(
        sa.text(
            "SELECT count(*) FROM (SELECT jid FROM contacts "
            "GROUP BY jid HAVING count(*) > 1) x"
        )
    ).scalar()
    if repetidos:
        raise RuntimeError(
            f"{repetidos} contacto(s) estan en mas de una cuenta. Deshacer "
            "obligaria a borrar la agenda de una de ellas."
        )

    indices = _indices(conexion, "contacts")
    restricciones = _restricciones(conexion, "contacts")
    if "ix_contacts_account" in indices:
        op.drop_index("ix_contacts_account", table_name="contacts")
    if "uq_contacts_account_jid" in restricciones:
        op.drop_constraint("uq_contacts_account_jid", "contacts", type_="unique")
    op.create_unique_constraint("contacts_jid_key", "contacts", ["jid"])
    if "fk_contacts_account" in restricciones:
        op.drop_constraint("fk_contacts_account", "contacts", type_="foreignkey")
    op.alter_column(
        "contacts",
        "whatsapp_account_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=True,
    )
