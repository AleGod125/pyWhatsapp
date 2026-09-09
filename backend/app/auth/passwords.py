"""Hash de contrasenas con Argon2id.

POR QUE ARGON2id
----------------
Es el ganador del Password Hashing Competition y la recomendacion actual de
OWASP. Un hash rapido (SHA-256, MD5) no sirve aqui: la velocidad es justo lo
que necesita quien prueba millones de candidatas contra una base robada.

El coste va en la configuracion, no aqui, para poder subirlo cuando el
hardware avance sin tocar codigo.

REHASH TRANSPARENTE
-------------------
``verify`` avisa si el hash guardado usa parametros mas debiles que los
actuales. Quien llama puede volver a guardarlo con el coste nuevo, y asi las
contrasenas antiguas se refuerzan solas al iniciar sesion.
"""

from __future__ import annotations

from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

#: Longitud minima. Se prioriza la LONGITUD sobre las reglas de composicion:
#: exigir simbolos y mayusculas produce ``Password1!`` una y otra vez, que es
#: peor que una frase larga.
LONGITUD_MINIMA = 8
LONGITUD_RECOMENDADA = 12

#: Argon2 procesa el hash entero; un limite alto evita que una entrada enorme
#: se convierta en una negacion de servicio barata.
LONGITUD_MAXIMA = 1024


@dataclass(frozen=True)
class ResultadoVerificacion:
    """Si la contrasena vale, y si conviene volver a guardarla."""

    valida: bool
    necesita_rehash: bool = False


class PasswordHasherService:
    """Envoltorio fino sobre Argon2id. Sin criptografia propia."""

    def __init__(
        self,
        *,
        time_cost: int = 3,
        memory_cost_kib: int = 65536,
        parallelism: int = 4,
    ) -> None:
        self._ph = PasswordHasher(
            time_cost=time_cost,
            memory_cost=memory_cost_kib,
            parallelism=parallelism,
        )

    def hash(self, password: str) -> str:
        problema = validar_password(password)
        if problema is not None:
            raise ValueError(problema)
        return self._ph.hash(password)

    def verify(self, hash_guardado: str | None, password: str) -> ResultadoVerificacion:
        """Comprueba la contrasena. NUNCA lanza por una contrasena mala.

        Con ``hash_guardado`` a ``None`` (cuenta solo de Google) se hace de
        todas formas una verificacion de descarte contra un hash ficticio: sin
        ella, responder al instante revelaria que esa cuenta no tiene
        contrasena local, que es informacion sobre un usuario concreto.
        """
        if not hash_guardado:
            self._quemar_tiempo(password)
            return ResultadoVerificacion(valida=False)

        try:
            self._ph.verify(hash_guardado, password)
        except VerifyMismatchError:
            return ResultadoVerificacion(valida=False)
        except InvalidHashError:
            # Un hash corrupto en la base no es una contrasena valida, y
            # tampoco puede tumbar el login.
            return ResultadoVerificacion(valida=False)
        except Exception:  # noqa: BLE001 - ninguna sorpresa autentica a nadie
            return ResultadoVerificacion(valida=False)

        return ResultadoVerificacion(
            valida=True,
            necesita_rehash=self._ph.check_needs_rehash(hash_guardado),
        )

    def _quemar_tiempo(self, password: str) -> None:
        """Gasta el mismo trabajo que una verificacion real."""
        try:
            self._ph.verify(_HASH_FICTICIO, password)
        except Exception:  # noqa: BLE001 - solo interesa el tiempo consumido
            pass


#: Hash de una contrasena que nadie tiene, generado con los parametros por
#: defecto. Existe solo para que el camino "no hay contrasena" tarde lo mismo
#: que el camino "la contrasena no coincide".
_HASH_FICTICIO = PasswordHasher().hash("una contrasena que no es de nadie")


def validar_password(password: str) -> str | None:
    """El motivo por el que no vale, o ``None`` si vale.

    Se comprueba SIEMPRE en el servidor. Lo que valide el navegador es
    comodidad para el usuario, no una garantia.
    """
    if not isinstance(password, str) or not password:
        return "La contrasena no puede estar vacia."
    if len(password) < LONGITUD_MINIMA:
        return f"La contrasena necesita al menos {LONGITUD_MINIMA} caracteres."
    if len(password) > LONGITUD_MAXIMA:
        return f"La contrasena no puede pasar de {LONGITUD_MAXIMA} caracteres."
    return None
