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

from app.config import ConfigError, Settings, load_settings
from app.database import Database, DatabaseError
from app.logging_setup import get_logger, setup_logging
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


def start_database(settings: Settings) -> Database:
    """Conecta y comprueba que las migraciones estan aplicadas."""
    database = Database(settings)
    database.connect()

    revision = database.applied_migration()
    if revision is None:
        raise DatabaseError(
            "no hay ninguna migracion aplicada en esta base de datos.\n"
            "        Ejecuta:  python -m alembic upgrade head"
        )
    log.info("Migraciones verificadas (revision=%s)", revision)
    return database


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


def own_jid_from_session(session_file: Any) -> str | None:
    """JID propio del DeviceStore, para marcar el emisor de los mensajes propios."""
    import json

    if not session_file.exists():
        return None
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
        jid = data.get("jid")
        if isinstance(jid, dict) and jid.get("user"):
            return f"{jid['user']}@{jid.get('server', 's.whatsapp.net')}"
    except (OSError, ValueError):
        log.debug("No se pudo leer el JID propio del DeviceStore")
    return None


def describe_environment(settings: Settings, summary: dict[str, Any]) -> None:
    version = summary.get("wa_version")
    origin = summary.get("wa_version_origin")
    if version is not None:
        log.info("Version de WhatsApp Web: %s (%s)", ".".join(map(str, version)), origin)


# ---------------------------------------------------------------------------
# Cableado de eventos
# ---------------------------------------------------------------------------


def wire_gui(
    app: Any,
    client: WhatsAppClient,
    database: Database,
    settings: Settings,
    state: Any,
) -> None:
    """Conecta eventos y estado con las vistas.

    La regla que gobierna todo esto: el visor NO se abre hasta que el servidor
    confirma el login. ``connected`` de pywhats no basta, porque se emite en
    cuanto termina el handshake, antes de que el servidor conteste.
    """
    from app.gui import ACCENT, ERROR, FG, MUTED, WARN
    from app.session_state import AppState

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
        client.stop()
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
        if not state.viewer_allowed or app.viewer is None:
            return
        data = event.payload or {}
        app.viewer.refresh_chats()
        app.viewer.reload_if_open(data.get("chat_id"))

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


def wire_history_ingestion(
    client: WhatsAppClient,
    database: Database,
    own_jid: str | None,
    signal_db_path: Any,
) -> None:
    """Persiste cada History Sync en cuanto llega.

    ``signal_db_path`` se inyecta por parametro a proposito: la version
    anterior lo tomaba de un ``settings`` que no existia en este ambito y
    reventaba con NameError JUSTO al persistir, despues de que el blob ya
    hubiera llegado. Un dato que hace falta aqui dentro se pasa, no se busca
    en un global.

    El callback corre en el hilo del cliente, NO en el de Tkinter: asi las
    escrituras en PostgreSQL no congelan la interfaz. Cuando termina, avisa a
    la GUI por la cola de eventos para que se refresque.
    """
    from app.compat import history_compat
    from app.history_service import ingest_history_sync

    sync_log = get_logger("SYNC")

    def ingest(full: Any) -> None:
        # Se avisa ANTES de persistir: el backfill solo necesita saber que su
        # chat aparecio en un blob para dejar de esperar.
        if _BACKFILL is not None:
            _BACKFILL.notify_history(full)
        try:
            with database.transaction() as session:
                result = ingest_history_sync(
                    session, full, own_jid=own_jid,
                    signal_db=signal_db_path,
                )
        except Exception as exc:  # noqa: BLE001 - el blob ya esta archivado en disco
            sync_log.exception("Fallo al persistir el History Sync: %s", exc)
            sync_log.warning(
                "El blob sigue en data/history/; se puede reintentar con "
                "'python ingest_blobs.py' sin volver a pedir nada al servidor"
            )
            return
        client._publish("history_ingested", str(result))

    history_compat.set_callback(ingest)


# Instancia unica del backfill: la crea wire_backfill() y la consume el
# post_connect. Se guarda a nivel de modulo porque ambos cableados corren en
# hilos distintos y necesitan la misma referencia.
_BACKFILL: Any = None


def wire_backfill(
    client: WhatsAppClient, database: Database, settings: Settings
) -> Any:
    """Prepara la recuperacion historica ON_DEMAND."""
    from app.backfill_service import BackfillService

    global _BACKFILL
    _BACKFILL = BackfillService(settings, database)
    # El backfill debe saber quienes somos para no pedirse historial a si mismo.
    from inspect_db import own_identity

    own_pn, own_lid = own_identity(settings)
    _BACKFILL.set_own_identity(own_pn, own_lid)
    log.info("Identidad propia: pn=%s lid=%s", bool(own_pn), bool(own_lid))
    return _BACKFILL


def wire_contacts(client: WhatsAppClient, database: Database) -> Any:
    """Guarda los nombres que llegan por app-state (agenda del telefono)."""
    from app.contacts_service import ContactService

    service = ContactService(database)
    client.sinks["contact"] = service.handle_contact
    client.sinks["pushname"] = service.handle_pushname
    return service


def wire_live_messages(
    client: WhatsAppClient, database: Database, own_jid: str | None
) -> None:
    """Guarda en PostgreSQL cada mensaje que llega en vivo."""
    from app.live_service import LiveMessageService

    service = LiveMessageService(database, own_jid=own_jid)
    client.sinks["message"] = service.handle


def wire_media_downloads(
    client: WhatsAppClient, database: Database, settings: Settings
) -> None:
    """Descarga los adjuntos pendientes una vez conectados.

    Corre en el event loop del cliente y en segundo plano, para no retrasar la
    recepcion de mensajes.
    """
    from app.media_service import MediaService

    media_log = get_logger("MEDIA")

    async def download(pywhats_client: Any) -> None:
        service = MediaService(settings, database, pywhats_client)
        stats = await service.run()
        if stats.downloaded or stats.deduplicated:
            client._publish("media_downloaded", str(stats))
        else:
            media_log.info("Multimedia: %s", stats)

    async def post_connect(pywhats_client: Any) -> None:
        # 1) Nombres: se piden explicitamente porque pywhats solo sincroniza
        #    el app-state cuando el servidor le empuja una notificacion.
        from app.contacts_service import fetch_contact_names

        try:
            await fetch_contact_names(pywhats_client)
            # Los nombres llegan por telefono y los chats por @lid: hay que
            # preguntar al servidor la correspondencia o no casan.
            from app.contacts_service import resolve_lids_via_usync

            await resolve_lids_via_usync(pywhats_client, database)
            client._publish("contacts_synced", None)
        except Exception:  # noqa: BLE001 - sin nombres se sigue funcionando
            media_log.debug("No se pudieron sincronizar los nombres")

        # 2) Multimedia ya detectada: rapida y no depende del telefono.
        await download(pywhats_client)

        # 3) Backfill historico: este si depende del telefono principal.
        if _BACKFILL is not None:
            # CANARY primero: un solo chat. El backfill global no se activa
            # hasta ver una recuperacion real, para no recorrer 9 chats
            # repitiendo el mismo fallo.
            _BACKFILL._client = pywhats_client
            if _BACKFILL.capability_confirmed():
                # Esta sesion ya demostro que ON_DEMAND funciona: repetir el
                # canary en cada arranque solo gasta una peticion.
                log.info("Canary omitido: capability ya confirmada para esta sesion")
                ok = True
            else:
                ok = await _BACKFILL.run_canary(pywhats_client)
            if ok and settings.backfill_all_after_canary:
                await _BACKFILL.run(pywhats_client)
            client._publish("backfill_done", str(_BACKFILL.stats))

    client.post_connect = post_connect


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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print("[APP] Iniciando...")

    # -- Configuracion --
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[CONFIG] ERROR: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.log_level, log_file=settings.diagnostics_dir / "app.log")
    settings.ensure_directories()
    get_logger("CONFIG").info("Configuracion cargada (entorno=%s)", settings.app_env)

    # -- PostgreSQL --
    try:
        database = start_database(settings)
    except DatabaseError as exc:
        get_logger("DB").error("%s", exc)
        return 3

    exit_code = 0
    client: WhatsAppClient | None = None
    try:
        # -- Compatibilidades y version --
        summary = prepare_pywhats(settings)
        describe_environment(settings, summary)

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
            log.info("Comprobacion completada correctamente")
            health = database.health()
            log.info(
                "PostgreSQL %s, base=%s, encoding=%s",
                health["server_version"],
                health["database"],
                health["encoding"],
            )
            return 0

        # -- Cliente de WhatsApp --
        events: queue.Queue[ClientEvent] = queue.Queue()
        client = WhatsAppClient(settings, events)

        # VERIFICACION DE ESTADO: se decide que ensenar ANTES de conectar.
        has_session = client.session_exists
        stored = stored_summary(database)
        log.info(
            "Sesion %s | en PostgreSQL: %d chats, %d mensajes",
            "detectada" if has_session else "no encontrada",
            stored["chats"],
            stored["messages"],
        )

        use_gui = settings.gui_enabled and not args.no_gui
        if args.viewer and not use_gui:
            log.error("--viewer necesita la GUI; quita --no-gui o pon GUI_ENABLED=true")
            return 4

        if not use_gui:
            client.start()
            wire_logging(events)
            return exit_code

        from app.gui import App
        from app.session_state import AppState, SessionState, install_success_hook, set_success_callback

        state = SessionState()
        # El <success> del servidor es la unica confirmacion de que el login
        # fue aceptado: pywhats no lo expone como evento.
        install_success_hook()
        set_success_callback(lambda: client._publish("session_valid", None))

        def _stop_workers() -> None:
            """Detiene backfill y multimedia antes de cerrar la sesion."""
            if _BACKFILL is not None:
                _BACKFILL.stop()

        client.on_shutdown = _stop_workers

        def _on_close() -> None:
            log.info("Cerrando: deteniendo trabajos y sesion...")
            if client is not None:
                client.stop()
            log.info("Cierre completado")

        app = App(events, on_close=_on_close)
        app.attach_viewer(database.session, settings.media_dir)
        wire_gui(app, client, database, settings, state)

        if args.viewer:
            # Modo explicito de solo lectura: el usuario pide ver la copia
            # local a sabiendas de que no hay conexion.
            log.info("Modo visor: no se conectara a WhatsApp")
            state.set(AppState.DISCONNECTED, reason="--viewer")
            state.set(AppState.CONNECTED, reason="modo local explicito")
            app.show_viewer(connected=False)
            app.run()
            return exit_code

        wire_history_ingestion(
            client,
            database,
            own_jid_from_session(settings.session_file),
            signal_db_path=settings.signal_store_file,
        )
        wire_media_downloads(client, database, settings)
        wire_live_messages(client, database, own_jid_from_session(settings.session_file))
        wire_backfill(client, database, settings)
        wire_contacts(client, database)

        if has_session:
            # Hay credenciales en disco, pero eso NO significa que sigan
            # siendo validas: el visor espera al <success> del servidor.
            log.info("Sesion existente detectada")
            state.set(AppState.CHECKING_SESSION, reason="device.json presente")
            wa_log.info("Validando sesion existente...")
            app.show_status(
                "Conectando con WhatsApp...",
                "Comprobando que la sesion guardada sigue vinculada.",
            )
        else:
            log.info("Sin sesion: hay que vincular el dispositivo")
            state.set(AppState.PAIRING_REQUIRED, reason="no hay device.json")
            app.show_pairing()

        client.start()
        app.run()  # bloquea hasta que se cierra la ventana

    except KeyboardInterrupt:
        log.info("Interrumpido por el usuario")
    except Exception as exc:  # noqa: BLE001 - se reporta con traza completa
        log.exception("Fallo no controlado: %s", exc)
        exit_code = 1
    finally:
        if client is not None:
            client.stop()
        database.dispose()
        log.info("Terminado")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
