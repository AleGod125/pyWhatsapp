"""La unicidad va sobre el TELEFONO, no sobre `wa_pn`.

POR QUE LA ANTERIOR NO SERVIA
-----------------------------
`f7a8b9c0d1e2` puso `UNIQUE (user_id, wa_pn)` para que un telefono no se
pudiera vincular dos veces. No lo impedia.

`wa_pn` llega de WhatsApp con el identificador de DISPOSITIVO pegado, y ese
cambia en cada re-vinculacion. Se midio, con la restriccion ya puesta::

    e2492d66   wa_pn = 573008927374@s.whatsapp.net       linked
    4f9774ab   wa_pn = 573008927374:30@s.whatsapp.net    revoked

Dos filas, el mismo telefono, y PostgreSQL sin nada que objetar: las cadenas
son distintas. Bastaba con volver a escanear.

`phone_number` guarda solo los digitos --``573008927374``--, que es lo que de
verdad identifica a una persona.

LOS DUPLICADOS QUE YA HAY
-------------------------
No se pueden dejar: la restriccion no se crearia. Y no se borran a ciegas.

Se borra UNICAMENTE lo que no puede contener nada de nadie: filas repetidas
por telefono que ademas no tienen ni un chat, ni un mensaje, y no estan
``linked``. Son el rastro de vinculaciones a medias.

Si tras eso siguen quedando duplicados, la migracion PARA. Elegir cual de dos
filas con conversaciones sobra no lo decide una migracion en silencio.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f9a0b1"
down_revision = "b2c3d4e5f8a9"
branch_labels = None
depends_on = None

VIEJA = "uq_whatsapp_accounts_user_pn"
NUEVA = "uq_whatsapp_accounts_user_phone"


def _restricciones(conexion) -> set[str]:
    return {
        f[0]
        for f in conexion.execute(
            sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid = "
                "CAST('whatsapp_accounts' AS regclass)"
            )
        )
    }


def upgrade() -> None:
    conexion = op.get_bind()

    if VIEJA in _restricciones(conexion):
        op.drop_constraint(VIEJA, "whatsapp_accounts", type_="unique")

    # Vacias, repetidas y no vinculadas: rastro de intentos a medias.
    borradas = conexion.execute(
        sa.text(
            """
            DELETE FROM whatsapp_accounts a
            WHERE a.phone_number IS NOT NULL
              AND a.session_status <> 'linked'
              AND NOT EXISTS (SELECT 1 FROM chats c
                              WHERE c.whatsapp_account_id = a.id)
              AND EXISTS (SELECT 1 FROM whatsapp_accounts b
                          WHERE b.user_id = a.user_id
                            AND b.phone_number = a.phone_number
                            AND b.id <> a.id)
            """
        )
    ).rowcount
    if borradas:
        print(f"  {borradas} fila(s) repetidas y VACIAS borradas")

    repetidos = conexion.execute(
        sa.text(
            "SELECT count(*) FROM (SELECT user_id, phone_number "
            "FROM whatsapp_accounts WHERE phone_number IS NOT NULL "
            "GROUP BY user_id, phone_number HAVING count(*) > 1) x"
        )
    ).scalar()
    if repetidos:
        raise RuntimeError(
            f"{repetidos} telefono(s) siguen en mas de una cuenta del mismo "
            "usuario, y esas filas TIENEN conversaciones. Hay que decidir cual "
            "se queda antes de aplicar esto: borrar la que no toca se lleva "
            "por delante los chats de alguien."
        )

    if NUEVA not in _restricciones(conexion):
        op.create_unique_constraint(
            NUEVA, "whatsapp_accounts", ["user_id", "phone_number"]
        )


def downgrade() -> None:
    conexion = op.get_bind()
    if NUEVA in _restricciones(conexion):
        op.drop_constraint(NUEVA, "whatsapp_accounts", type_="unique")
    op.create_unique_constraint(VIEJA, "whatsapp_accounts", ["user_id", "wa_pn"])
