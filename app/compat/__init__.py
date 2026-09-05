"""Adaptaciones locales sobre pywhats 0.2.0.

Cada modulo de aqui corrige un bug VERIFICADO en el paquete instalado. Nada se
edita dentro de ``.venv/Lib/site-packages/``: los parches se aplican en memoria
al arrancar y son reversibles quitando el flag correspondiente del .env.

Auditoria realizada el 2026-09-01 contra pywhats 0.2.0 (ultima release en PyPI,
publicada el 2026-07-10):

    wa_version.py     bug real  -- WA_WEB_VERSION obsoleta -> PairingFailed 405
    windows_store.py  bug real  -- os.fchmod() no existe en Windows
    pairing_compat.py bug real  -- el socket se cierra antes del restart 515
    prekey_compat.py  bug real  -- PreKeySignalMessage repetido exige una OPK
                                   que ya fue consumida legitimamente
    history_compat.py carencia  -- el blob de History Sync se descarga y
                                   descifra pero solo se cuenta

Ninguna de estas adaptaciones debilita la criptografia ni desactiva una
verificacion. Ver README.md -> "Compatibilidades" para el detalle.
"""

from __future__ import annotations

__all__ = ["apply_all"]


def apply_all(settings) -> list[str]:  # type: ignore[no-untyped-def]
    """Aplica las compatibilidades activadas y devuelve las etiquetas aplicadas.

    Se importa cada modulo de forma perezosa para que desactivar un flag
    signifique tambien no cargar su codigo.
    """
    from app.core.logging_setup import get_logger

    log = get_logger("COMPAT")
    applied: list[str] = []

    # Historial completo al vincular. Va PRIMERO: el DeviceProps se
    # construye durante el registro del companion, antes que nada mas.
    if settings.pairing_full_sync:
        from app.compat import history_config

        if history_config.apply(settings):
            applied.append("full_history")

    # Nuestro propio par PN<->LID en el mapa de Signal. Sin el, los mensajes
    # que el usuario envia desde su telefono llegan pero no se descifran:
    # "no session for peer <nuestro propio LID>".
    if settings.compat_own_lid_map:
        from app.compat import own_lid_map

        if own_lid_map.apply(settings):
            applied.append("own_lid_map")

    # Observacion del camino real del receptor cuando llega un mensaje
    # NUESTRO. No cambia nada: solo dice por que encuentra (o no) la sesion,
    # y de cual de mis dispositivos venia.
    if settings.compat_own_lid_map:
        from app.compat import lid_diagnostics

        if lid_diagnostics.apply(settings):
            applied.append("lid_diagnostics")

        # Y una foto del almacen: un dispositivo propio con sesion por numero
        # Y por LID es el mismo aparato con dos ratchets, que es la
        # explicacion medible de que alguna copia del telefono no cuadre.
        from app.core.own_device import avisar_de_sesiones_duplicadas

        try:
            avisar_de_sesiones_duplicadas(settings)
        except Exception:  # noqa: BLE001 - mirar no puede impedir el arranque
            log.debug("No se pudo auditar las sesiones propias")

    # Busca anclas en las mutaciones de app-state. APAGADA por defecto: se
    # midio contra la cuenta real y no aparecio ni una clave de mensaje
    # (61 mutaciones, con_clave=0). Se conserva como diagnostico, para poder
    # repetir la medicion sin volver a escribir nada, pero no forma parte del
    # arranque normal. Se enciende con COMPAT_APPSTATE_SEEDS=true.
    if settings.compat_appstate_seeds:
        from app.compat import appstate_seeds

        if appstate_seeds.apply(settings):
            applied.append("appstate_seeds")

    if settings.compat_windows_store:
        from app.compat import windows_store

        if windows_store.apply():
            applied.append("windows_store")

    if settings.compat_prekey_replay:
        from app.compat import prekey_compat

        # El registro de establecimientos es estado NUESTRO y vive junto a la
        # sesion, nunca dentro del Signal Store de pywhats.
        if prekey_compat.apply(settings.session_dir / "compat_prekey.db"):
            applied.append("prekey_replay")

    if settings.compat_history_messages:
        from app.compat import history_compat

        # Los blobs inflados se archivan antes de interpretarlos: un fallo de
        # normalizacion no puede costar historial.
        history_compat.set_blob_dir(settings.data_dir / "history")
        if history_compat.apply():
            applied.append("history_messages")

    # Sin category="peer" el servidor confirma la stanza pero no la encamina
    # como operacion entre dispositivos, y ON_DEMAND nunca responde.
    # Sin esto, los ProtocolMessage que pywhats emite por el evento
    # 'message' acaban guardados como mensajes del chat propio.
    from app.compat import protocol_flag

    if protocol_flag.apply():
        applied.append("protocol_flag")

    from app.compat import peer_message

    if peer_message.apply():
        applied.append("peer_message")

    if settings.compat_pairing_515:
        from app.compat import pairing_compat

        if pairing_compat.apply(timeout=settings.pairing_515_timeout):
            applied.append("pairing_515")

    if applied:
        log.info("Adaptaciones activas: %s", ", ".join(applied))
    else:
        log.info("Sin adaptaciones activas")
    return applied
