# Plan J3.1: la prueba simétrica

Fecha de preparación: 5 de septiembre de 2026.
Base: backend `c3fc37e`, frontend `28e3ece`.

**Estado: instrumentación lista y probada. Nada limpiado, nada desvinculado,
nada emparejado.** Este documento es el protocolo y, cuando se ejecute, el
sitio donde van los resultados.

---

## 1. La pregunta, y por qué las tres fases anteriores no la contestaron

El Plan J se sostenía sobre esta comparación:

```
ANCLAS DE LA PRINCIPAL, SOLO DEL BOOTSTRAP
   8 chats · ventana 10:39:48 → 10:39:48    (un instante)

ANCLAS DE WEB, DE SU ALMACEN
  37 chats · ventana 10:41:23 → 10:42:26    (acumulado de DIAS)
```

Ocho contra treinta y siete. Pero las ocho son lo que la principal recibió en
un segundo, y las treinta y siete son lo que el navegador acumuló en varios
días: su bootstrap **más** todo el tráfico en vivo de esos días. La principal
también acumula —9 chats más por tráfico en vivo, 16 por excavación— y nadie
ha medido nunca el bootstrap del navegador por separado.

**«8 contra 37» puede no demostrar nada sobre el arranque. Puede demostrar sólo
que una sesión era más vieja que la otra.**

La corrección no es medir mejor. Es **medir a la misma edad**.

---

## 2. Estado de las hipótesis al empezar

| | veredicto | por qué |
|---|---|---|
| **H1** perdemos trozos del bootstrap | **DESCARTADA** | 16 emparejamientos en 5 días, todos `chunk=0 progress=0`; 0 descartes por `non-self`, 0 fallos de parseo, 982 notificaciones contabilizadas |
| **`full_sync`** | **descartado** | activo desde el 2 sep 17:21; antes 39–40 convos / 100–114 msgs, después 40–41 / 98–113 |
| **H2** capacidades del cliente | **DÉBIL** | la principal ya se anuncia `os="Mac OS"`, `platform_type=CHROME`, v10.15.7, más `requires_full_sync` y 3650 días |
| **H3** el servidor trata distinto a un navegador | **INCONCLUSA** | no se puede separar de la edad de la sesión |

H1 y el `full_sync` no vuelven a investigarse en esta fase. **H3 es lo que se
mide.**

---

## 3. El principio: la misma vara

Los dos lados, con el mismo código, la misma clasificación de conversaciones y
el mismo criterio de ancla válida, en **T+0, T+30, T+60 y T+120** desde el T0
de cada uno. T+300 sólo si a los 120 segundos seguía moviéndose.

Todo eso vive en un módulo único, `app/discovery/symmetric_snapshot.py`, para
que no pueda volver a pasar que cada lado se mida con un criterio distinto.

### Qué se cuenta (§13)

`session_age_seconds` · `raw_chat_count` · `user_chat_count` ·
`individual_count` · `group_count` · `special_entity_count` ·
`chats_with_name` · `chats_with_activity` · `chats_with_real_message` ·
`chats_with_valid_seed` · `cached_message_count` · `unique_wamid_count`

### Qué es una conversación de usuario (§14)

Cuentan `individual`, `individual_archived`, `group`, `group_archived` y
`business`. **No** cuentan boletines, listas de difusión, estados, bots,
entidades de sistema ni el nodo padre de una comunidad — ése contiene grupos,
no es un chat. Se cuentan aparte para poder rehacer la resta sin volver a
medir.

### Qué es un ancla (§16)

Conversación real, identificador **real** de WhatsApp y marca real. Un
identificador fabricado por nosotros recibe confirmación del servidor y después
silencio; es el fallo más caro de diagnosticar del proyecto, y por eso se
rechaza en la medición y no más adelante.

### De dónde salió, y qué cuenta como arranque (§17, §18)

| origen | ¿cuenta como arranque? |
|---|---|
| `initial_bootstrap` | **sí** |
| `offline` | sí |
| `on_demand` | **no** — necesita un ancla previa: contarlo es contarla dos veces |
| `live` | **no** — mide la actividad de la cuenta, no lo que trajo vincular |
| `web_store` | **no** — lo consiguió el SEGUNDO dispositivo |
| `web_local_fetch` | **no** — sondeo, se mide aparte (§57) |

La línea de `web_store` no es evidente y se comprobó midiendo: **sin ella, la
sesión principal aparecía con 40 anclas de arranque cuando de verdad tiene 8.**
Atribuirle a un dispositivo lo que consiguió el otro es exactamente el error
que esta fase existe para no repetir.

---

## 4. Dos correcciones que sólo aparecieron al medir en seco

Antes de tocar nada, la herramienta se pasó sobre la sesión actual. Dos cosas
salieron mal, y las dos habrían llegado al informe como hallazgos falsos.

**Los nombres no viven donde parecía.** `chats.name` está a `NULL` en las 51
conversaciones: el nombre de una conversación individual vive en `contacts`.
Contar sólo la primera daba **0 nombres en la principal contra 30 y pico en el
navegador** — una asimetría enorme que no existe. Corregido: 33.

**Las anclas del navegador contaban como arranque de la principal.** Daba 40
donde son 8.

Después de las dos correcciones, la herramienta reproduce exactamente las
cifras ya conocidas de la sesión real:

```
por origen del ancla:
  initial_bootstrap  8   ← coincide con lo medido en el Plan J
  web_store         32
  web_local_fetch   23
  on_demand         16
  live               9
CON ANCLA (solo arranque)   8
```

---

## 5. Qué se ha construido

| pieza | qué hace |
|---|---|
| `app/discovery/symmetric_snapshot.py` | la vara única: métricas, clasificación, validez de ancla, comparación |
| `app/experimental/j31_recorder.py` | cronometra los dos lados desde su propio T0 y guarda las fotos |
| `web_companion/store_adapter.js` → `instantaneaJ31` | lee el almacén del navegador **sin pedirle nada** |
| `web_companion/worker.js` → `j31_store_snapshot` | el comando que lo expone |
| `app/api/web_companion_routes.py` → `POST /web-companion/j31/snapshot` | la foto del navegador a mano |
| `tools/j31_capture.py` | una foto suelta de la principal |
| `tools/j31_compare.py` | la tabla, la cobertura, el caso, y `comparison.json` |
| `tools/j31_own_live.py` | los mensajes propios, antes y después del navegador |
| `tools/j31_signal_fingerprint.py` | huellas Signal sin material de clave |
| `tools/check_clean_state.py` | ¿de verdad quedó limpio? |

### Tres banderas, todas apagadas por defecto

```
PLAN_J31_ENABLED=false          cronometra y guarda
PLAN_J31_PRIMARY_ONLY=false     fase A: el navegador no arranca por ninguna puerta
PLAN_J31_FREEZE_HISTORY=false   corta ON_DEMAND durante la ventana
```

`PRIMARY_ONLY` cierra las tres puertas por las que entra el worker: el
reintento automático, la API y `start()` directo. No basta con una: `start()`
se llama también desde herramientas.

`FREEZE_HISTORY` corta en `_request_once`, que es el único sitio por donde
pasan todas las peticiones ON_DEMAND —canario, excavación y borde reciente—,
más `request_diagnostico`. Y lo dice en el registro: es una pausa deliberada,
no un límite del servidor.

### Un hueco que se encontró en la limpieza

`tools/reset_test_account.py` **no borraba** `session/web_bootstrap` —la sesión
del otro dispositivo experimental— ni los ficheros laterales de SQLite
(`-wal`, `-shm`). Un `-wal` superviviente devuelve escrituras no volcadas de la
vinculación anterior en cuanto se abre un archivo nuevo con el mismo nombre.
Los seis están añadidos. Sin eso, `check_clean_state` habría dado la limpieza
por incompleta — y con razón.

---

## 6. El protocolo

### Fase A — la principal, sola

1. `PLAN_J31_ENABLED=1`, `PLAN_J31_PRIMARY_ONLY=1`, `PLAN_J31_FREEZE_HISTORY=1`
2. desvincular a mano desde el teléfono
3. `py tools/reset_test_account.py` (en seco) y luego `--execute --confirm RESET_TEST_ACCOUNT`
4. `py tools/check_clean_state.py` → tiene que decir **LIMPIO**
5. `py tools/j31_signal_fingerprint.py --etiqueta antes_de_todo`
6. `py service.py`, escanear **un** código QR
7. la escalera se dispara sola: `primary_t000/030/060/120.json` y
   `PRIMARY_BOOTSTRAP_FINAL.json`
8. **no tocar nada durante los 120 segundos** — ni sincronizar, ni abrir chats,
   ni escribir
9. `py tools/j31_signal_fingerprint.py --etiqueta despues_del_arranque`
10. desde el teléfono, al mismo chat estable: `OWN-LIVE-J31-PREWEB-001`, `-002`,
    `-003`, con ~10 s entre ellos
11. `py tools/j31_own_live.py --tanda PREWEB`

### Fase B — el navegador, recién nacido

12. `PLAN_J31_PRIMARY_ONLY=0`, reiniciar el servicio
13. `py tools/j31_signal_fingerprint.py --etiqueta antes_de_vincular_web`
14. escanear el **segundo** código QR
15. la escalera del navegador se dispara sola desde SU T0
16. **no navegar** durante sus 120 segundos
17. `py tools/j31_signal_fingerprint.py --etiqueta despues_de_vincular_web`
18. sólo entonces, el sondeo normal → `WEB_AFTER_PROBES`
19. `OWN-LIVE-J31-POSTWEB-001..003` al **mismo chat**
20. `py tools/j31_own_live.py --comparar`
21. `py tools/j31_compare.py`

### Reglas durante toda la prueba

No sincronizar a mano, no recuperar, no abrir chats a propósito, no pulsar F5
para comprobar el vivo, y **no arreglar nada a mitad**. Si aparece un fallo, se
anota. Cambiar el experimento en marcha destruye la comparación, que es lo
único que se está comprando con el reset.

---

## 7. Cómo se leerán los resultados

| caso | qué significa |
|---|---|
| **A** | el navegador recién nacido tiene bastantes más anclas Y más chats → la diferencia es real, **H3 fuerte** |
| **B** | a la misma edad se parecen → **la premisa anterior era falsa**: las 30–40 anclas eran acumulación |
| **D** | ve más chats, anclas parecidas → hueco de **descubrimiento** |
| **E** | mismos chats, muchas más anclas → hueco de **historial/caché** |
| **INCONCLUSO** | las edades no cuadran, o la diferencia va al revés |

**Si las dos fotos no son de la misma edad, no se emite veredicto.** Está
comprobado por una prueba automática: una foto de 120 s contra una de tres días
devuelve `INCONCLUSO` y lo dice. Repetir el error que motivó esta fase por no
mirar un campo sería difícil de justificar.

`ONE_QR` será **VIABLE** con ≥95% de conversaciones de usuario y ≥90% de
anclas; **VIABLE_CON_TRABAJO** si la diferencia se explica por materialización,
app-state o caché sobre la misma sesión; y **BLOQUEADO** si no.

`OWN_LIVE` va por separado. Y hay un resultado que vale por sí solo: **si los
mensajes propios ya fallan sin navegador vinculado, el segundo dispositivo no
puede ser causa necesaria.** Eso se sabrá en el paso 11, antes de tocar la
fase B.

---

## 8. Lo que NO se toca en esta fase

Ni el bloque `<keys>` del reintento (§82, §106), ni las capacidades del
emparejamiento, ni Signal, ni la forma del cable de ON_DEMAND, ni el frontend.
Aunque salga VIABLE, el segundo dispositivo **no se quita en el mismo turno**:
primero el informe (§105).

Ninguna migración. Las tres banderas son de entorno y el registrador escribe en
`debug/plan_j31/`.

---

## 9. Verificación de lo entregado

```
1491 pruebas de backend       PASAN   (43 nuevas)
  103 pruebas del worker      PASAN
py service.py --check         correcto, revision a3d71c9b40e2
py tools/check_clean_state.py detecta bien el estado sucio actual
py tools/reset_test_account.py  en seco, cubre las 11 rutas
```

Las 43 pruebas nuevas fijan, entre otras: que las anclas del navegador no
cuentan como arranque de la principal; que un identificador fabricado no es un
ancla; que dos fotos de edades distintas no producen veredicto; que
`PRIMARY_ONLY` cierra también la puerta de `start()`; que una captura ya escrita
no se pisa; que lo congelado no se reescribe; y que de la foto y de la huella
Signal no sale ni un identificador ni un byte de clave.

---

## 10. Resultados

*(pendiente de ejecución)*

| | PRINCIPAL T+120 | NAVEGADOR T+120 |
|---|---|---|
| conversaciones de usuario | | |
| con ancla válida | | |

```
H2        =
H3        =
ONE_QR    =
OWN_LIVE  =
```

> El segundo QR ___ es necesario actualmente porque ___
