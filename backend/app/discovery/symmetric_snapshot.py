"""La misma vara para los dos dispositivos. Plan J3.1.

POR QUE EXISTE
--------------
El Plan J llevaba tres fases sostenido sobre una comparación que no era una
comparación: ocho conversaciones con ancla en la sesión principal contra
treinta y siete en el navegador. Al mirar las marcas de tiempo, las ocho eran
de un instante —el bootstrap— y las treinta y siete eran el almacén del
navegador después de **días** conectado, con todo su tráfico en vivo dentro.

Eso no dice que el navegador reciba un bootstrap mejor. Puede decir sólo que
su sesión era más vieja. No se sabe, porque nunca se midió su bootstrap
aparte.

Este módulo existe para que eso no pueda volver a pasar: **una sola
definición** de cada métrica, aplicada igual a los dos lados, con la misma
clasificación de conversaciones y el mismo criterio de ancla válida. Si las
dos fotos salen de aquí, la resta significa algo.

LO QUE NO HACE
--------------
No pide nada a la red, no descifra, no escribe en la base y no fabrica
identificadores. Lee lo que ya hay y cuenta.

PRIVACIDAD
----------
Fuera de este proceso no sale ni un JID, ni un nombre, ni un texto. Las
conversaciones viajan como un hash corto y estable que permite cruzar los dos
lados sin nombrar a nadie.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Clasificación de conversaciones (§14)
# ---------------------------------------------------------------------------
#
# Contar «chats» a secas es lo que produce el 41 contra 50 que nadie sabe
# interpretar: dentro de esos 50 hay difusiones, boletines y entidades de
# sistema que el producto no tiene que recuperar. Se separa antes de comparar.

INDIVIDUAL = "individual"
INDIVIDUAL_ARCHIVADO = "individual_archived"
GRUPO = "group"
GRUPO_ARCHIVADO = "group_archived"
NEGOCIO = "business"
COMUNIDAD = "community"
BOLETIN = "newsletter"
ESTADOS = "status_broadcast"
DIFUSION = "broadcast_list"
BOT = "bot"
SISTEMA = "system"
OTRO = "other_special"

#: Lo que el producto de verdad muestra y recupera. Es la única cifra con la
#: que tiene sentido decir «cobertura».
#:
#: Las comunidades quedan FUERA a propósito: el nodo padre de una comunidad es
#: un contenedor de grupos, no una conversación. Se cuenta aparte para que
#: quien prefiera incluirlas pueda rehacer la resta sin volver a medir.
CLASES_DE_USUARIO = frozenset(
    {INDIVIDUAL, INDIVIDUAL_ARCHIVADO, GRUPO, GRUPO_ARCHIVADO, NEGOCIO}
)

#: Ni conversaciones ni recuperables. Se cuentan para poder explicar la resta.
CLASES_ESPECIALES = frozenset(
    {BOLETIN, ESTADOS, DIFUSION, BOT, SISTEMA, OTRO, COMUNIDAD}
)

_SERVIDORES = {
    "g.us": GRUPO,
    "lid": INDIVIDUAL,
    "s.whatsapp.net": INDIVIDUAL,
    "c.us": INDIVIDUAL,
    "newsletter": BOLETIN,
    "bot": BOT,
}


def clasificar(
    jid: str | None,
    *,
    archivado: bool | None = None,
    es_negocio: bool | None = None,
    es_comunidad: bool | None = None,
) -> str:
    """En qué cajón cae esta conversación.

    El JID decide la familia; los indicadores que trae el cliente afinan. Lo
    que no se sabe NO se inventa: sin indicador de archivado, un individual es
    ``individual``, no ``individual_archived``.
    """
    if not jid or "@" not in jid:
        return SISTEMA
    usuario, _, servidor = jid.partition("@")
    if servidor == "broadcast":
        return ESTADOS if usuario == "status" else DIFUSION

    clase = _SERVIDORES.get(servidor, OTRO)

    if clase == GRUPO and es_comunidad:
        return COMUNIDAD
    if clase == GRUPO:
        return GRUPO_ARCHIVADO if archivado else GRUPO
    if clase == INDIVIDUAL:
        if es_negocio:
            return NEGOCIO
        return INDIVIDUAL_ARCHIVADO if archivado else INDIVIDUAL
    return clase


def es_de_usuario(clase: str) -> bool:
    return clase in CLASES_DE_USUARIO


# ---------------------------------------------------------------------------
# Qué cuenta como ancla, y de dónde salió (§16, §17)
# ---------------------------------------------------------------------------

#: Nombres normalizados de origen. Los que usa la base no coinciden del todo
#: con los del encargo, y mezclarlos haría que «de dónde salen las anclas»
#: dependiera de qué tabla se mirase.
ORIGEN_BOOTSTRAP = "initial_bootstrap"
ORIGEN_OFFLINE = "offline"
ORIGEN_LIVE = "live"
ORIGEN_ALMACEN_WEB = "web_store"
ORIGEN_FETCH_WEB = "web_local_fetch"
ORIGEN_ON_DEMAND = "on_demand"
ORIGEN_OTRO = "other"

_ALIAS_DE_ORIGEN = {
    "initial_bootstrap": ORIGEN_BOOTSTRAP,
    "recent_history": ORIGEN_BOOTSTRAP,
    "full_history": ORIGEN_BOOTSTRAP,
    "blob_scan": ORIGEN_BOOTSTRAP,
    "offline": ORIGEN_OFFLINE,
    "live": ORIGEN_LIVE,
    "retry_resend": ORIGEN_LIVE,
    "on_demand": ORIGEN_ON_DEMAND,
    "web_store": ORIGEN_ALMACEN_WEB,
    "web_fetch1": ORIGEN_FETCH_WEB,
    "web_local_fetch": ORIGEN_FETCH_WEB,
}


def normalizar_origen(origen: str | None) -> str:
    return _ALIAS_DE_ORIGEN.get((origen or "").strip().lower(), ORIGEN_OTRO)


#: Lo que NO cuenta como cobertura del arranque (§18).
#:
#: ``on_demand`` necesita un ancla previa para poder pedirse: contarlo sería
#: contar dos veces el ancla que ya se tenía. ``live`` necesita que alguien
#: escriba, así que mide la actividad de la cuenta, no lo que trae vincular.
#: ``web_local_fetch`` es la lectura de caché del navegador, que se mide
#: aparte para poder separar almacén natural de sondeo (§57).
#:
#: Y ``web_store`` queda fuera por la misma razón, aunque cueste verlo: es un
#: ancla que trajo el SEGUNDO dispositivo. Contarla como arranque de la
#: principal es precisamente atribuirle a una lo que consiguió la otra, que es
#: el error que esta fase existe para no repetir. Se comprobó midiendo la
#: sesión real: sin esta línea, la principal aparecía con 40 anclas de
#: arranque cuando de verdad tiene 8.
FUERA_DEL_BOOTSTRAP = frozenset(
    {ORIGEN_ON_DEMAND, ORIGEN_LIVE, ORIGEN_FETCH_WEB, ORIGEN_ALMACEN_WEB}
)


def cuenta_como_bootstrap(origen: str | None) -> bool:
    return normalizar_origen(origen) not in FUERA_DEL_BOOTSTRAP


def ancla_valida(
    *,
    chat_jid: str | None,
    wa_msg_id: str | None,
    timestamp: Any,
) -> bool:
    """Si esto sirve de verdad para pedirle historial al servidor (§16).

    Hacen falta las tres cosas a la vez: conversación real, identificador real
    de WhatsApp y marca real. Un identificador nuestro recibe confirmación del
    servidor y después silencio, que es el fallo más caro de diagnosticar de
    todo el proyecto; por eso se rechaza aquí y no más adelante.
    """
    if not chat_jid or "@" not in chat_jid:
        return False
    if not wa_msg_id or not isinstance(wa_msg_id, str):
        return False

    from app.services.repository import is_valid_history_cursor_id

    if not is_valid_history_cursor_id(wa_msg_id):
        return False
    try:
        marca = int(timestamp)
    except (TypeError, ValueError):
        return False
    return marca > 0


def hash_de(jid: str | None) -> str:
    """Identificador estable que no nombra a nadie."""
    return hashlib.sha256((jid or "").encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# La foto (§13)
# ---------------------------------------------------------------------------


@dataclass
class Fila:
    """Una conversación, ya anónima."""

    chat: str
    clase: str
    tiene_nombre: bool
    tiene_actividad: bool
    tiene_mensaje_real: bool
    tiene_ancla: bool
    mensajes_en_memoria: int
    origen_del_ancla: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "chat": self.chat,
            "class": self.clase,
            "user_visible": es_de_usuario(self.clase),
            "has_name": self.tiene_nombre,
            "has_activity": self.tiene_actividad,
            "has_real_message": self.tiene_mensaje_real,
            "has_seed": self.tiene_ancla,
            "cached_messages": self.mensajes_en_memoria,
            "seed_source": self.origen_del_ancla,
        }


@dataclass
class Foto:
    """Las métricas de §13, idénticas para los dos lados."""

    lado: str
    etiqueta: str
    t0_epoch: float
    capturado_epoch: float
    modo: str = "native"
    filas: list[Fila] = field(default_factory=list)
    mensajes_en_cache: int = 0
    wamids_distintos: int = 0
    por_origen: dict[str, int] = field(default_factory=dict)
    notas: dict[str, Any] = field(default_factory=dict)

    # -- Derivadas ---------------------------------------------------------

    @property
    def session_age_seconds(self) -> float:
        return round(max(0.0, self.capturado_epoch - self.t0_epoch), 3)

    @property
    def raw_chat_count(self) -> int:
        return len(self.filas)

    @property
    def de_usuario(self) -> list[Fila]:
        return [f for f in self.filas if es_de_usuario(f.clase)]

    @property
    def user_chat_count(self) -> int:
        return len(self.de_usuario)

    @property
    def individual_count(self) -> int:
        return sum(
            1
            for f in self.filas
            if f.clase in (INDIVIDUAL, INDIVIDUAL_ARCHIVADO, NEGOCIO)
        )

    @property
    def group_count(self) -> int:
        return sum(1 for f in self.filas if f.clase in (GRUPO, GRUPO_ARCHIVADO))

    @property
    def special_entity_count(self) -> int:
        return sum(1 for f in self.filas if f.clase in CLASES_ESPECIALES)

    @property
    def chats_with_name(self) -> int:
        return sum(1 for f in self.filas if f.tiene_nombre)

    @property
    def chats_with_activity(self) -> int:
        return sum(1 for f in self.filas if f.tiene_actividad)

    @property
    def chats_with_real_message(self) -> int:
        return sum(1 for f in self.filas if f.tiene_mensaje_real)

    @property
    def chats_with_valid_seed(self) -> int:
        return sum(1 for f in self.filas if f.tiene_ancla)

    @property
    def user_chats_with_valid_seed(self) -> int:
        return sum(1 for f in self.de_usuario if f.tiene_ancla)

    def por_clase(self) -> dict[str, int]:
        salida: dict[str, int] = {}
        for fila in self.filas:
            salida[fila.clase] = salida.get(fila.clase, 0) + 1
        return dict(sorted(salida.items()))

    def to_json(self) -> dict[str, Any]:
        return {
            "side": self.lado,
            "label": self.etiqueta,
            "mode": self.modo,
            "t0_epoch": round(self.t0_epoch, 3),
            "captured_epoch": round(self.capturado_epoch, 3),
            "session_age_seconds": self.session_age_seconds,
            "metrics": {
                "raw_chat_count": self.raw_chat_count,
                "user_chat_count": self.user_chat_count,
                "individual_count": self.individual_count,
                "group_count": self.group_count,
                "special_entity_count": self.special_entity_count,
                "chats_with_name": self.chats_with_name,
                "chats_with_activity": self.chats_with_activity,
                "chats_with_real_message": self.chats_with_real_message,
                "chats_with_valid_seed": self.chats_with_valid_seed,
                "user_chats_with_valid_seed": self.user_chats_with_valid_seed,
                "cached_message_count": self.mensajes_en_cache,
                "unique_wamid_count": self.wamids_distintos,
            },
            "by_class": self.por_clase(),
            "by_seed_source": dict(sorted(self.por_origen.items())),
            "notes": self.notas,
            "chats": [f.to_json() for f in self.filas],
        }


# ---------------------------------------------------------------------------
# El lado principal: se lee de la base
# ---------------------------------------------------------------------------


def fotografiar_principal(
    sesion: Any,
    *,
    account_id: Any = None,
    t0: float,
    etiqueta: str,
    ahora: float | None = None,
    solo_bootstrap: bool = True,
) -> Foto:
    """La foto de la sesión principal, tal como está la base ahora mismo.

    ``solo_bootstrap`` decide qué anclas cuentan (§18): con él puesto —que es
    lo normal para la escalera— se descartan las de ``on_demand``, las de
    ``live`` y las del sondeo del navegador, que no son cobertura del arranque.
    """
    from sqlalchemy import func, or_, select

    from app.models import Chat, Contact, HistorySeed, Message

    ahora = time.time() if ahora is None else ahora

    consulta = select(Chat)
    if account_id is not None:
        consulta = consulta.where(Chat.whatsapp_account_id == account_id)
    chats = list(sesion.execute(consulta).scalars())

    # -- Anclas, por conversación y por origen ------------------------------
    anclas = select(
        HistorySeed.chat_jid,
        HistorySeed.source,
        HistorySeed.wa_msg_id,
        HistorySeed.timestamp,
        HistorySeed.valid,
    )
    if account_id is not None:
        anclas = anclas.where(HistorySeed.whatsapp_account_id == account_id)

    mejor_origen: dict[str, str] = {}
    por_origen: dict[str, int] = {}
    vistos_por_origen: dict[str, set[str]] = {}
    for chat_jid, origen, wamid, marca, valida in sesion.execute(anclas).all():
        if not valida:
            continue
        if not ancla_valida(chat_jid=chat_jid, wa_msg_id=wamid, timestamp=marca):
            continue
        normal = normalizar_origen(origen)
        vistos_por_origen.setdefault(normal, set()).add(chat_jid)
        if solo_bootstrap and not cuenta_como_bootstrap(origen):
            continue
        mejor_origen.setdefault(chat_jid, normal)
    for origen, conjunto in vistos_por_origen.items():
        por_origen[origen] = len(conjunto)

    # -- Mensajes reales ----------------------------------------------------
    con_mensaje_real = {
        j
        for (j,) in sesion.execute(
            select(func.distinct(Message.chat_jid)).where(
                Message.whatsapp_message_id.is_not(None)
            )
        ).all()
        if j
    }
    en_memoria = dict(
        sesion.execute(
            select(Message.chat_jid, func.count()).group_by(Message.chat_jid)
        ).all()
    )
    total_mensajes = int(
        sesion.execute(select(func.count()).select_from(Message)).scalar() or 0
    )
    wamids = int(
        sesion.execute(
            select(func.count(func.distinct(Message.whatsapp_message_id))).where(
                Message.whatsapp_message_id.is_not(None)
            )
        ).scalar()
        or 0
    )

    # -- Nombres ------------------------------------------------------------
    #
    # OJO CON ESTO. El nombre de una conversación individual NO vive en
    # ``chats.name`` sino en ``contacts``, así que contar sólo la primera daba
    # CERO nombres en la principal contra treinta y tantos en el navegador. Esa
    # asimetría no existe: es de dónde se mira. Se midió sobre la sesión real
    # antes de que se colara en el informe.
    con_nombre = {
        j
        for (j,) in sesion.execute(
            select(Contact.jid).where(
                or_(
                    Contact.display_name.is_not(None),
                    Contact.push_name.is_not(None),
                    Contact.business_name.is_not(None),
                )
            )
        ).all()
        if j
    }
    con_nombre |= {
        j
        for (j,) in sesion.execute(
            select(Contact.lid).where(
                Contact.lid.is_not(None),
                or_(
                    Contact.display_name.is_not(None),
                    Contact.push_name.is_not(None),
                    Contact.business_name.is_not(None),
                ),
            )
        ).all()
        if j
    }

    filas: list[Fila] = []
    for chat in chats:
        metadatos = chat.raw_metadata or {}
        clase = clasificar(
            chat.jid,
            archivado=metadatos.get("archived"),
            es_negocio=metadatos.get("is_business"),
            es_comunidad=metadatos.get("is_community"),
        )
        origen = mejor_origen.get(chat.jid)
        filas.append(
            Fila(
                chat=hash_de(chat.jid),
                clase=clase,
                tiene_nombre=bool(chat.name) or chat.jid in con_nombre,
                tiene_actividad=bool(chat.last_message_timestamp),
                tiene_mensaje_real=chat.jid in con_mensaje_real,
                tiene_ancla=origen is not None,
                mensajes_en_memoria=int(en_memoria.get(chat.jid, 0)),
                origen_del_ancla=origen,
            )
        )

    return Foto(
        lado="primary",
        etiqueta=etiqueta,
        t0_epoch=t0,
        capturado_epoch=ahora,
        modo="bootstrap_only" if solo_bootstrap else "all_sources",
        filas=sorted(filas, key=lambda f: f.chat),
        mensajes_en_cache=total_mensajes,
        wamids_distintos=wamids,
        por_origen=por_origen,
    )


# ---------------------------------------------------------------------------
# El lado del navegador: se lee de lo que devuelve el worker
# ---------------------------------------------------------------------------


def fotografiar_navegador(
    respuesta: dict[str, Any],
    *,
    t0: float,
    etiqueta: str,
    ahora: float | None = None,
    modo: str = "native_store",
) -> Foto:
    """La foto del navegador a partir de ``j31_store_snapshot``.

    ``modo`` distingue las dos mediciones que §57 exige no mezclar:
    ``native_store`` es lo que el navegador tiene por sí solo, y
    ``after_probes`` es lo que tiene después de que nuestro sondeo le haya
    pedido cosas.
    """
    ahora = time.time() if ahora is None else ahora
    filas: list[Fila] = []
    por_origen: dict[str, int] = {}
    wamids: set[str] = set()
    total = 0

    for cruda in respuesta.get("chats") or []:
        jid = cruda.get("id") or cruda.get("chat_jid")
        if not jid:
            continue
        clase = clasificar(
            jid,
            archivado=cruda.get("archived"),
            es_negocio=cruda.get("is_business"),
            es_comunidad=cruda.get("is_community"),
        )
        ultimo = cruda.get("newest") or {}
        wamid = ultimo.get("wa_msg_id")
        marca = ultimo.get("t")
        tiene_ancla = ancla_valida(chat_jid=jid, wa_msg_id=wamid, timestamp=marca)
        if tiene_ancla and isinstance(wamid, str):
            wamids.add(wamid)
        origen = ORIGEN_ALMACEN_WEB if tiene_ancla else None
        if origen:
            por_origen[origen] = por_origen.get(origen, 0) + 1
        memoria = int(cruda.get("msgs_in_memory") or 0)
        total += memoria
        filas.append(
            Fila(
                chat=hash_de(jid),
                clase=clase,
                tiene_nombre=bool(cruda.get("name")),
                tiene_actividad=bool(cruda.get("last_activity")),
                tiene_mensaje_real=bool(wamid),
                tiene_ancla=tiene_ancla,
                mensajes_en_memoria=memoria,
                origen_del_ancla=origen,
            )
        )

    return Foto(
        lado="web",
        etiqueta=etiqueta,
        t0_epoch=t0,
        capturado_epoch=ahora,
        modo=modo,
        filas=sorted(filas, key=lambda f: f.chat),
        mensajes_en_cache=int(respuesta.get("store_msg_total") or total),
        wamids_distintos=len(wamids),
        por_origen=por_origen,
        notas={"worker_elapsed_ms": respuesta.get("elapsed_ms")},
    )


# ---------------------------------------------------------------------------
# La comparación (§66, §67, §68-§72)
# ---------------------------------------------------------------------------

CASO_DIFERENCIA_REAL = "A_DIFERENCIA_REAL"
CASO_PREMISA_FALSA = "B_PREMISA_FALSA"
CASO_HUECO_DE_DESCUBRIMIENTO = "D_DISCOVERY_GAP"
CASO_HUECO_DE_ANCLAS = "E_SEED_GAP"
CASO_INCONCLUSO = "INCONCLUSO"

#: Por debajo de esto, dos cifras no se consideran «parecidas». Es holgado a
#: propósito: la pregunta es 8 contra 32, no 30 contra 32.
MARGEN = 0.15


def _cobertura(nuestro: int, suyo: int) -> float | None:
    if suyo <= 0:
        return None
    return round(nuestro / suyo, 4)


def _parecidos(a: int, b: int) -> bool:
    mayor = max(a, b)
    if mayor == 0:
        return True
    return abs(a - b) / mayor <= MARGEN


def comparar(principal: Foto, navegador: Foto) -> dict[str, Any]:
    """La tabla de §66 y el caso de §68-§72, sin forzar una conclusión.

    Las dos fotos tienen que ser de la misma edad. Si no lo son, se dice —y no
    se emite veredicto—: repetir el error que motivó esta fase por no mirar un
    campo sería difícil de justificar.
    """
    edad_principal = principal.session_age_seconds
    edad_navegador = navegador.session_age_seconds
    simetrica = abs(edad_principal - edad_navegador) <= max(
        10.0, 0.10 * max(edad_principal, edad_navegador)
    )

    chats_parecidos = _parecidos(principal.user_chat_count, navegador.user_chat_count)
    anclas_parecidas = _parecidos(
        principal.user_chats_with_valid_seed, navegador.user_chats_with_valid_seed
    )
    web_tiene_mas_anclas = (
        navegador.user_chats_with_valid_seed > principal.user_chats_with_valid_seed
    )

    if not simetrica:
        caso = CASO_INCONCLUSO
    elif chats_parecidos and anclas_parecidas:
        caso = CASO_PREMISA_FALSA
    elif chats_parecidos and not anclas_parecidas:
        caso = CASO_HUECO_DE_ANCLAS if web_tiene_mas_anclas else CASO_INCONCLUSO
    elif not chats_parecidos and anclas_parecidas:
        caso = CASO_HUECO_DE_DESCUBRIMIENTO
    else:
        caso = CASO_DIFERENCIA_REAL if web_tiene_mas_anclas else CASO_INCONCLUSO

    return {
        "symmetric": simetrica,
        "primary_age_seconds": edad_principal,
        "web_age_seconds": edad_navegador,
        "case": caso,
        "primary_user_chat_coverage_vs_web": _cobertura(
            principal.user_chat_count, navegador.user_chat_count
        ),
        "primary_seed_coverage_vs_web": _cobertura(
            principal.user_chats_with_valid_seed,
            navegador.user_chats_with_valid_seed,
        ),
        "table": {
            "raw_chats": [principal.raw_chat_count, navegador.raw_chat_count],
            "user_chats": [principal.user_chat_count, navegador.user_chat_count],
            "groups": [principal.group_count, navegador.group_count],
            "individuals": [principal.individual_count, navegador.individual_count],
            "cached_messages": [
                principal.mensajes_en_cache,
                navegador.mensajes_en_cache,
            ],
            "chats_with_real_message": [
                principal.chats_with_real_message,
                navegador.chats_with_real_message,
            ],
            "valid_seed_chats": [
                principal.chats_with_valid_seed,
                navegador.chats_with_valid_seed,
            ],
            "user_seed_chats": [
                principal.user_chats_with_valid_seed,
                navegador.user_chats_with_valid_seed,
            ],
            "names": [principal.chats_with_name, navegador.chats_with_name],
        },
    }


def solo_del_navegador(principal: Foto, navegador: Foto) -> list[dict[str, Any]]:
    """Las conversaciones que ve el navegador y la principal no (§73)."""
    nuestros = {f.chat for f in principal.filas}
    return [f.to_json() for f in navegador.filas if f.chat not in nuestros]


def cobertura_normalizada(principal: Foto, navegador: Foto) -> dict[str, Any]:
    """La cobertura después de quitar lo que no es conversación (§74)."""
    solo_web = solo_del_navegador(principal, navegador)
    pertinentes = [f for f in solo_web if f["user_visible"]]
    return {
        "web_only_total": len(solo_web),
        "web_only_user_visible": len(pertinentes),
        "web_only_special": len(solo_web) - len(pertinentes),
        "normalized_user_chat_coverage": _cobertura(
            principal.user_chat_count,
            principal.user_chat_count + len(pertinentes),
        ),
    }


def entidades(iterable: Iterable[Fila]) -> dict[str, int]:
    salida: dict[str, int] = {}
    for fila in iterable:
        salida[fila.clase] = salida.get(fila.clase, 0) + 1
    return salida
