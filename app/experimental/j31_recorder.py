"""La escalera del Plan J3.1: mismas marcas de tiempo para los dos lados.

QUE PROBLEMA RESUELVE
---------------------
La medición anterior comparó el bootstrap de la principal —un instante— contra
el almacén del navegador después de días. La corrección no es medir mejor: es
medir **a la misma edad**. Y eso no se puede dejar en manos de un cronómetro
humano, porque nadie captura a los 30 segundos exactos dos veces seguidas.

Así que el propio servicio se cronometra. Cuando la sesión principal queda
lista, apunta su T0 y saca una foto a los 0, 30, 60 y 120 segundos. Cuando el
navegador queda listo, hace exactamente lo mismo con su propio T0.

REGLAS QUE SE CUMPLEN AQUI
--------------------------
* **§20-§21** — cada lado cuenta desde SU T0, no desde el arranque de Python.
* **§19** — cada captura es un archivo aparte, inmutable.
* **§43** — si algo cambió entre los 60 y los 120 segundos, se alarga hasta
  los 300; si estaba quieto, se para. La decisión queda escrita.
* **§44/§63** — al terminar se congela el resultado y NO se vuelve a tocar,
  aunque después entren mensajes en vivo.
* **§56** — al navegador se le lee el almacén tal cual: ni ``fetchMessages``
  ni ``loadEarlierMsgs`` durante la ventana.

APAGADO POR DEFECTO
-------------------
No hace nada salvo que ``PLAN_J31_ENABLED`` esté puesto. Es instrumentación de
un experimento concreto, no una función del producto.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("J31")

#: Los peldaños, en segundos desde el T0 de cada lado.
ESCALERA = (0, 30, 60, 120)

#: El peldaño opcional. Sólo se sube si a los 120 segundos la cosa seguía
#: moviéndose (§43).
PELDANO_LARGO = 300

#: Cada cuánto se pregunta si el lado ya está listo. Un segundo es de sobra:
#: la ventana que se mide son dos minutos.
LATIDO = 1.0

#: Lo que hace que un peldaño se considere «se movió» entre dos capturas.
CAMPOS_QUE_DECIDEN = (
    "user_chat_count",
    "user_chats_with_valid_seed",
    "cached_message_count",
)

CARPETA = Path("debug/plan_j31")


def _iso(momento: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(momento))


class RegistradorJ31:
    """Cronometra los dos lados y guarda las fotos.

    Un hilo por lado. No comparten estado más que la carpeta de salida, y cada
    uno arranca su cuenta cuando SU lado está listo: si el navegador se vincula
    veinte minutos después, su escalera empieza entonces.
    """

    def __init__(self, runtime: Any, *, carpeta: Path | None = None) -> None:
        self.runtime = runtime
        self.carpeta = carpeta or CARPETA
        self._parar = threading.Event()
        self._hilos: list[threading.Thread] = []
        #: Lo congelado. Una vez escrito, no se vuelve a escribir (§44).
        self.congelado: dict[str, dict[str, Any]] = {}

    # -- Ciclo de vida -----------------------------------------------------

    def arrancar(self) -> None:
        """Pone en marcha los dos vigilantes. Nunca lanza."""
        try:
            self.carpeta.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("J31 no pudo crear %s: %s", self.carpeta, exc)
            return
        for lado, objetivo in (("primary", self._vigilar_principal),
                               ("web", self._vigilar_navegador)):
            hilo = threading.Thread(
                target=self._envolver, args=(objetivo,), name=f"j31-{lado}", daemon=True
            )
            hilo.start()
            self._hilos.append(hilo)
        log.info("J31 escalera armada: %s s (+%s si sigue moviendose)",
                 list(ESCALERA), PELDANO_LARGO)

    def parar(self) -> None:
        self._parar.set()

    def _envolver(self, objetivo: Any) -> None:
        try:
            objetivo()
        except Exception as exc:  # noqa: BLE001 - un fallo aqui no puede tumbar el servicio
            log.warning("J31 vigilante caido: %s", exc)

    def _dormir(self, segundos: float) -> bool:
        """Espera. Devuelve ``False`` si mientras tanto se pidió parar."""
        return not self._parar.wait(segundos)

    # -- Lado principal ----------------------------------------------------

    def _vigilar_principal(self) -> None:
        from app.core.primary import primary_ready

        while self._dormir(LATIDO):
            if primary_ready(self.runtime):
                break
        else:
            return
        t0 = time.time()
        log.info("J31 T0 PRINCIPAL marcado")
        self._marcar_t0("primary", t0)
        self._subir_escalera("primary", t0, self._foto_principal)

    def _foto_principal(self, t0: float, etiqueta: str) -> Any:
        from app.discovery.symmetric_snapshot import fotografiar_principal

        with self.runtime.database.session() as sesion:
            return fotografiar_principal(
                sesion,
                account_id=getattr(self.runtime, "runtime_owner_account_id", None),
                t0=t0,
                etiqueta=etiqueta,
            )

    # -- Lado navegador ----------------------------------------------------

    def _vigilar_navegador(self) -> None:
        while self._dormir(LATIDO):
            if self._navegador_listo():
                break
        else:
            return
        t0 = time.time()
        log.info("J31 T0 NAVEGADOR marcado")
        self._marcar_t0("web", t0)
        self._subir_escalera("web", t0, self._foto_navegador)

    def _navegador_listo(self) -> bool:
        companion = getattr(self.runtime, "web_companion", None)
        if companion is None:
            return False
        try:
            estado = companion.snapshot()
        except Exception:  # noqa: BLE001
            return False
        return bool(estado.get("store_ready")) and bool(estado.get("web_client_ready"))

    def _foto_navegador(self, t0: float, etiqueta: str) -> Any:
        from app.discovery.symmetric_snapshot import fotografiar_navegador

        companion = self.runtime.web_companion
        respuesta = companion.enviar({"cmd": "j31_store_snapshot"}, timeout=45.0)
        return fotografiar_navegador(respuesta, t0=t0, etiqueta=etiqueta)

    def _marcar_t0(self, lado: str, t0: float) -> None:
        """Deja el T0 por escrito.

        Sin esto, cualquier captura hecha a mano despues tendria que inventarse
        una edad de sesion, y comparar edades inventadas es exactamente el
        error que motivo esta fase.
        """
        self._guardar(
            f"{lado}_t0.json",
            {"side": lado, "t0_epoch": round(t0, 3), "t0_iso": _iso(t0)},
        )

    # -- La escalera, igual para los dos -----------------------------------

    def _subir_escalera(self, lado: str, t0: float, fotografo: Any) -> None:
        capturas: dict[str, Any] = {}
        peldanos = list(ESCALERA)
        alargado = False
        indice = 0

        while indice < len(peldanos):
            offset = peldanos[indice]
            indice += 1
            objetivo = t0 + offset
            espera = objetivo - time.time()
            if espera > 0 and not self._dormir(espera):
                return

            etiqueta = f"t{offset:03d}"
            try:
                foto = fotografo(t0, etiqueta)
            except Exception as exc:  # noqa: BLE001
                log.warning("J31 %s %s fallo: %s", lado, etiqueta, exc)
                continue
            capturas[etiqueta] = foto
            self._guardar(f"{lado}_{etiqueta}.json", foto.to_json())
            log.info(
                "J31 %s %s chats=%s usuario=%s anclas=%s mensajes=%s",
                lado, etiqueta, foto.raw_chat_count, foto.user_chat_count,
                foto.user_chats_with_valid_seed, foto.mensajes_en_cache,
            )

            # §43: alargar sólo si a los 120 seguía cambiando.
            if offset == 120 and not alargado:
                alargado = True
                if self._se_movio(capturas.get("t060"), foto):
                    peldanos.append(PELDANO_LARGO)
                    log.info("J31 %s sigue moviendose: se alarga a t300", lado)

        self._congelar(lado, t0, capturas, alargado)

    @staticmethod
    def _se_movio(antes: Any, despues: Any) -> bool:
        """¿Cambió algo que importe entre dos capturas? (§43)"""
        if antes is None or despues is None:
            return True
        a = antes.to_json()["metrics"]
        b = despues.to_json()["metrics"]
        return any(a.get(campo) != b.get(campo) for campo in CAMPOS_QUE_DECIDEN)

    def _congelar(
        self, lado: str, t0: float, capturas: dict[str, Any], alargado: bool
    ) -> None:
        """Guarda el resultado final y lo deja intocable (§44, §63)."""
        if not capturas:
            return
        ultima = capturas[max(capturas, key=lambda k: int(k[1:]))]
        nombre = "PRIMARY_BOOTSTRAP_FINAL" if lado == "primary" else "WEB_BOOTSTRAP_FINAL"
        if nombre in self.congelado:
            log.info("J31 %s ya estaba congelado: no se toca", nombre)
            return
        cuerpo = {
            "name": nombre,
            "side": lado,
            "t0_epoch": round(t0, 3),
            "ladder": sorted(int(k[1:]) for k in capturas),
            "extended_to_300": alargado and PELDANO_LARGO in [int(k[1:]) for k in capturas],
            "extension_decision": (
                "alargado: seguia cambiando entre t060 y t120"
                if any(int(k[1:]) == PELDANO_LARGO for k in capturas)
                else "no alargado: estable entre t060 y t120"
            ),
            "final": ultima.to_json(),
        }
        self.congelado[nombre] = cuerpo
        self._guardar(f"{nombre}.json", cuerpo)
        log.info(
            "J31 %s CONGELADO usuario=%s anclas=%s",
            nombre, ultima.user_chat_count, ultima.user_chats_with_valid_seed,
        )

    def _guardar(self, nombre: str, cuerpo: dict[str, Any]) -> None:
        """Escribe una captura. Nunca pisa una que ya exista (§19)."""
        destino = self.carpeta / nombre
        if destino.exists():
            log.warning("J31 %s ya existe: no se sobrescribe", nombre)
            return
        try:
            destino.write_text(
                json.dumps(cuerpo, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("J31 no pudo escribir %s: %s", nombre, exc)


def arrancar_si_procede(runtime: Any) -> RegistradorJ31 | None:
    """Pone el registrador en marcha sólo si el experimento está encendido."""
    ajustes = getattr(runtime, "settings", None)
    if not getattr(ajustes, "plan_j31_enabled", False):
        return None
    registrador = RegistradorJ31(runtime)
    registrador.arrancar()
    return registrador
