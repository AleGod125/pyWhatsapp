'use strict';
/**
 * Baileys -> los MISMOS eventos que emitia pywhats.
 *
 * LA DECISION QUE HACE QUIRURGICA ESTA MIGRACION
 * ----------------------------------------------
 * Por aqui NO viaja un mensaje "normalizado a la manera de Baileys". Viaja el
 * `WebMessageInfo` en crudo, serializado a protobuf y en base64.
 *
 * Es lo que permite que la ingesta de Python no cambie ni una linea: nuestro
 * `parse_web_message_info` sigue siendo el unico sitio donde se decide que es
 * un mensaje, de que tipo, con que adjunto y de quien. Si el normalizador
 * mejora, mejora para el historial y para lo que llega en vivo a la vez, y
 * para las dos librerias.
 *
 * Lo contrario --normalizar aqui-- habria creado un segundo criterio en
 * JavaScript que tarde o temprano discrepa del de Python, y esas discrepancias
 * no dan error: dan mensajes que faltan.
 *
 * SIN IMPORTAR BAILEYS
 * --------------------
 * Este modulo no importa la libreria a proposito: asi las pruebas
 * (`node --test`) comprueban la traduccion sin socket, sin red y sin sesion.
 * Lo que necesita de Baileys --serializar un WebMessageInfo-- entra como
 * funcion.
 */

/** Los tipos de sincronizacion, con el nombre que ya entiende Python. */
const TIPOS_DE_SYNC = {
  0: 'INITIAL_BOOTSTRAP',
  1: 'INITIAL_STATUS_V3',
  2: 'FULL',
  3: 'RECENT',
  4: 'PUSH_NAME',
  5: 'NON_BLOCKING_DATA',
  6: 'ON_DEMAND',
};

/**
 * `endOfHistoryTransferType`, tal cual lo manda el telefono.
 *
 * NO se traduce a un booleano aqui. El 0 significa "completo PERO queda mas en
 * el principal" y el 1 "completo y no queda nada": colapsarlos a `end_of_history`
 * fue lo que hizo que se dieran por terminadas conversaciones que aun tenian
 * historial. Se manda el numero y decide Python.
 */
const FIN_COMPLETO_Y_NO_QUEDA_MAS = 1;

/** El `sync_type` legible, venga como numero o como cadena. */
function nombreDeSync(valor) {
  if (typeof valor === 'string' && valor) return valor;
  if (typeof valor === 'number') return TIPOS_DE_SYNC[valor] || `DESCONOCIDO_${valor}`;
  return 'DESCONOCIDO';
}

/**
 * Un JID de Baileys como cadena canonica.
 *
 * No se convierte un `@lid` en telefono ni al reves: son espacios de
 * identificadores distintos y mezclarlos corrompe los datos. Es la misma regla
 * que aplica `jid_to_string` en Python.
 */
function jidTexto(jid) {
  if (!jid) return null;
  const texto = String(jid);
  if (!texto) return null;
  // Baileys usa `numero:dispositivo@servidor` cuando el mensaje viene de un
  // dispositivo concreto. La parte de dispositivo no identifica la
  // conversacion, asi que se quita -- igual que hace pywhats con `device=0`.
  const arroba = texto.indexOf('@');
  if (arroba === -1) return texto;
  const usuario = texto.slice(0, arroba).split(':')[0];
  const servidor = texto.slice(arroba + 1);
  return usuario ? `${usuario}@${servidor}` : null;
}

/** El par PN<->LID que viaja en la clave del mensaje, si viene. (C-23) */
function cosecharLid(clave) {
  if (!clave) return null;
  const pares = [
    [clave.remoteJid, clave.remoteJidAlt],
    [clave.participant, clave.participantAlt],
  ];
  for (const [uno, otro] of pares) {
    const a = jidTexto(uno);
    const b = jidTexto(otro);
    if (!a || !b) continue;
    const lid = a.endsWith('@lid') ? a : b.endsWith('@lid') ? b : null;
    const pn = a.endsWith('@s.whatsapp.net') ? a : b.endsWith('@s.whatsapp.net') ? b : null;
    if (lid && pn) return { lid, pn };
  }
  return null;
}

/**
 * Un `WebMessageInfo` de Baileys -> la carga del evento `message`.
 *
 * @param serializar funcion que devuelve los bytes protobuf del WebMessageInfo
 */
function traducirMensaje(info, serializar) {
  const clave = info && info.key ? info.key : {};
  const chat = jidTexto(clave.remoteJid);
  if (!chat) return null;

  // El remitente real: en un grupo es el participante; en un individual, el
  // propio chat. Si es nuestro, `fromMe` lo dira.
  const remitente = jidTexto(clave.participant) || chat;

  let crudo = null;
  try {
    const bytes = serializar(info);
    if (bytes && bytes.length) crudo = Buffer.from(bytes).toString('base64');
  } catch (fallo) {
    // Sin los bytes el mensaje sigue siendo util, solo que Python no podra
    // clasificar por protobuf. Se dice y se sigue.
    crudo = null;
  }

  return {
    id: clave.id || null,
    chat,
    sender: remitente,
    from_me: Boolean(clave.fromMe),
    timestamp: Number(info.messageTimestamp || 0) || 0,
    push_name: info.pushName || null,
    // EL protobuf entero. Es lo que hace que la ingesta de Python no cambie.
    raw_proto: crudo,
    lid_par: cosecharLid(clave),
  };
}

/**
 * Un lote de `messaging-history.set` -> la carga del evento `history_sync`.
 *
 * La forma es la MISMA que produce `app/compat/history_compat.py` para
 * pywhats: conversaciones con sus mensajes en crudo. Asi
 * `ingest_history_sync` recibe lo de siempre.
 */
function traducirHistorial(lote, serializar) {
  const chats = Array.isArray(lote.chats) ? lote.chats : [];
  const mensajes = Array.isArray(lote.messages) ? lote.messages : [];

  // Los mensajes vienen en una lista plana; se reparten por conversacion.
  const porChat = new Map();
  // Y de paso se cosecha el par PN<->LID de cada clave.
  //
  // Esto se estaba perdiendo ENTERO: `cosecharLid` solo corria sobre los
  // mensajes en vivo, y el historial --que son casi todos-- pasaba de largo.
  // Medido: 5452 mensajes ingeridos y cero pares guardados. Sin el par no hay
  // nombre, porque el nombre esta guardado contra el telefono y el chat viene
  // identificado por `@lid`.
  const pares = new Map();
  for (const info of mensajes) {
    const clave = info && info.key;
    const par = cosecharLid(clave);
    if (par && par.lid && par.pn) pares.set(par.lid, par.pn);
    const chat = jidTexto(clave ? clave.remoteJid : null);
    if (!chat) continue;
    if (!porChat.has(chat)) porChat.set(chat, []);
    let crudo = null;
    try {
      const bytes = serializar(info);
      if (bytes && bytes.length) crudo = Buffer.from(bytes).toString('base64');
    } catch (fallo) {
      crudo = null;
    }
    if (crudo) porChat.get(chat).push([crudo, porChat.get(chat).length]);
  }

  const conversaciones = [];
  const vistos = new Set();
  for (const chat of chats) {
    const jid = jidTexto(chat.id);
    if (!jid) continue;
    vistos.add(jid);
    conversaciones.push(conversacion(jid, chat, porChat.get(jid) || []));
  }
  // Una conversacion puede traer mensajes sin venir en `chats`. No se pierde:
  // se manda igual, sin metadatos.
  for (const [jid, msgs] of porChat) {
    if (!vistos.has(jid)) conversaciones.push(conversacion(jid, {}, msgs));
  }

  return {
    sync_type: nombreDeSync(lote.syncType),
    progress: Number(lote.progress || 0) || 0,
    chunk_order: Number(lote.chunkOrder || 0) || 0,
    is_latest: Boolean(lote.isLatest),
    conversations: conversaciones,
    pushnames: pushnames(lote.contacts),
    lid_pares: [...pares].map(([lid, pn]) => ({ lid, pn })),
  };
}

function conversacion(jid, chat, mensajes) {
  const fin = marcadorDeFin(chat);
  return {
    jid,
    name: chat.name || chat.subject || null,
    last_message_timestamp: Number(chat.conversationTimestamp || 0) || null,
    unread_count: Number(chat.unreadCount || 0) || null,
    messages: mensajes,
    end_of_history_type: fin.tipo,
    end_of_history: fin.terminado,
  };
}

/**
 * El marcador de fin de una conversacion. (C-10, compuerta G2)
 *
 * Es lo unico que distingue "no queda nada" de "queda mas en el telefono", y
 * de el cuelga todo el estado `exhausted`. Se leen los dos nombres porque
 * Baileys los ha movido entre versiones; si no viene ninguno se devuelve
 * `null`, que significa "no lo se" -- nunca "terminado".
 */
function marcadorDeFin(chat) {
  const crudo =
    chat.endOfHistoryTransferType !== undefined && chat.endOfHistoryTransferType !== null
      ? chat.endOfHistoryTransferType
      : chat.endOfHistoryTransfer;
  if (crudo === undefined || crudo === null) return { tipo: null, terminado: false };
  const tipo = Number(crudo);
  if (!Number.isFinite(tipo)) return { tipo: null, terminado: false };
  return { tipo, terminado: tipo === FIN_COMPLETO_Y_NO_QUEDA_MAS };
}

/** Los nombres publicos que vengan en el lote, como pares [jid, nombre]. */
function pushnames(contactos) {
  if (!Array.isArray(contactos)) return [];
  const pares = [];
  for (const c of contactos) {
    const jid = jidTexto(c && c.id);
    const nombre = c && (c.notify || c.name || c.verifiedName);
    if (jid && nombre) pares.push([jid, String(nombre)]);
  }
  return pares;
}

/**
 * `messages.update` trae edicion Y borrado por el mismo sitio. (C-eventos)
 *
 * En pywhats llegaban ya separados. Aqui hay que mirar la carga: un borrado
 * deja el mensaje con `REVOKED` en su `messageStubType` o con el
 * `protocolMessage` de revocacion.
 */
function clasificarActualizacion(update) {
  const cambio = (update && update.update) || {};
  const stub = cambio.messageStubType;
  const revocado =
    stub === 1 ||
    String(stub || '') === 'REVOKE' ||
    Boolean(cambio.key && cambio.key.fromMe === undefined && cambio.messageStubType === 1);
  if (revocado) return 'message_revoke';
  if (cambio.message || cambio.editedMessage) return 'message_edit';
  return null;
}

/**
 * Un `Contact` de Baileys -> el evento `contact`.
 *
 * OJO CON `id`: PUEDE VENIR EN CUALQUIERA DE LOS DOS ESPACIOS.
 * ----------------------------------------------------------
 * El propio tipo lo dice: "ID either in lid or jid format". Asi que `id` no
 * es "el numero": es el identificador principal, y el otro lado viaja en
 * `lid` o en `jid` segun cual sea. Leerlo siempre como numero dejaba el par
 * al reves --o vacio-- y sin par no hay nombre: 56 de 57 conversaciones
 * individuales llegan por `@lid` mientras el nombre esta guardado contra el
 * telefono.
 *
 * Se devuelve `null` si no se puede sacar un telefono: `contacts` exige el
 * JID como clave, y una fila con solo el LID no se podria emparejar despues.
 */
function traducirContacto(contacto) {
  if (!contacto) return null;
  const principal = jidTexto(contacto.id);
  const esLid = Boolean(principal && principal.endsWith('@lid'));

  const jid = esLid ? jidTexto(contacto.jid) : principal;
  const lid = esLid ? principal : jidTexto(contacto.lid);
  if (!jid) return null;

  return {
    jid,
    // El nombre de la AGENDA y el nombre PUBLICO son cosas distintas y se
    // guardan por separado: el primero lo puso el usuario, el segundo lo pone
    // el contacto. `upsert_contact` no degrada lo que ya hubiera.
    full_name: contacto.name || null,
    first_name: contacto.name || null,
    push_name: contacto.notify || contacto.verifiedName || null,
    lid: lid || null,
  };
}

/**
 * La AGENDA que viaja dentro de `messaging-history.set`.
 *
 * Es la unica fuente real de nombres, y estuvo tirandose entera: de ese lote
 * solo se leian pares `[jid, nombre]` --se perdia el LID-- y ademas
 * `contacts.set`, que es el evento donde se esperaban, NO EXISTE en Baileys
 * 6.7.24. El resultado medido: 205 chats y 4 contactos, los 4 de grupos.
 */
function contactosDelLote(contactos) {
  if (!Array.isArray(contactos)) return [];
  const salida = [];
  for (const c of contactos) {
    const carga = traducirContacto(c);
    if (carga && (carga.full_name || carga.push_name || carga.lid)) salida.push(carga);
  }
  return salida;
}

module.exports = {
  TIPOS_DE_SYNC,
  clasificarActualizacion,
  contactosDelLote,
  conversacion,
  cosecharLid,
  jidTexto,
  marcadorDeFin,
  nombreDeSync,
  pushnames,
  traducirContacto,
  traducirHistorial,
  traducirMensaje,
};
