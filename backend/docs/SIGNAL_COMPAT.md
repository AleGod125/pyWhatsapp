# Compatibilidades Signal

Parches sobre `pywhats` 0.2.0, aplicados en `app/compat/`. **Ninguno modifica
`site-packages`** y ninguno relaja una comprobación criptográfica.

## COMPAT_PREKEY_REPLAY — PreKeySignalMessage repetido

Una OPK se consume y se borra en el primer uso. Si el mismo `pkmsg` se
reenvía (retransmisión), pywhats vuelve a buscar esa OPK y falla con
`unknown one-time pre-key id N`.

`app/compat/prekey_compat.py` registra cada establecimiento
(`session_id → base_key + identity_key`) tras un descifrado correcto. Si llega
un `pkmsg` **con la misma base_key y la misma identidad remota** y hay sesión
guardada, reutiliza el ratchet existente en vez de rehacer X3DH.

Condiciones para reutilizar, todas obligatorias:

- `base_key` idéntica — si cambia, es un rekey real y va por el camino normal;
- `identity_key` idéntica;
- sesión existente en el store.

**El MAC se verifica igual** (`verify_mac=` se pasa explícitamente al
`ratchet_decrypt`). Lo único que se evita es repetir el X3DH.

### El registro tiene vida propia

`archive_session()` cierra el registro y lo deja en `None` para poder mover el
archivo en Windows. `apply()` **reabre el registro siempre**, aunque el parche
ya esté puesto.

Se midió lo contrario: `apply()` salía pronto por estar ya parcheada, el
registro se quedaba en `None` y el cuerpo del parche caía de largo al camino
original. Tras un re-pairing en el mismo proceso, la reutilización dejaba de
existir **en silencio** y volvía `unknown one-time pre-key id N` en cada
reenvío.

## COMPAT_OWN_LID_MAP — nuestro propio par PN↔LID

Los mensajes que el usuario envía **desde su teléfono** llegan al companion
pero fallan con `no session for peer <mi propio LID>`. La sesión existe, pero
guardada bajo la dirección de teléfono.

Se siembra el par propio en el `lid_map` de pywhats para que
`_migrate_known_lid_sender()` pueda migrarla. `verify()` comprueba que el mapa
**resuelve de verdad** y devuelve `False` si no.

Es un **alias de identidad, no una clonación criptográfica**: no se copian
sesiones ni ratchets entre PN y LID.

## Los otros

| Flag | Qué arregla |
|---|---|
| `COMPAT_WA_VERSION` | Versión de WhatsApp Web que el servidor acepta. |
| `COMPAT_WINDOWS_STORE` | Persistencia del store SQLite en Windows. |
| `COMPAT_PAIRING_515` | Mantiene vivo el socket tras `pair-device-sign` hasta el restart 515. |
| `COMPAT_HISTORY_MESSAGES` | Expone los `WebMessageInfo` del blob de History Sync, que pywhats descarga y descifra pero solo cuenta. |
| `COMPAT_APPSTATE_SEEDS` | Diagnóstico, apagado. Se midió: 61 mutaciones, cero claves de mensaje. |

## Reglas que no se tocan

- No desactivar verificación MAC.
- No copiar sesiones ni ratchets entre direcciones.
- No relajar la validación de identidad.
- No modificar `site-packages`.
