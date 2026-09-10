"""La cuenta de WhatsApp, tambien en `media_files`.

POR QUE, SI YA SE PODIA DEDUCIR
-------------------------------
`media_files.chat_id` es NOT NULL y apunta a `chats`, que si tiene
`whatsapp_account_id`. Asi que la cuenta de un adjunto SIEMPRE se ha podido
saber -- con un join.

El problema no es que no se pueda: es que hay que acordarse. Toda consulta de
multimedia que no una con `chats` no sabe de quien es lo que devuelve, y nada
se lo recuerda. Con la columna aqui, filtrar por cuenta es imposible de
olvidar, y una consulta escrita deprisa no puede entregar el adjunto de otra
persona.

Es redundante A PROPOSITO. La clave ajena a `chats` sigue siendo la verdad;
esto es la misma verdad puesta donde se usa.

SE RELLENA DESDE `chats`, DE UN SALTO
-------------------------------------
No desde `messages`: `chat_id` ya esta en la propia fila y es obligatorio, asi
que el camino corto es tambien el fiable. Pasar por `messages` anadiria un
join que puede no casar --un adjunto cuyo mensaje se purgo-- y dejaria filas
sin rellenar sin motivo.

QUEDA NULLABLE
--------------
Un adjunto sin cuenta ya no deberia poder crearse, pero apretar la columna en
esta migracion obligaria a decidir que hacer con las filas que no se puedan
atribuir, y borrar multimedia de alguien no lo decide una migracion en
silencio. La clave ajena si va puesta: si se borra la cuenta, sus adjuntos se
van con ella.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f7"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conexion = op.get_bind()

    columnas = {
        f[0]
        for f in conexion.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'media_files'"
            )
        )
    }
    if "whatsapp_account_id" not in columnas:
        op.add_column(
            "media_files",
            sa.Column(
                "whatsapp_account_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )

    # Rellenar desde el chat padre. Un solo salto.
    rellenadas = conexion.execute(
        sa.text(
            "UPDATE media_files mf SET whatsapp_account_id = c.whatsapp_account_id "
            "FROM chats c WHERE c.id = mf.chat_id "
            "AND mf.whatsapp_account_id IS DISTINCT FROM c.whatsapp_account_id"
        )
    ).rowcount
    if rellenadas:
        print(f"  {rellenadas} adjunto(s) atribuidos a su cuenta")

    restricciones = {
        f[0]
        for f in conexion.execute(
            sa.text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = CAST('media_files' AS regclass)"
            )
        )
    }
    if "fk_media_files_account" not in restricciones:
        op.create_foreign_key(
            "fk_media_files_account",
            "media_files",
            "whatsapp_accounts",
            ["whatsapp_account_id"],
            ["id"],
            ondelete="CASCADE",
        )

    indices = {
        f[0]
        for f in conexion.execute(
            sa.text("SELECT indexname FROM pg_indexes WHERE tablename = 'media_files'")
        )
    }
    if "ix_media_files_account" not in indices:
        op.create_index(
            "ix_media_files_account", "media_files", ["whatsapp_account_id"]
        )


def downgrade() -> None:
    conexion = op.get_bind()
    indices = {
        f[0]
        for f in conexion.execute(
            sa.text("SELECT indexname FROM pg_indexes WHERE tablename = 'media_files'")
        )
    }
    if "ix_media_files_account" in indices:
        op.drop_index("ix_media_files_account", table_name="media_files")
    op.drop_constraint("fk_media_files_account", "media_files", type_="foreignkey")
    op.drop_column("media_files", "whatsapp_account_id")
