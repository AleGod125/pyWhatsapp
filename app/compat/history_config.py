"""Pedir historial COMPLETO al vincular, en vez de un adelanto.

EL PROBLEMA, MEDIDO
-------------------
Tras un pairing nuevo, 32 de 40 chats llegaron sin un solo mensaje. El caso de
control, "Isaac Virtual Tec", vino asi en el ``INITIAL_BOOTSTRAP``::

    id                    = 64940106866902@lid
    conversationTimestamp = 1788199305      <- hubo actividad
    (campo 2 'messages')  = AUSENTE         <- cero mensajes

Y no se perdio nada al parsear: se comparo, chat por chat, lo que venia CRUDO
en los blobs contra lo persistido, y no hay ni un chat con ``crudo>0`` y
``persistido=0``. WhatsApp entrego esas conversaciones como metadata y punto.

LA CAUSA
--------
``pywhats.pairing._device_props()`` registra el companion con::

    dp.requires_full_sync = False
    # y NINGUN history_sync_config

Esos dos campos son lo que le dice al servidor cuanto historial sembrar en la
sincronizacion inicial. Sin ellos, manda el adelanto minimo.

QUE HACE ESTE MODULO
--------------------
Anade ``requires_full_sync`` y un ``HistorySyncConfig`` al DeviceProps. NO
toca ni una linea de criptografia: el handshake Noise, el ADV, el
``pair-device-sign``, el restart 515 y todo Signal siguen exactamente igual.
Lo unico que cambia es UN campo de metadatos del registro.

Los nombres y numeros de campo NO son inventados: salen del descriptor que
trae el propio paquete (``pywhats.proto.DeviceProps.HistorySyncConfig``),
verificado en el paquete instalado::

    1 full_sync_days_limit
    2 full_sync_size_mb_limit
    3 storage_quota_mb
    4 inline_initial_payload_in_notification
    5 non_inline_initial_payload_in_notification
    6 media_request_config

SOLO SIRVE AL VINCULAR
----------------------
El DeviceProps viaja en el registro del companion. Cambiarlo NO afecta a una
sesion ya establecida: para que surta efecto hay que vincular de nuevo.

LO QUE NO SE PROMETE
--------------------
Que se pida el historial completo no garantiza recibirlo. El servidor acota lo
que envia por su cuenta, y no puede entregar lo que el telefono ya no tiene.
Esto pide una ventana amplia; cuanto se reciba de verdad se sabra midiendolo,
no suponiendolo.
"""

from __future__ import annotations

from typing import Any

from app.core.logging_setup import get_logger

log = get_logger("PAIRING")

_MARKER = "_whatsapp_backup_history_config"


def describe(settings: Any) -> dict[str, Any]:
    """Los valores que se enviaran. Para poder registrarlos y probarlos."""
    return {
        "requires_full_sync": settings.pairing_full_sync,
        "full_sync_days_limit": settings.pairing_full_sync_days,
        "full_sync_size_mb_limit": settings.pairing_full_sync_size_mb,
        "storage_quota_mb": settings.pairing_storage_quota_mb,
    }


def apply(settings: Any) -> bool:
    """Envuelve ``_device_props`` para incluir la configuracion de historial.

    Se envuelve la funcion original y se le anaden campos al protobuf que
    devuelve; no se reescribe. Asi el ``os``, la ``version`` y el
    ``platform_type`` que pywhats eligio a proposito (se hace pasar por un
    navegador para que el servidor no rechace el pairing) quedan intactos.

    Idempotente: aplicarlo dos veces no hace nada.
    """
    from pywhats import pairing as pairing_module
    from pywhats.proto import DeviceProps

    original = pairing_module._device_props
    if getattr(original, _MARKER, False):
        return True

    valores = describe(settings)

    def _device_props(pairing_name: str) -> bytes:
        # Se parte del payload original y se le anade, no se sustituye.
        dp = DeviceProps()
        dp.ParseFromString(original(pairing_name))

        dp.requires_full_sync = bool(valores["requires_full_sync"])

        configuracion = dp.history_sync_config
        configuracion.full_sync_days_limit = int(valores["full_sync_days_limit"])
        configuracion.full_sync_size_mb_limit = int(
            valores["full_sync_size_mb_limit"]
        )
        configuracion.storage_quota_mb = int(valores["storage_quota_mb"])
        # El payload inicial NO va en linea dentro de la notificacion: se
        # descarga como blob, que es lo que ya sabemos procesar y lo que evita
        # que un historial grande no quepa en el aviso.
        configuracion.inline_initial_payload_in_notification = False

        return bytes(dp.SerializeToString())

    setattr(_device_props, _MARKER, True)
    pairing_module._device_props = _device_props  # type: ignore[assignment]

    log.info(
        "Historial completo solicitado al vincular: full_sync=%s dias=%d "
        "tamano=%dMB cuota=%dMB",
        valores["requires_full_sync"],
        valores["full_sync_days_limit"],
        valores["full_sync_size_mb_limit"],
        valores["storage_quota_mb"],
    )
    log.info(
        "Solo surte efecto en una vinculacion NUEVA: el DeviceProps viaja en "
        "el registro del companion"
    )
    return True
