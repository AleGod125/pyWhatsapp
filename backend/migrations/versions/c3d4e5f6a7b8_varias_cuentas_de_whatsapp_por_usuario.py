"""Varias cuentas de WhatsApp por usuario.

LO QUE LO IMPEDIA
-----------------
Una sola restriccion, puesta a proposito::

    UniqueConstraint("user_id", name="uq_memberships_user")
    # "UNA cuenta de WhatsApp por persona. La regla de producto, en la base."

Era correcta mientras no habia forma de elegir cuenta en la interfaz: soportar
varias sin selector solo produce estados que nadie puede resolver. Ahora si la
hay, asi que la regla cambia.

QUE LA SUSTITUYE
----------------
No "cualquier cosa": otra regla, mas estrecha de lo que parece. Un usuario
puede tener N cuentas pero **solo una ACTIVA**, y eso se sostiene con un
indice unico parcial::

    UNIQUE (user_id) WHERE is_active

Asi la base garantiza que no puedan quedar dos activas a la vez. Sin eso, dos
peticiones simultaneas dejarian al usuario con dos cuentas activas y la
siguiente lectura elegiria una al azar -- el mismo fallo que se acaba de
cerrar en ``cuentas_de``, pero un nivel mas abajo y mas dificil de ver.

LOS METADATOS
-------------
``display_name`` es lo que el usuario lee y puede cambiar. Se rellena al
vincular con el nombre del perfil que da WhatsApp (``creds.me.name``), pero es
SUYO: renombrarla no toca nada mas.

``account_type`` sale de lo que declare el propio WhatsApp. Vale ``unknown``
mientras no se pueda determinar de forma fiable: inventarse "personal" seria
peor que no decir nada.

LO QUE NO SE TOCA
-----------------
Ni una carpeta de Drive, ni una sesion, ni un mensaje. La copia de seguridad
ya vivia en ``accounts/<id>/`` --por id de CUENTA, no de usuario-- asi que la
segunda cuenta estrena su espacio sin rozar el de la primera.

Y ``app_state`` se queda como esta: sus claves ya llevan el id de la cuenta
dentro (``backfill_session_fingerprint:<uuid>``). Anadir una columna con esa
misma informacion crearia dos fuentes para el mismo dato, que es como se
empieza a divergir.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- Metadatos de la cuenta ---------------------------------------------
    op.add_column(
        "whatsapp_accounts",
        sa.Column("display_name", sa.Text(), nullable=True),
    )
    op.add_column(
        "whatsapp_accounts",
        sa.Column(
            "account_type",
            sa.String(length=16),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "whatsapp_accounts",
        sa.Column("avatar_url", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_whatsapp_accounts_type",
        "whatsapp_accounts",
        "account_type IN ('personal','business','unknown')",
    )

    # -- La cuenta ACTIVA de cada usuario -----------------------------------
    op.add_column(
        "user_whatsapp_memberships",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # La que ya tenia se queda activa. Sin esto, alguien con una sola cuenta
    # abriria la aplicacion sin ninguna seleccionada y no veria sus chats.
    op.execute(
        """
        UPDATE user_whatsapp_memberships SET is_active = true
        WHERE id IN (
            SELECT DISTINCT ON (user_id) id
            FROM user_whatsapp_memberships
            ORDER BY user_id, created_at, id
        )
        """
    )

    # -- Fuera el techo de una cuenta ---------------------------------------
    op.drop_constraint(
        "uq_memberships_user", "user_whatsapp_memberships", type_="unique"
    )

    # Y la regla que lo sustituye: como mucho UNA activa por usuario.
    op.create_index(
        "uq_memberships_activa",
        "user_whatsapp_memberships",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    # Volver atras solo es posible si nadie ha aprovechado el cambio: con dos
    # cuentas por usuario, restaurar la restriccion fallaria a mitad y dejaria
    # la base en un estado peor que el de partida. Se comprueba antes.
    filas = (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT count(*) FROM (
                    SELECT user_id FROM user_whatsapp_memberships
                    GROUP BY user_id HAVING count(*) > 1
                ) t
                """
            )
        )
        .scalar()
    )
    if filas:
        raise RuntimeError(
            f"{filas} usuario(s) tienen mas de una cuenta de WhatsApp. "
            "Volver atras dejaria sin acceso a las demas: primero hay que "
            "decidir cual se conserva."
        )

    op.drop_index("uq_memberships_activa", table_name="user_whatsapp_memberships")
    op.create_unique_constraint(
        "uq_memberships_user", "user_whatsapp_memberships", ["user_id"]
    )
    op.drop_column("user_whatsapp_memberships", "is_active")

    op.drop_constraint(
        "ck_whatsapp_accounts_type", "whatsapp_accounts", type_="check"
    )
    op.drop_column("whatsapp_accounts", "avatar_url")
    op.drop_column("whatsapp_accounts", "account_type")
    op.drop_column("whatsapp_accounts", "display_name")
