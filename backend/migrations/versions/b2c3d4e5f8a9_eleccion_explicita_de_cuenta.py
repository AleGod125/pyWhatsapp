"""Distinguir la cuenta que ELIGIO el usuario de la que puso el sistema.

EL FALLO
--------
Se vinculaba el segundo telefono, se seleccionaba en el selector, y setenta
segundos despues el sistema volvia solo al primero::

    [API] la cuenta activa no era presentable; se pasa a 31198759

`is_active` dice cual se esta mirando, pero no de donde salio. Y esas dos
cosas se tratan distinto:

* una cuenta que la PERSONA eligio se respeta aunque parezca vacia -- acaba de
  vincularse y todavia no ha traido ni un chat;
* una que puso el sistema por defecto se puede sustituir sin preguntar.

Sin la distincion, la unica regla posible era "si no parece presentable,
cambia", y esa regla movia al usuario de cuenta en silencio.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f8a9"
down_revision = "a1b2c3d4e5f7"
branch_labels = None
depends_on = None

TABLA = "user_whatsapp_memberships"
COLUMNA = "elegida_por_el_usuario"


def upgrade() -> None:
    conexion = op.get_bind()
    columnas = {
        f[0]
        for f in conexion.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"
            ),
            {"t": TABLA},
        )
    }
    if COLUMNA not in columnas:
        op.add_column(
            TABLA,
            sa.Column(
                COLUMNA,
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
    # Lo que ya estaba activo NO se marca como elegido: no consta que nadie lo
    # pidiera, y darlo por elegido congelaria una eleccion que quiza hizo el
    # sistema. En cuanto la persona toque el selector, se marca.


def downgrade() -> None:
    op.drop_column(TABLA, COLUMNA)
