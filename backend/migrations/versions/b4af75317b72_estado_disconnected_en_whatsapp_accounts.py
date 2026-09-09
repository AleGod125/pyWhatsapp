"""estado disconnected en whatsapp_accounts

Revision ID: b4af75317b72
Revises: effd3e8aa39f
Create Date: 2026-09-03 16:34:34.911143

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4af75317b72'
down_revision: Union[str, Sequence[str], None] = 'effd3e8aa39f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Anade 'disconnected' a los estados permitidos.

    El socket se cae constantemente —red, suspension, reinicio del servicio—
    y eso NO desvincula nada. Sin este estado habria que elegir entre mentir
    ("linked" con el socket caido) o desvincular al usuario cada vez que se
    va el wifi.
    """
    op.drop_constraint("ck_whatsapp_accounts_status", "whatsapp_accounts", type_="check")
    op.create_check_constraint(
        "ck_whatsapp_accounts_status",
        "whatsapp_accounts",
        "session_status IN "
        "('never_linked','linked','disconnected','revoked','error')",
    )
    """Upgrade schema."""
    pass


def downgrade() -> None:
    # Las filas en 'disconnected' pasan a 'linked': siguen vinculadas, que es
    # lo que ese estado significa.
    op.execute(
        "UPDATE whatsapp_accounts SET session_status = 'linked' "
        "WHERE session_status = 'disconnected'"
    )
    op.drop_constraint("ck_whatsapp_accounts_status", "whatsapp_accounts", type_="check")
    op.create_check_constraint(
        "ck_whatsapp_accounts_status",
        "whatsapp_accounts",
        "session_status IN ('never_linked','linked','revoked','error')",
    )
    """Downgrade schema."""
    pass
