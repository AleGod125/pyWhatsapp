# Plan J3: la prueba final de ONE-QR

Fecha: 5 de septiembre de 2026.

**Checkpoint (§3):** backend `c3fc37e`, frontend `28e3ece`. Cero cambios sin
guardar en ninguno de los dos; los 6 archivos nuevos del backend son la
documentación y las herramientas de los Planes I y J.

**No se ha limpiado, desvinculado ni re-vinculado nada.** Lo que sigue explica
por qué me he parado antes, y qué haría falta cambiar del experimento.

---

## Lo que se ha resuelto sin tocar nada

Dos de las tres hipótesis se pueden cerrar leyendo código y registros. No hacía
falta un emparejamiento nuevo para eso.

### H1 — «perdemos chunks»: **DESCARTADA**

Dieciséis emparejamientos, cinco días, todos idénticos:

```
2026-09-01 18:12  INITIAL_BOOTSTRAP chunk=0 progress=0 convos=39 msgs=100
2026-09-02 16:25  INITIAL_BOOTSTRAP chunk=0 progress=0 convos=40 msgs=113
2026-09-03 22:48  INITIAL_BOOTSTRAP chunk=0 progress=0 convos=39 msgs=104
2026-09-05 10:38  INITIAL_BOOTSTRAP chunk=0 progress=0 convos=41 msgs=103
     … los 16, sin excepción
```

**Nunca un `chunk=1`. Nunca `progress>0`.** Y las tres vías por las que se
podría perder algo están limpias:

| comprobación | resultado |
|---|---|
| notificaciones descartadas por `non-self` | **0** |
| fallos de descarga o parseo | **0** |
| notificaciones parseadas | 982, todas contabilizadas |

El descarte por `non-self` era el sospechoso serio: `receiver.py` compara
`sender.user` con `self._own_jid.user`, y con la dualidad PN/LID eso podía
tirar la notificación entera en silencio. **No ocurre ni una vez.**

WhatsApp manda un trozo, y sólo uno. No lo estamos perdiendo.

### El `full_sync` tampoco era: **descartado de paso**

`requires_full_sync=True` con 3650 días y 100 GB de cuota se activó el 2 de
septiembre a las 17:21. Antes y después:

| | conversaciones | mensajes |
|---|---:|---:|
| 1–2 sep, **sin** `full_sync` | 39–40 | 100–114 |
| 2–5 sep, **con** `full_sync` | 40–41 | 98–113 |

La diferencia es ruido: 39→41 sigue el crecimiento natural de la cuenta. **La
bandera no cambió el bootstrap.**

### H2 — «el navegador anuncia capacidades que nosotros no»: **DÉBIL**

Nuestra principal ya se anuncia como navegador. `pywhats.pairing._device_props`
lo hace a propósito, y nuestro parche sólo añade la configuración de historial:

```
os              = "Mac OS"
platform_type   = CHROME
version         = 10.15.7
requires_full_sync   = True      ← nuestro
history_sync_config  = 3650 días, 100 GB   ← nuestro
```

No es un caso de «pedimos poco». Pedimos lo máximo y nos hacemos pasar por
Chrome, y aun así llega un trozo con ~100 mensajes.

---

## El problema del experimento, y por qué me paro

**La comparación que sostiene todo el Plan J no es la misma medida en los dos
lados.**

```
ANCLAS DE LA PRINCIPAL, SOLO DEL BOOTSTRAP
  8 chats · ventana 10:39:48 → 10:39:48   (un instante)

ANCLAS DE WEB, DE SU ALMACEN
  37 chats · ventana 10:41:23 → 10:42:26  (acumulado de DIAS)
```

El almacén del navegador llevaba días llenándose: su propio bootstrap, **más
todo el tráfico en vivo de esos días**, más sus propias sincronizaciones. El
bootstrap de la principal es un instante.

Se ha comparado *lo que la principal recibió en un segundo* contra *lo que el
navegador acumuló en días*. Y la principal también acumula: 9 chats más sólo
por tráfico en vivo, 16 más por excavación.

**«8 contra 37» no demuestra que el bootstrap del navegador sea mejor.** Puede
que sólo demuestre que su sesión era más vieja. No lo sabemos, porque nunca se
ha medido el bootstrap del navegador por separado.

Y eso importa mucho, porque el experimento tal y como está diseñado —§114—
mide la principal en una ventana forense de 5 minutos y luego vincula el
navegador. Si al navegador se le mide su almacén recién nacido, se comparará
un bootstrap contra otro bootstrap y la respuesta será limpia. **Pero si se le
mide después de dejarlo asentar, o se compara contra el número histórico de
37, volveremos a comparar edades.**

---

## Lo que propongo cambiar del experimento

Sólo una cosa, y es barata:

**Medir el bootstrap del navegador en su primer minuto, y compararlo contra el
primer minuto de la principal.** Nada de comparar con los 37 acumulados.

Concretamente, en la fase B:

1. vincular el navegador,
2. medir su almacén a T+30 s, T+60 s y T+120 s —**la misma escalera que la
   principal**—,
3. comparar T+120 contra T+120.

Si a los dos minutos el navegador tiene 32 chats con mensaje y la principal 8,
la diferencia es real y H3 se vuelve fuerte. Si a los dos minutos los dos
tienen ~8, **la premisa entera se cae** y ONE-QR deja de estar bloqueado por lo
que creíamos.

---

## Estado de las hipótesis

| | veredicto | evidencia |
|---|---|---|
| **H1** perdemos chunks | **DESCARTADA** | 16 emparejamientos, 1 trozo siempre, 0 descartes |
| **H2** capacidades | **DÉBIL** | ya nos anunciamos CHROME + full_sync |
| **H3** trato del servidor | **INCONCLUSA** | no se puede separar de la edad de la sesión |

**`ONE_QR`: INCONCLUSO.** No por falta de datos sobre la principal, sino porque
la referencia contra la que se compara no es válida.

---

## Por qué no seguí con la limpieza

Estaba autorizada, y el orden de §114 la pone en el paso 5. Me detuve en el 4
por lo que dice §101: si aparece un hallazgo que cambia el planteamiento,
pararse.

El hallazgo es que **el experimento, tal y como está diseñado, puede volver a
darnos una comparación entre edades en vez de entre bootstraps**. Gastar el
reset —que es la última bala, según §88 del Plan I— en una medición que puede
salir sesgada me parece peor que perder media hora en ajustar el diseño.

El cambio es pequeño y ya está descrito arriba. Con él, el clean-run contesta
la pregunta de verdad.

---

## Qué queda listo para cuando se ejecute

Ya existe, probado y sin conectar a producción:

| pieza | qué hace |
|---|---|
| `app/discovery/primary_inventory.py` | lee los 26 campos que pywhats descarta |
| `tools/trace_seed_sources.py` | de dónde sale cada ancla, con hashes sin PII |
| `tools/diagnose_own_live.py` | copias propias y bordes con agujero |
| `tools/reset_test_account.py` | limpieza con `--dry-run`, ya usada dos veces |
| `tools/capture_baseline.py` | foto antes y después |

Falta —y no lo he escrito porque depende del diseño final— el registrador de
trozos con manifiesto (§5–§8) y `tools/analyze_bootstrap_chunks.py`. Con H1
descartada, su valor ha bajado bastante: sabemos que llega un trozo.

---

## Lo que NO se hizo

- **No se limpió, ni se desvinculó, ni se emparejó.**
- **No se instrumentó** el registrador de trozos ni el manifiesto.
- **No se clasificaron** las 9 conversaciones que sólo ve el navegador (§27).
  Sigue pendiente desde el Plan J2.5 y sigue siendo barato.
- **No se auditaron** los DeviceProps de Baileys y whatsmeow (§81). Con H2
  débil, bajó de prioridad.
- **No se tocó** el retry `<keys>`, como pide §54.
