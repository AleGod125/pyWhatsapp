# Experimento: anunciarse como Desktop

## RESULTADO — 9 sep 2026: la hipótesis no se puede probar. WhatsApp lo rechaza.

Al migrar a Baileys el experimento se hizo solo: `Browsers.macOS('Desktop')`
junto a `syncFullHistory` sube `webInfo.webSubPlatform` a `DARWIN`
(`Utils/validate-connection.js:31`). El servidor **cierra el WebSocket con
código 1011** justo después del saludo Noise, sin llegar a emitir un QR.

Matriz medida contra el servidor real, cinco intentos:

| Navegador anunciado | `syncFullHistory` | `webSubPlatform` | Resultado |
|---|---|---|---|
| Mac OS / Desktop | sí | `DARWIN` (3) | **428, ningún QR** |
| Windows / Desktop | sí | `WIN32` (4) | **428, ningún QR** |
| Windows / Chrome | sí | `WIN32` (4) | **428, ningún QR** |
| Mac OS / Desktop | no | `WEB_BROWSER` (0) | QR en 0,4 s |
| Ubuntu / Chrome | sí | `WEB_BROWSER` (0) | QR en 0,4 s |

Es `webSubPlatform` y nada más: `userAgent.platform` vale `WEB` (14) en los
cinco casos, y la versión anunciada no cambia el resultado (probado con la
compilada en Baileys y con la que sirve `sw.js` en vivo).

La ventana de historial que se buscaba aquí ya se consiguió por otra vía: 315
días con perfil de navegador, y en 34 de 34 conversaciones agotadas fue el
**teléfono** quien dio el historial por terminado. El techo no era el perfil.

Lo que sigue abajo es la hipótesis original, que se conserva porque explica
por qué se probó.

---

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
