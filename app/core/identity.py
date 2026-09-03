"""Identidad propia del dispositivo vinculado, y enmascarado de identificadores.

Estas dos funciones vivian en ``inspect_db.py``, una herramienta de
diagnostico, y sin embargo las usaban ``main.py`` y el orquestador. La
dependencia iba al reves de como debe ir: la aplicacion no puede necesitar un
script de depuracion para arrancar. Ahora viven en el nucleo y las
herramientas las importan de aqui.

Ninguna de las dos toca material criptografico: leen el JID y el LID que el
pairing ya persistio, que son identificadores, no claves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def mask(jid: str | None) -> str:
    """Enmascara el identificador conservando el servidor, que si es util.

    Se usa en TODO lo que se imprime o se registra. Un JID completo es un
    numero de telefono: no aparece nunca en los logs.
    """
    if not jid:
        return "-"
    user, _, server = jid.partition("@")
    return f"{user[:6]}***@{server}" if server else f"{user[:6]}***"


def own_identity(settings: Any) -> tuple[str | None, str | None]:
    """``(own_pn, own_lid)`` leidos del DeviceStore.

    Ambos identifican LA MISMA cuenta. No se deducen el uno del otro: se leen
    de lo que persistio el pairing. Deducirlos seria inventar.
    """
    session_file: Path = settings.session_file
    if not session_file.exists():
        return None, None
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None

    jid = data.get("jid") or {}
    pn = (
        f"{jid['user']}@{jid.get('server', 's.whatsapp.net')}"
        if isinstance(jid, dict) and jid.get("user")
        else None
    )
    raw_lid = data.get("lid")
    lid = None
    if isinstance(raw_lid, str) and raw_lid:
        # El lid guardado puede venir con sufijo de dispositivo (".75@lid").
        user = raw_lid.split("@")[0].split(".")[0]
        lid = f"{user}@lid"
    return pn, lid


def session_fingerprint(settings: Any) -> str | None:
    """Huella NO sensible de la sesion guardada, leida del disco.

    Se deriva del JID y del ``device_id`` del companion, que identifican la
    vinculacion sin ser material criptografico, y ademas solo se conserva su
    SHA-256 truncado: el identificador en claro no se guarda ni se registra.

    Tiene que dar EXACTAMENTE lo mismo que
    ``BackfillService.session_fingerprint()``, que la calcula del dispositivo
    ya conectado. Si divergieran, la confirmacion del historial inicial se
    guardaria bajo una huella y se buscaria bajo otra, y la espera de 180
    segundos volveria en cada arranque sin que nada lo delatara. Hay una
    prueba que compara ambas.
    """
    import hashlib

    session_file: Path = settings.session_file
    if not session_file.exists():
        return None
    try:
        datos = json.loads(session_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    jid = datos.get("jid") or {}
    if not jid.get("user"):
        return None
    crudo = (
        f"{jid['user']}:{jid.get('server', 's.whatsapp.net')}:"
        f"{datos.get('device_id', '')}"
    )
    return hashlib.sha256(crudo.encode()).hexdigest()[:16]


def own_jid(settings: Any) -> str | None:
    """JID propio, para marcar el emisor de los mensajes salientes."""
    return own_identity(settings)[0]
