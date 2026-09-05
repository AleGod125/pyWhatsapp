# Experimento: anunciarse como Desktop

**No implementado. No activar sobre la sesión actual.**

## La hipótesis

WhatsApp puede entregar más historial —en particular sincronizaciones
`RECENT`— a un dispositivo vinculado que se anuncia como cliente de escritorio,
frente a uno que se anuncia como navegador.

En esta cuenta, `RECENT` no ha llegado **ni una vez** en 318 blobs y 4
emparejamientos. Si esa hipótesis fuera cierta, explicaría por qué 28
conversaciones siguen sin ninguna referencia.

## Qué habría que mirar

`DeviceProps`, que viaja en el registro del dispositivo:

- `platform`
- `requireFullSync`
- `historySyncConfig` — `recentSyncDaysLimit`, `storageQuotaMb`,
  `supportRecentSyncChunkMessageCountTuning`
- `webSubPlatform`

Los valores actuales del proyecto salen de las variables `PAIRING_FULL_SYNC*`
de `.env`.

## Por qué no se toca ahora

`DeviceProps` viaja **solo en el registro**. Cambiarlo no afecta a una sesión
ya vinculada: exigiría desvincular y volver a emparejar.

Y eso, sobre la sesión actual, significaría:

- perder la vinculación que funciona
- volver a escanear un QR
- un `device.json` y un Signal Store nuevos
- rehacer el emparejamiento con todo lo que arrastra

A cambio de una hipótesis **sin comprobar**. No compensa.

## Cómo probarlo bien, el día que toque

Con una **cuenta y una instalación separadas**, nunca sobre la actual:

1. Otro `SESSION_DIR` y otra base de datos.
2. Ajustar `DeviceProps` a valores de escritorio.
3. Emparejar de cero.
4. Medir los tipos de History Sync recibidos y compararlos con la tabla de
   [PLAN_E_HISTORY_RECOVERY.md](PLAN_E_HISTORY_RECOVERY.md).

El criterio es simple: **¿llega `RECENT`?** Si no llega, la hipótesis queda
descartada y no hay que tocar nada más.

## Riesgos

- Anunciar una plataforma que no somos puede cambiar cómo trata el servidor a
  la sesión, y no se sabe cómo.
- Un emparejamiento nuevo consume una ranura de dispositivo vinculado.
- El resultado puede ser exactamente el mismo, y entonces se habrá perdido una
  sesión buena por nada.
