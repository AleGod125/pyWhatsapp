"""Motor y sesiones de PostgreSQL.

Este modulo es lo unico que sabe COMO se llega a la base de datos. El resto de
la aplicacion recibe sesiones y nunca construye una URL de conexion, de modo
que mover PostgreSQL de localhost a un servidor remoto sea un cambio de .env
y no un cambio de codigo.

Ningun mensaje de error de aqui incluye la password: se usa siempre
``settings.safe_database_url()``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.logging_setup import get_logger

log = get_logger("DB")


class DatabaseError(RuntimeError):
    """Fallo al conectar o inicializar PostgreSQL, ya con mensaje presentable."""


class Database:
    """Envoltorio del engine de SQLAlchemy con el ciclo de vida explicito."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None

    # -- Ciclo de vida -------------------------------------------------------

    def connect(self) -> None:
        """Crea el engine y verifica que PostgreSQL responde de verdad.

        ``create_engine`` es perezoso, asi que no basta: se abre una conexion
        y se ejecuta un SELECT para que un servidor caido falle aqui y no en
        medio de la ingesta.
        """
        log.info("Conectando a PostgreSQL...")
        log.debug("URL: %s", self._settings.safe_database_url())

        try:
            self._engine = create_engine(
                self._settings.database_url,
                # pool_pre_ping evita entregar conexiones muertas tras una
                # pausa larga (habitual: backfill de horas).
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=5,
                # El SQL se loguea solo con APP_DEBUG y LOG_LEVEL=DEBUG.
                echo=False,
                future=True,
            )
        except SQLAlchemyError as exc:
            raise DatabaseError(
                f"ERROR: no fue posible crear el motor de PostgreSQL "
                f"({self._settings.safe_database_url()}): {_clean(exc)}"
            ) from exc

        try:
            with self._engine.connect() as conn:
                version = conn.execute(text("SHOW server_version")).scalar_one()
                current_db = conn.execute(text("SELECT current_database()")).scalar_one()
        except SQLAlchemyError as exc:
            self._engine.dispose()
            self._engine = None
            raise DatabaseError(
                f"ERROR: no fue posible conectar con PostgreSQL en "
                f"{self._settings.postgres_host}:{self._settings.postgres_port} "
                f"(base '{self._settings.postgres_db}').\n"
                f"        Detalle: {_clean(exc)}\n"
                f"        Revisa que el servicio este arrancado, que la base exista "
                f"y que POSTGRES_USER / POSTGRES_PASSWORD del .env sean correctos."
            ) from exc

        self._session_factory = sessionmaker(
            bind=self._engine, expire_on_commit=False, future=True
        )
        log.info("PostgreSQL disponible (server_version=%s, base=%s)", version, current_db)

    def dispose(self) -> None:
        """Cierra el pool. Seguro de llamar mas de una vez."""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._session_factory = None
            log.debug("Pool de conexiones cerrado")

    # -- Acceso --------------------------------------------------------------

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            raise DatabaseError("la base de datos no esta conectada: llama a connect() primero")
        return self._engine

    def session(self) -> Session:
        """Sesion suelta. El llamante es responsable de cerrarla."""
        if self._session_factory is None:
            raise DatabaseError("la base de datos no esta conectada: llama a connect() primero")
        return self._session_factory()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """Sesion con commit al salir y rollback ante cualquier excepcion.

        Es la via normal para escribir. Los lotes de ingesta abren UNA
        transaccion y hacen muchos inserts dentro: nunca un commit por mensaje.
        """
        session = self.session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- Diagnostico ---------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Datos basicos del servidor, para el health check y ``inspect_db``."""
        with self.engine.connect() as conn:
            return {
                "server_version": conn.execute(text("SHOW server_version")).scalar_one(),
                "database": conn.execute(text("SELECT current_database()")).scalar_one(),
                "user": conn.execute(text("SELECT current_user")).scalar_one(),
                "encoding": conn.execute(
                    text("SELECT pg_encoding_to_char(encoding) FROM pg_database "
                         "WHERE datname = current_database()")
                ).scalar_one(),
            }

    def applied_migration(self) -> str | None:
        """Revision actual segun ``alembic_version``. ``None`` si no hay tabla."""
        with self.engine.connect() as conn:
            exists = conn.execute(
                text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            ).scalar_one()
            if not exists:
                return None
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()


def _clean(exc: Exception) -> str:
    """Primera linea del error, sin la URL con credenciales que SQLAlchemy adjunta."""
    message = str(exc).split("\n")[0].strip()
    # SQLAlchemy anade "(Background on this error at: https://...)".
    marker = "(Background on this error"
    if marker in message:
        message = message.split(marker)[0].strip()
    return message
