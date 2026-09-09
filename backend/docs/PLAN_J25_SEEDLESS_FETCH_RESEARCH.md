# Plan J2.5: ¿qué hace `loadEarlierMsgs`, y puede hacerlo la sesión principal?

Fecha: 5 de septiembre de 2026.
**Nada se ha instrumentado, cambiado, limpiado ni re-vinculado.** La respuesta
salió de los registros que ya teníamos.

---

## El hallazgo, primero

**La llamada que creíamos que era una petición de red sin ancla, no lo es.**

`fetchMessages({limit:1})` → `WAWebChatLoadMessages.loadEarlierMsgs({chat})` es
**híbrida**, y las dos mitades se distinguen perfectamente en los tiempos:

| | llamadas | resultado | tiempo por llamada |
|---|---:|---|---:|
| sirvió de local | **23** | **válido** | **~40 ms** |
| salió a la red | **53** | **vacío** | **~2 s** |

Las 23 que produjeron ancla **cayeron todas dentro del mismo segundo**
—10:41:55— siendo llamadas secuenciales con `await`. Eso es velocidad de
caché, no de una ida y vuelta a los servidores de WhatsApp desde Colombia.

Las 53 que sí tardaron lo que tarda la red volvieron **vacías las 53**.

**La parte que produce anclas es la local. La parte de red no produjo ni una.**

---

## Cómo se midió, y por qué es falsable

Sin instrumentar nada nuevo: el registro por conversación que se añadió en el
Plan G.1 ya distingue la vía.

```
via=store  resultado=valid   224
via=fetch1 resultado=valid    23   ← todas en 10:41:55, ventana de 0 s
via=fetch1 resultado=empty    53   ← repartidas en 154 s
```

Y cuadra con la base al mensaje exacto: hay **23** filas con
`source='web_fetch1'`, creadas entre `10:41:55.188` y `10:41:55.573` — **384
milisegundos para 23 conversaciones**.

**Cómo refutar esto:** si alguien mide una llamada a `loadEarlierMsgs` sobre un
chat con caché vacía que devuelve un mensaje real, esta conclusión cae. La
predicción concreta es que devolverá vacío, como hicieron las 53.

---

## Respuestas directas

**A. ¿Qué hace `loadEarlierMsgs`?** Pide los mensajes anteriores de un chat.
Primero mira lo que ya tiene el cliente; si no hay, sale a la red.

**B. ¿Hace red?** Sí, pero sólo cuando no tiene nada local — y en esta sesión
esa rama devolvió vacío 53 de 53.

**C. ¿Necesita ancla previa?** No la necesita para *pedir*. Pero sin caché no
*devuelve* nada, así que en la práctica no resuelve el problema.

**D–F. ¿Qué manda, qué devuelve, de dónde sale su cursor?** No se ha llegado a
la forma del cable, y **no hace falta**: la rama de red no produce anclas, así
que reproducirla no aportaría ninguna. Documentar su forma sería trabajo sobre
un camino que ya sabemos que no lleva a donde queremos.

**G–I. ¿Puede la principal reproducirlo? ¿Se probó?** No se ha probado, y
**deliberadamente no se ha escrito el probe**. §30 del encargo lo prohíbe sin
conocer la petición exacta, y §28 lo condiciona a que la forma quede
demostrada. Además sería reproducir una operación cuya única aportación
medida es cero.

---

## Clasificaciones

**Decisión #1 — `loadEarlierMsgs`: `HYBRID`**, con el matiz que importa: la vía
que produce anclas es `LOCAL_CACHE_ONLY`.

**Decisión #2 — replicación desde la principal:
`NOT_POSSIBLE_WITH_CURRENT_EVIDENCE`.** No porque el servidor lo rechace, sino
porque **no hay una operación de red que replicar**: la que existe devuelve
vacío.

---

## Entonces, ¿de dónde salen de verdad las anclas de Web?

De su caché local. Y esa caché se llenó con **su propia sincronización de
historial al vincularse** — el mismo mecanismo que usa la principal.

Ahí está la pregunta que queda, y es distinta de la que traíamos:

> Los dos son dispositivos companion de la misma cuenta. ¿Por qué la
> sincronización inicial del navegador trajo mensajes de ~32 conversaciones y
> la nuestra sólo de 8?

Ya sabemos que **no es la configuración del emparejamiento**: `full_sync=True`,
`days=3650` y cuota de 100 GB llevaban activos desde el 2 de septiembre, antes
del emparejamiento actual, y el bootstrap siguió siendo **un solo trozo**,
`chunk=0 progress=0`, 41 conversaciones y 103 mensajes.

Quedan tres hipótesis, y ninguna se puede medir sin un emparejamiento nuevo:

1. **Hubo más trozos y se perdieron.** Sólo se observó `chunk=0`. Si WhatsApp
   mandó más y no se recogieron, ahí están las anclas.
2. **El navegador anuncia una capacidad que nosotros no.** Sería el caso §72:
   habría que distinguir «nuestra principal no lo anunció» de «una principal no
   puede».
3. **El servidor trata distinto a un companion de navegador.** Sería el
   obstáculo real.

---

## Cobertura, recalculada

Pendiente de §40–42: clasificar las 9 conversaciones que Web ve y la principal
no. No lo he hecho — requiere el índice de Web en marcha y me pareció menos
urgente que cerrar la pregunta central. Si varias resultan no ser
conversaciones de usuario (difusiones, newsletters, entidades de sistema), el
82% nominal sube.

---

## Veredicto

**`ONE_QR`: BLOQUEADO por ahora** — pero el obstáculo ha cambiado de sitio, y
eso es progreso.

Ya no es «falta una operación de protocolo que Web tiene y nosotros no». Es
**«la sincronización inicial de la principal trae menos mensajes que la del
navegador, y no sabemos por qué»**.

Eso es una pregunta mucho mejor: es sobre un mecanismo que ya tenemos, no sobre
uno que habría que descubrir.

---

## Lo que haría falta para cerrarla

Sólo se puede medir en un emparejamiento nuevo. Si algún día se hace:

1. **Registrar todos los trozos del bootstrap** con su `progress`, y si llega
   más de uno. Es la hipótesis 1 y la más barata de descartar.
2. **Comparar los DeviceProps** que anuncia el navegador contra los nuestros.
3. **Contar mensajes por conversación** en el primer bootstrap de cada
   dispositivo, de la misma cuenta.

Con la 1 sola ya se sabría mucho, y **no exige tocar nada hasta entonces**.

---

## Lo que NO se hizo, y por qué

- **No se instrumentó el Web Companion.** La respuesta salió de los registros
  existentes; instrumentar habría sido tocar producción para llegar al mismo
  sitio.
- **No se escribió `tools/probe_recent_message_primary.py`.** §30 lo prohíbe
  sin la petición exacta, y la evidencia dice que replicaría una operación que
  devuelve vacío.
- **No se auditaron los enums de Baileys y whatsmeow.** Tenía sentido cuando
  buscábamos una operación sin ancla; ya no es la pregunta.
- **No se clasificaron las 9 conversaciones** que sólo ve Web.
