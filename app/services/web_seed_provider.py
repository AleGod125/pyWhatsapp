"""Semillas obtenidas por un dispositivo auxiliar. SOLO produce candidatos.

POR QUE EXISTE
--------------
33 conversaciones llegaron del pairing como pura metadata y sin un solo
identificador de mensaje. Sin uno, ``HISTORY_SYNC_ON_DEMAND`` no puede pedir
nada: va anclado por definicion.

Las fuentes nativas se agotaron, y esta medido:

===========================  ==========================================
INITIAL_BOOTSTRAP            sin identificador (auditado campo a campo)
blobs de History Sync        sin identificador
PostgreSQL                   sin identificador
alias PN/LID                 sin identificador
app-state incremental        0 claves en 61 mutaciones
app-state snapshot COMPLETO  0 claves en 93 mutaciones
===========================  ==========================================

LA FRONTERA
-----------
Este proveedor obtiene UNA clave de mensaje y termina. No es un segundo
backup:

* no escribe mensajes ni multimedia;
* no guarda historial;
* no se queda escuchando;
* no comparte NADA de criptografia con pywhats.

Lo unico que devuelve es un :class:`WebSeed`. Quien decide que hacer con el es
el mismo camino que ya usa una semilla llegada en vivo. Hay pruebas que fijan
esta frontera, porque es lo que impide que esto se convierta con el tiempo en
un segundo sistema.

LA SESION ES OTRA
-----------------
Vive en ``session/web_bootstrap/`` y es una vinculacion INDEPENDIENTE de la
cuenta: otro dispositivo en la lista del telefono, con su propio QR. No se
copia ni una clave entre las dos, porque son sesiones Signal distintas y
mezclarlas corrompe ambas.

SE PUEDE BORRAR ENTERO
----------------------
Si un dia sobra: se elimina ``web_bootstrap/`` y ``session/web_bootstrap/``, y
el resto del sistema sigue exactamente igual.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.core.logging_setup import get_logger

log = get_logger("WA")

# Prefijo de todo lo que escribe este proveedor. Se distingue a proposito del
# resto: su QR NO es el de pywhats, y confundirlos seria caro.
ETIQUETA = "[WEB_BOOTSTRAP]"

ESTADOS = (
    "starting",
    "qr_required",
    "connecting",
    "searching_chat",
    "seed_found",
    "seed_validated",
    "completed",
    "failed",
)

# Nunca sirven como ancla de una conversacion.
SERVIDORES_EXCLUIDOS = ("broadcast", "newsletter")

# Forma de un identificador de WhatsApp: hexadecimal, 16-32 caracteres.
#
# Hace falta comprobarla AQUI, y no basta con el filtro del backfill. Ese
# filtro (``is_valid_history_cursor_id``) solo rechaza los prefijos que
# generamos nosotros, porque da por hecho que el identificador vino de
# WhatsApp. Una semilla de una fuente EXTERNA no tiene esa garantia: sin este
# chequeo colarian cosas como "xx" o un timestamp.
FORMA_DE_ID = re.compile(r"^[0-9A-Fa-f]{16,32}$")


@dataclass
class WebSeed:
    """Una clave de mensaje real, con la prueba de a que chat pertenece."""

    remote_jid: str
    message_id: str
    from_me: bool
    timestamp: int
    participant: str | None = None
    source: str = "web_bootstrap"

    @property
    def huella(self) -> str:
        """Identificador acortado: se registra esto, nunca el completo."""
        return hashlib.sha256(self.message_id.encode()).hexdigest()[:8]

    def to_json(self) -> dict[str, Any]:
        return {
            "remote_jid_masked": _corto(self.remote_jid),
            "message_id_fp": self.huella,
            "from_me": self.from_me,
            "timestamp": self.timestamp,
            "source": self.source,
        }


@dataclass
class SeedJob:
    """Estado de una busqueda. Lo consulta el frontend mientras corre."""

    job_id: str
    chat_id: int
    state: str = "starting"
    qr: str | None = None
    seed: WebSeed | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    # Que paso DESPUES de tener la semilla. Se separa a proposito: obtenerla y
    # conseguir excavar con ella son dos resultados distintos.
    candidate_emitted: bool = False
    backfill_enqueued: bool = False
    transport_available: bool | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "chat_id": self.chat_id,
            "state": self.state,
            "qr_available": self.qr is not None,
            "seed": self.seed.to_json() if self.seed else None,
            "candidate_emitted": self.candidate_emitted,
            "backfill_enqueued": self.backfill_enqueued,
            "transport_available": self.transport_available,
            "error": self.error,
            "elapsed_seconds": int(
                (self.finished_at or time.time()) - self.started_at
            ),
        }


def _corto(jid: str) -> str:
    user, _, server = jid.partition("@")
    return f"{user[:6]}***@{server}" if server else f"{user[:6]}***"


def _usuario(jid: str) -> str:
    return jid.split("@")[0].split(":")[0].split(".")[0]


class WebSeedProvider:
    """Lanza el proceso auxiliar, valida lo que devuelve y se olvida de el."""

    # Tope de vida del proceso. Un dispositivo auxiliar que se queda
    # conectado deja de ser efimero, que es justo lo que no queremos.
    TIMEOUT_SEGUNDOS = 180

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    # -- Rutas ---------------------------------------------------------------

    @property
    def raiz(self) -> Path:
        """Donde vive el componente Node."""
        return Path(self._settings.session_dir).parent / "web_bootstrap"

    @property
    def session_dir(self) -> Path:
        """Sesion AUXILIAR. Nunca la de pywhats."""
        return Path(self._settings.session_dir) / "web_bootstrap"

    def available(self) -> tuple[bool, str | None]:
        """Si el componente esta instalado y se puede usar."""
        if not (self.raiz / "seed.js").exists():
            return False, "falta web_bootstrap/seed.js"
        if not (self.raiz / "node_modules").exists():
            return False, "faltan las dependencias: ejecuta 'npm install' en web_bootstrap/"
        if shutil.which("node") is None:
            return False, "no hay Node instalado"
        return True, None

    def linked(self) -> bool:
        """Si la sesion auxiliar ya esta vinculada (no hara falta QR)."""
        return (self.session_dir / "creds.json").exists()

    def forget(self) -> bool:
        """Borra SOLO la sesion auxiliar. No toca nada de pywhats.

        Devuelve ``True`` si habia algo que borrar. La sesion principal, el
        Signal Store y PostgreSQL quedan intactos: son cosas distintas y esta
        separacion es la que permite quitar el Plan D sin consecuencias.
        """
        if not self.session_dir.exists():
            return False
        shutil.rmtree(self.session_dir, ignore_errors=True)
        log.info("%s Sesion auxiliar eliminada (la principal NO se ha tocado)", ETIQUETA)
        return True

    # -- Validacion ----------------------------------------------------------

    def validate(self, crudo: dict[str, Any], aliases: set[str]) -> WebSeed | None:
        """Convierte la salida del proceso en semilla, si de verdad sirve.

        Se rechaza en cuanto algo no encaja. Traer una clave que apunte a otra
        conversacion seria peor que no traer ninguna: anclaria un chat con el
        mensaje de otro.
        """
        remote = str(crudo.get("remote_jid") or "")
        message_id = str(crudo.get("message_id") or "")
        if not remote or "@" not in remote or not message_id:
            return None

        servidor = remote.partition("@")[2]
        if servidor in SERVIDORES_EXCLUIDOS:
            log.warning("%s Clave descartada: %s no es una conversacion", ETIQUETA, servidor)
            return None

        # Dos comprobaciones, y las dos hacen falta: la FORMA (que este filtro
        # externo aporta) y el filtro del backfill (que rechaza lo que
        # generamos nosotros).
        if not FORMA_DE_ID.match(message_id):
            log.warning(
                "%s Clave descartada: el identificador no tiene forma de WAMID",
                ETIQUETA,
            )
            return None

        from app.services.repository import is_valid_history_cursor_id

        if not is_valid_history_cursor_id(message_id):
            log.warning("%s Clave descartada: el identificador no sirve de ancla", ETIQUETA)
            return None

        # Pertenencia DEMOSTRADA, no supuesta: se compara por usuario para que
        # un contacto por telefono y por LID cuente como el mismo chat.
        if aliases and _usuario(remote) not in {_usuario(a) for a in aliases}:
            log.warning(
                "%s Clave descartada: pertenece a otra conversacion", ETIQUETA
            )
            return None

        try:
            marca = int(crudo.get("timestamp") or 0)
        except (TypeError, ValueError):
            marca = 0
        if marca <= 0:
            log.warning("%s Clave descartada: sin marca de tiempo real", ETIQUETA)
            return None

        return WebSeed(
            remote_jid=remote,
            message_id=message_id,
            from_me=bool(crudo.get("from_me")),
            timestamp=marca,
            participant=crudo.get("participant") or None,
        )

    # -- Ejecucion -----------------------------------------------------------

    def run(
        self,
        targets: list[dict[str, Any]],
        *,
        on_seed: Callable[[int, WebSeed], None] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        timeout: int | None = None,
    ) -> dict[int, WebSeed]:
        """Busca semillas para VARIOS chats con una sola conexion.

        ``targets`` es ``[{"chat_id": 9, "jids": [...]}, ...]``. Levantar un
        proceso por chat significaria treinta vinculaciones seguidas contra el
        servidor, y el historial llega junto de todas formas: se aprovecha esa
        unica entrega.

        Nunca lanza. Devuelve ``{chat_id: WebSeed}`` con lo encontrado, que
        puede estar vacio: no encontrar semilla es un resultado legitimo, no
        un fallo.
        """
        encontradas: dict[int, WebSeed] = {}

        ok, motivo = self.available()
        if not ok:
            log.error("%s No se puede usar: %s", ETIQUETA, motivo)
            if on_event:
                on_event("failed", {"error": motivo})
            return encontradas
        if not targets:
            return encontradas

        limite = timeout or self.TIMEOUT_SEGUNDOS
        self.session_dir.mkdir(parents=True, exist_ok=True)

        alias_por_chat = {
            int(t["chat_id"]): {str(j) for j in (t.get("jids") or []) if j}
            for t in targets
        }

        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fichero:
            json.dump(
                [
                    {"chat_id": cid, "jids": sorted(jids)}
                    for cid, jids in alias_por_chat.items()
                ],
                fichero,
            )
            ruta_targets = fichero.name

        orden = [
            "node",
            str(self.raiz / "seed.js"),
            "--auth",
            str(self.session_dir),
            "--targets",
            ruta_targets,
            "--timeout",
            str(limite),
        ]

        log.info(
            "%s Buscando claves de mensaje para %d conversacion(es)",
            ETIQUETA,
            len(alias_por_chat),
        )

        try:
            proceso = subprocess.Popen(  # noqa: S603 - orden construida aqui
                orden,
                cwd=str(self.raiz),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("%s No se pudo lanzar el proceso auxiliar", ETIQUETA)
            if on_event:
                on_event("failed", {"error": str(exc)[:200]})
            return encontradas

        vigilante = threading.Timer(limite + 20, proceso.kill)
        vigilante.daemon = True
        vigilante.start()
        try:
            for linea in proceso.stdout or ():
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    evento = json.loads(linea)
                except ValueError:
                    log.debug("%s salida no JSON: %s", ETIQUETA, linea[:120])
                    continue

                nombre = evento.get("event")
                if nombre == "qr":
                    log.info(
                        "%s QR_REQUIRED (vinculacion AUXILIAR, no la principal)",
                        ETIQUETA,
                    )
                    if on_event:
                        on_event("qr_required", {"qr": evento.get("data")})
                elif nombre == "status":
                    self._registrar_estado(evento, on_event)
                elif nombre == "seed":
                    chat_id = evento.get("chat_id")
                    semilla = self.validate(
                        evento, alias_por_chat.get(int(chat_id or -1), set())
                    )
                    if semilla is None:
                        continue
                    encontradas[int(chat_id)] = semilla
                    log.info(
                        "%s WEB_SEED_FOUND chat_id=%s jid=%s message_id_fp=%s "
                        "from_me=%s timestamp=%d",
                        ETIQUETA,
                        chat_id,
                        _corto(semilla.remote_jid),
                        semilla.huella,
                        semilla.from_me,
                        semilla.timestamp,
                    )
                    if on_seed:
                        on_seed(int(chat_id), semilla)
                elif nombre == "error":
                    log.warning("%s %s", ETIQUETA, evento.get("message"))
                elif nombre == "done":
                    log.info(
                        "%s Terminado: %s encontrada(s), %s sin clave",
                        ETIQUETA,
                        evento.get("found"),
                        evento.get("pending"),
                    )
                    break
        except Exception:  # noqa: BLE001 - el auxiliar no puede tumbar nada
            log.exception("%s Fallo leyendo la salida del proceso auxiliar", ETIQUETA)
        finally:
            vigilante.cancel()
            self._cerrar(proceso)
            try:
                Path(ruta_targets).unlink(missing_ok=True)
            except OSError:
                pass

        return encontradas

    def _registrar_estado(
        self, evento: dict[str, Any], on_event: Callable[..., None] | None
    ) -> None:
        estado = evento.get("state")
        if estado == "history":
            # El detalle importa: si los chats objetivo aparecen en la lista
            # pero sin mensajes, es el mismo muro que en pywhats.
            log.info(
                "%s historial: tipo=%s progreso=%s chats=%s mensajes=%s "
                "objetivos_presentes=%s ultimo=%s",
                ETIQUETA,
                evento.get("sync_type"),
                evento.get("progress"),
                evento.get("chats"),
                evento.get("messages"),
                evento.get("targets_present"),
                evento.get("is_latest"),
            )
            if on_event:
                on_event("searching_chat", {})
        else:
            log.info("%s %s", ETIQUETA, estado)
            if estado == "connected" and on_event:
                on_event("searching_chat", {})

    def _cerrar(self, proceso: Any) -> None:
        """Se asegura de que el auxiliar no siga vivo."""
        try:
            if proceso.poll() is None:
                proceso.terminate()
                try:
                    proceso.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proceso.kill()
        except Exception:  # noqa: BLE001
            pass
        for flujo in (proceso.stdout, proceso.stderr):
            try:
                if flujo:
                    flujo.close()
            except Exception:  # noqa: BLE001
                pass
