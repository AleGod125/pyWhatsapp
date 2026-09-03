"""estados de semilla en chat_history_state

Anade dos estados al CHECK de ``history_status``:

``waiting_seed``
    WhatsApp entrego la conversacion pero SIN ningun mensaje, asi que no hay
    ancla desde la que pedir historial. NO es "sincronizado": es "esperando
    una semilla". En cuanto llegue un mensaje real, ese mensaje se convierte
    en ancla y el chat pasa a ``pending`` para excavarse.

``empty_confirmed``
    Se comprobo que la conversacion esta vacia de verdad.

Por que hacia falta: los 32 chats que quedaban en ``no_valid_cursor`` se
presentaban igual que los terminados, y en el telefono SI tenian mensajes.
Decir "historial sincronizado" con ``message_count=0`` era falso.

Los datos existentes NO se tocan aqui: la reclasificacion la hace el
mantenimiento, con evidencia, y es reversible.

Revision ID: e4cf6b0f8590
Revises: 8c123f819019
"""

from __future__ import annotations

from alembic import op

revision = "e4cf6b0f8590"
down_revision = "8c123f819019"
branch_labels = None
depends_on = None

_ANTIGUOS = (
    "pending", "fetching", "exhausted", "server_limited",
    "timeout", "error", "no_valid_cursor",
)
_NUEVOS = _ANTIGUOS + ("waiting_seed", "empty_confirmed")


def _check(valores: tuple[str, ...]) -> str:
    lista = ", ".join(f"'{v}'" for v in valores)
    return f"history_status IN ({lista})"


def upgrade() -> None:
    op.drop_constraint("ck_history_state_status", "chat_history_state", type_="check")
    op.create_check_constraint(
        "ck_history_state_status", "chat_history_state", _check(_NUEVOS)
    )


def downgrade() -> None:
    # Antes de estrechar el CHECK hay que devolver las filas que usan los
    # estados nuevos a uno que el CHECK antiguo acepte. Sin esto el downgrade
    # falla contra datos reales.
    op.execute(
        "UPDATE chat_history_state "
        "SET history_status = 'no_valid_cursor' "
        "WHERE history_status IN ('waiting_seed', 'empty_confirmed')"
    )
    op.drop_constraint("ck_history_state_status", "chat_history_state", type_="check")
    op.create_check_constraint(
        "ck_history_state_status", "chat_history_state", _check(_ANTIGUOS)
    )
