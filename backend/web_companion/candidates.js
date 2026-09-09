'use strict';
/**
 * Que se puede proponer como referencia, y que no.
 *
 * ESTE MODULO NO DECIDE NADA
 * --------------------------
 * Aqui solo se descarta lo que es EVIDENTEMENTE inservible, para no mandarle a
 * Python miles de cosas que va a rechazar. La validacion de verdad —dueno de
 * la cuenta, alias PN/LID, conversacion existente, forma del identificador— la
 * hace Python con sus reglas, que son las que ya funcionan.
 *
 * Dicho de otra forma: esto propone CANDIDATOS. La ultima palabra no es suya.
 *
 * LA REGLA QUE NO SE ROMPE
 * ------------------------
 * No se fabrica un identificador, ni una marca de tiempo, ni un remitente. Una
 * referencia inventada recibe confirmacion del servidor y despues silencio, y
 * eso es lo mas caro de diagnosticar que tiene este proyecto. Si un mensaje no
 * trae identificador real, no es candidato: se dice y ya esta.
 */

/** Servidores que nunca son una conversacion de la que pedir historial. */
const SERVIDORES_EXCLUIDOS = new Set(['broadcast', 'newsletter', 'status@broadcast']);

/**
 * De donde salio el candidato. Se conserva para poder medir que via sirve.
 *
 * Faltaban las tres del indice, asi que `web_last_message` y `web_fetch1`
 * salian etiquetados como `web_store`: las metricas por via daban todas cero
 * en la misma casilla y no habia forma de saber cual de los tres intentos
 * estaba funcionando. Un origen que no esta en la lista NO invalida el
 * candidato -- solo se etiqueta -- asi que olvidarse de anadirlo aqui no
 * rompe nada, solo ciega la medicion.
 */
const ORIGENES = new Set([
  'web_store',
  'web_loaded',
  'web_load_earlier',
  'web_last_message',
  'web_fetch1',
  'web_discovery',
]);

/**
 * El identificador serializado de un chat o mensaje, tal y como lo da el Store.
 *
 * WhatsApp Web usa objetos `Wid`, no cadenas. Segun por donde se lea, el mismo
 * chat aparece como `id._serialized`, como `id.toString()` o ya como cadena.
 * Normalizarlo aqui evita que el mismo chat se cuente dos veces.
 */
function serializar(id) {
  if (!id) return null;
  if (typeof id === 'string') return id.trim() || null;
  if (typeof id === 'object') {
    if (typeof id._serialized === 'string' && id._serialized) return id._serialized;
    if (typeof id.toString === 'function') {
      const texto = id.toString();
      // `toString` de un objeto plano da "[object Object]": eso no es un JID.
      if (texto && texto !== '[object Object]') return texto;
    }
  }
  return null;
}

/** El JID en la forma con la que se comparan dos conversaciones. */
function normalizarJid(id) {
  const texto = serializar(id);
  if (!texto) return null;
  const limpio = texto.trim().toLowerCase();
  if (!limpio.includes('@')) return null;
  // El sufijo de dispositivo (`:12`) es del aparato, no de la conversacion.
  const [usuario, servidor] = limpio.split('@');
  return `${usuario.split(':')[0]}@${servidor}`;
}

function esConversacion(jid) {
  if (!jid) return false;
  const servidor = jid.split('@')[1] || '';
  if (SERVIDORES_EXCLUIDOS.has(servidor)) return false;
  return !jid.startsWith('status@');
}

/**
 * La marca de tiempo, en SEGUNDOS.
 *
 * El Store las da en segundos, pero no todos los campos: alguno viene en
 * milisegundos. NO se convierte a ojo — dividir por mil es adivinar la unidad,
 * y equivocarse produce un cursor que el servidor confirma y nunca responde.
 * Lo que no encaje en el rango razonable se descarta.
 */
function segundos(valor) {
  const numero = Number(valor);
  if (!Number.isFinite(numero) || numero <= 0) return null;
  // ~2001 hasta ~2096. Una marca en milisegundos cae muy por encima.
  if (numero < 1000000000 || numero > 4000000000) return null;
  return Math.trunc(numero);
}

/**
 * Convierte un modelo de mensaje del Store en un candidato, o devuelve el
 * motivo por el que no sirve.
 */
/**
 * Motivos por los que un mensaje NO puede ser una referencia.
 *
 * Se cuentan uno por uno a proposito: "0 candidatos de 22 conversaciones con
 * mensajes" no dice nada. "22 rechazados por sin_id" dice exactamente donde
 * mirar — y fue justo asi como se encontro el fallo anterior.
 */
const RECHAZOS = {
  SIN_MODELO: 'sin_modelo',
  SIN_ID: 'sin_id',
  ID_NO_REAL: 'id_no_real',
  SIN_TIMESTAMP: 'sin_timestamp',
  TIMESTAMP_UNIDAD_INVALIDA: 'timestamp_unidad_invalida',
  SIN_CHAT: 'sin_chat',
  CHAT_NO_CONVERSACION: 'chat_no_conversacion',
  FROM_ME_INDETERMINADO: 'from_me_indeterminado',
  OTRO: 'otro',
};

/** Forma de un WAMID: hexadecimal. Medido en vivo: 20 y 32 caracteres. */
const FORMA_DE_WAMID = /^[0-9A-Fa-f]{16,32}$/;

/** Identificadores que NO son de WhatsApp sino del cliente o de la base. */
const PREFIJOS_LOCALES = ['opaque-', 'synthetic-', 'local-', 'temp-', 'fake-'];

/**
 * El identificador serializado de un Wid, venga como venga.
 *
 * Medido en la sesion real: el objeto `key` del mensaje NO tiene
 * `_serialized`, pero el `Wid` de `key.remote` SI. Por eso no se puede
 * resolver los dos con la misma regla.
 */
function widSerializado(valor) {
  if (!valor) return null;
  if (typeof valor === 'string') return valor.trim() || null;
  if (typeof valor === 'object') {
    if (typeof valor._serialized === 'string' && valor._serialized) return valor._serialized;
    const texto = attemptToString(valor);
    if (texto && texto.includes('@')) return texto;
  }
  return null;
}

function attemptToString(valor) {
  try {
    const texto = valor.toString();
    return texto && texto !== '[object Object]' ? texto : null;
  } catch {
    return null;
  }
}

/**
 * Un mensaje real -> una referencia utilizable, o el motivo por el que no.
 *
 * ACEPTA DOS FORMAS, Y ESO NO ES CAPRICHO
 * ---------------------------------------
 * El modelo crudo del Store trae `id` como OBJETO (`{id, fromMe, remote}`),
 * y el adaptador lo devuelve ya aplanado (`wa_msg_id`, `from_me`). Antes cada
 * modulo suponia la forma del otro: el adaptador aplanaba y el normalizador
 * esperaba el objeto, asi que `typeof clave === 'object'` era falso y los 22
 * chats con mensajes daban CERO candidatos por `sin_id`.
 *
 * Aceptar las dos formas explicitamente es lo que impide que ese desajuste
 * vuelva sin que nadie lo note.
 *
 * NO decide nada definitivo: propone. Python valida despues con sus reglas
 * (dueno de la cuenta, alias PN/LID, conversacion existente).
 */
function extractSeedCandidate(modelo, { chatJid, source, chat } = {}) {
  if (!modelo) return { rechazado: RECHAZOS.SIN_MODELO };

  // -- Identificador -------------------------------------------------------
  const clave = modelo.id ?? modelo.key ?? null;
  const bruto =
    // Forma aplanada del adaptador.
    (typeof modelo.wa_msg_id === 'string' && modelo.wa_msg_id) ||
    // Modelo crudo del Store: el WAMID vive en `id.id`.
    (clave && typeof clave === 'object' && typeof clave.id === 'string' && clave.id) ||
    // Y si `id` ya viene como cadena, tambien vale.
    (typeof clave === 'string' && clave) ||
    (typeof modelo.msgId === 'string' && modelo.msgId) ||
    null;

  const waMsgId = bruto ? bruto.trim() : null;
  if (!waMsgId) return { rechazado: RECHAZOS.SIN_ID };

  const minusculas = waMsgId.toLowerCase();
  if (PREFIJOS_LOCALES.some((prefijo) => minusculas.startsWith(prefijo))) {
    return { rechazado: RECHAZOS.ID_NO_REAL };
  }
  // `false_<chat>_<id>` es la forma serializada, no el WAMID: lleva pegados
  // el chat y la direccion, y el servidor no la reconoce como ancla.
  if (waMsgId.includes('_') || waMsgId.includes('@')) {
    return { rechazado: RECHAZOS.ID_NO_REAL };
  }
  if (!FORMA_DE_WAMID.test(waMsgId)) return { rechazado: RECHAZOS.ID_NO_REAL };

  // -- Marca de tiempo -----------------------------------------------------
  const crudo = modelo.t ?? modelo.timestamp ?? modelo.__x_t ?? null;
  const numero = Number(crudo);
  if (!Number.isFinite(numero) || numero <= 0) {
    return { rechazado: RECHAZOS.SIN_TIMESTAMP };
  }
  // Medido en vivo: `t` tiene 10 digitos, o sea SEGUNDOS. Una marca en
  // milisegundos se rechaza y NO se divide por mil: adivinar la unidad
  // produce un cursor que el servidor confirma y nunca responde.
  const marca = segundos(numero);
  if (marca === null) return { rechazado: RECHAZOS.TIMESTAMP_UNIDAD_INVALIDA };

  // -- Conversacion --------------------------------------------------------
  //
  // Manda el chat CONTENEDOR: es el que Python pidio y el que ya resolvio a
  // una conversacion suya. `key.remote` se usa solo si no hay contenedor —
  // y en un grupo `remote` ES el grupo, nunca el participante, asi que
  // tampoco ahi se cuela un JID equivocado.
  const delContenedor = widSerializado(chatJid) || widSerializado(chat?.id);
  const delMensaje = widSerializado(
    (clave && typeof clave === 'object' ? clave.remote : null) ?? modelo.remote,
  );
  const jid = normalizarJid(delContenedor || delMensaje);
  if (!jid) return { rechazado: RECHAZOS.SIN_CHAT };
  if (!esConversacion(jid)) return { rechazado: RECHAZOS.CHAT_NO_CONVERSACION };

  // -- Direccion -----------------------------------------------------------
  //
  // Medido: `msg.fromMe` es `undefined`; el dato vive en `key.fromMe`. Si no
  // esta en ninguno de los dos NO se supone `false`: viaja en la peticion
  // ON_DEMAND y equivocarse ahi cuesta una peticion que no responde.
  const direccion =
    typeof modelo.from_me === 'boolean' ? modelo.from_me
      : typeof modelo.fromMe === 'boolean' ? modelo.fromMe
        : clave && typeof clave === 'object' && typeof clave.fromMe === 'boolean' ? clave.fromMe
          : typeof modelo.__x_isSentByMe === 'boolean' ? modelo.__x_isSentByMe
            : null;
  if (direccion === null) return { rechazado: RECHAZOS.FROM_ME_INDETERMINADO };

  return {
    candidato: {
      chat_jid: jid,
      wa_msg_id: waMsgId,
      timestamp: marca,
      from_me: direccion,
      source: ORIGENES.has(source) ? source : 'web_store',
      // El tipo viaja para que Python pueda aplicar SU filtro de mensajes de
      // protocolo. Node no decide eso.
      message_type: typeof modelo.type === 'string' ? modelo.type : null,
    },
  };
}

/** Nombre anterior. Se conserva para no romper a quien ya lo llamaba. */
function desdeMensaje(modelo, opciones) {
  return extractSeedCandidate(modelo, opciones);
}

/**
 * El candidato mas ANTIGUO de una lista.
 *
 * Se excava hacia atras, asi que la referencia util es la mas antigua
 * conocida: lo que queda por recuperar esta antes de ella.
 */
function masAntiguo(candidatos) {
  let mejor = null;
  for (const c of candidatos) {
    if (!c) continue;
    if (mejor === null || c.timestamp < mejor.timestamp) mejor = c;
  }
  return mejor;
}

/**
 * Une las dos vias de descubrimiento en una sola lista sin repetidos.
 *
 * `getChats()` es la lista de siempre; `Store.Chat.getModelsArray()` puede
 * traer conversaciones que la primera no da. Se anota de donde salio cada una
 * para poder medir si la segunda aporta algo de verdad.
 */
function unirInventario(deGetChats, deStore) {
  const porJid = new Map();

  const anadir = (fila, via) => {
    const jid = normalizarJid(fila?.id);
    if (!esConversacion(jid)) return;
    const existente = porJid.get(jid);
    if (existente) {
      existente.sources.push(via);
      // Los metadatos del primero mandan; solo se rellenan los huecos.
      if (existente.name === null && fila.name) existente.name = fila.name;
      return;
    }
    porJid.set(jid, {
      chat_jid: jid,
      name: fila?.name ?? null,
      is_group: jid.endsWith('@g.us'),
      msgs_in_memory: Number.isFinite(fila?.msgs_in_memory) ? fila.msgs_in_memory : 0,
      sources: [via],
    });
  };

  for (const fila of deGetChats || []) anadir(fila, 'get_chats');
  for (const fila of deStore || []) anadir(fila, 'store');

  return [...porJid.values()];
}

/**
 * Que CLASE de conversacion es, por su servidor.
 *
 * Sirve para decir de que son las que Web ve y Python no, sin exponer ni un
 * identificador: importa si son diez grupos o diez difusiones, no cuales.
 */
function clasificarJid(jid) {
  if (!jid || !jid.includes('@')) return 'unknown';
  const servidor = jid.split('@')[1];
  if (servidor === 'g.us') return 'group';
  if (servidor === 'newsletter') return 'newsletter';
  if (servidor === 'broadcast') return jid.startsWith('status@') ? 'status' : 'broadcast';
  if (servidor === 'lid' || servidor === 'c.us' || servidor === 's.whatsapp.net') return 'individual';
  return 'unknown';
}

/** Cuantos hay de cada clase. */
function contarPorClase(jids) {
  const conteo = {};
  for (const jid of jids || []) {
    const clase = clasificarJid(jid);
    conteo[clase] = (conteo[clase] || 0) + 1;
  }
  return conteo;
}

/** Las metricas del inventario, comparadas contra lo que ya sabe Python. */
function compararInventario(union, jidsDePython) {
  const dePython = new Set((jidsDePython || []).map(normalizarJid).filter(Boolean));
  const deWeb = new Set(union.map((c) => c.chat_jid));

  return {
    python_chats: dePython.size,
    web_get_chats: union.filter((c) => c.sources.includes('get_chats')).length,
    web_store_chats: union.filter((c) => c.sources.includes('store')).length,
    union_chats: deWeb.size,
    // Los que Web ve y Python no. Es el numero que dice si esto aporta algo.
    extra_vs_python: [...deWeb].filter((j) => !dePython.has(j)).length,
    // Y al reves: los que Python tiene y Web no llega a ver.
    missing_vs_python: [...dePython].filter((j) => !deWeb.has(j)).length,
    individual: union.filter((c) => !c.is_group).length,
    group: union.filter((c) => c.is_group).length,
  };
}

module.exports = {
  RECHAZOS,
  clasificarJid,
  contarPorClase,
  FORMA_DE_WAMID,
  extractSeedCandidate,
  widSerializado,
  serializar,
  normalizarJid,
  esConversacion,
  segundos,
  desdeMensaje,
  masAntiguo,
  unirInventario,
  compararInventario,
  ORIGENES,
};
