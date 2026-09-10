"""Un telefono, una cuenta por usuario.

EL FALLO QUE ESTO CIERRA
------------------------
Nada en el esquema impedia vincular el mismo movil dos veces bajo el mismo
usuario. Y no es teorico: se midio.

    fila 93c64772   telefono 573008927374   132 chats   1138 mensajes
    fila 91a8470b   telefono 573008927374   327 chats   6620 mensajes

El mismo telefono, vinculado a las 10:11 y otra vez a las 10:47. Las 132
conversaciones de la primera estaban tambien en la segunda, con los MISMOS
identificadores de mensaje -- 654 de 654 en la mas grande. No son dos copias:
es una copia contada dos veces, que ademas hace que dos workers compitan por
la misma sesion de Signal.

Habia una comprobacion en el codigo (`marcar_vinculada`), y sigue estando
porque da un mensaje util. Pero una regla que solo vive en el codigo se salta
por cualquier camino que no pase por ella.

POR QUE `wa_pn` Y NO `phone_number`
-----------------------------------
`phone_number` es la etiqueta, y es justamente el campo que puede estar mal:
se midio una fila con el numero de una persona y la sesion de otra. `wa_pn` se
copia de `creds.json`, que es lo que contesta WhatsApp al vincular.

LOS NULL NO CHOCAN, Y ESO ES LO QUE SE QUIERE
---------------------------------------------
PostgreSQL trata los NULL como distintos entre si en una restriccion unica.
Asi que varias cuentas creadas y todavia sin vincular --``wa_pn IS NULL``--
conviven sin problema, que es el estado normal de quien acaba de pulsar
"Agregar cuenta". Solo choca lo que de verdad es el mismo telefono.

Un ``GROUP BY user_id, wa_pn`` SI agrupa los NULL, asi que al buscar
duplicados antes de aplicar esto aparecen como si chocaran. No chocan.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f7a8b9c0d1e2"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None

NOMBRE = "uq_whatsapp_accounts_user_pn"


def upgrade() -> None:
    conexion = op.get_bind()

    # Con dos filas del mismo telefono la restriccion no se puede crear, y
    # elegir cual sobra no lo decide una migracion en silencio: una de las dos
    # tiene los chats del usuario colgando.
    repetidos = conexion.execute(
        sa.text(
            "SELECT count(*) FROM (SELECT user_id, wa_pn FROM whatsapp_accounts "
            "WHERE wa_pn IS NOT NULL GROUP BY user_id, wa_pn "
            "HAVING count(*) > 1) x"
        )
    ).scalar()
    if repetidos:
        raise RuntimeError(
            f"{repetidos} telefono(s) vinculados mas de una vez al mismo "
            "usuario. Hay que quedarse con una fila --la que tenga los chats-- "
            "antes de aplicar esta migracion. No se elige aqui: borrar la que "
            "no toca se lleva por delante las conversaciones."
        )

    ya_esta = conexion.execute(
        sa.text(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = CAST('whatsapp_accounts' AS regclass) AND conname = :n"
        ),
        {"n": NOMBRE},
    ).scalar()
    if not ya_esta:
        op.create_unique_constraint(
            NOMBRE, "whatsapp_accounts", ["user_id", "wa_pn"]
        )


def downgrade() -> None:
    op.drop_constraint(NOMBRE, "whatsapp_accounts", type_="unique")
