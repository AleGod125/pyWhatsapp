"""Cursor persistido y reintentos de historial

El cursor de un chat vivia repartido: el motor lo recalculaba desde
``messages`` en cada vuelta y el Plan E anotaba anclas en ``history_seeds``.
Ninguno de los dos guardaba ``oldestMsgFromMe``, que SI viaja en la peticion,
ni cuando se puede volver a intentar tras un timeout.

Aqui se le da sitio a las dos cosas:

* ``oldest_from_me`` / ``cursor_source``: el cursor activo, completo, y de
  donde salio.
* ``attempt_count`` / ``last_attempt_at`` / ``next_retry_at``: la espera
  creciente entre reintentos. Sin ella un chat en 'timeout' volvia a pedirse
  en la pasada siguiente sin que nada hubiera cambiado.

Ningun mensaje se toca. Ningun cursor existente se borra.

Revision ID: a3d71c9b40e2
Revises: e85126395c4a
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3d71c9b40e2"
down_revision = "e85126395c4a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_history_state",
        sa.Column(
            "oldest_from_me",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "chat_history_state",
        sa.Column("cursor_source", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "chat_history_state",
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "chat_history_state",
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "chat_history_state",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Los chats que ya se pueden excavar quedan marcados con la procedencia
    # real de su cursor. Nada se inventa: solo se etiqueta lo que ya habia.
    op.execute(
        "UPDATE chat_history_state SET cursor_source = 'message' "
        "WHERE oldest_message_id IS NOT NULL AND cursor_source IS NULL"
    )
    # Un chat en 'timeout' puede reintentarse ya: la espera empieza a contar
    # desde el proximo intento, no retroactivamente.
    op.create_index(
        "ix_history_state_next_retry", "chat_history_state", ["next_retry_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_history_state_next_retry", table_name="chat_history_state")
    for columna in (
        "next_retry_at",
        "last_attempt_at",
        "attempt_count",
        "cursor_source",
        "oldest_from_me",
    ):
        op.drop_column("chat_history_state", columna)
