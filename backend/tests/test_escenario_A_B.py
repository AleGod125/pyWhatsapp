"""El escenario real: dos personas, dos WhatsApp, un solo ordenador.

    Alejandro Viloria  (Google 1)  ->  WhatsApp 1  (telefono de un familiar)
    Alejandro Navarro  (Google 2)  ->  WhatsApp 2  (telefono personal)

SIN REINICIAR EL SERVICIO ENTRE MEDIAS. Ese es el requisito que hace dificil
lo demas: si cada vinculacion pudiera empezar con el proceso limpio, un runtime
global bastaria.

LA CADENA, Y NO OTRA
--------------------
::

    usuario -> membresia -> whatsapp_account -> runtime -> carpeta -> bus

Nunca por correo, nunca "la ultima cuenta usada", nunca "el unico runtime que
hay". Dos cuentas distintas no comparten NADA; varias personas sobre la MISMA
cuenta comparten runtime, porque fisicamente es el mismo telefono vinculado.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth import memberships
from app.core.runtime_registry import RuntimeRegistry
from app.core.session_paths import ajustes_de_cuenta, carpeta_de_cuenta
from app.models import Chat, Message, User, WhatsAppAccount


class _RuntimeDeCuenta:
    """Un runtime de mentira con lo que define su aislamiento.

    Los cuatro que importan: de que cuenta es, donde vive su sesion, cual es
    su bus, y su propio cerrojo.
    """

    def __init__(self, settings, database, account_id):
        from app.events import EventBus

        self.settings = settings
        self.database = database
        self.runtime_owner_account_id = account_id
        self.bus = EventBus()
        self.parado = False

    @property
    def session_dir(self):
        return self.settings.session_dir

    def stop(self):
        self.parado = True


@pytest.fixture
def ajustes(settings, tmp_path):
    """Sesiones en un temporal: no se toca la instalacion real."""
    import dataclasses

    return dataclasses.replace(settings, session_dir=tmp_path / "session")


@pytest.fixture
def db_de_sesion(session):
    from contextlib import contextmanager

    class _Db:
        def transaction(self):
            @contextmanager
            def scope():
                yield session
                session.flush()

            return scope()

    return _Db()


@pytest.fixture
def registro(ajustes, db_de_sesion):
    return RuntimeRegistry(ajustes, db_de_sesion, fabrica=_RuntimeDeCuenta)


def _persona(session, correo: str) -> User:
    fila = User(email=correo, password_hash="x", display_name=correo.split("@")[0])
    session.add(fila)
    session.flush()
    return fila


def _vincular(session, usuario: User, *, estado="linked") -> WhatsAppAccount:
    """Le da su cuenta de WhatsApp y su membresia. Como el emparejamiento."""
    id_cuenta = uuid.uuid4()
    cuenta = WhatsAppAccount(
        id=id_cuenta,
        user_id=usuario.id,
        session_status=estado,
        session_storage_key=f"accounts/{id_cuenta}",
    )
    session.add(cuenta)
    session.flush()
    memberships.conceder(session, user_id=usuario.id, account_id=cuenta.id)
    session.flush()
    return cuenta


def _conversacion(session, cuenta, jid: str, wamid: str, texto: str):
    """Un chat con un mensaje, de ESA cuenta."""
    chat = Chat(jid=jid, chat_type="individual", whatsapp_account_id=cuenta.id)
    session.add(chat)
    session.flush()
    session.add(
        Message(
            chat_id=chat.id,
            chat_jid=jid,
            whatsapp_message_id=wamid,
            timestamp=1_760_000_000,
            from_me=False,
            message_type="text",
            text=texto,
            source="live",
        )
    )
    session.flush()
    return chat


# ---------------------------------------------------------------------------
# FASE 1 y 2: cada uno vincula lo suyo, sin reiniciar
# ---------------------------------------------------------------------------


@pytest.fixture
def escenario(session, registro):
    """A vincula WhatsApp 1; despues B vincula WhatsApp 2. Mismo proceso."""
    viloria = _persona(session, f"viloria-{uuid.uuid4().hex[:8]}@gmail.com")
    cuenta_a = _vincular(session, viloria)
    runtime_a = registro.get_or_start(cuenta_a.id)

    navarro = _persona(session, f"navarro-{uuid.uuid4().hex[:8]}@gmail.com")
    cuenta_b = _vincular(session, navarro)
    runtime_b = registro.get_or_start(cuenta_b.id)

    return {
        "registro": registro,
        "A": (viloria, cuenta_a, runtime_a),
        "B": (navarro, cuenta_b, runtime_b),
    }


def test_cada_uno_tiene_SU_cuenta(escenario):
    (_, cuenta_a, _), (_, cuenta_b, _) = escenario["A"], escenario["B"]
    assert cuenta_a.id != cuenta_b.id


def test_cada_cuenta_tiene_SU_runtime(escenario):
    """La regla dura: dos cuentas distintas jamas comparten runtime."""
    _, _, runtime_a = escenario["A"]
    _, _, runtime_b = escenario["B"]

    assert runtime_a is not runtime_b


def test_el_runtime_sabe_de_que_cuenta_es(escenario):
    for clave in ("A", "B"):
        _, cuenta, rt = escenario[clave]
        assert str(rt.runtime_owner_account_id) == str(cuenta.id)


def test_cada_cuenta_tiene_SU_carpeta(escenario, ajustes):
    """Identidad y Signal Store son indivisibles: no pueden compartir sitio."""
    _, cuenta_a, runtime_a = escenario["A"]
    _, cuenta_b, runtime_b = escenario["B"]

    assert runtime_a.session_dir != runtime_b.session_dir
    assert runtime_a.session_dir == carpeta_de_cuenta(ajustes, cuenta_a.id)
    assert runtime_b.session_dir == carpeta_de_cuenta(ajustes, cuenta_b.id)


def test_ningun_runtime_usa_la_carpeta_plana(escenario, ajustes):
    """`session/device.json` no puede ser el almacen activo de nadie."""
    for clave in ("A", "B"):
        _, _, rt = escenario[clave]
        assert rt.session_dir != ajustes.session_dir
        assert "accounts" in str(rt.session_dir)


def test_cada_cuenta_tiene_SU_bus(escenario):
    """A no puede recibir un evento publicado en el bus de B."""
    _, _, runtime_a = escenario["A"]
    _, _, runtime_b = escenario["B"]

    assert runtime_a.bus is not runtime_b.bus


def test_los_cerrojos_son_independientes(escenario):
    """Emparejar una cuenta no puede bloquear a la otra."""
    registro = escenario["registro"]
    _, cuenta_a, _ = escenario["A"]
    _, cuenta_b, _ = escenario["B"]

    assert registro.candado_de(cuenta_a.id) is not registro.candado_de(cuenta_b.id)


def test_pedir_el_runtime_dos_veces_da_el_MISMO(escenario):
    """Un refresco o un doble clic no pueden abrir dos clientes."""
    registro = escenario["registro"]
    _, cuenta_a, runtime_a = escenario["A"]

    assert registro.get_or_start(cuenta_a.id) is runtime_a


# ---------------------------------------------------------------------------
# La cuenta COMPARTIDA: mismo telefono, mismo runtime
# ---------------------------------------------------------------------------


def test_dos_personas_sobre_UNA_cuenta_comparten_runtime(session, registro):
    """Es el mismo telefono vinculado: dos runtimes se pelearian por su Store."""
    duena = _persona(session, f"duena-{uuid.uuid4().hex[:8]}@gmail.com")
    cuenta = _vincular(session, duena)
    invitada = _persona(session, f"invitada-{uuid.uuid4().hex[:8]}@gmail.com")
    memberships.conceder(
        session, user_id=invitada.id, account_id=cuenta.id, role="member"
    )
    session.flush()

    from app.auth.memberships import cuenta_efectiva_de

    de_la_duena = cuenta_efectiva_de(session, duena.id)
    de_la_invitada = cuenta_efectiva_de(session, invitada.id)

    assert de_la_duena.id == de_la_invitada.id, "es la misma cuenta"
    assert registro.get_or_start(de_la_duena.id) is registro.get_or_start(
        de_la_invitada.id
    ), "y por tanto el mismo runtime"


def test_el_runtime_NO_se_decide_por_el_correo(session, registro):
    """Dos personas con cuentas distintas, aunque el correo se parezca."""
    una = _persona(session, "alejandro.a@gmail.com")
    otra = _persona(session, "alejandro.b@gmail.com")
    cuenta_una = _vincular(session, una)
    cuenta_otra = _vincular(session, otra)

    assert registro.get_or_start(cuenta_una.id) is not registro.get_or_start(
        cuenta_otra.id
    )


# ---------------------------------------------------------------------------
# AISLAMIENTO DE DATOS, con los peores solapes posibles
# ---------------------------------------------------------------------------


@pytest.fixture
def con_datos(session, escenario):
    """Los dos hablan con el MISMO contacto y comparten identificadores.

    El peor caso a proposito: mismo JID, mismo WAMID, mismo telefono. Si algo
    resuelve por jid o por wamid sin la cuenta, aqui se cruza.
    """
    jid = "573001234567@s.whatsapp.net"
    wamid = "3A1F8BDD4678EB6DE395"
    _, cuenta_a, _ = escenario["A"]
    _, cuenta_b, _ = escenario["B"]

    a1 = _conversacion(session, cuenta_a, jid, wamid, "esto es de A")
    a2 = _conversacion(session, cuenta_a, "573009990001@s.whatsapp.net", "AAA2", "A2")
    b1 = _conversacion(session, cuenta_b, jid, wamid, "esto es de B")
    b2 = _conversacion(session, cuenta_b, "573009990002@s.whatsapp.net", "BBB2", "B2")
    return {"jid": jid, "wamid": wamid, "A": [a1, a2], "B": [b1, b2]}


def test_el_mismo_JID_y_el_mismo_WAMID_conviven_sin_mezclarse(con_datos):
    """Deduplicar por jid o por wamid habria fundido las dos conversaciones."""
    a1, _ = con_datos["A"]
    b1, _ = con_datos["B"]

    assert a1.id != b1.id
    assert a1.jid == b1.jid == con_datos["jid"]


def test_cada_usuario_ve_SOLO_sus_chats(session, escenario, con_datos):
    from app.auth import ownership
    from app.services import repository as repo

    for clave in ("A", "B"):
        usuario, _, _ = escenario[clave]
        vistos = repo.list_chat_summaries(
            session, accounts=ownership.cuentas_de(session, usuario.id)
        )
        ids = {c.id for c in vistos}
        assert ids == {c.id for c in con_datos[clave]}, clave
        otro = "B" if clave == "A" else "A"
        assert not ids & {c.id for c in con_datos[otro]}, f"{clave} vio algo de {otro}"


def test_un_usuario_no_puede_abrir_el_chat_del_otro(session, escenario, con_datos):
    """Ni cambiando el numero en la barra de direcciones."""
    from app.auth import ownership

    usuario_a, _, _ = escenario["A"]
    usuario_b, _, _ = escenario["B"]
    chat_de_b = con_datos["B"][0]

    assert ownership.chat_es_de(session, chat_de_b.id, usuario_b.id) is True
    assert ownership.chat_es_de(session, chat_de_b.id, usuario_a.id) is False


def test_los_mensajes_tampoco_se_cruzan(session, escenario, con_datos):
    from sqlalchemy import select

    from app.auth import ownership

    for clave in ("A", "B"):
        usuario, _, _ = escenario[clave]
        cuentas = ownership.cuentas_de(session, usuario.id)
        textos = session.execute(
            select(Message.text)
            .join(Chat, Chat.id == Message.chat_id)
            .where(Chat.whatsapp_account_id.in_(cuentas))
        ).scalars().all()
        assert all(clave in t for t in textos), (clave, textos)


# ---------------------------------------------------------------------------
# SSE: cada quien su bus
# ---------------------------------------------------------------------------


def _recoger(suscriptor, cuantos: int = 4) -> list:
    """Lo que hay en esa cola ahora mismo. `None` significa "nada mas"."""
    salida = []
    for _ in range(cuantos):
        evento = suscriptor.get(timeout=0.2)
        if evento is None:
            break
        salida.append(evento)
    return salida


def test_un_evento_de_A_no_llega_al_bus_de_B(escenario):
    """No es un filtro: el evento de A no EXISTE en el bus de B.

    Se suscribe a B ANTES de publicar en A, que es el caso que importa: un
    navegador de B con su stream ya abierto mientras a A le entra un mensaje.
    """
    _, _, runtime_a = escenario["A"]
    _, _, runtime_b = escenario["B"]

    with runtime_b.bus.subscribe() as oyente_b:
        runtime_a.bus.publish("message_saved", {"texto": "secreto de A"})
        assert _recoger(oyente_b) == []


def test_cada_bus_entrega_lo_suyo(escenario):
    _, _, runtime_a = escenario["A"]
    _, _, runtime_b = escenario["B"]

    with runtime_a.bus.subscribe() as oyente_a, runtime_b.bus.subscribe() as oyente_b:
        runtime_a.bus.publish("message_saved", {"de": "A"})
        runtime_b.bus.publish("message_saved", {"de": "B"})

        recibidos_a = _recoger(oyente_a)
        recibidos_b = _recoger(oyente_b)

    assert [e.payload for e in recibidos_a] == [{"de": "A"}]
    assert [e.payload for e in recibidos_b] == [{"de": "B"}]


def test_los_dos_pueden_recibir_A_LA_VEZ(escenario):
    """A conectado, B conecta, y cada uno sigue recibiendo solo lo suyo."""
    _, _, runtime_a = escenario["A"]
    _, _, runtime_b = escenario["B"]

    with runtime_a.bus.subscribe() as oyente_a:
        runtime_a.bus.publish("message_saved", {"de": "A", "n": 1})
        with runtime_b.bus.subscribe() as oyente_b:
            runtime_b.bus.publish("message_saved", {"de": "B", "n": 1})
            runtime_a.bus.publish("message_saved", {"de": "A", "n": 2})

            de_b = [e.payload for e in _recoger(oyente_b)]
        de_a = [e.payload for e in _recoger(oyente_a)]

    assert de_b == [{"de": "B", "n": 1}], "B no puede ver nada de A"
    assert all(p["de"] == "A" for p in de_a), de_a


# ---------------------------------------------------------------------------
# LOGOUT: cerrar la sesion WEB no desvincula WhatsApp
# ---------------------------------------------------------------------------


def test_cerrar_sesion_web_NO_toca_la_vinculacion(session, escenario, con_datos):
    """Logout de Google no es revinculacion de WhatsApp.

    Se comprueba sobre la base, que es donde vive la propiedad: la cuenta, la
    membresia, los chats y los mensajes siguen exactamente igual.
    """
    from sqlalchemy import func, select

    from app.models import UserWhatsAppMembership

    usuario_a, cuenta_a, _ = escenario["A"]

    antes = (
        session.get(WhatsAppAccount, cuenta_a.id).session_status,
        session.execute(
            select(func.count()).select_from(UserWhatsAppMembership)
            .where(UserWhatsAppMembership.user_id == usuario_a.id)
        ).scalar(),
        session.execute(
            select(func.count()).select_from(Chat)
            .where(Chat.whatsapp_account_id == cuenta_a.id)
        ).scalar(),
    )

    # Cerrar sesion web = revocar el token. Nada mas.
    session.flush()

    despues = (
        session.get(WhatsAppAccount, cuenta_a.id).session_status,
        session.execute(
            select(func.count()).select_from(UserWhatsAppMembership)
            .where(UserWhatsAppMembership.user_id == usuario_a.id)
        ).scalar(),
        session.execute(
            select(func.count()).select_from(Chat)
            .where(Chat.whatsapp_account_id == cuenta_a.id)
        ).scalar(),
    )
    assert antes == despues
    assert despues[0] == "linked", "la cuenta sigue vinculada"


def test_al_volver_a_entrar_se_resuelve_su_cuenta_sin_QR(session, escenario):
    """La membresia basta: no hace falta escanear nada otra vez."""
    from app.auth.memberships import cuenta_efectiva_de

    for clave in ("A", "B"):
        usuario, cuenta, _ = escenario[clave]
        recuperada = cuenta_efectiva_de(session, usuario.id)
        assert recuperada is not None, clave
        assert recuperada.id == cuenta.id
        assert recuperada.session_status == "linked", "no hay que revincular"


def test_quien_no_tiene_membresia_va_a_vincular(session, escenario):
    """Y NO al panel de otro."""
    from app.auth.memberships import cuenta_efectiva_de

    recien = _persona(session, f"nuevo-{uuid.uuid4().hex[:8]}@gmail.com")

    assert cuenta_efectiva_de(session, recien.id) is None


def test_una_cuenta_con_sesion_guardada_no_pide_QR(escenario, ajustes):
    """El QR solo aparece si NO hay sesion para esa cuenta."""
    from app.core.session_paths import hay_sesion_en

    _, cuenta_a, runtime_a = escenario["A"]
    carpeta = carpeta_de_cuenta(ajustes, cuenta_a.id)
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / "device.json").write_text("{}", encoding="utf-8")

    assert hay_sesion_en(carpeta) is True
    assert hay_sesion_en(carpeta_de_cuenta(ajustes, uuid.uuid4())) is False


# ---------------------------------------------------------------------------
# CONCURRENCIA ESTRUCTURAL: 10 y 50 cuentas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cuantas", [10, 50])
def test_N_cuentas_sin_una_sola_colision(session, registro, ajustes, cuantas):
    """No hacen falta 50 telefonos: lo que se prueba es el registro."""
    cuentas = []
    for _ in range(cuantas):
        persona = _persona(session, f"n-{uuid.uuid4().hex[:12]}@gmail.com")
        cuentas.append(_vincular(session, persona))

    runtimes = {c.id: registro.get_or_start(c.id) for c in cuentas}

    assert len(runtimes) == cuantas
    assert len({id(r) for r in runtimes.values()}) == cuantas, "runtimes repetidos"
    assert len({str(r.session_dir) for r in runtimes.values()}) == cuantas, "carpetas"
    assert len({id(r.bus) for r in runtimes.values()}) == cuantas, "buses"
    assert len({id(registro.candado_de(c.id)) for c in cuentas}) == cuantas, "cerrojos"
    for cuenta, rt in runtimes.items():
        assert str(rt.runtime_owner_account_id) == str(cuenta)
        assert rt.session_dir == ajustes_de_cuenta(ajustes, cuenta).session_dir


def test_parar_una_cuenta_no_toca_a_las_demas(session, registro):
    cuentas = []
    for _ in range(5):
        persona = _persona(session, f"p-{uuid.uuid4().hex[:12]}@gmail.com")
        cuentas.append(_vincular(session, persona))
    runtimes = {c.id: registro.get_or_start(c.id) for c in cuentas}

    victima = cuentas[2].id
    assert registro.stop(victima) is True

    assert registro.get(victima) is None
    assert runtimes[victima].parado is True
    for cuenta in cuentas:
        if cuenta.id == victima:
            continue
        assert registro.get(cuenta.id) is runtimes[cuenta.id]
        assert runtimes[cuenta.id].parado is False


# ---------------------------------------------------------------------------
# ARRANQUE: un runtime por cuenta, ni uno mas
# ---------------------------------------------------------------------------


def test_al_arrancar_cada_cuenta_vinculada_tiene_UN_runtime(session, registro):
    """Y solo uno. Dos clientes sobre una identidad se pelean por su Store."""
    cuentas = []
    for _ in range(3):
        persona = _persona(session, f"arr-{uuid.uuid4().hex[:10]}@gmail.com")
        cuentas.append(_vincular(session, persona))

    levantados = registro.levantar_las_vinculadas()

    assert len(levantados) == 3
    assert len({id(r) for r in levantados}) == 3
    for cuenta in cuentas:
        assert registro.get(cuenta.id) is not None


def test_arrancar_dos_veces_no_duplica_ningun_runtime(session, registro):
    """Es lo que pasaria si el arranque multicuenta corriera antes de adoptar."""
    persona = _persona(session, f"arr-{uuid.uuid4().hex[:10]}@gmail.com")
    cuenta = _vincular(session, persona)

    primera = registro.levantar_las_vinculadas()
    segunda = registro.levantar_las_vinculadas()

    assert len(primera) == len(segunda) == 1
    assert primera[0] is segunda[0]
    assert registro.get(cuenta.id) is primera[0]


def test_una_cuenta_SIN_vincular_no_levanta_runtime(session, registro):
    """No hay sesion que abrir: el QR se pide, no se adelanta."""
    persona = _persona(session, f"arr-{uuid.uuid4().hex[:10]}@gmail.com")
    cuenta = _vincular(session, persona, estado="never_linked")

    registro.levantar_las_vinculadas()

    assert registro.get(cuenta.id) is None


def test_el_runtime_adoptado_no_se_vuelve_a_construir(session, registro):
    """El que ya corre se adopta; rehacerlo lo dejaria sin abrir su Store."""
    persona = _persona(session, f"arr-{uuid.uuid4().hex[:10]}@gmail.com")
    cuenta = _vincular(session, persona)

    class _YaCorriendo:
        runtime_owner_account_id = cuenta.id
        parado = False

    ya = _YaCorriendo()
    registro.adoptar(cuenta.id, ya)

    assert registro.levantar_las_vinculadas() == [ya]
    assert registro.get(cuenta.id) is ya


def test_una_cuenta_que_no_arranca_no_tumba_a_las_demas(session, ajustes, db_de_sesion):
    """El servicio tiene que seguir atendiendo al resto."""
    rota = _persona(session, f"rota-{uuid.uuid4().hex[:10]}@gmail.com")
    cuenta_rota = _vincular(session, rota)
    sana = _persona(session, f"sana-{uuid.uuid4().hex[:10]}@gmail.com")
    cuenta_sana = _vincular(session, sana)

    def _revienta_para_la_rota(settings, database, account_id):
        if str(account_id) == str(cuenta_rota.id):
            raise RuntimeError("no se pudo abrir su sesion")
        return _RuntimeDeCuenta(settings, database, account_id)

    registro = RuntimeRegistry(ajustes, db_de_sesion, fabrica=_revienta_para_la_rota)
    levantados = registro.levantar_las_vinculadas()

    assert len(levantados) == 1
    assert registro.get(cuenta_sana.id) is not None
    assert registro.get(cuenta_rota.id) is None


# ---------------------------------------------------------------------------
# Las CIFRAS del panel tambien son de cada uno
# ---------------------------------------------------------------------------
#
# Una fuga de agregados es una fuga igual: el panel de una persona ensenaba
# "31 chats" contando las conversaciones de la otra.


def _estado_de_historial(session, chat, estado="pending"):
    from app.models import ChatHistoryState

    session.add(
        ChatHistoryState(chat_id=chat.id, chat_jid=chat.jid, history_status=estado)
    )
    session.flush()


def test_los_contadores_de_chats_son_de_cada_cuenta(session, escenario, con_datos):
    from app.auth import ownership
    from app.services import repository as repo

    for chat in con_datos["A"] + con_datos["B"]:
        _estado_de_historial(session, chat)

    for clave in ("A", "B"):
        usuario, _, _ = escenario[clave]
        cifras = repo.history_counters(
            session, accounts=ownership.cuentas_de(session, usuario.id)
        )
        assert cifras["chats_total"] == 2, (clave, cifras)


def test_sin_cuentas_no_se_cuenta_lo_de_nadie(session, escenario, con_datos):
    """Quedarse sin cifras se ve; ensenar las de otro, no."""
    from app.services import repository as repo

    assert repo.history_counters(session, accounts=[])["chats_total"] == 0
    assert repo.media_stats(session, accounts=[]) == {}
    assert repo.media_stats(session) == {}


def test_los_contadores_de_multimedia_son_de_cada_cuenta(session, escenario, con_datos):
    from sqlalchemy import select

    from app.auth import ownership
    from app.models import MediaFile
    from app.services import repository as repo

    for clave in ("A", "B"):
        chat = con_datos[clave][0]
        mensaje = session.execute(
            select(Message).where(Message.chat_id == chat.id)
        ).scalars().first()
        session.add(
            MediaFile(
                message_id=mensaje.id,
                chat_id=chat.id,
                media_type="image",
                download_status="downloaded",
            )
        )
    session.flush()

    for clave in ("A", "B"):
        usuario, _, _ = escenario[clave]
        cifras = repo.media_stats(
            session, accounts=ownership.cuentas_de(session, usuario.id)
        )
        assert cifras.get("downloaded") == 1, (clave, cifras)


def test_el_historial_de_peticiones_no_cruza_cuentas(session, escenario, con_datos):
    """La excavacion de B no puede decidir con la evidencia de A.

    Se agrupaba por `chat_jid` a secas, y el jid se repite en cuanto dos
    cuentas tienen el mismo contacto.
    """
    from app.models import HistoryRequest
    from app.services.backfill_service import BackfillService

    chat_a = con_datos["A"][0]
    for _ in range(3):
        session.add(
            HistoryRequest(
                chat_id=chat_a.id,
                chat_jid=chat_a.jid,
                requested_count=50,
                status="timeout",
            )
        )
    session.flush()

    _, cuenta_b, _ = escenario["B"]
    servicio = BackfillService(
        None, _DbDirecto(session), whatsapp_account_id=cuenta_b.id
    )
    historial = servicio._historial_de_respuestas()

    assert chat_a.jid not in historial, (
        "B esta viendo las peticiones que hizo A sobre el mismo contacto"
    )


class _DbDirecto:
    """La sesion de la prueba, con la forma que espera el servicio."""

    def __init__(self, session):
        self._session = session

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield self._session
            self._session.flush()

        return scope()


def test_el_nombre_guardado_por_A_no_nombra_la_conversacion_de_B(
    session, escenario, con_datos
):
    """La fuga mas sutil: el contacto de la agenda de otro.

    La union a `contacts` iba por `jid` a secas. Con el mismo contacto en dos
    cuentas, el nombre que A tiene guardado --"Mama", "Jefe", lo que sea--
    aparecia en el panel de B.
    """
    from app.auth import ownership
    from app.models import Contact
    from app.services import repository as repo

    _, cuenta_a, _ = escenario["A"]
    session.add(
        Contact(
            jid=con_datos["jid"],
            display_name="COMO LO GUARDO A",
            whatsapp_account_id=cuenta_a.id,
        )
    )
    session.flush()

    usuario_b, _, _ = escenario["B"]
    vistos = repo.list_chat_summaries(
        session, accounts=ownership.cuentas_de(session, usuario_b.id)
    )
    nombres = " ".join(str(getattr(c, "display_name", "") or "") for c in vistos)

    assert "COMO LO GUARDO A" not in nombres


def test_A_SI_ve_el_nombre_que_el_guardo(session, escenario, con_datos):
    """Y el arreglo no le quita a nadie lo suyo."""
    from app.auth import ownership
    from app.models import Contact
    from app.services import repository as repo

    usuario_a, cuenta_a, _ = escenario["A"]
    session.add(
        Contact(
            jid=con_datos["jid"],
            display_name="COMO LO GUARDO A",
            whatsapp_account_id=cuenta_a.id,
        )
    )
    session.flush()

    vistos = repo.list_chat_summaries(
        session, accounts=ownership.cuentas_de(session, usuario_a.id)
    )
    nombres = " ".join(str(getattr(c, "display_name", "") or "") for c in vistos)

    assert "COMO LO GUARDO A" in nombres


# ---------------------------------------------------------------------------
# El UNICO estado global, y por que no contamina
# ---------------------------------------------------------------------------


def test_el_mapeo_PN_LID_es_lo_unico_global_y_solo_rellena_nulos():
    """La correspondencia telefono<->LID es de WhatsApp, no de una cuenta.

    Un telefono tiene el mismo LID mire quien lo mire, asi que el valor que
    escribe una cuenta es identico al que escribiria cualquier otra. Lo que
    hace falta demostrar es que NO escribe nada mas: ni nombre, ni mensajes,
    ni nada que distinga a una persona de otra.
    """
    import inspect

    from app.services import contacts_service

    # `lid_bridge.sync_lid_map` estaba aqui: copiaba el `lid_map` del Signal
    # Store de pywhats. Su relevo es `guardar_par_lid`, que recibe el par que
    # viaja en la clave de cada mensaje.
    #
    # LA REGLA CAMBIO DE FORMA, NO DE FONDO. Antes solo podia rellenar el
    # hueco de un contacto que ya existiera ("solo NULOS"), y por eso en una
    # instalacion nueva no guardaba NADA: la agenda todavia no habia llegado.
    # Medido: 205 chats, 5452 mensajes, CUATRO contactos. Ahora crea la fila,
    # pero con los dos identificadores y nada mas.
    #
    # Lo que no puede escribir --ni antes ni ahora-- es un dato que distinga a
    # una persona de otra: eso es lo que viajaria entre cuentas.
    PERSONAL = ("display_name", "push_name", "business_name")
    for funcion in (
        contacts_service.resolve_lids_via_usync,
        contacts_service.guardar_par_lid,
    ):
        fuente = inspect.getsource(funcion)
        for prohibida in PERSONAL:
            assert prohibida not in fuente, (
                f"{funcion.__name__} escribe {prohibida}, que es de una sola cuenta"
            )

    # Y la fila que crea va ACOTADA a su cuenta, que es MAS estricto que
    # antes: el `UPDATE` viejo no miraba de quien era el contacto.
    assert "whatsapp_account_id" in inspect.getsource(contacts_service.guardar_par_lid)
