"""Cerrojo de sesion: un solo proceso manda sobre el companion.

EL PELIGRO CONCRETO
-------------------
``session/device.json`` y ``session/device.json.signal.db`` son el estado del
protocolo: claves de Noise, sesiones Signal, prekeys, contadores del ratchet.
Dos procesos escribiendo ahi a la vez no producen "un conflicto"; producen una
sesion corrupta que WhatsApp rechaza, y entonces hay que volver a vincular.

Por eso dos ``py service.py`` no pueden abrir la sesion a la vez.

COMO SE HACE
------------
Un archivo ``session/runtime.lock`` creado con ``O_EXCL``: o lo crea este
proceso, o ya existe. Dentro va el PID y el nombre de quien manda, para poder
decir en el error QUIEN tiene la sesion, no solo que esta ocupada.

CERROJOS HUERFANOS
------------------
Si un proceso muere de mala manera el archivo se queda. Para detectarlo hacen
falta DOS senales, y la segunda no es opcional:

1. que el PID siga vivo, y
2. que el cerrojo tenga un LATIDO reciente.

El PID solo no basta, y no es teoria: se midio. Windows reutiliza los PID, y un
cerrojo de un proceso muerto se quedo bloqueando el arranque porque otro
``python.exe`` cualquiera habia heredado su numero. Con el latido eso se acaba:
el dueno reescribe el archivo cada pocos segundos, asi que un cerrojo sin
refrescar en un minuto esta muerto aunque su PID exista.

Retirar un cerrojo caducado es la unica situacion en la que se borra un archivo
de ``session/``, y solo se borra ESE: nunca ``device.json`` ni la base Signal.

LO QUE NO ES
------------
No protege PostgreSQL. Dos procesos pueden LEER la base a la vez sin problema:
es justo lo que hace ``service.py --local``. Lo que se serializa es la sesion de
WhatsApp.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("APP")

LOCK_FILENAME = "runtime.lock"

# Cada cuanto refresca su cerrojo el proceso que lo tiene.
HEARTBEAT_INTERVAL = 15.0

# Sin latido durante este tiempo, el cerrojo se da por muerto. Con holgura
# sobre el intervalo para que una maquina cargada no pierda el suyo.
STALE_AFTER = 60.0


class SessionLockedError(RuntimeError):
    """Otro proceso ya controla la sesion de WhatsApp."""

    def __init__(self, holder: "LockInfo") -> None:
        self.holder = holder
        super().__init__(str(holder))


@dataclass(frozen=True)
class LockInfo:
    """Quien tiene el cerrojo, con identidad suficiente para distinguirlo.

    El PID solo no basta para saber QUE proceso es. Se midio en esta maquina:
    habia dos ``service.py`` a la vez, uno del ``.venv`` y otro del Python
    global, y por el numero no habia forma de decir cual era cual. Por eso se
    guarda tambien el interprete y la linea de ordenes.
    """

    pid: int
    owner: str
    acquired_at: str
    executable: str = ""
    command_line: str = ""

    def __str__(self) -> str:
        return (
            f"la sesion de WhatsApp esta bloqueada por OTRO proceso: "
            f"'{self.owner}' (PID {self.pid}, desde {self.acquired_at})"
        )

    def describe(self) -> str:
        """Varias lineas, para que el usuario identifique el proceso exacto."""
        lineas = [
            f"  PID          {self.pid}",
            f"  owner        {self.owner}",
            f"  desde        {self.acquired_at}",
        ]
        if self.executable:
            lineas.append(f"  interprete   {self.executable}")
        if self.command_line:
            lineas.append(f"  linea        {self.command_line}")
        return "\n".join(lineas)


def _process_alive(pid: int) -> bool:
    """``True`` si ese PID sigue existiendo.

    En Windows no hay ``os.kill(pid, 0)`` fiable, asi que se pregunta al
    sistema con ``tasklist``. Ante la duda se responde que SI esta vivo: dar
    por muerto un proceso que no lo esta seria justo el error que corrompe la
    sesion.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import csv
        import io
        import subprocess

        try:
            salida = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
        except Exception:  # noqa: BLE001 - si no se sabe, se asume vivo
            return True
        # Se compara la COLUMNA del PID, no la salida entera: la de memoria
        # trae numeros con separador de miles ("8.560 KB") y un "in" suelto
        # daria por vivo a un proceso muerto.
        for fila in csv.reader(io.StringIO(salida)):
            if len(fila) >= 2 and fila[1].strip() == str(pid):
                return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SessionLock:
    """Cerrojo de la sesion. Se usa como gestor de contexto."""

    def __init__(self, session_dir: Path, *, owner: str) -> None:
        self._path = Path(session_dir) / LOCK_FILENAME
        self._owner = owner
        self._held = False
        self._stop_heartbeat = threading.Event()
        self._heartbeat: threading.Thread | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._held

    # -- Lectura -------------------------------------------------------------

    def read(self) -> LockInfo | None:
        """Quien tiene el cerrojo ahora mismo, o ``None`` si esta libre."""
        try:
            datos = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            return LockInfo(
                pid=int(datos.get("pid", 0)),
                owner=str(datos.get("owner", "desconocido")),
                acquired_at=str(datos.get("acquired_at", "")),
                # Un cerrojo escrito por una version anterior no los trae.
                executable=str(datos.get("executable", "")),
                command_line=str(datos.get("command_line", "")),
            )
        except (TypeError, ValueError):
            return None

    def _seconds_since_heartbeat(self) -> float:
        """Segundos desde el ultimo latido. Infinito si no se puede leer."""
        try:
            return max(0.0, time.time() - self._path.stat().st_mtime)
        except OSError:
            return float("inf")

    def _is_stale(self, titular: LockInfo | None) -> bool:
        """``True`` si el cerrojo esta muerto y se puede retirar.

        Dos senales, y basta con que falle UNA:

        * el PID ya no existe, o
        * nadie lo ha refrescado en ``STALE_AFTER`` segundos.

        La segunda es la que cubre la reutilizacion de PID, que en Windows
        pasa de verdad: sin ella, un cerrojo de un proceso muerto bloquea el
        arranque porque otro ``python.exe`` heredo su numero.
        """
        if titular is None:
            return True
        if not _process_alive(titular.pid):
            return True
        return self._seconds_since_heartbeat() > STALE_AFTER

    # -- Toma y suelta -------------------------------------------------------

    def acquire(self) -> "SessionLock":
        """Toma el cerrojo. Lanza :class:`SessionLockedError` si esta ocupado."""
        self._path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._crear()
        except FileExistsError:
            titular = self.read()
            if not self._is_stale(titular):
                raise SessionLockedError(titular) from None
            # Cerrojo huerfano. Se retira SOLO este archivo; device.json y la
            # base Signal no se tocan jamas.
            log.warning(
                "Cerrojo huerfano (PID %s, sin latido desde hace %.0fs); se retira",
                titular.pid if titular else "?",
                self._seconds_since_heartbeat(),
            )
            try:
                self._path.unlink()
            except OSError:  # pragma: no cover - otro proceso se adelanto
                pass
            try:
                self._crear()
            except FileExistsError:
                titular = self.read()
                raise SessionLockedError(
                    titular or LockInfo(0, "otro proceso", "")
                ) from None

        self._held = True
        self._start_heartbeat()
        # "adquirido", no "bloqueada por": lo acaba de tomar ESTE proceso y va
        # a seguir. "Bloqueada por" se reserva para cuando la tiene OTRO, que
        # es el unico caso en que alguien se queda fuera.
        log.info(
            "Cerrojo de sesion adquirido (%s PID %d)", self._owner, os.getpid()
        )
        return self

    def _start_heartbeat(self) -> None:
        """Refresca el cerrojo periodicamente mientras este proceso viva.

        Es lo que permite distinguir un dueno vivo de un PID reutilizado.
        """
        self._stop_heartbeat.clear()

        def latir() -> None:
            while not self._stop_heartbeat.wait(HEARTBEAT_INTERVAL):
                try:
                    os.utime(self._path, None)
                except OSError:
                    return

        self._heartbeat = threading.Thread(
            target=latir, name="session-lock-heartbeat", daemon=True
        )
        self._heartbeat.start()

    def _crear(self) -> None:
        # O_EXCL: la creacion es atomica. O la hace este proceso, o falla.
        descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            import sys

            contenido = json.dumps(
                {
                    "pid": os.getpid(),
                    "owner": self._owner,
                    "acquired_at": datetime.now(timezone.utc)
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M:%S"),
                    # Con que interprete y con que orden se arranco. Es lo que
                    # permite distinguir el service.py del .venv del que se
                    # lanzo con el Python global.
                    "executable": sys.executable,
                    "command_line": " ".join(sys.argv),
                },
                ensure_ascii=False,
            )
            os.write(descriptor, contenido.encode("utf-8"))
        finally:
            os.close(descriptor)

    def release(self) -> None:
        """Suelta el cerrojo si es nuestro. Nunca suelta el de otro."""
        self._stop_heartbeat.set()
        self._heartbeat = None
        if not self._held:
            return
        titular = self.read()
        if titular is not None and titular.pid != os.getpid():
            log.warning("El cerrojo pertenece a otro proceso; no se retira")
            self._held = False
            return
        try:
            self._path.unlink()
        except OSError:  # pragma: no cover
            pass
        self._held = False
        log.info("Cerrojo de sesion liberado (%s)", self._owner)

    def __enter__(self) -> "SessionLock":
        return self.acquire()

    def __exit__(self, *_excinfo: Any) -> None:
        self.release()


def explain(error: SessionLockedError) -> str:
    """Mensaje para el usuario. Dice que hacer, no solo que fallo."""
    return "\n".join(
        (
            f"No se puede abrir la sesion de WhatsApp: {error.holder}.",
            "",
            "Solo un proceso puede tener la sesion abierta a la vez. Compartirla",
            "corromperia el estado del protocolo y obligaria a volver a vincular",
            "el dispositivo.",
            "",
            "Cierra el otro proceso y vuelve a intentarlo:  py service.py",
            "",
            "Para leer la copia local sin tocar la sesion, y con el otro proceso",
            "abierto, usa:  py service.py --local",
        )
    )


def probe(session_dir: Path) -> LockInfo | None:
    """Quien controla la sesion AHORA, sin intentar tomar el cerrojo.

    Devuelve ``None`` si esta libre o si el cerrojo que hay esta muerto (su
    proceso ya no existe, o nadie lo refresca). Sirve para que un segundo
    arranque pueda decir "ya hay un service.py, PID X" y salir, en vez de
    seguir a medias.

    Nunca mata ni toca el proceso que lo tiene: solo informa.
    """
    cerrojo = SessionLock(session_dir, owner="probe")
    titular = cerrojo.read()
    if titular is None:
        return None
    if cerrojo._is_stale(titular):
        return None
    return titular
