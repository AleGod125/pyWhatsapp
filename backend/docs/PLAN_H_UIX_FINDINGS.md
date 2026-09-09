# Plan H.1: la auditoría visual, y qué se arregló

Cada entrada tiene la misma forma: **antes**, **problema**, **causa**, **cambio**,
**resultado**. Lo que no se arregló está en la última sección, dicho como lo
que es.

Fecha: 5 de septiembre de 2026.

---

## La auditoría

| zona | estado | nota |
|---|---|---|
| carril lateral | `INCONSISTENT` | dos botones con el mismo icono |
| lista de conversaciones | `NEEDS_POLISH` | nombre sin alias, «sin nombre» prematuro |
| filtros y buscador | `NEEDS_POLISH` | no buscaba por alias; etiquetas fijas |
| cabecera del chat | `BROKEN` | dos botones `disabled` |
| burbujas de mensaje | `INCONSISTENT` | tamaño fijo: no seguía la escala |
| enlaces | `NOT_IMPLEMENTED` | texto crudo |
| audio | `NEEDS_POLISH` | `<audio controls>` del navegador |
| documentos | `NEEDS_POLISH` | emoji y nombre suelto |
| estado de recuperación | `BROKEN` | cuatro bloques a la vez, medio panel |
| panel de configuración | `GOOD` | recién hecho |
| temas | `GOOD` | cinco + personalizado |
| i18n | `INCONSISTENT` | seis catálogos, mezclados en pantalla |
| estados vacíos | `NEEDS_POLISH` | texto fijo |
| accesibilidad | `NEEDS_POLISH` | faltaban `aria-label` en iconos |
| responsive | `NEEDS_POLISH` | sólo revisado el panel nuevo |
| dashboard configurable | `NOT_IMPLEMENTED` | — |

---

## 1. El estado de recuperación se comía el panel

**Antes.** Cuatro bloques a la vez contando lo mismo: «We're getting your
conversations ready…», «Recuperación parcial», «Copia incompleta» y
«Sincronizando…». Entre todos, casi la mitad del panel lateral.

**Problema.** En una lista con pocas conversaciones, el estado tapaba justo lo
que el usuario venía a ver.

**Causa.** Cada panel se añadió en una fase distinta, cada uno resolvía su
propio caso, y nadie los miró juntos.

**Cambio.** Un solo componente, `recovery-status`, con tres tamaños: tarjeta
compacta (una barra y tres cifras), detalle (cada categoría por su nombre) y
colapsado (una línea). Lo que el usuario elija se guarda en sus preferencias.

**Resultado.** De ~40% del panel a unas pocas líneas. Y si le estorba, lo
esconde y no vuelve a abrirse solo — llegan varios eventos por minuto, así que
esa parte importaba.

---

## 2. Dos botones con el mismo icono

**Antes.** El engranaje se usaba para «configuración» y para «recuperación
avanzada», uno encima del otro en el mismo carril.

**Problema.** Dos botones idénticos, y ninguna forma de saber cuál era cuál sin
pasar el ratón por encima.

**Cambio.** El engranaje es **sólo** configuración. La recuperación avanzada
usa una llave inglesa. Se añadieron ocho iconos más para no volver a reutilizar
uno por falta de otro.

**Resultado.** Un botón, un significado.

---

## 3. La escala del texto no llegaba a los mensajes

**Antes.** Cambiar el tamaño movía la lista de conversaciones y dejaba las
burbujas igual.

**Causa, medida.** **74 `font-size` en píxeles fijos** contra **4** con token.
La lista heredaba del `body`; las burbujas traían su propio tamaño.

**Cambio.** 62 tamaños pasados a token. El tope de ancho de la burbuja crece
con la escala —`min(64%, calc(620px * var(--font-scale)))`—, porque un límite
fijo al 150% deja líneas de cinco palabras. Los espaciados de la lista de
mensajes pasan a tokens de densidad.

**Quedan 12 fijos**, y es a propósito: son glifos decorativos de 21–46 px
(ilustraciones de estado vacío, emoji grandes). Escalarlos con el texto los
deformaría.

**Resultado.** 90/100/110/125/150% se nota en toda la aplicación, y compacta /
cómoda / amplia también en el cuerpo de mensajes.

---

## 4. Textos mezclados

**Antes.** «We're getting your conversations ready…» junto a «Recuperación
parcial» en la misma pantalla.

**Causa.** Lo nuevo pasaba por i18n y el idioma se resolvía del navegador
(inglés); lo viejo tenía el castellano escrito a mano. Las dos cosas eran
ciertas a la vez.

**Cambio.** Migradas las superficies que producían la mezcla: carril, cabecera
del chat, estado de recuperación, tarjetas de media. Añadidas 10 claves nuevas
a los seis catálogos.

**Sigue habiendo texto fijo** en pantallas de autenticación, emparejamiento,
paneles de diagnóstico y algunas partes de la lista. Está en los pendientes.

---

## 5. Botones que no hacían nada

**Antes.** En la cabecera del chat, una lupa y tres puntos, ambos `disabled`.

**Problema.** Un botón que no funciona es peor que no tenerlo: se pulsa, no
pasa nada, y a partir de ahí se desconfía del resto.

**Cambio.** Fuera los dos. En su lugar, un menú real: editar nombre, usar el
original (sólo si hay alias) y ver información.

---

## 6. Conversaciones sin nombre

**Antes.** «Contacto sin nombre», dicho como algo definitivo.

**Causa, medida.** `chats.name` estaba a `NULL` en las **51** conversaciones de
la sesión real: el nombre vive en `contacts.display_name` y llega después.

**Cambio.** Una sola función decide el nombre: alias → nombre resuelto → texto
de espera. Mientras la metadata llega se dice «Cargando contacto…» en gris y
cursiva, y se distingue si es grupo. Un nombre que es el propio JID no cuenta
como nombre.

**Resultado.** El usuario ya no da por perdido algo que aparece en veinte
segundos.

---

## 7. Alias de conversaciones

**Antes.** El backend estaba listo desde la fase anterior; la interfaz no.

**Cambio.** Menú en la cabecera → editar → guardar. El nombre cambia al
instante en la cabecera y en la lista. La búsqueda mira el alias **y** el
nombre original. «Usar el nombre original» lo quita.

**Lo que no se toca.** Ni el JID, ni `contacts.display_name`, ni los metadatos.
El alias vive en las preferencias del usuario y se aplica al mostrar.

---

## 8. Enlaces, audio y documentos

**Antes.** Enlaces como texto azul crudo. Audio con los controles nativos del
navegador —que cambian de forma en cada uno y no siguen el tema—. Documentos
como un emoji y el nombre.

**Cambio.**

- **Enlaces:** tarjeta con inicial de marca, dominio y URL recortada. Reconoce
  YouTube, TikTok, Facebook, Instagram, X y GitHub — sólo para el color.
  `target="_blank"` con `rel="noopener noreferrer"`. **Cero peticiones**: no
  hay descarga de metadatos, porque eso significaría mandar a servidores de
  terceros lo que el usuario recibió por WhatsApp.
- **Audio:** reproductor propio con play/pausa, barra, tiempos, y 1×/1,5×/2×.
  **Sólo uno suena a la vez** — la variable es de módulo, no de componente,
  porque hay un reproductor por mensaje.
- **Documentos:** icono con la extensión encima, nombre, tipo y tamaño en las
  unidades del idioma en uso. Colores por familia.

Ninguno toca el descargador: reciben una URL que la capa de media ya resolvió.
`404` es «no disponible» y `410` es «caducado», y se dicen distinto porque una
puede volver y la otra no.

---

## 9. Contadores

Se conserva la lógica honesta de la fase anterior y mejora la presentación: las
cifras que valen cero no se pintan, y el resumen se compone sólo con lo que
tiene algo que decir.

---

## Lo que NO se hizo

Dicho como es, sin adornos:

- **Dashboard configurable** (widgets, mostrar/ocultar, arrastrar) — no
  empezado. La estructura de preferencias ya lo soporta (`dashboard.order`,
  `dashboard.hidden`, que hoy usa el panel de recuperación).
- **Auditoría anti-hardcode automática** — no hay script ni test que la
  detecte. La auditoría de esta fase fue manual.
- **i18n completo** — faltan autenticación, emparejamiento, paneles de
  diagnóstico y partes de la lista.
- **Estados vacíos** — las claves están en los seis catálogos; los componentes
  no.
- **Filtros nuevos** (archivados, pendientes, recuperados) y búsqueda dentro de
  una conversación.
- **Responsive** — sólo revisado el panel de configuración.
- **Fijar / archivar / proteger** — no hay banderas reales en el backend, y
  añadir botones que no guardan nada sería repetir el error del punto 5.
