'use strict';
/**
 * El índice: qué conversaciones existen y cuál es el último mensaje de cada una.
 *
 * EL CAMBIO DE PAPEL
 * ------------------
 * Hasta ahora este worker contestaba a una pregunta estrecha: «de estas
 * conversaciones que Python ya conoce y no tienen ancla, ¿cuáles ves?». Eso
 * dejaba fuera todo lo que Python nunca llegó a descubrir, y medía sólo lo que
 * ya estaba materializado en memoria.
 *
 * Ahora la pregunta es la de verdad: **qué conversaciones existen**, y de cada
 * una lo último real que hay. Python reconcilia después contra su base.
 *
 * EL FALLO QUE SE ARREGLA AQUÍ
 * ----------------------------
 * Se midió en la sesión real: `getChats()` devolvió **cero**. Había un camino
 * de respaldo por el Store — que sí veía las 50 conversaciones — pero ese
 * camino **devolvía la lista y se acababa ahí**: ni miraba el último mensaje,
 * ni el Store, ni pedía uno. Resultado: `chats=50 seeds=0 cobertura=0%`,
 * mientras el sondeo antiguo, que lee el Store por JID, encontraba 14.
 *
 * O sea: no era que WhatsApp Web no tuviera mensajes. Era que por ese camino
 * no se llegaba a preguntar. Ahora hay UNA sola lista de conversaciones —la
 * unión de las dos vías— y los tres intentos se hacen siempre, venga la
 * conversación de donde venga.
 *
 * TRES INTENTOS, EN ESTE ORDEN, Y SE PARA EN EL PRIMERO QUE SIRVA
 * ---------------------------------------------------------------
 *   1. `chat.lastMessage`        lo que la propia lista ya tiene delante
 *   2. mensajes en memoria       lo que el Store haya materializado
 *   3. `fetchMessages({limit:1})` materializar UNO, y sólo uno
 *
 * El tercero pide a la red, así que va el último y con un tope de uno. No es
 * un extractor: sólo hace falta **una** referencia real para que el motor de
 * siempre pueda pedir el historial completo. Cargar más aquí sería duplicar
 * un trabajo que la otra sesión hace mejor.
 *
 * LO QUE NO HACE
 * --------------
 * Ni `fetchMessages({limit: Infinity})`, ni cargadores de historial, ni
 * desplazamiento agresivo del panel. Este worker indexa; excavar es de la
 * sesión principal.
 */

const candidatos = require('./candidates');

/** Cuántos chats se piden mensajes a la red como mucho en una pasada. */
const TOPE_FETCH = 60;

/** Cuántos mensajes ya materializados se miran por chat. */
const TOPE_MENSAJES = 25;

/** Un JID que no es una conversación. */
function esConversacion(jid) {
  const clase = candidatos.clasificarJid(jid);
  return clase === 'individual' || clase === 'group';
}

/**
 * El nombre, con la misma preferencia que usa Python.
 *
 * Nunca un identificador crudo: eso lo decide el backend, que sabe de alias.
 */
function nombreDe(chat) {
  const candidatosDeNombre = [
    chat?.name,
    chat?.formattedTitle,
    chat?.contact?.name,
    chat?.contact?.pushname,
    chat?.groupMetadata?.subject,
  ];
  for (const valor of candidatosDeNombre) {
    if (typeof valor === 'string' && valor.trim()) return valor.trim();
  }
  return null;
}

/**
 * Un JID reconocible en un registro, pero que no identifica a nadie.
 *
 * Un JID completo es un número de teléfono. Para depurar basta con poder
 * distinguir dos conversaciones entre sí.
 */
function redactado(jid) {
  if (typeof jid !== 'string' || !jid.includes('@')) return '?';
  const [usuario, servidor] = jid.split('@');
  return `${usuario.slice(0, 4)}***@${servidor}`;
}

/** El último mensaje que la lista ya tiene, sin pedir nada. */
function delUltimoMensaje(chat, chatJid) {
  const ultimo = chat?.lastMessage;
  if (!ultimo) return { rechazado: 'sin_last_message' };
  return candidatos.extractSeedCandidate(ultimo, {
    chatJid,
    source: 'web_last_message',
    chat,
  });
}

/**
 * Las conversaciones que hay, vengan de donde vengan.
 *
 * `getChats()` da objetos completos —con `lastMessage` y `fetchMessages`— pero
 * se midió devolviendo CERO en la sesión real: mapea todos los modelos y basta
 * que uno falle para quedarse sin lista. El Store da los identificadores
 * aunque aquello falle. Se usan LAS DOS y se unen por JID: la de antes elegía
 * una y descartaba la otra, y por ahí se perdían las 50.
 */
async function conversacionesDe(cliente, adaptador) {
  const porJid = new Map();
  const origenes = [];

  let deGetChats = [];
  try {
    deGetChats = (await cliente.getChats()) || [];
  } catch (error) {
    deGetChats = [];
  }
  if (deGetChats.length) origenes.push('getChats');

  for (const chat of deGetChats) {
    const jid = candidatos.normalizarJid(chat?.id);
    if (!jid || !esConversacion(jid)) continue;
    porJid.set(jid, {
      jid,
      chat,
      is_group: Boolean(chat?.isGroup) || jid.endsWith('@g.us'),
      name: nombreDe(chat),
      last_activity: Number(chat?.timestamp || chat?.t || 0) || null,
      unread: Number(chat?.unreadCount || 0) || 0,
      msgs_in_memory: null,
      via_descubrimiento: 'get_chats',
    });
  }

  let delStore = [];
  try {
    delStore = (await adaptador.chatsDelStore(cliente.pupPage)) || [];
  } catch (error) {
    delStore = [];
  }
  if (delStore.length) origenes.push('store');

  for (const fila of delStore) {
    const jid = candidatos.normalizarJid(fila?.id);
    if (!jid || !esConversacion(jid)) continue;
    const existente = porJid.get(jid);
    if (existente) {
      // El objeto de `getChats()` manda; del Store sólo se rellenan huecos.
      if (existente.last_activity === null) existente.last_activity = fila.last_activity ?? null;
      existente.msgs_in_memory = fila.msgs_in_memory ?? existente.msgs_in_memory;
      existente.via_descubrimiento = 'ambas';
      continue;
    }
    porJid.set(jid, {
      jid,
      // Sin objeto: para pedir a la red habrá que construirlo por JID.
      chat: null,
      is_group: Boolean(fila.is_group) || jid.endsWith('@g.us'),
      name: null,
      last_activity: fila.last_activity ?? null,
      unread: 0,
      msgs_in_memory: fila.msgs_in_memory ?? 0,
      via_descubrimiento: 'store',
    });
  }

  return {
    conversaciones: [...porJid.values()],
    origen: origenes.join('+') || 'ninguna',
  };
}

/**
 * El objeto con el que se puede pedir a la red.
 *
 * Si `getChats()` no lo dio, se construye por identificador. `getChatById`
 * falla —o no— conversación a conversación, en vez de dejar sin lista a las
 * cincuenta porque una diera error.
 */
async function chatParaPedir(cliente, entrada) {
  if (entrada.chat && typeof entrada.chat.fetchMessages === 'function') return entrada.chat;
  if (typeof cliente.getChatById !== 'function') return null;
  const chat = await cliente.getChatById(entrada.jid);
  return chat && typeof chat.fetchMessages === 'function' ? chat : null;
}

/** El orden en que se gasta la cuota de red. */
function prioridadDe(entrada, prioritarios, conocidos) {
  // 1. Las que esperan referencia: son las que bloquean el producto.
  if (prioritarios.has(entrada.jid)) return 0;
  // 2. Las que sólo ve Web: sin esto no entran nunca.
  if (conocidos.size && !conocidos.has(entrada.jid)) return 1;
  return 2;
}

function comoConjunto(lista) {
  const conjunto = new Set();
  for (const valor of lista || []) {
    const jid = candidatos.normalizarJid(valor);
    if (jid) conjunto.add(jid);
  }
  return conjunto;
}

/**
 * Inventario completo.
 *
 * @param {object} cliente  el cliente de whatsapp-web.js, ya listo
 * @param {object} adaptador  `store_adapter`, para lo que la API no expone
 * @param {object} opciones
 * @param {boolean} opciones.permitirFetch  si se puede pedir a la red
 * @param {string[]} opciones.prioritarios  las que esperan referencia
 * @param {string[]} opciones.omitir  las que ya tienen cursor válido
 * @param {string[]} opciones.conocidos  las que Python ya conoce
 * @param {boolean} opciones.debug  detalle por conversación (redactado)
 */
async function inventarioCompleto(cliente, adaptador, opciones = {}) {
  const permitirFetch = opciones.permitirFetch !== false;
  const prioritarios = comoConjunto(opciones.prioritarios);
  const omitir = comoConjunto(opciones.omitir);
  const conocidos = comoConjunto(opciones.conocidos);
  const topeMensajes = Number(opciones.topeMensajes) > 0
    ? Math.min(Number(opciones.topeMensajes), 100)
    : TOPE_MENSAJES;
  const comenzo = Date.now();

  const { conversaciones, origen } = await conversacionesDe(cliente, adaptador);

  const metricas = {
    // -- Lo que hay --------------------------------------------------------
    total: 0,
    individual: 0,
    group: 0,
    web_inventory_total: 0,
    // -- De dónde salió cada referencia ------------------------------------
    seed_from_last_message: 0,
    seed_from_store: 0,
    seed_from_fetch1: 0,
    // -- La red ------------------------------------------------------------
    fetch1_attempted: 0,
    fetch1_success: 0,
    fetch1_empty: 0,
    fetch1_error: 0,
    fetch1_no_chat: 0,
    fetch1_skipped: 0,
    // -- Encontrar un mensaje NO es tener una referencia --------------------
    //
    // Se cuentan aparte a propósito: si `fetch1` trae mensajes y aun así no
    // sale ninguna referencia, el problema es del filtro, no de WhatsApp. Con
    // una sola cifra eso es indistinguible.
    messages_found: 0,
    valid_seeds: 0,
    seed_invalid: 0,
    seed_total: 0,
    // -- Nombres de antes, para no romper a quien ya los leía ---------------
    con_last_message: 0,
    sin_last_message: 0,
    con_memoria: 0,
    con_fetch: 0,
    con_candidato: 0,
    sin_candidato: 0,
    fetch_intentados: 0,
    fetch_fallidos: 0,
  };
  const rechazos = {};
  const anotarRechazo = (motivo) => {
    if (!motivo) return;
    rechazos[motivo] = (rechazos[motivo] || 0) + 1;
  };

  const detalle = [];
  const anotarDetalle = (jid, source, result, reason) => {
    if (!opciones.debug) return;
    detalle.push({ chat: redactado(jid), source, result, reason: reason || null });
  };

  const filas = [];
  const pendientesDeFetch = [];

  for (const entrada of conversaciones) {
    const chatJid = entrada.jid;
    metricas.total += 1;
    metricas[entrada.is_group ? 'group' : 'individual'] += 1;

    const fila = {
      chat_jid: chatJid,
      is_group: entrada.is_group,
      name: entrada.name,
      last_activity: entrada.last_activity,
      unread: entrada.unread,
      msgs_in_memory: entrada.msgs_in_memory,
      discovered_by: entrada.via_descubrimiento,
      candidate: null,
      via: null,
      // Por qué no hay referencia. Python lo cuenta; no es un error del chat.
      no_seed_reason: null,
    };

    // 1) Lo que la lista ya tiene delante. Sólo si vino con objeto: del Store
    //    llegan identificadores, no modelos.
    const porUltimo = entrada.chat
      ? delUltimoMensaje(entrada.chat, chatJid)
      : { rechazado: 'sin_last_message' };
    if (porUltimo.candidato) {
      metricas.seed_from_last_message += 1;
      metricas.con_last_message += 1;
      metricas.messages_found += 1;
      fila.candidate = porUltimo.candidato;
      fila.via = 'last_message';
      filas.push(fila);
      anotarDetalle(chatJid, 'last', 'valid');
      continue;
    }
    if (porUltimo.rechazado && porUltimo.rechazado !== 'sin_last_message') {
      // Había mensaje: lo que falló fue el filtro, y eso se cuenta aparte.
      metricas.messages_found += 1;
      metricas.seed_invalid += 1;
      anotarRechazo(`last_message:${porUltimo.rechazado}`);
      anotarDetalle(chatJid, 'last', 'invalid', porUltimo.rechazado);
    }
    metricas.sin_last_message += 1;

    // 2) Lo que el Store haya materializado por su cuenta. Es exactamente lo
    //    que hace el sondeo antiguo, que sí encontraba referencias.
    let porMemoria = null;
    let huboMensajesEnMemoria = false;
    try {
      const memoria = await adaptador.mensajesEnMemoria(cliente.pupPage, chatJid, topeMensajes);
      const mensajes = memoria?.mensajes || [];
      huboMensajesEnMemoria = mensajes.length > 0;
      for (const mensaje of mensajes) {
        const intento = candidatos.extractSeedCandidate(mensaje, {
          chatJid,
          source: 'web_store',
        });
        if (intento.candidato) {
          porMemoria = intento.candidato;
          break;
        }
        metricas.seed_invalid += 1;
        anotarRechazo(`memoria:${intento.rechazado}`);
      }
    } catch (error) {
      anotarRechazo('memoria:error');
      anotarDetalle(chatJid, 'store', 'error');
    }
    if (huboMensajesEnMemoria) metricas.messages_found += 1;
    if (porMemoria) {
      metricas.seed_from_store += 1;
      metricas.con_memoria += 1;
      fila.candidate = porMemoria;
      fila.via = 'store_memory';
      filas.push(fila);
      anotarDetalle(chatJid, 'store', 'valid');
      continue;
    }
    if (huboMensajesEnMemoria) anotarDetalle(chatJid, 'store', 'invalid');

    // 3) Y si no, se materializa UNO. Esto sí pide a la red, así que va al
    //    final y en una segunda vuelta con tope.
    filas.push(fila);
    if (omitir.has(chatJid)) {
      // Ya tiene con qué excavar: gastar una petición aquí no aporta nada.
      metricas.fetch1_skipped += 1;
      fila.no_seed_reason = 'WEB_SKIPPED_ALREADY_RESOLVED';
      anotarDetalle(chatJid, 'fetch1', 'skipped', 'ya_resuelto');
      continue;
    }
    pendientesDeFetch.push({ entrada, fila });
  }

  if (permitirFetch && pendientesDeFetch.length) {
    // Primero las que esperan referencia, luego las que sólo ve Web, y dentro
    // de cada grupo las de actividad más reciente. La cuota de red es
    // limitada: se gasta donde desbloquea algo.
    pendientesDeFetch.sort((a, b) => {
      const pa = prioridadDe(a.entrada, prioritarios, conocidos);
      const pb = prioridadDe(b.entrada, prioritarios, conocidos);
      if (pa !== pb) return pa - pb;
      return (b.fila.last_activity || 0) - (a.fila.last_activity || 0);
    });

    for (const { entrada, fila } of pendientesDeFetch.slice(0, TOPE_FETCH)) {
      metricas.fetch1_attempted += 1;
      metricas.fetch_intentados += 1;
      let chat = null;
      try {
        chat = await chatParaPedir(cliente, entrada);
      } catch (error) {
        chat = null;
      }
      if (!chat) {
        metricas.fetch1_no_chat += 1;
        fila.no_seed_reason = 'WEB_FETCH1_FAILED';
        anotarRechazo('fetch1:sin_objeto_chat');
        anotarDetalle(entrada.jid, 'fetch1', 'error', 'sin_objeto_chat');
        continue;
      }
      try {
        // UNO. Nunca `Infinity`: no somos el extractor profundo.
        const mensajes = (await chat.fetchMessages({ limit: 1 })) || [];
        if (!mensajes.length) {
          metricas.fetch1_empty += 1;
          // No se inventa nada: sigue esperando referencia, y se dice por qué.
          fila.no_seed_reason = 'WEB_NO_MATERIALIZED_MESSAGE';
          anotarDetalle(entrada.jid, 'fetch1', 'empty');
          continue;
        }
        metricas.fetch1_success += 1;
        metricas.messages_found += 1;
        // Del más reciente al más antiguo, hasta que UNO sirva. Después STOP.
        const ordenados = [...mensajes].sort(
          (a, b) => Number(b?.t ?? b?.timestamp ?? 0) - Number(a?.t ?? a?.timestamp ?? 0),
        );
        let conseguido = null;
        for (const mensaje of ordenados) {
          const intento = candidatos.extractSeedCandidate(mensaje, {
            chatJid: fila.chat_jid,
            source: 'web_fetch1',
          });
          if (intento.candidato) {
            conseguido = intento.candidato;
            break;
          }
          metricas.seed_invalid += 1;
          anotarRechazo(`fetch1:${intento.rechazado}`);
        }
        if (conseguido) {
          metricas.seed_from_fetch1 += 1;
          metricas.con_fetch += 1;
          fila.candidate = conseguido;
          fila.via = 'fetch_limit_1';
          anotarDetalle(entrada.jid, 'fetch1', 'valid');
        } else {
          // Vino mensaje y aun así no hay referencia: es el filtro, no Web.
          fila.no_seed_reason = 'WEB_MESSAGE_NOT_USABLE';
          anotarDetalle(entrada.jid, 'fetch1', 'invalid');
        }
      } catch (error) {
        metricas.fetch1_error += 1;
        metricas.fetch_fallidos += 1;
        // No es un error permanente del chat: se vuelve a intentar en una
        // ronda futura, mientras dure la ventana de hidratación.
        fila.no_seed_reason = 'WEB_FETCH1_FAILED';
        anotarRechazo('fetch1:error');
        anotarDetalle(entrada.jid, 'fetch1', 'error');
      }
    }
  }

  for (const fila of filas) {
    if (fila.candidate) {
      metricas.con_candidato += 1;
    } else {
      metricas.sin_candidato += 1;
      if (!fila.no_seed_reason) fila.no_seed_reason = 'WEB_NO_MATERIALIZED_MESSAGE';
    }
  }
  metricas.web_inventory_total = metricas.total;
  metricas.valid_seeds = metricas.con_candidato;
  metricas.seed_total = metricas.con_candidato;

  return {
    event: 'web_inventory',
    source: origen,
    elapsed_ms: Date.now() - comenzo,
    metrics: metricas,
    rejections: rechazos,
    per_chat: opciones.debug ? detalle : undefined,
    chats: filas,
  };
}

module.exports = {
  inventarioCompleto,
  nombreDe,
  esConversacion,
  redactado,
  conversacionesDe,
  TOPE_FETCH,
  TOPE_MENSAJES,
};
