"""Membresias: quien puede entrar a que cuenta de WhatsApp.

EL FALLO QUE CIERRA
-------------------
Un usuario recien registrado, sin ninguna cuenta de WhatsApp, veia "cuenta
vinculada" y entraba al panel. La razon: el backend miraba el estado GLOBAL
del proceso --habia OTRA cuenta conectada en el servidor-- en vez de mirar que
tenia ESE usuario.

Con esta tabla el acceso deja de ser una propiedad del proceso y pasa a ser un
hecho de la base: o hay una fila que asocia a esta persona con una cuenta, o
no la hay.

LAS DOS REGLAS
--------------
``UNIQUE(user_id)``      una persona, como mucho UNA cuenta de WhatsApp.
sin unicidad de cuenta   varias personas SI pueden compartir una cuenta.

La asimetria es deliberada: la segunda es la puerta que esta tabla abre
--socios, parejas, equipos-- sin duplicar sesion, Signal Store ni mensajes.

EL BACKFILL
-----------
Cada vinculacion que ya existe se convierte en una membresia ``owner`` de
quien la creo. Es idempotente: se salta las que ya estan, asi que volver a
ejecutarla no duplica nada ni falla.

Revision ID: f1a2b3c4d5e6
Revises: a3d71c9b40e2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "a3d71c9b40e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_whatsapp_memberships",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "whatsapp_account_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False, server_default="owner"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("role IN ('owner','member')", name="ck_memberships_role"),
        # UNA cuenta de WhatsApp por persona: la regla de producto, en la base.
        sa.UniqueConstraint("user_id", name="uq_memberships_user"),
        sa.UniqueConstraint(
            "user_id", "whatsapp_account_id", name="uq_memberships_user_account"
        ),
    )
    op.create_index(
        "ix_memberships_account", "user_whatsapp_memberships", ["whatsapp_account_id"]
    )

    # -- Backfill: no se pierde ninguna asociacion actual --------------------
    #
    # Cada vinculacion existente pasa a ser una membresia `owner` de quien la
    # creo. `NOT EXISTS` la hace idempotente, y `DISTINCT ON` protege del caso
    # --hoy imposible, manana quiza no-- de un usuario con dos cuentas: se
    # queda con la mas antigua en vez de reventar contra `uq_memberships_user`.
    op.execute(
        """
        INSERT INTO user_whatsapp_memberships (user_id, whatsapp_account_id, role)
        SELECT DISTINCT ON (wa.user_id) wa.user_id, wa.id, 'owner'
        FROM whatsapp_accounts wa
        WHERE NOT EXISTS (
            SELECT 1 FROM user_whatsapp_memberships m WHERE m.user_id = wa.user_id
        )
        ORDER BY wa.user_id, wa.created_at ASC
        """
    )


def downgrade() -> None:
    # Solo se retira la tabla nueva. `whatsapp_accounts.user_id` no se toca en
    # ningun momento, asi que al bajar la asociacion original sigue intacta y
    # no se pierde el acceso de nadie.
    op.drop_index("ix_memberships_account", table_name="user_whatsapp_memberships")
    op.drop_table("user_whatsapp_memberships")
