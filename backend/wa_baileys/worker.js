'use strict';
/**
 * La capa de WhatsApp sobre Baileys. SOLO LECTURA.
 *
 * QUE ES
 * ------
 * Un proceso hijo de `service.py` que habla el protocolo WhatsApp y traduce
 * todo a los MISMOS eventos que emitia pywhats. Python no sabe cual de los dos
 * esta corriendo, y no tiene por que saberlo.
 *
 * SOLO LEE
 * --------
 * No hay ni una funcion de envio. Ni `sendMessage`, ni acuses de lectura, ni
 * presencia. El producto es una copia de seguridad de los mensajes propios: lo
 * que no esta implementado no se puede llamar por error, y ademas reduce el
 * motivo mas comun de bloqueo de cuenta.
 *
 * LA CARPETA DE SESION ES UNA UNIDAD
 * ----------------------------------
 * `useMultiFileAuthState` guarda credenciales Y el almacen de Signal en la
 * misma carpeta. Borrar media carpeta deja un dispositivo NUEVO usando
 * ratchets VIEJOS: el sintoma es `unknown one-time pre-key id` y peticiones de
 * historial que reciben ACK y despues nada. Se borra entera o no se borra.
 *
 * STDOUT ES SOLO PROTOCOLO
 * ------------------------
 * Una linea, un JSON, un evento. Lo legible va a stderr. `blindarStdout()`
 * redirige cualquier `console.log` -- propio o de Baileys -- para que una
 * linea suelta no deje al supervisor sin canal.
 */

const fs = require('node:fs');
const path = require('node:path');

const {
  emitir,
  registrar,
  troceador,
  blindarStdout,
  exigirCuenta,
} = require('./protocol');
const traducir = require('./traducir');

blindarStdout();
// SIN CUENTA NO SE ARRANCA.
//
// Cada evento que sale de aqui va firmado con `WA_ACCOUNT_ID` para que el
// supervisor pueda comprobar de quien es en vez de suponerlo. Un worker sin
// firma obligaria a elegir entre descartar su historial o creerselo a ciegas,
// y creerselo a ciegas es como 195 conversaciones acabaron en la cuenta de
// otra persona. Se sale con codigo 1 y se dice por que.
exigirCuenta();

// --- Configuracion, toda por entorno ---------------------------------------

const CARPETA_SESION = process.env.WA_BAILEYS_SESSION_DIR;
const CARPETA_HISTORIAL = process.env.WA_BAILEYS_HISTORY_DIR || '';
// El tamano de tanda que pide el proyecto.
//
// EL TOPE DE 50 NO EXISTE EN EL CODIGO. Se leyo en la documentacion de Baileys
// y se dio por bueno; al mirar `Socket/messages-recv.js:707` resulta que
// `fetchMessageHistory` mete el `count` TAL CUAL en `onDemandMsgCount` y llama
// a `sendPeerDataOperationMessage`. No comprueba nada.
//
// Es decir: la peticion que manda Baileys y la que manda pywhats son la MISMA
// stanza. El "plan A" y el "plan B" resultaron ser el mismo camino, y el
// tamano de tanda de 500 que ya funciona hoy se puede pedir igual.
const TANDA_PEDIDA = Number(process.env.WA_BAILEYS_ONDEMAND_COUNT || '500') || 500;

if (!CARPETA_SESION) {
  registrar('[baileys] falta WA_BAILEYS_SESSION_DIR');
  process.exit(2);
}

let baileys;
try {
  baileys = require('@whiskeysockets/baileys');
} catch (fallo) {
  // Sin la dependencia instalada el worker no puede arrancar, pero tiene que
  // decirlo por el canal: si muere en silencio, Python solo ve un proceso que
  // se fue y reintenta en bucle sin saber por que.
  emitir({ event: 'fatal', code: 'BAILEYS_NO_INSTALADO', message: String(fallo).slice(0, 300) });
  process.exit(3);
}

const {
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  makeWASocket,
  useMultiFileAuthState,
  Browsers,
  proto,
} = baileys;

/** Serializa un WebMessageInfo a protobuf. Es lo que viaja a Python. */
function serializarInfo(info) {
  return proto.WebMessageInfo.encode(proto.WebMessageInfo.fromObject(info)).finish();
}

// --- Estado del worker ------------------------------------------------------

let sock = null;
let cerrando = false;
let reintentos = 0;
// Si el servidor llego a mandarnos un QR alguna vez. Un cierre ANTES del
// primer QR y sin credenciales guardadas significa que el registro fue
// rechazado, que es un fallo muy distinto de perder la conexion.
let vioQR = false;
/** Peticiones de historial en vuelo, por id, para poder correlacionar. (C-13) */
const enVuelo = new Map();

// --- Archivado del lote CRUDO ----------------------------------------------

/**
 * Guarda el lote de historial ANTES de interpretarlo. (C-10)
 *
 * Es lo que salvo 5920 mensajes cuando la base se vacio: los blobs
 * sobrevivieron. Aqui se archiva el lote ya traducido en JSON, que es el
 * equivalente exacto -- lleva los `WebMessageInfo` en crudo, que es lo unico
 * de lo que se puede reconstruir todo.
 */
function archivarLote(carga) {
  if (!CARPETA_HISTORIAL) return null;
  try {
    fs.mkdirSync(CARPETA_HISTORIAL, { recursive: true });
    const sello = new Date().toISOString().replace(/[-:.]/g, '').slice(0, 15);
    const nombre = `${sello}-${carga.sync_type}-chunk${String(carga.chunk_order).padStart(3, '0')}-${Date.now().toString(36)}.json`;
    const ruta = path.join(CARPETA_HISTORIAL, nombre);
    fs.writeFileSync(ruta, JSON.stringify(carga), 'utf8');
    return nombre;
  } catch (fallo) {
    registrar('[baileys] no se pudo archivar el lote:', String(fallo).slice(0, 200));
    return null;
  }
}

// --- Eventos hacia Python ---------------------------------------------------

/** Un evento del contrato. `name` tiene que ser uno de `app/wa/port.py`. */
function evento(name, payload, extra) {
  emitir({ event: 'client', name, payload: payload === undefined ? null : payload, extra: extra || {} });
}

// --- El socket --------------------------------------------------------------

/**
 * Que version de WhatsApp Web anunciar. (C-05)
 *
 * MEDIDO, y no es lo que decia el plan. `fetchLatestBaileysVersion()` devolvio
 * 2.3000.1043857760 mientras nuestro propio resolutor encontraba en vivo
 * 2.3000.1047086005 -- mas nueva. O sea que "la ultima" de Baileys no siempre
 * lo es.
 *
 * Por defecto NO se pasa ninguna: se deja la que Baileys trae compilada.
 *
 * Y se midio que da IGUAL cual se anuncie. Aqui se llego a creer que la
 * compilada era "la unica con la que se consiguio un QR"; al aislar el fallo
 * de vinculacion resulto que la version no pintaba nada -- con la compilada
 * (2.3000.1043857760) y con la de `sw.js` en vivo (2.3000.1047094411) el
 * resultado es el mismo en los dos sentidos. Lo que decidia era el perfil de
 * navegador; ver `navegadorAAnunciar()`.
 *
 * `WA_BAILEYS_VERSION` permite fijar otra sin tocar codigo, para poder medir
 * cuando haga falta.
 */
async function versionAAnunciar() {
  const fijada = (process.env.WA_BAILEYS_VERSION || '').trim();
  if (fijada) {
    const partes = fijada.split('.').map(Number);
    if (partes.length === 3 && partes.every(Number.isFinite)) {
      registrar('[baileys] version fijada a mano:', fijada);
      return partes;
    }
    registrar('[baileys] WA_BAILEYS_VERSION no vale:', fijada, '-- se usa la de la libreria');
  }
  if (process.env.WA_BAILEYS_FETCH_VERSION === 'true') {
    const { version } = await fetchLatestBaileysVersion();
    registrar('[baileys] version consultada:', version.join('.'));
    return version;
  }
  return null;
}

/**
 * Con que perfil presentarse. (C-14)
 *
 * MEDIDO, y sale al reves de lo que decia el plan.
 *
 * La idea era anunciarse como cliente de ESCRITORIO para recibir una ventana
 * de historial mas amplia. Baileys lo hace solo: si `syncFullHistory` esta
 * puesto Y el sistema del navegador esta en su `PLATFORM_MAP`, cambia
 * `webInfo.webSubPlatform` de WEB_BROWSER a DARWIN o WIN32
 * (`Utils/validate-connection.js:31`).
 *
 * Hoy WhatsApp RECHAZA esa pareja. El servidor completa el saludo Noise,
 * recibe nuestro registro y cierra el WebSocket con codigo 1011, que Baileys
 * traduce a `Connection Terminated` 428. Nunca llega a mandar el QR, asi que
 * la pantalla se queda cargando para siempre sin un solo error visible.
 *
 * La matriz, cuatro intentos seguidos contra el servidor de verdad:
 *
 *     Mac OS  / Desktop + syncFullHistory  -> DARWIN(3)      428, sin QR
 *     Windows / Desktop + syncFullHistory  -> WIN32(4)       428, sin QR
 *     Windows / Chrome  + syncFullHistory  -> WIN32(4)       428, sin QR
 *     Mac OS  / Desktop - syncFullHistory  -> WEB_BROWSER(0) QR en 0,4 s
 *     Ubuntu  / Chrome  + syncFullHistory  -> WEB_BROWSER(0) QR en 0,4 s
 *
 * Es el `webSubPlatform` y nada mas: `userAgent.platform` vale WEB(14) en los
 * cinco casos, y la version anunciada tampoco cambia nada (se probo con la
 * compilada y con la que sirve `sw.js` en vivo; las dos fallan con DARWIN y
 * las dos funcionan con WEB_BROWSER).
 *
 * NO SE PIERDE HISTORIAL. Lo que de verdad pide el historial completo es
 * `requireFullSync`, que viaja en `deviceProps` sin mirar el navegador
 * (`validate-connection.js:72`), y `syncFullHistory` sigue en `true`. Solo se
 * renuncia a la pista de subplataforma, que es justo la que veta el servidor.
 *
 * Ubuntu/Chrome ademas es lo MISMO que anunciaba pywhats --`Platform.WEB` con
 * `PlatformType.CHROME`--, y con eso se llegaron a traer 315 dias de
 * historial. Es la configuracion probada, no una apuesta.
 */
function navegadorAAnunciar() {
  const fijado = (process.env.WA_BAILEYS_BROWSER || '').trim();
  if (fijado) {
    const partes = fijado.split(',').map((x) => x.trim());
    if (partes.length === 3) {
      registrar('[baileys] navegador fijado a mano:', partes.join(' / '));
      return partes;
    }
    registrar('[baileys] WA_BAILEYS_BROWSER no vale:', fijado, '-- se usa el de siempre');
  }
  return Browsers.ubuntu('Chrome');
}

async function arrancar() {
  const { state, saveCreds } = await useMultiFileAuthState(CARPETA_SESION);
  const version = await versionAAnunciar();

  sock = makeWASocket({
    ...(version ? { version } : {}),
    auth: state,
    browser: navegadorAAnunciar(),
    // Sigue pidiendo el historial entero: `requireFullSync` va en
    // `deviceProps` y no depende del navegador de arriba.
    syncFullHistory: true,
    // No marcar presencia: esto solo lee, y menos ruido es menos motivo de
    // atencion sobre la cuenta.
    markOnlineOnConnect: false,
    // Los reintentos de descifrado los lleva Baileys, con el contador REAL y
    // el bloque <keys> que pywhats no mandaba. (C-21)
    maxMsgRetryCount: 5,
    retryRequestDelayMs: 2000,
    // Baileys registra por su cuenta; se le da un logger mudo para que no
    // escriba en stdout bajo ningun concepto.
    logger: registroMudo(),
    // No se guarda historial en memoria: la fuente de verdad es PostgreSQL.
    getMessage: async () => undefined,
  });

  sock.ev.on('creds.update', saveCreds);
  registrarManejadores(sock);
  return sock;
}

/** Un logger con la forma que pide Baileys (pino) que no escribe en stdout. */
function registroMudo() {
  const nada = () => {};
  const logger = {
    level: 'silent',
    trace: nada,
    debug: nada,
    info: nada,
    warn: (...a) => registrar('[baileys]', ...a.map(textoCorto)),
    error: (...a) => registrar('[baileys]', ...a.map(textoCorto)),
    fatal: (...a) => registrar('[baileys]', ...a.map(textoCorto)),
  };
  logger.child = () => logger;
  return logger;
}

function textoCorto(valor) {
  try {
    return typeof valor === 'string' ? valor.slice(0, 200) : JSON.stringify(valor).slice(0, 200);
  } catch (fallo) {
    return '[?]';
  }
}

/**
 * La agenda de un lote de historial -> eventos `contact`, `pushname` y el par.
 *
 * Los tres van por separado a proposito: un contacto puede traer solo el
 * nombre publico, o solo la correspondencia PN<->LID, y guardar de menos era
 * perder mientras que guardar de mas no cuesta nada.
 */
function emitirContactos(contactos) {
  const lista = traducir.contactosDelLote(contactos);
  if (!lista.length) return;
  for (const carga of lista) {
    evento('contact', carga);
    if (carga.push_name) evento('pushname', { jid: carga.jid, name: carga.push_name });
    if (carga.lid) emitir({ event: 'lid_par', lid: carga.lid, pn: carga.jid });
  }
  registrar(`[baileys] agenda del lote: ${lista.length} contactos`);
}

/**
 * El par PN<->LID que algunos `Chat` del lote traen consigo.
 *
 * Baileys ha ido moviendo el nombre de este campo entre versiones, asi que se
 * miran los tres que ha usado. Es una fuente mas, barata: si no viene, no
 * pasa nada -- el par tambien se cosecha de las claves de los mensajes.
 */
function emitirParesDeChats(chats) {
  for (const ch of chats || []) {
    const jid = traducir.jidTexto(ch && ch.id);
    if (!jid || !jid.endsWith('@lid')) continue;
    const pn = traducir.jidTexto(ch.pnJid || ch.lidJid || ch.remoteJidAlt);
    if (pn && pn.endsWith('@s.whatsapp.net')) {
      emitir({ event: 'lid_par', lid: jid, pn });
    }
  }
}

function registrarManejadores(sock) {
  // -- Ciclo de vida (C-01, C-03, C-06) -------------------------------------
  sock.ev.on('connection.update', (u) => {
    if (u.qr) {
      vioQR = true;
      evento('qr', { qr: u.qr });
    }

    if (u.connection === 'open') {
      reintentos = 0;
      const yo = sock.user || {};
      evento('paired', { jid: traducir.jidTexto(yo.id), lid: traducir.jidTexto(yo.lid) });
      evento('connected', {
        jid: traducir.jidTexto(yo.id),
        lid: traducir.jidTexto(yo.lid),
        name: yo.name || null,
      });
      // El login ACEPTADO. `connection: 'open'` en Baileys es lo que en
      // pywhats era el `<success>` del servidor: si la sesion estuviera
      // revocada llegaria un cierre 401 y nunca se pasaria por aqui.
      //
      // De esto cuelga persistir la vinculacion y pasar a CONNECTED. Va
      // DESPUES de `connected` a proposito: la pantalla anuncia primero que
      // hay sesion y solo entonces se da por buena.
      evento('session_valid', null);
      // La identidad propia, con lo que hace falta para la HUELLA de sesion.
      //
      // `device_id` es el numero de ranura que da el servidor y va dentro del
      // jid (`573...:12@s.whatsapp.net`); `registration_id` se genera nuevo en
      // cada vinculacion. Los dos entran en la huella, y tienen que salir de
      // aqui: si el disco y el dispositivo vivo la calcularan distinto, el
      // historial inicial se confirmaria bajo una huella y se buscaria bajo
      // otra, y la espera de 180 s volveria en cada arranque sin que nada lo
      // delatara.
      const credenciales = (sock.authState && sock.authState.creds) || {};
      const idCompleto = String((credenciales.me || {}).id || '');
      const ranura = idCompleto.includes('@')
        ? (idCompleto.slice(0, idCompleto.indexOf('@')).split(':')[1] || '')
        : '';
      emitir({
        event: 'device',
        jid: traducir.jidTexto(yo.id),
        lid: traducir.jidTexto(yo.lid),
        device_id: ranura,
        registration_id: credenciales.registrationId === undefined
          ? '' : String(credenciales.registrationId),
      });
    }

    if (u.connection === 'close') manejarCierre(u);
  });

  // -- Mensajes en vivo (C-eventos) -----------------------------------------
  sock.ev.on('messages.upsert', ({ messages, type }) => {
    for (const info of messages || []) {
      // Un mensaje que llego y NO se pudo descifrar. Baileys lo entrega con
      // el stub CIPHERTEXT (2) y sin contenido; es lo que en pywhats era el
      // "decrypt failed" del receptor.
      //
      // Se cuenta y ya: el reintento lo lleva Baileys por dentro. Pero tiene
      // que salir por el canal, porque distinguir "no se pudo leer" de "no
      // llego" es lo unico que separa una copia con un hueco de una copia
      // completa.
      if (info && info.messageStubType === 2) {
        evento('decrypt_error', {
          chat: traducir.jidTexto(info.key && info.key.remoteJid),
          message_id: info.key && info.key.id,
          from_me: Boolean(info.key && info.key.fromMe),
          reason: 'ciphertext',
        });
        continue;
      }
      const carga = traducir.traducirMensaje(info, serializarInfo);
      if (!carga) continue;
      if (carga.lid_par) emitir({ event: 'lid_par', ...carga.lid_par });
      evento('message', carga, { upsert_type: type || null });
    }
  });

  // -- Historial (C-10) ------------------------------------------------------
  sock.ev.on('messaging-history.set', (lote) => {
    // LA AGENDA PRIMERO, y antes de tocar los mensajes.
    //
    // Este lote es la unica fuente de los nombres guardados, y solo llega
    // completo en el bootstrap de una vinculacion nueva. Se emite antes de
    // ingerir para que el chat que se cree a continuacion ya tenga con que
    // nombrarse en vez de aparecer como un identificador.
    emitirContactos(lote.contacts);
    emitirParesDeChats(lote.chats);
    const carga = traducir.traducirHistorial(lote, serializarInfo);
    // Los pares que venian en las claves de estos mensajes. Es la fuente que
    // no depende del bootstrap: llega con cualquier lote, tambien ON_DEMAND.
    for (const par of carga.lid_pares || []) {
      emitir({ event: 'lid_par', lid: par.lid, pn: par.pn });
    }
    const archivo = archivarLote(carga);
    // La correlacion con la peticion que lo pidio. (C-13)
    const pendiente = carga.sync_type === 'ON_DEMAND' ? tomarPendiente() : null;
    evento('history_sync', carga, {
      archivo,
      request_id: pendiente ? pendiente.id : null,
      chat_jid: pendiente ? pendiente.chat : null,
    });
  });

  // -- Recibos, reacciones, ediciones ---------------------------------------
  sock.ev.on('message-receipt.update', (lista) => {
    for (const r of lista || []) {
      evento('receipt', {
        from_jid: traducir.jidTexto(r.key && r.key.remoteJid),
        message_ids: [r.key && r.key.id].filter(Boolean),
      });
    }
  });

  sock.ev.on('messages.reaction', (lista) => {
    for (const r of lista || []) {
      evento('reaction', {
        chat: traducir.jidTexto(r.key && r.key.remoteJid),
        message_id: r.key && r.key.id,
        text: (r.reaction && r.reaction.text) || '',
        key_from_me: Boolean(r.key && r.key.fromMe),
      });
    }
  });

  sock.ev.on('messages.update', (lista) => {
    for (const u of lista || []) {
      const nombre = traducir.clasificarActualizacion(u);
      if (!nombre) continue;
      evento(nombre, {
        chat: traducir.jidTexto(u.key && u.key.remoteJid),
        message_id: u.key && u.key.id,
      });
    }
  });

  // -- Contactos y nombres (C-30) -------------------------------------------
  //
  // `contacts.set` estaba en esta lista y NO EXISTE en Baileys 6.7.24: la
  // agenda no llega por ningun evento propio, viaja dentro del lote de
  // `messaging-history.set` (ver `emitirContactos`). `contacts.upsert`
  // tampoco lo emite el socket; `contacts.update` si, con el nombre publico
  // que trae cada mensaje.
  for (const nombre of ['contacts.upsert', 'contacts.update']) {
    sock.ev.on(nombre, (lista) => {
      for (const c of lista || []) {
        const carga = traducir.traducirContacto(c);
        if (!carga) continue;
        evento('contact', carga);
        if (carga.push_name) evento('pushname', { jid: carga.jid, name: carga.push_name });
        if (carga.lid) emitir({ event: 'lid_par', lid: carga.lid, pn: carga.jid });
      }
    });
  }

  // -- Nombres de grupo ------------------------------------------------------
  //
  // El `subject` llega por su propio evento y NO viene siempre en el lote de
  // historial. Se emite como `contact` sobre el JID del grupo, que es donde
  // el panel busca el nombre de una conversacion.
  for (const nombre of ['groups.upsert', 'groups.update']) {
    sock.ev.on(nombre, (lista) => {
      for (const g of lista || []) {
        const jid = traducir.jidTexto(g && g.id);
        if (!jid || !g.subject) continue;
        evento('contact', {
          jid,
          full_name: g.subject,
          first_name: g.subject,
          push_name: g.subject,
          lid: null,
        });
      }
    });
  }

  sock.ev.on('chats.update', (lista) => {
    for (const c of lista || []) {
      const jid = traducir.jidTexto(c.id);
      if (!jid) continue;
      // Se manda el VALOR, no solo el si/no. `pinned` es la marca de tiempo
      // en que se fijo --y es la que ordena entre varios fijados-- y
      // `muteEndTime` es hasta cuando dura el silencio. Reducirlos a booleano
      // tiraba justo el dato que hace falta para ordenar y para saber cuando
      // vuelve a sonar. El booleano se manda igual porque es lo que estaba en
      // el contrato y hay quien lo lee.
      if (c.mute !== undefined) {
        evento('mute', {
          jid,
          muted: Boolean(c.mute),
          // 0 es "para siempre" en WhatsApp, asi que no se puede colapsar
          // con "sin silenciar": nulo es lo uno y 0 lo otro.
          mute_until: c.mute === null ? null : Number(c.mute),
        });
      }
      if (c.pinned !== undefined) {
        evento('pin', {
          jid,
          pinned: Boolean(c.pinned),
          pinned_at: Number(c.pinned || 0) || null,
        });
      }
      if (c.archived !== undefined) {
        evento('archive', { jid, archived: Boolean(c.archived) });
      }
    }
  });

  sock.ev.on('presence.update', (p) => {
    evento('presence', { jid: traducir.jidTexto(p.id) });
    // "escribiendo..." / "grabando...". No se persiste nada: esta en el
    // contrato porque lo estaba en el anterior, y un consumidor que lo espere
    // no puede quedarse sin el.
    for (const [quien, estado] of Object.entries((p && p.presences) || {})) {
      const tipo = estado && estado.lastKnownPresence;
      if (tipo === 'composing' || tipo === 'recording' || tipo === 'paused') {
        evento('chat_presence', {
          chat: traducir.jidTexto(p.id),
          jid: traducir.jidTexto(quien),
          state: tipo,
        });
      }
    }
  });
}

/**
 * Cierre de conexion. (C-06)
 *
 * Distinguir `loggedOut` del resto es la diferencia entre reconectar solo y
 * pedirle al usuario un QR nuevo. Antes se hurgaba en `_sock._closed`, un
 * atributo privado de pywhats.
 */
function manejarCierre(u) {
  const codigo = u.lastDisconnect && u.lastDisconnect.error
    ? (u.lastDisconnect.error.output || {}).statusCode
    : undefined;

  if (codigo === DisconnectReason.loggedOut) {
    // La sesion ya no existe. NO se reconecta y NO se borra nada desde aqui:
    // la carpeta es una unidad y quien decide borrarla es Python.
    evento('logged_out', { reason: 'loggedOut' });
    emitir({ event: 'estado', state: 'logged_out' });
    return;
  }

  evento('disconnected', { code: codigo === undefined ? null : codigo });
  if (cerrando) return;

  // El fallo que dejaba la pantalla cargando sin decir nada: el servidor
  // acepta el saludo, rechaza el registro y cierra. Sin QR y sin sesion
  // guardada no hay nada que reconectar, asi que se dice en voz alta en vez
  // de reintentar en silencio.
  if (!vioQR && !fs.existsSync(path.join(CARPETA_SESION, 'creds.json'))) {
    registrar(
      `[baileys] cerrada (${codigo}) ANTES de recibir ningun QR: el servidor`,
      'rechazo el registro. Suele ser el perfil anunciado --ver navegadorAAnunciar()--',
      'no un problema de red.',
    );
  }

  // Espera creciente con tope. Un socket que no levanta no levanta mejor por
  // intentarlo cien veces seguidas.
  reintentos += 1;
  const espera = Math.min(60000, 1000 * 2 ** Math.min(reintentos, 6));
  registrar(`[baileys] reconectando en ${espera} ms (intento ${reintentos})`);
  setTimeout(() => {
    arrancar().catch((fallo) => {
      registrar('[baileys] fallo al reconectar:', String(fallo).slice(0, 200));
    });
  }, espera);
}

// --- Peticiones desde Python ------------------------------------------------

function tomarPendiente() {
  // La respuesta no dice a que peticion contesta mas alla del orden, asi que
  // se toma la mas antigua en vuelo. Python correlaciona de verdad por su
  // tabla `history_requests`; esto es una ayuda, no la fuente.
  const primera = enVuelo.keys().next();
  if (primera.done) return null;
  const pendiente = enVuelo.get(primera.value);
  enVuelo.delete(primera.value);
  return pendiente;
}

const ORDENES = {
  /**
   * Pedir historial anterior de una conversacion. (C-11, compuerta G1)
   *
   * SE MANDA LA STANZA CRUDA, no `fetchMessageHistory`.
   *
   * No es por desconfianza del atajo: es que `fetchMessageHistory` no hace
   * mas que rellenar este mismo objeto y llamar a
   * `sendPeerDataOperationMessage`. Construirlo aqui permite dos cosas que el
   * atajo no deja: pedir la tanda entera y anadir `accountLid`, que es lo que
   * manda hoy pywhats.
   *
   * `sendPeerDataOperationMessage` envia al PROPIO dispositivo principal con
   * `category: 'peer'`. Sin ese atributo el servidor confirma la stanza y la
   * descarta en silencio -- 8 peticiones, 6 timeouts, 0 mensajes.
   */
  async pedir_historial(orden) {
    const { chat_jid: chat, message_id: id, from_me: fromMe, timestamp } = orden;
    if (!sock) throw new Error('sin sesion');
    if (!chat || !id) throw new Error('faltan chat o ancla');

    // EL ANCLA VA EN SEGUNDOS.
    //
    // El campo del protobuf se llama `...MS` y aun asi el telefono espera
    // segundos. Multiplicar por 1000 pone el ancla ~56.000 anos en el futuro:
    // la stanza se acepta, llega el ACK y no vuelve ninguna respuesta. Es el
    // fallo que mas costo encontrar, asi que aqui hay una guarda.
    const marca = Number(timestamp);
    if (!Number.isFinite(marca) || marca <= 0) throw new Error('ancla sin marca de tiempo');
    if (marca > 4102444800) {
      throw new Error(`ancla en milisegundos (${marca}): el telefono espera SEGUNDOS`);
    }

    const cuantos = Number(orden.count) > 0 ? Number(orden.count) : TANDA_PEDIDA;
    const peticion = {
      peerDataOperationRequestType:
        proto.Message.PeerDataOperationRequestType.HISTORY_SYNC_ON_DEMAND,
      historySyncOnDemandRequest: {
        chatJid: chat,
        oldestMsgId: id,
        oldestMsgFromMe: Boolean(fromMe),
        oldestMsgTimestampMs: marca,
        onDemandMsgCount: cuantos,
      },
    };
    if (orden.account_lid) {
      peticion.historySyncOnDemandRequest.accountLid = orden.account_lid;
    }

    const enviado = await sock.sendPeerDataOperationMessage(peticion);
    const idPeticion = String(enviado || `${chat}:${id}`);
    enVuelo.set(idPeticion, { id: idPeticion, chat, pedido: cuantos });
    return {
      request_id: idPeticion,
      count_pedido: cuantos,
      count_enviado: cuantos,
      // Ya no hay recorte: la stanza sale con lo que se pide. Se deja el campo
      // porque el arnes de G1 lo lee.
      capado: false,
      via: 'peer_data_operation',
    };
  },

  /** El asunto de un grupo. (C-32) El ritmo lo marca Python: 5 y 0,4 s. */
  async info_de_grupo(orden) {
    if (!sock) throw new Error('sin sesion');
    const meta = await sock.groupMetadata(orden.jid);
    return { subject: (meta && meta.subject) || null };
  },

  /** Resolver numeros y su LID. (C-31) */
  async resolver_lids(orden) {
    if (!sock) throw new Error('sin sesion');
    const salida = await sock.onWhatsApp(...(orden.numeros || []));
    return {
      resultados: (salida || []).map((r) => ({
        jid: traducir.jidTexto(r.jid),
        lid: traducir.jidTexto(r.lid),
        existe: Boolean(r.exists),
      })),
    };
  },

  /** Re-sincronizar las colecciones de app-state que traen nombres. (C-30) */
  async resync_appstate(orden) {
    if (!sock) throw new Error('sin sesion');
    const colecciones = orden.colecciones || ['critical_unblock_low', 'regular_high'];
    await sock.resyncAppState(colecciones, Boolean(orden.completo));
    return { colecciones };
  },

  /**
   * Descargar un adjunto a disco. (C-40)
   *
   * El binario NO viaja por el canal: se escribe desde Node y se devuelve la
   * ruta. `reuploadRequest` es lo que pywhats no tenia: si el CDN ya caduco
   * --pasa a los ~30 dias-- le pide al telefono que lo vuelva a subir.
   */
  async descargar_media(orden) {
    if (!sock) throw new Error('sin sesion');
    const info = proto.WebMessageInfo.decode(Buffer.from(orden.raw_proto, 'base64'));
    const datos = await downloadMediaMessage(
      info,
      'buffer',
      {},
      { reuploadRequest: sock.updateMediaMessage },
    );
    fs.mkdirSync(path.dirname(orden.destino), { recursive: true });
    fs.writeFileSync(orden.destino, datos);
    return { bytes: datos.length, destino: orden.destino };
  },
};

function atender(linea) {
  let orden;
  try {
    orden = JSON.parse(linea);
  } catch (fallo) {
    return;
  }
  const id = orden.id;
  const manejador = ORDENES[orden.cmd];
  if (!manejador) {
    emitir({ event: 'respuesta', id, ok: false, error: `orden desconocida: ${orden.cmd}` });
    return;
  }
  Promise.resolve()
    .then(() => manejador(orden))
    .then((datos) => emitir({ event: 'respuesta', id, ok: true, data: datos || {} }))
    .catch((fallo) =>
      emitir({ event: 'respuesta', id, ok: false, error: String(fallo && fallo.message ? fallo.message : fallo).slice(0, 300) }),
    );
}

// --- Arranque ---------------------------------------------------------------

process.stdin.setEncoding('utf8');
process.stdin.on('data', troceador(atender));
process.stdin.on('end', () => {
  cerrando = true;
  process.exit(0);
});

for (const senal of ['SIGTERM', 'SIGINT']) {
  process.on(senal, () => {
    cerrando = true;
    process.exit(0);
  });
}

arrancar()
  .then(() => emitir({ event: 'estado', state: 'starting' }))
  .catch((fallo) => {
    emitir({ event: 'fatal', code: 'ARRANQUE', message: String(fallo).slice(0, 300) });
    process.exit(4);
  });
