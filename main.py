"""Punto de entrada de whatsapp_backup.

Flujo:

    cargar .env -> conectar PostgreSQL -> verificar migraciones
    -> aplicar compatibilidades -> resolver version de WhatsApp
    -> buscar sesion
         si NO existe: QR -> escaneo fisico -> pair-success -> persistir
         si existe:    login directo
    -> connected

Uso:
    python main.py                 arranque normal
    python main.py --no-gui        sin ventana (solo logs); el QR no se pinta
    python main.py --check         solo comprueba entorno, DB y compatibilidades
    python main.py --fresh         archiva la sesion actual y vuelve a vincular
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
from typing import Any

from app.core.config import ConfigError, Settings, load_settings
from app.core.database import Database, DatabaseError
from app.core.logging_setup import get_logger, setup_logging
from app.whatsapp_client import ClientEvent, WhatsAppClient, archive_session, prepare_pywhats

log = get_logger("APP")
wa_log = get_logger("WA")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py", description="Backup local de WhatsApp via dispositivo vinculado"
    )
    parser.add_argument("--no-gui", action="store_true", help="no abrir la ventana de Tkinter")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verificar entorno, PostgreSQL y compatibilidades, y salir",
    )
    parser.add_argument(
        "--viewer",
        action="store_true",
        help="abrir solo el visor de chats sobre PostgreSQL, sin conectar a WhatsApp",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help=(
            "archivar la sesion actual en diagnostics/ y vincular de nuevo. "
            "NO borra PostgreSQL, ni multimedia, ni diagnosticos."
        ),
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Arranque por etapas
# ---------------------------------------------------------------------------


def stored_summary(database: Database) -> dict[str, int]:
    """Cuantos datos hay ya guardados. Se consulta a PostgreSQL, no a memoria."""
    from sqlalchemy import func, select

    from app.models import Chat, Contact, Message

    with database.transaction() as session:
        return {
            "chats": session.execute(select(func.count()).select_from(Chat)).scalar_one(),
            "messages": session.execute(select(func.count()).select_from(Message)).scalar_one(),
            "contacts": session.execute(select(func.count()).select_from(Contact)).scalar_one(),
        }


def describe_environment(settings: Settings, summary: dict[str, Any]) -> None:
    version = summary.get("wa_version")
    origin = summary.get("wa_version_origin")
    if version is not None:
        log.info("Version de WhatsApp Web: %s (%s)", ".".join(map(str, version)), origin)


# ---------------------------------------------------------------------------
# Cableado de eventos
# ---------------------------------------------------------------------------


def wire_gui(app: Any, runtime: Any) -> None:
    """Conecta eventos y estado con las vistas.

    Es lo UNICO especifico de Tkinter que queda aqui. Todo lo demas (base,
    sesion, ingesta, backfill, multimedia, mantenimiento) lo monta
    ``AppRuntime``, que es exactamente el mismo que usa ``service.py``.

    La regla que gobierna esto: el visor NO se abre hasta que el servidor
    confirma el login. ``connected`` de pywhats no basta, porque se emite en
    cuanto termina el handshake, antes de que el servidor conteste.
    """
    from app.gui import ACCENT, ERROR, FG, MUTED, WARN
    from app.core.session_state import AppState

    settings = runtime.settings
    state = runtime.state
    pairing = app.pairing
    app.session_state = state

    # -- Pairing --

    def on_qr(event: ClientEvent) -> None:
        state.set(AppState.PAIRING_REQUIRED, reason="QR emitido")
        app.show_pairing()
        # El payload se pasa TAL CUAL: no se recorta ni se normaliza.
        app.show_qr(event.payload)
        pairing.set_status("Esperando escaneo", color=MUTED)

    def on_qr_expired(event: ClientEvent) -> None:
        data = event.payload or {}
        pairing.set_status(
            f"El codigo caduco sin escanearse. Generando uno nuevo "
            f"({data.get('attempt')}/{data.get('attempts')})...",
            color=WARN,
        )

    def on_paired(event: ClientEvent) -> None:
        log.info("Dispositivo vinculado (jid=%s)", event.payload)
        pairing.set_status("Vinculando dispositivo...", color=WARN)

    # -- Ciclo de vida de la sesion --

    def on_connected(_event: ClientEvent) -> None:
        # Provisional: el handshake termino, pero el servidor todavia puede
        # rechazar el login con un <failure reason="401">.
        state.set(AppState.CONNECTING, reason="handshake completado")
        wa_log.info("Validando sesion existente...")
        app.show_status(
            "Conectando con WhatsApp...",
            "Comprobando que la sesion guardada sigue vinculada.",
        )

    def on_session_valid(_event: ClientEvent) -> None:
        """Llego el <success>: el servidor acepto el login. AHORA si."""
        if state.state in (AppState.SESSION_INVALID, AppState.PAIRING_REQUIRED):
            return
        state.set(AppState.CONNECTED, reason="<success> del servidor")
        wa_log.info("Sesion valida")
        log.info("Conectado")
        app.show_viewer(connected=True)
        log.info("Visor habilitado")

    def on_logged_out(event: ClientEvent) -> None:
        reason = event.payload
        wa_log.error("Login rechazado reason=%s", reason)
        state.set(AppState.SESSION_INVALID, reason=f"login rechazado {reason}")
        wa_log.warning("Sesion invalida")
        log.info("Visor bloqueado")
        app.show_status(
            "Esta sesion ya no esta vinculada",
            "\n".join(
                (
                    "WhatsApp rechazo la sesion guardada.",
                    "Debes vincular este equipo nuevamente.",
                    "",
                    "Tus datos locales NO se han borrado.",
                )
            ),
            titulo_ventana="WhatsApp Backup - Sesion invalida",
            color=ERROR,
            action=("Vincular nuevamente", lambda: _relink(reason)),
        )
        # Teardown en segundo plano para no bloquear la interfaz.
        threading.Thread(
            target=_teardown_invalid_session, args=(reason,), daemon=True
        ).start()

    def _teardown_invalid_session(reason: Any) -> None:
        """Cierra el cliente y archiva la sesion. NUNCA toca PostgreSQL."""
        wa_log.info("Cerrando cliente...")
        if runtime.client is not None:
            runtime.client.stop()
        wa_log.info("Signal Store cerrado")
        archived = archive_session(settings, reason=f"invalid-{reason}")
        if archived is not None:
            wa_log.info("Sesion archivada para diagnostico")
        wa_log.warning("Se requiere nueva vinculacion")
        state.set(AppState.PAIRING_REQUIRED, reason="sesion archivada")

    def _relink(_reason: Any) -> None:
        app.show_status(
            "Reinicia la aplicacion",
            "\n".join(
                (
                    "La sesion anterior se ha archivado en diagnostics/.",
                    "Cierra esta ventana y ejecuta de nuevo:  python main.py",
                    "",
                    "Se mostrara un codigo QR para vincular.",
                )
            ),
            color=FG,
        )

    def on_error(event: ClientEvent) -> None:
        log.error("Error del cliente: %s", event.payload)
        state.set(AppState.ERROR, reason=str(event.payload)[:120])
        app.show_status("Error de conexion", str(event.payload)[:200], color=ERROR)

    # -- Refrescos (todos condicionados al estado) --

    def _refresh_if_live(_event: ClientEvent = None) -> None:
        if app.viewer is not None and state.viewer_allowed:
            app.schedule_sidebar_refresh()

    def on_message_stored(event: ClientEvent) -> None:
        """Llega un mensaje: se toca lo minimo (seccion 34).

        Primero se intenta refrescar SOLO la fila de ese chat. Reconstruir el
        sidebar entero con cada mensaje es destruir y recrear todos los
        widgets para cambiar una linea; solo se hace cuando el chat es nuevo
        y de verdad hay que crear su fila.
        """
        if not state.viewer_allowed or app.viewer is None:
            return
        data = event.payload or {}
        chat_id = data.get("chat_id")
        if not app.viewer.refresh_chat_row(chat_id):
            app.schedule_sidebar_refresh()
        # Y en la conversacion, anadir al final en vez de repintarla: repintar
        # devuelve el scroll abajo y arrastraria a quien este leyendo mensajes
        # antiguos.
        if not app.viewer.append_new_message(chat_id):
            app.viewer.reload_if_open(chat_id)

    def on_status(event: ClientEvent) -> None:
        """Estado del trabajo de fondo -> barra inferior (secciones 29 y 30)."""
        if app.viewer is not None and event.payload is not None:
            app.viewer.update_status(event.payload)

    def on_waiting_history(_event: ClientEvent) -> None:
        wa_log.info("Historial inicial en curso; el visor ya esta disponible")

    def on_maintenance(event: ClientEvent) -> None:
        log.info("Mantenimiento automatico: %s", event.payload)
        _refresh_if_live()

    def on_history_ingested(event: ClientEvent) -> None:
        log.info("Historial ingerido (%s)", event.payload)
        _refresh_if_live()

    def on_media_downloaded(event: ClientEvent) -> None:
        log.info("Multimedia lista (%s)", event.payload)
        if app.viewer is not None and state.viewer_allowed:
            app.viewer.reload_current_chat()

    def on_backfill_done(event: ClientEvent) -> None:
        log.info("Backfill terminado (%s)", event.payload)
        _refresh_if_live()

    app.on("qr", on_qr)
    app.on("qr_expired", on_qr_expired)
    app.on("paired", on_paired)
    app.on("connected", on_connected)
    app.on("session_valid", on_session_valid)
    app.on("logged_out", on_logged_out)
    app.on("client_error", on_error)
    app.on("contacts_synced", _refresh_if_live)
    app.on("message_stored", on_message_stored)
    app.on("history_ingested", on_history_ingested)
    app.on("media_downloaded", on_media_downloaded)
    app.on("backfill_done", on_backfill_done)
    app.on("status", on_status)
    app.on("waiting_initial_history", on_waiting_history)
    app.on("maintenance_done", on_maintenance)


def wire_logging(events: queue.Queue[ClientEvent]) -> None:
    """Modo sin GUI: vuelca los eventos al log desde el hilo principal."""
    import time

    wa = get_logger("WA")
    sync = get_logger("SYNC")
    pairing_log = get_logger("PAIRING")

    while True:
        try:
            event = events.get(timeout=0.5)
        except queue.Empty:
            time.sleep(0.05)
            continue

        if event.name == "qr":
            # Nunca se imprime el payload.
            pairing_log.info("QR emitido (longitud=%d). Sin GUI no se puede escanear.",
                             len(event.payload or ""))
        elif event.name == "paired":
            pairing_log.info("Dispositivo vinculado jid=%s", event.payload)
        elif event.name == "connected":
            wa.info("Conectado")
        elif event.name == "history_sync":
            payload = event.payload
            sync.info(
                "HistorySync type=%s chunk=%s conversaciones=%s mensajes=%s",
                getattr(payload, "sync_type", "?"),
                getattr(payload, "chunk_order", "?"),
                getattr(payload, "conversation_count", "?"),
                getattr(payload, "message_count", "?"),
            )
        elif event.name in ("client_stopped", "logged_out"):
            wa.info("Cliente detenido (%s)", event.name)
            return
        elif event.name == "client_error":
            wa.error("Error del cliente: %s", event.payload)
            return


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _verificar_migraciones(runtime: Any) -> bool:
    """Sin migraciones aplicadas no se arranca: el esquema no existiria."""
    revision = runtime.database.applied_migration()
    if revision is None:
        get_logger("DB").error(
            "no hay ninguna migracion aplicada en esta base de datos.\n"
            "        Ejecuta:  python -m alembic upgrade head"
        )
        return False
    log.info("Migraciones verificadas (revision=%s)", revision)
    return True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print("[APP] Iniciando...")

    # -- Configuracion --
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[CONFIG] ERROR: {exc}", file=sys.stderr)
        return 2

    get_logger("CONFIG").info("Configuracion cargada (entorno=%s)", settings.app_env)

    # AppRuntime es la MISMA capa que usa service.py: configuracion, base,
    # sesion, ingesta, backfill, multimedia y mantenimiento. Aqui no se
    # duplica ninguna de esas cosas; solo se les pone una ventana delante.
    from app.core.lock import SessionLockedError, explain
    from app.core.runtime import build_runtime

    runtime = build_runtime(owner="main.py", settings=settings)

    # -- PostgreSQL --
    try:
        runtime.start_local()
    except DatabaseError as exc:
        get_logger("DB").error("%s", exc)
        runtime.stop()
        return 3
    if not _verificar_migraciones(runtime):
        runtime.stop()
        return 3

    exit_code = 0
    try:
        if args.fresh:
            archived = archive_session(settings, reason="fresh")
            if archived is None:
                log.info("--fresh: no habia sesion que archivar")
            else:
                log.info(
                    "--fresh: sesion archivada. PostgreSQL, multimedia y diagnosticos "
                    "NO se han tocado."
                )

        if args.check:
            summary = prepare_pywhats(settings)
            describe_environment(settings, summary)
            log.info("Comprobacion completada correctamente")
            health = runtime.database.health()
            log.info(
                "PostgreSQL %s, base=%s, encoding=%s",
                health["server_version"],
                health["database"],
                health["encoding"],
            )
            return 0

        use_gui = settings.gui_enabled and not args.no_gui
        if args.viewer and not use_gui:
            log.error("--viewer necesita la GUI; quita --no-gui o pon GUI_ENABLED=true")
            return 4

        # -- Modo visor: SOLO lectura, sin abrir la sesion ni pedir el cerrojo --
        # Por eso funciona con service.py abierto: dos lectores de PostgreSQL
        # no estorban; dos duenos de la sesion si.
        if args.viewer:
            return _run_viewer(runtime)

        # -- Se abre la sesion: aqui si hace falta el cerrojo --
        try:
            runtime.start(connect=False)
        except SessionLockedError as exc:
            log.error("%s", explain(exc))
            return 5

        stored = stored_summary(runtime.database)
        log.info(
            "Sesion %s | en PostgreSQL: %d chats, %d mensajes",
            "detectada" if runtime.session_exists else "no encontrada",
            stored["chats"],
            stored["messages"],
        )

        if not use_gui:
            # Sin ventana se hace exactamente el mismo trabajo; lo unico que
            # falta es la interfaz. Los eventos se vuelcan al log.
            runtime.client.start()
            wire_logging(runtime.subscribe().queue)
            return exit_code

        return _run_gui(runtime)

    except KeyboardInterrupt:
        log.info("Interrumpido por el usuario")
    except Exception as exc:  # noqa: BLE001 - se reporta con traza completa
        log.exception("Fallo no controlado: %s", exc)
        exit_code = 1
    finally:
        runtime.stop()
        log.info("Terminado")

    return exit_code


def _run_viewer(runtime: Any) -> int:
    """``--viewer``: la copia local, sin conectar con WhatsApp."""
    from app.core.session_state import AppState
    from app.gui import App

    log.info("Modo visor: no se conectara a WhatsApp")
    app = App(runtime.subscribe().queue, on_close=runtime.stop)
    app.attach_viewer(runtime.database.session, runtime.settings.media_dir)
    wire_gui(app, runtime)
    runtime.state.set(AppState.DISCONNECTED, reason="--viewer")
    runtime.state.set(AppState.CONNECTED, reason="modo local explicito")
    app.show_viewer(connected=False)
    app.run()
    return 0


def _run_gui(runtime: Any) -> int:
    """Arranque normal: ventana Tkinter sobre la sesion ya cableada."""
    from app.core.session_state import AppState
    from app.gui import App

    def _on_close() -> None:
        log.info("Cerrando: deteniendo trabajos y sesion...")
        runtime.stop()
        log.info("Cierre completado")

    # La ventana recibe SU PROPIA cola del bus. Cada cliente SSE recibira otra:
    # por eso el bus reparte copias en vez de repartirse los eventos.
    app = App(runtime.subscribe().queue, on_close=_on_close)
    app.attach_viewer(runtime.database.session, runtime.settings.media_dir)
    wire_gui(app, runtime)

    if runtime.session_exists:
        # Hay credenciales en disco, pero eso NO significa que sigan siendo
        # validas: el visor espera al <success> del servidor.
        log.info("Sesion existente detectada")
        runtime.state.set(AppState.CHECKING_SESSION, reason="device.json presente")
        wa_log.info("Validando sesion existente...")
        app.show_status(
            "Conectando con WhatsApp...",
            "Comprobando que la sesion guardada sigue vinculada.",
        )
    else:
        log.info("Sin sesion: hay que vincular el dispositivo")
        runtime.state.set(AppState.PAIRING_REQUIRED, reason="no hay device.json")
        app.show_pairing()

    runtime.client.start()
    app.run()  # bloquea hasta que se cierra la ventana
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
