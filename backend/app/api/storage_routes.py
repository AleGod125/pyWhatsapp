"""Estado y preparacion del almacenamiento.

Angular no sabe que existe Google Drive: pide ``/storage/status`` y recibe
"conectado / al dia / con errores". Ningun identificador de archivo, ningun
token y ningun enlace publico cruzan esta frontera.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify

from app.auth.web import requiere_sesion, usuario_actual
from app.core.logging_setup import get_logger
from app.storage.interface import StorageAuthError, StorageError

log = get_logger("STORAGE")

storage_api = Blueprint("storage_api", __name__)


def _runtime() -> Any:
    return current_app.config["RUNTIME"]


def _error(code: str, message: str, status: int, **extra: Any):
    cuerpo: dict[str, Any] = {"error": {"code": code, "message": message}}
    cuerpo.update(extra)
    return jsonify(cuerpo), status


@storage_api.get("/storage/status")
@requiere_sesion
def storage_status():
    """Como va la copia hacia Drive.

    Se separa del estado de WhatsApp a proposito: que Drive no acepte una
    subida no significa que la sincronizacion con WhatsApp falle, y mezclarlo
    manda al usuario a buscar el problema donde no esta.
    """
    rt = _runtime()
    usuario = usuario_actual()
    almacenamiento = getattr(rt, "storage", None)

    if almacenamiento is None or not almacenamiento.habilitado:
        return jsonify(
            {
                "provider": "google_drive",
                "enabled": False,
                "connected": False,
                "authorized": False,
                "root_ready": False,
                "pending_jobs": 0,
                "failed_jobs": 0,
                "paused_jobs": 0,
                "bytes_uploaded": 0,
                "last_upload_at": None,
                "state": "disabled",
            }
        )

    google = rt.google.estado(usuario.id) if rt.google else None
    autorizado = bool(google and google.drive_authorized)
    resumen = almacenamiento.jobs.resumen(usuario.id)
    deposito = _deposito(rt, usuario.id)
    # Los trabajos en cola NO son la medida completa: un mensaje que todavia
    # no se ha agrupado en un segmento no tiene trabajo, y decir "al dia" con
    # miles asi seria mentir sobre una copia de seguridad.
    sin_subir = _mensajes_sin_subir(rt, usuario.id)

    bloqueado = False
    limite = int(getattr(rt.settings, "max_pending_storage_bytes", 0) or 0)
    if limite > 0 and resumen["pending_bytes"] >= limite:
        bloqueado = True

    return jsonify(
        {
            "provider": "google_drive",
            "enabled": True,
            "connected": bool(google and google.google_connected),
            "authorized": autorizado,
            "root_ready": bool(deposito and deposito.get("root_folder_id")),
            **{k: v for k, v in resumen.items() if k != "pending_bytes"},
            "pending_bytes": resumen["pending_bytes"],
            "bytes_uploaded": (deposito or {}).get("bytes_uploaded", 0),
            "files_uploaded": (deposito or {}).get("files_uploaded", 0),
            "last_upload_at": (deposito or {}).get("last_upload_at"),
            "encrypted": bool(
                getattr(rt.settings, "storage_encryption_enabled", True)
            ),
            "unstored_messages": sin_subir,
            "state": _estado_legible(
                autorizado=autorizado,
                resumen=resumen,
                bloqueado=bloqueado,
                sin_subir=sin_subir,
            ),
        }
    )


def _estado_legible(
    *, autorizado: bool, resumen: dict, bloqueado: bool, sin_subir: int = 0
) -> str:
    """Un solo estado para pintar. El orden es de mas urgente a menos.

    ``up_to_date`` exige que NO quede nada: ni trabajos en cola ni mensajes
    sin agrupar. Decirlo con miles de mensajes todavia en local le diria al
    usuario que su copia esta a salvo cuando no lo esta.
    """
    if not autorizado:
        return "reauthorization_required"
    if bloqueado:
        return "blocked"
    if resumen.get("paused_jobs"):
        return "paused"
    if resumen.get("failed_jobs"):
        return "error"
    if resumen.get("pending_jobs") or sin_subir:
        return "syncing"
    return "up_to_date"


def _mensajes_sin_subir(rt: Any, user_id: Any) -> int:
    """Mensajes del usuario cuyo contenido sigue solo en PostgreSQL."""
    from sqlalchemy import func, select

    from app.models import Chat, Message, WhatsAppAccount

    if rt.database is None:
        return 0
    with rt.database.transaction() as sesion:
        return int(
            sesion.execute(
                select(func.count())
                .select_from(Message)
                .join(Chat, Chat.id == Message.chat_id)
                .join(
                    WhatsAppAccount,
                    WhatsAppAccount.id == Chat.whatsapp_account_id,
                )
                .where(
                    WhatsAppAccount.user_id == user_id,
                    Message.storage_status != "ready",
                )
            ).scalar()
            or 0
        )


def _deposito(rt: Any, user_id: Any) -> dict[str, Any] | None:
    from sqlalchemy import select

    from app.models.storage import GoogleDriveStorage

    if rt.database is None:
        return None
    with rt.database.transaction() as sesion:
        fila = sesion.execute(
            select(GoogleDriveStorage).where(GoogleDriveStorage.user_id == user_id)
        ).scalar_one_or_none()
        if fila is None:
            return None
        return {
            "root_folder_id": fila.root_folder_id,
            "bytes_uploaded": int(fila.bytes_uploaded or 0),
            "files_uploaded": int(fila.files_uploaded or 0),
            "last_upload_at": (
                fila.last_upload_at.isoformat() if fila.last_upload_at else None
            ),
        }


@storage_api.post("/storage/setup")
@requiere_sesion
def storage_setup():
    """Prepara la carpeta de la copia en el Drive del usuario.

    Idempotente: si ya existe no crea otra. Se puede llamar tantas veces como
    haga falta sin ensuciar el Drive de nadie.
    """
    rt = _runtime()
    usuario = usuario_actual()

    if rt.google is None or not rt.google.estado(usuario.id).drive_authorized:
        return _error(
            "DRIVE_NOT_AUTHORIZED", "Conecta Google Drive para continuar.", 403
        )

    try:
        almacenamiento = rt.storage_para(usuario.id)
        raiz = almacenamiento.ensure_user_storage()
    except StorageAuthError as exc:
        return _error("DRIVE_NOT_AUTHORIZED", exc.message, 403)
    except StorageError as exc:
        return _error(exc.code, exc.message, 502)

    # El identificador de carpeta NO se devuelve: Angular no lo necesita para
    # nada y darselo solo amplia lo que puede filtrarse desde el navegador.
    return jsonify({"root_ready": bool(raiz)})


@storage_api.post("/storage/resume")
@requiere_sesion
def storage_resume():
    """Reanuda las subidas paradas tras reconectar Google."""
    rt = _runtime()
    almacenamiento = getattr(rt, "storage", None)
    if almacenamiento is None:
        return _error("STORAGE_UNAVAILABLE", "El almacenamiento no esta activo.", 409)
    reanudados = almacenamiento.jobs.reanudar(usuario_actual().id)
    return jsonify({"resumed": reanudados})
