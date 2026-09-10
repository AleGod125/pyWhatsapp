"""Identidad propia del dispositivo vinculado, y enmascarado de identificadores.

Estas dos funciones vivian en ``inspect_db.py``, una herramienta de
diagnostico, y sin embargo las usaba el orquestador. La
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

    return _identidad_de_credenciales(data)


def _sin_dispositivo(jid: Any) -> str | None:
    """``"573001234567:12@s.whatsapp.net"`` -> ``"573001234567@s.whatsapp.net"``.

    Baileys guarda el JID propio CON la ranura de dispositivo. Esa parte
    identifica al companion, no a la cuenta: dejarla dentro haria que el
    numero propio no coincidiera con el de ningun mensaje, y los salientes se
    atribuirian a otra persona.
    """
    if not isinstance(jid, str) or "@" not in jid:
        return None
    usuario, _, servidor = jid.partition("@")
    usuario = usuario.split(":")[0].split(".")[0]
    return f"{usuario}@{servidor}" if usuario else None


def _identidad_de_credenciales(data: Any) -> tuple[str | None, str | None]:
    """``(pn, lid)`` sacados del ``creds.json`` de Baileys.

    Los dos salen de ``me``, que el servidor rellena en el ``<success>``: el
    ``lid`` no llega en el ``pair-success``, llega despues. Por eso esto se
    lee cuando ya hay conexion y no en el momento de vincular.
    """
    if not isinstance(data, dict):
        return None, None
    yo = data.get("me")
    if not isinstance(yo, dict):
        return None, None
    return _sin_dispositivo(yo.get("id")), _sin_dispositivo(yo.get("lid"))


def nombre_del_perfil(settings: Any) -> str | None:
    """El nombre que el usuario tiene puesto en SU WhatsApp, si se sabe.

    Lo trae Baileys en ``creds.me.name`` y sirve para que una cuenta recien
    anadida ya se llame de alguna forma en el selector, en vez de aparecer
    como un numero. Es un punto de partida: el usuario puede renombrarla, y a
    partir de ahi manda lo que el escribio.
    """
    session_file: Path = settings.session_file
    if not session_file.exists():
        return None
    try:
        datos = json.loads(session_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    yo = datos.get("me")
    if not isinstance(yo, dict):
        return None
    nombre = str(yo.get("name") or "").strip()
    return nombre or None


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
    pn, _ = _identidad_de_credenciales(datos)
    if not pn:
        return None
    usuario, _, servidor = pn.partition("@")
    # La RANURA de dispositivo, que Baileys guarda dentro del propio jid.
    completo = str((datos.get("me") or {}).get("id") or "")
    ranura = ""
    if "@" in completo:
        partes = completo[: completo.index("@")].split(":")
        ranura = partes[1] if len(partes) > 1 else ""
    # ``registration_id`` entra a proposito. ``device_id`` es un NUMERO DE
    # RANURA que el servidor reutiliza: al desvincular todos los
    # dispositivos la numeracion puede volver atras, y dos identidades
    # distintas acabarian con la misma huella. Entonces se daria por
    # confirmado el historial inicial de una sesion que ya no existe y la
    # espera del bootstrap se saltaria sin que nada lo delatara. El
    # registration_id se genera nuevo en cada vinculacion.
    crudo = (
        f"{usuario}:{servidor}:{ranura}:{datos.get('registrationId', '')}"
    )
    return hashlib.sha256(crudo.encode()).hexdigest()[:16]


def own_jid(settings: Any) -> str | None:
    """JID propio, para marcar el emisor de los mensajes salientes."""
    return own_identity(settings)[0]
