# Plan I: ¿un solo QR? La investigación, y la respuesta

Fecha: 5 de septiembre de 2026.
Commit base: backend `c3fc37e`, frontend `28e3ece`. Ambos repos limpios.

---

## Resumen de un párrafo

La sesión principal **ya recibe** el inventario de conversaciones que se creía
que sólo veía WhatsApp Web: viene en el `INITIAL_BOOTSTRAP` y `pywhats` lo tira
porque su modelo describe 5 de los 31 campos que trae el cable. Eso se ha
implementado y medido: **41 conversaciones, todas con marca de actividad, 34
con el par PN↔LID**. Pero el mismo escaneo confirma que **no hay ni una sola
ancla** para las conversaciones sin mensajes — y sin ancla no se puede pedir
historial. Por eso **ONE-QR no es viable todavía**: quitar el segundo
dispositivo costaría pasar de 45 conversaciones con referencia a 8.

---

## ADR-1: leer los campos que pywhats descarta

**Contexto.** Se asumía que la sesión principal «descubría menos». Medido: en
el bootstrap real llegan 41 conversaciones y WhatsApp Web veía 50.

**Alternativas.**

- **A** — Añadir descubrimiento a la sesión principal.
- **B** — Un único motor Web con historia profunda propia.
- **C** — Una identidad consumida por dos capas.
- **D** — Dejarlo como está.

**Decisión: A**, y con una precisión que sólo aparece al mirar el cable.

`pywhats` modela cinco campos de una `Conversation`:

```
1 id    2 messages    3 name    5 last_msg_timestamp    6 unread_count
```

El cable trae treinta y uno. Escaneando los bytes del blob archivado, en las 41
conversaciones aparecen además:

| campo | qué es | en cuántas |
|---:|---|---:|
| 12 | marca de actividad | **41 de 41** |
| 13 | asunto del grupo | 7 |
| 21 | opaco (11 bytes) | 32 |
| 22 | marca | 32 |
| 23 | opaco (32 bytes) | 33 |
| 28 | marca | 25 |
| 38 / 43 | push name | 7 |
| 39 | **JID de teléfono (PN)** | 34 |
| 44 | categoría | 33 |
| 49 | **LID del contacto** | 34 |

Los campos 39 y 49 juntos son **el par PN↔LID**, que costó fases enteras
resolver por `usync` y por diagnósticos aparte. Estaba en el primer blob.

**Consecuencias.** Se implementó `app/discovery/primary_inventory.py`, que lee
los campos por número — sin tocar `site-packages` y sin reescribir el `.proto`
de nadie. Medido contra el blob real:

```json
{
  "primary_chats": 41, "groups": 6, "individuals": 35,
  "with_name": 14, "with_last_activity": 41,
  "with_pn_lid_pair": 34, "with_seed": 0
}
```

---

## ADR-2: no quitar todavía el segundo dispositivo

**Contexto.** El objetivo era un solo QR.

**La evidencia que lo impide.** Dos números.

**Cobertura: 41 contra 50 = 82%.** El objetivo era ≥95%.

**Anclas: cero.** Y ésta es la que decide. Para una conversación sin mensajes,
el cable **no trae ningún identificador de mensaje**: los campos opacos son de
11 y 32 bytes, y un WAMID son 20 o 32 caracteres hexadecimales. Ya se había
comprobado por otra vía; este escaneo lo confirma desde los bytes.

Sin ancla no hay `ON_DEMAND`. Y las anclas son justo lo que aportaba Web:

| | con referencia utilizable |
|---|---:|
| WhatsApp Web (medido) | **45 de 50** |
| Bootstrap principal | **8** (sólo las que traen mensajes) |

**Decisión.** No se quita. Quitarlo cambiaría «un QR menos» por perder la
historia profunda de 37 conversaciones, y eso contradice tres reglas
explícitas: no sacrificar cobertura, no sacrificar historia, no aceptar una
regresión por estética de onboarding.

**Consecuencias.** El inventario principal queda implementado y probado, y
sirve **hoy** para dos cosas reales aunque no se quite el segundo QR: dar
nombres y actividad desde el primer segundo, y resolver el par PN↔LID sin
`usync`. La arquitectura Plan G sigue en pie.

---

## ADR-3: no copiar estado Signal entre motores

Se descartó sin experimentar. Compartir una identidad entre `pywhats` y
`whatsapp-web.js` exige copiar el Signal Store o convertir formatos, y eso son
dos ratchets sobre la misma identidad: la receta exacta de los `unknown
one-time pre-key id` que ya costó una fase. **PN y LID del mismo aparato son
direcciones criptográficas distintas.** Identidad canónica y dirección
criptográfica no son lo mismo, y resolver la primera no autoriza a mezclar la
segunda.

---

## Lo que se sabe del bug de LIVE

Medido antes de esta fase, sobre la sesión real:

```
reintentos=22  recuperados=0  sin_resolver=22
```

El acuse de reintento sale, el servidor lo acepta con `ack->ok class=receipt`,
y **no vuelve nada**. La dirección Signal era la correcta —`sesion_por_lid=True`—,
así que no era un fallo de resolución sino un ratchet desincronizado.

Se corrigió el contador del acuse (antes decía siempre «1») y no bastó. El
siguiente sospechoso está identificado y **no** se ha implementado: `pywhats`
no manda el bloque `<keys>` que sí manda Baileys — identidad, prekey firmada,
OPK y `device-identity`. Nuestras prekeys sí están en el servidor
(`server holds 6 OPKs`), así que el emisor podría pedirlas.

---

## Por qué no se hizo la limpieza total

Estaba autorizada, y no se ejecutó **a propósito**.

La limpieza servía para probar la arquitectura ONE-QR desde cero. La
investigación previa —que es lo que se pedía hacer primero— dice que esa
arquitectura regresaría de 45 conversaciones con referencia a 8. Borrar la
sesión que produce esa evidencia, para probar algo que la evidencia dice que va
a ir peor, no es una prueba: es perder los datos.

Además, la sesión actual es la única que reproduce el bug de LIVE, y sigue
siendo el mejor banco de pruebas que hay para el bloque `<keys>`.

**Nada se ha borrado.** La base, las sesiones, el LocalAuth y Drive están
intactos.

---

## Estados

| | estado | por qué |
|---|---|---|
| `ONE_QR` | **NO_RESUELTO** | 82% de cobertura y cero anclas |
| `OWN_LIVE` | **PARCIAL** | causa localizada; el contador no bastó |
| `DEEP_HISTORY` | **OK** | intacto |
| `DISCOVERY` | **OK** | y ahora también desde la sesión principal |

---

## Rollback

No hace falta: nada destructivo se ha ejecutado y todo lo nuevo es aditivo.
Si aun así se quisiera volver atrás:

```
git -C C:\Users\aleja\GitHub\pyWhatsapp     reset --hard c3fc37e
git -C C:\Users\aleja\GitHub\WhatsappBackup reset --hard 28e3ece
```
