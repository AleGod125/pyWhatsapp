"""Entorno de Alembic.

La URL de conexion NO vive en alembic.ini: se toma de la misma configuracion
que usa la aplicacion (``app.core.config.load_settings``), para que exista una sola
fuente de verdad y para que alembic.ini pueda versionarse sin secretos.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# La raiz del proyecto tiene que estar en sys.path para poder importar ``app``
# cuando Alembic se ejecuta desde cualquier directorio.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import load_settings  # noqa: E402
from app.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic compara contra este metadata para autogenerar migraciones.
target_metadata = Base.metadata

_settings = load_settings()
# set_main_option escapa los '%' porque el valor pasa por ConfigParser.
config.set_main_option("sqlalchemy.url", _settings.database_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    """Genera el SQL sin conectarse (``alembic upgrade head --sql``)."""
    context.configure(
        url=_settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Aplica las migraciones contra la base real."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Detectar cambios de tipo y de server_default en autogenerate.
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
