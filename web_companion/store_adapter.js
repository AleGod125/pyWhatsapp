'use strict';
/** Adapter READ-ONLY sobre los internals que usa whatsapp-web.js 1.34.x. */

const LOADERS = [
  'chat.loadEarlierMsgs', 'chat.msgs.loadEarlierMsgs', 'chat.msgs.loadEarlier',
  'chat.loadOlderMsgs', 'Store.ConversationMsgs.loadEarlierMsgs',
  'Store.ConversationMsgs.fetchPage',
];
const HISTORY = ['chat.syncHistory', 'Store.HistorySync.sendPeerDataOperationRequest'];

function safeError(error) {
  return {
    error_name: String(error?.name || 'Error').slice(0, 60),
    error_message: String(error?.message || error || 'unknown').slice(0, 160),
  };
}

/** Diagnóstico acotado. No devuelve valores de window, sólo nombres/tipos. */
async function descubrir(page) {
  const started = Date.now();
  try {
    const result = await page.evaluate(() => {
      const errors = [];
      const attempt = (method, fn, fallback = null) => {
        try { return fn(); } catch (error) {
          errors.push({ method, error_name: String(error?.name || 'Error').slice(0, 60), error_message: String(error?.message || error || 'unknown').slice(0, 160) });
          return fallback;
        }
      };
      const req = (name) => typeof window.require === 'function'
        ? attempt(`window.require(${name})`, () => window.require(name)) : null;
      const collections = window.Store || req('WAWebCollections') || {};
      const chatCollection = window.Store?.Chat || collections?.Chat || null;
      const msgCollection = window.Store?.Msg || collections?.Msg || null;
      const widFactory = window.Store?.WidFactory || req('WAWebWidFactory') || null;
      const conversationMsgs = window.Store?.ConversationMsgs || req('WAWebConversationMsgs') || null;
      const historySync = window.Store?.HistorySync || req('WAWebHistorySync') || null;
      const models = attempt('Chat.getModelsArray', () => chatCollection?.getModelsArray?.() || [], []);
      const sample = models[0] || null;
      const functionType = (value) => typeof value === 'function';
      return {
        capabilities: {
          client_get_chats: functionType(window.WWebJS?.getChats),
          window_store: Boolean(window.Store), window_wwebjs: Boolean(window.WWebJS),
          window_auth_store: Boolean(window.AuthStore), window_require: functionType(window.require),
          store_chat: Boolean(chatCollection), store_chat_models: functionType(chatCollection?.getModelsArray),
          store_msg: Boolean(msgCollection), conversation_msgs: Boolean(conversationMsgs),
          history_sync: Boolean(historySync), wid_factory: functionType(widFactory?.createWid),
          chat_load_earlier: functionType(sample?.loadEarlierMsgs),
          msgs_load_earlier: functionType(sample?.msgs?.loadEarlierMsgs) || functionType(sample?.msgs?.loadEarlier),
          fetch_page: functionType(conversationMsgs?.fetchPage),
          chat_sync_history: functionType(sample?.syncHistory),
          peer_data_operation: functionType(historySync?.sendPeerDataOperationRequest),
        },
        diagnostics: {
          relevant_window_keys: Object.keys(window).filter((key) => /store|chat|msg|conversation|history|wid|waweb|wweb|webpack|require|module|socket/i.test(key)).slice(0, 60),
          webpack_runtime_keys: Object.keys(window).filter((key) => /webpack|require|module/i.test(key)).slice(0, 30),
          strategy: window.Store?.Chat ? 'window.Store' : chatCollection ? 'window.require(WAWebCollections)' : window.WWebJS?.getChats ? 'window.WWebJS' : 'none',
          chat_models: Array.isArray(models) ? models.length : 0,
          loaders: {
            'chat.loadEarlierMsgs': typeof sample?.loadEarlierMsgs,
            'chat.msgs.loadEarlierMsgs': typeof sample?.msgs?.loadEarlierMsgs,
            'chat.msgs.loadEarlier': typeof sample?.msgs?.loadEarlier,
            'chat.loadOlderMsgs': typeof sample?.loadOlderMsgs,
            'Store.ConversationMsgs.loadEarlierMsgs': typeof conversationMsgs?.loadEarlierMsgs,
            'Store.ConversationMsgs.fetchPage': typeof conversationMsgs?.fetchPage,
            'chat.syncHistory': typeof sample?.syncHistory,
            'Store.HistorySync.sendPeerDataOperationRequest': typeof historySync?.sendPeerDataOperationRequest,
          }, errors,
        },
      };
    });
    result.diagnostics.elapsed_ms = Date.now() - started;
    return result;
  } catch (error) {
    return {
      capabilities: {
        client_get_chats: false, window_store: false, window_wwebjs: false,
        window_auth_store: false, window_require: false, store_chat: false,
        store_chat_models: false, store_msg: false, conversation_msgs: false,
        history_sync: false, wid_factory: false, chat_load_earlier: false,
        msgs_load_earlier: false, fetch_page: false, chat_sync_history: false,
        peer_data_operation: false,
      },
      diagnostics: { strategy: 'none', elapsed_ms: Date.now() - started, errors: [{ method: 'page.evaluate', ...safeError(error) }] },
    };
  }
}

/** Espera acotada: client ready y Store ready son hechos distintos. */
async function esperarStore(page, { timeoutMs = 45000, pollMs = 500 } = {}) {
  const start = Date.now();
  let result = await descubrir(page);
  while (!result.capabilities.store_chat_models && Date.now() - start < timeoutMs) {
    await new Promise((resolve) => setTimeout(resolve, pollMs));
    result = await descubrir(page);
  }
  return { ...result, store_ready: result.capabilities.store_chat_models, waited_ms: Date.now() - start };
}

/** Colección raw por window.Store o por el mismo WAWebCollections que usa WWebJS. */
async function chatsDelStore(page) {
  return page.evaluate(() => {
    const attempt = (fn, fallback) => { try { return fn(); } catch { return fallback; } };
    const collections = window.Store || (typeof window.require === 'function' ? attempt(() => window.require('WAWebCollections'), {}) : {});
    const chats = window.Store?.Chat || collections?.Chat;
    if (typeof chats?.getModelsArray !== 'function') return [];
    return (attempt(() => chats.getModelsArray(), []) || []).map((chat) => {
      const id = chat?.id?._serialized || attempt(() => chat?.id?.toString?.(), '') || '';
      if (!id || id.includes('status@')) return null;
      const messages = attempt(() => chat?.msgs?.getModelsArray?.(), null) || (Array.isArray(chat?.msgs?.models) ? chat.msgs.models : []);
      return { id, is_group: id.endsWith('@g.us'), last_activity: Number(chat?.t || chat?.timestamp || chat?.lastMessage?.t || 0) || null, msgs_in_memory: messages.length };
    }).filter(Boolean);
  });
}

/** Mensajes ya materializados. No ejecuta loaders y para al límite. */
async function mensajesEnMemoria(page, chatJid, limit) {
  return page.evaluate((jid, max) => {
    const attempt = (fn, fallback = null) => { try { return fn(); } catch { return fallback; } };
    const req = (name) => typeof window.require === 'function' ? attempt(() => window.require(name)) : null;
    const collections = window.Store || req('WAWebCollections') || {};
    const chats = window.Store?.Chat || collections?.Chat;
    const widFactory = window.Store?.WidFactory || req('WAWebWidFactory');
    if (!chats) return { encontrado: false, mensajes: [], source: 'none' };
    const wid = attempt(() => widFactory?.createWid?.(jid), jid) || jid;
    let chat = attempt(() => chats.get?.(wid)) || attempt(() => chats.get?.(jid));
    if (!chat && typeof chats.find === 'function') chat = attempt(() => chats.find(wid));
    if (!chat) return { encontrado: false, mensajes: [], source: 'store' };
    const models = attempt(() => chat.msgs?.getModelsArray?.(), null) || (Array.isArray(chat.msgs?.models) ? chat.msgs.models : []);
    return {
      encontrado: true,
      source: window.Store?.Chat ? 'window.Store' : 'window.require(WAWebCollections)',
      // Nombres EXPLICITOS, no `id` a secas.
      //
      // Antes esto devolvia `{id: <cadena>}` y el normalizador esperaba el
      // objeto `{id: {id, fromMe}}` del Store: `typeof clave === 'object'`
      // era falso y los 22 chats con mensajes daban CERO candidatos. Un
      // nombre ambiguo entre dos modulos es lo que permitio ese desajuste.
      //
      // `from_me` va como boolean o null, NUNCA como `false` por defecto:
      // viaja en la peticion ON_DEMAND y suponerlo cuesta una peticion que
      // no responde.
      mensajes: models.slice(0, max).map((message) => {
        const clave = message?.id ?? null;
        const remoto = clave && typeof clave === 'object' ? clave.remote : null;
        return {
          wa_msg_id: (clave && typeof clave === 'object' && typeof clave.id === 'string')
            ? clave.id : (typeof clave === 'string' ? clave : null),
          t: message?.t ?? message?.timestamp ?? null,
          from_me: (clave && typeof clave === 'object' && typeof clave.fromMe === 'boolean')
            ? clave.fromMe
            : (typeof message?.fromMe === 'boolean' ? message.fromMe : null),
          // El JID que declara el propio mensaje. En un grupo es el GRUPO,
          // no el participante. Se manda como respaldo del chat contenedor.
          remote: attempt(() => remoto?._serialized ?? (typeof remoto === 'string' ? remoto : null), null),
          // Para que Python aplique SU filtro de mensajes de protocolo.
          type: typeof message?.type === 'string' ? message.type : null,
        };
      }),
    };
  }, chatJid, limit);
}

/**
 * Radiografía ESTRUCTURAL de los mensajes ya materializados.
 *
 * Existe porque el sondeo daba 22 conversaciones con mensajes y 0 candidatos:
 * eso no es "WhatsApp no tiene datos", es que no estamos leyendo el modelo
 * donde de verdad vive el identificador. Y adivinar el nombre de la propiedad
 * es exactamente lo que no se puede hacer con internals sin documentar.
 *
 * Devuelve NOMBRES y TIPOS, nunca valores: ni texto, ni multimedia, ni
 * identificadores completos. Los pocos valores que salen van truncados y solo
 * para poder reconocer la FORMA de un WAMID.
 */
async function inspeccionarModelos(page, { chats = 3, mensajes = 2 } = {}) {
  return page.evaluate(
    (topeChats, topeMensajes) => {
      const attempt = (fn, fallback = null) => {
        try { return fn(); } catch { return fallback; }
      };
      const req = (name) => typeof window.require === 'function'
        ? attempt(() => window.require(name)) : null;
      const collections = window.Store || req('WAWebCollections') || {};
      const coleccion = window.Store?.Chat || collections?.Chat;
      if (typeof coleccion?.getModelsArray !== 'function') {
        return { error: 'sin_coleccion_de_chats' };
      }

      // Solo la FORMA: 4 primeros y 4 ultimos caracteres. Sirve para saber si
      // parece un WAMID hexadecimal sin exponer el identificador.
      const forma = (valor) => {
        if (typeof valor !== 'string' || !valor) return null;
        return {
          longitud: valor.length,
          inicio: valor.slice(0, 4),
          fin: valor.slice(-4),
          solo_hex: /^[0-9A-Fa-f]+$/.test(valor),
          tiene_guion_bajo: valor.includes('_'),
          tiene_arroba: valor.includes('@'),
        };
      };

      const tipos = (objeto, claves) => {
        const salida = {};
        for (const clave of claves) salida[clave] = attempt(() => typeof objeto?.[clave], 'error');
        return salida;
      };

      const modelos = attempt(() => coleccion.getModelsArray(), []) || [];
      const conMensajes = modelos.filter((c) => {
        const m = attempt(() => c?.msgs?.getModelsArray?.(), null)
          || (Array.isArray(c?.msgs?.models) ? c.msgs.models : []);
        return m.length > 0;
      });

      return {
        chats_totales: modelos.length,
        chats_con_mensajes: conMensajes.length,
        muestras: conMensajes.slice(0, topeChats).map((chat) => {
          const lista = attempt(() => chat?.msgs?.getModelsArray?.(), null)
            || (Array.isArray(chat?.msgs?.models) ? chat.msgs.models : []);
          return {
            chat: {
              claves: attempt(() => Object.keys(chat).slice(0, 40), []),
              id_tipo: attempt(() => typeof chat?.id, 'error'),
              id_claves: attempt(() => Object.keys(chat?.id || {}).slice(0, 20), []),
              id_serializado: forma(attempt(() => chat?.id?._serialized, null)),
              es_grupo: attempt(() => String(chat?.id?._serialized || '').endsWith('@g.us'), false),
              mensajes_en_memoria: lista.length,
            },
            mensajes: lista.slice(0, topeMensajes).map((msg) => ({
              // Todas las claves propias visibles del modelo.
              claves: attempt(() => Object.keys(msg).slice(0, 60), []),
              // Y las del prototipo: los modelos de WhatsApp Web usan
              // getters, asi que Object.keys puede no ensenar nada util.
              claves_prototipo: attempt(
                () => Object.getOwnPropertyNames(Object.getPrototypeOf(msg) || {}).slice(0, 60),
                [],
              ),
              tipos: tipos(msg, [
                'id', 't', 'timestamp', 'fromMe', 'chatId', 'from', 'to',
                'type', 'subtype', 'ack', 'isNewMsg', 'attributes',
                '__x_id', '__x_t', '__x_from', '__x_to', '__x_isSentByMe',
              ]),
              id: {
                tipo: attempt(() => typeof msg?.id, 'error'),
                claves: attempt(() => Object.keys(msg?.id || {}).slice(0, 20), []),
                tipo_de_id: attempt(() => typeof msg?.id?.id, 'error'),
                forma_de_id: forma(attempt(() => msg?.id?.id, null)),
                forma_serializado: forma(attempt(() => msg?.id?._serialized, null)),
                from_me: attempt(() => msg?.id?.fromMe, 'error'),
                remote_tipo: attempt(() => typeof msg?.id?.remote, 'error'),
                remote_forma: forma(
                  attempt(() => msg?.id?.remote?._serialized ?? msg?.id?.remote, null),
                ),
                participant_tipo: attempt(() => typeof msg?.id?.participant, 'error'),
              },
              // Si `id` fuera una cadena directamente, tambien hay que verlo.
              id_como_cadena: forma(attempt(() => (typeof msg?.id === 'string' ? msg.id : null), null)),
              marcas: {
                t: attempt(() => typeof msg?.t, 'error'),
                t_valor_orden: attempt(() => {
                  const v = Number(msg?.t);
                  // Ni el valor ni la fecha: solo cuantos digitos tiene, que
                  // es lo que distingue segundos de milisegundos.
                  return Number.isFinite(v) && v > 0 ? String(Math.trunc(v)).length : null;
                }, null),
                timestamp: attempt(() => typeof msg?.timestamp, 'error'),
                timestamp_valor_orden: attempt(() => {
                  const v = Number(msg?.timestamp);
                  return Number.isFinite(v) && v > 0 ? String(Math.trunc(v)).length : null;
                }, null),
              },
            })),
          };
        }),
      };
    },
    chats,
    mensajes,
  );
}

/** Deliberadamente no se ejecuta ningún loader en esta fase diagnóstica. */
async function cargarAnteriores() {
  return { ok: false, motivo: 'read_only_loader_disabled' };
}

module.exports = {
  CARGADORES_DE_MENSAJES: LOADERS, HISTORIAL: HISTORY, descubrir,
  esperarStore, chatsDelStore, mensajesEnMemoria, inspeccionarModelos,
  cargarAnteriores,
};
