# Ciclo de vida de la sesión

## Estados

`STARTING` → `NO_SESSION` / `PAIRING_REQUIRED` → `PAIRING` → `QR_READY` →
`CONNECTING` → `CONNECTED`

Y los de salida: `RECONNECTING`, `DISCONNECTED`, `SESSION_INVALID`, `ERROR`.

`/api/v1/session` añade `pairing_phase`, que es lo que el frontend necesita
para no mentir:

| Fase | Significa |
|---|---|
| `verifying_session` | Hay vinculación guardada y el servidor la está rechazando. Aún no se descarta. |
| `pairing_required` | Hace falta un código y todavía no hay. |
| `qr_ready` | Hay QR vigente. |
| `connecting` | Conectando o reconectando. |
| `connected` | Listo. |

Con `state` solo no se distingue "verificando" de "hace falta QR", y la
pantalla acaba diciendo "Preparando tu código QR" durante los tres rechazos.

## Identidad y Signal Store son indivisibles

`device.json` lleva la identidad (claves Noise, identity keypair,
`registration_id`). `device.json.signal.db` lleva el estado Signal construido
**bajo esa identidad**: sesiones, prekeys, sender keys, claves de app-state.

Dejar uno sin el otro produce una mezcla que no es la sesión de nadie. Se
midió: al archivar tras un 401, el store quedaba bloqueado y se saltaba. El
pairing siguiente creaba un `device.json` nuevo (`registration_id` 572666329)
sobre un store del anterior (1403204623), con 14 sesiones y 8 sender keys
heredadas. Síntoma: `unknown one-time pre-key id 66` en cada mensaje entrante.

Por eso `_descartar_sesion_revocada` **verifica que los dos archivos se fueron**
y, si queda alguno, entra en `ERROR` en vez de volver a vincular.

## Descarte de una sesión revocada

Un 401 suelto **no** destruye nada: puede ser un corte de red. Los dos
primeros solo se cuentan. Al **tercero seguido de la misma huella** el servidor
está diciendo tres veces que esa vinculación no existe:

```
archive_session()  → mueve session/* a diagnostics/session-<fecha>-revoked-401
verificar restos   → si queda alguno: ERROR, NO se vincula
                   → si no: PAIRING, se genera QR
```

Archivar no es borrar. Y no puede entrar en bucle: al desaparecer
`device.json` cambia la huella y la cuenta se reinicia.

La vía **explícita** para lo mismo es `py service.py --fresh`.

## Huella de sesión

`sha256(user:server:device_id:registration_id)[:16]`

`registration_id` entra a propósito: `device_id` es un **número de ranura** que
el servidor reutiliza al desvincular todos los dispositivos. Sin él, dos
identidades distintas compartirían huella y la segunda daría por confirmado el
historial inicial de la primera.

Va atada a la huella todo lo que describe una vinculación concreta:
`initial_history_confirmed`, la capacidad ON_DEMAND y la revalidación de chats
agotados. Cuando la huella cambia, esos estados se recalculan — **sin tocar
mensajes**.

Se calcula en dos sitios (`app/core/identity.py` desde disco, y
`BackfillService` desde el dispositivo vivo) y **tienen que dar lo mismo**. Hay
una prueba que llama al método real, no que repite la fórmula.

## Un solo dueño

`session/runtime.lock` con PID y heartbeat. Un cerrojo cuyo proceso ya no
existe se recupera solo; nunca se mata un proceso ajeno.
