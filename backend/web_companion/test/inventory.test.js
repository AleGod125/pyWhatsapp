'use strict';
/**
 * El índice: qué conversaciones existen y cuál es su último mensaje real.
 *
 * TRES INTENTOS, EN ORDEN, Y SE PARA EN EL PRIMERO QUE SIRVA
 * ----------------------------------------------------------
 *   1. `chat.lastMessage`         gratis: la lista ya lo tiene
 *   2. mensajes en memoria        gratis: el Store ya los materializó
 *   3. `fetchMessages({limit:1})` pide a la red, así que va el último
 *
 * Lo que estas pruebas protegen es sobre todo el orden y el tope. Sin ellos
 * esto se convierte en un segundo extractor de historial, que es justamente lo
 * que no queremos: para eso está la otra sesión, que lo hace mejor.
 *
 * Y una más, que es la que costó una fase entera: **que se llegue a
 * preguntar**. Se midió `getChats()` devolviendo cero, y el camino de
 * respaldo por el Store devolvía la lista sin intentar ninguna de las tres
 * cosas. `chats=50 seeds=0`, con el sondeo antiguo encontrando 14 sobre las
 * mismas conversaciones.
 */

const test = require('node:test');
const assert = require('node:assert');
const indice = require('../inventory');

const WAMID = 'AC7B0102030405060708090A0B0C24EB';
const OTRO_WAMID = 'BD8C0102030405060708090A0B0C35FC';
const T = 1760000000;

/** Un mensaje con la forma que devuelve el Store. */
const mensaje = (extra = {}) => ({
  id: { id: WAMID, fromMe: false, remote: { _serialized: '573001112233@c.us' } },
  t: T,
  type: 'chat',
  ...extra,
});

const chatDe = (jid, extra = {}) => ({
  id: { _serialized: jid },
  isGroup: jid.endsWith('@g.us'),
  name: null,
  timestamp: T + 100,
  lastMessage: null,
  fetchMessages: async () => [],
  ...extra,
});

/** Un adaptador que no habla con ningún navegador. */
const adaptador = (memoria = {}, delStore = []) => ({
  chatsDelStore: async () => delStore,
  mensajesEnMemoria: async (_page, jid) => ({
    encontrado: true,
    mensajes: memoria[jid] || [],
  }),
});

const clienteCon = (chats, extra = {}) => ({
  pupPage: {},
  getChats: async () => chats,
  ...extra,
});

// ---------------------------------------------------------------------------
// Descubrimiento
// ---------------------------------------------------------------------------

test('el inventario lista TODAS las conversaciones, no solo unas cuantas', async () => {
  // Es el cambio de fondo: antes se preguntaba "de estas que conozco,
  // cuales ves"; ahora "que conversaciones existen".
  const cliente = clienteCon([
    chatDe('573001112233@c.us'),
    chatDe('573004445566@c.us'),
    chatDe('120363000000000000@g.us'),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());
  assert.strictEqual(salida.metrics.total, 3);
  assert.strictEqual(salida.metrics.individual, 2);
  assert.strictEqual(salida.metrics.group, 1);
});

test('los estados y las difusiones no son conversaciones', async () => {
  const cliente = clienteCon([
    chatDe('573001112233@c.us'),
    chatDe('status@broadcast'),
    chatDe('1234@newsletter'),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());
  assert.strictEqual(salida.metrics.total, 1);
});

test('el nombre sale del primero que lo tenga', async () => {
  assert.strictEqual(indice.nombreDe({ name: 'Ana' }), 'Ana');
  assert.strictEqual(indice.nombreDe({ formattedTitle: 'Ana B' }), 'Ana B');
  assert.strictEqual(indice.nombreDe({ contact: { pushname: 'Anita' } }), 'Anita');
  assert.strictEqual(indice.nombreDe({ groupMetadata: { subject: 'Familia' } }), 'Familia');
});

test('sin nombre NO se inventa uno: lo decide Python', async () => {
  // Aqui no se sabe de alias ni de telefonos legibles.
  assert.strictEqual(indice.nombreDe({}), null);
  assert.strictEqual(indice.nombreDe({ name: '   ' }), null);
});

test('las dos vias se UNEN, no se elige una', async () => {
  // Antes: si getChats daba algo, el Store no se miraba; si no daba nada, se
  // usaba el Store y se perdian los objetos. Las conversaciones que solo ve
  // una de las dos se quedaban fuera en los dos casos.
  const cliente = clienteCon([chatDe('573001112233@c.us')]);
  const salida = await indice.inventarioCompleto(
    cliente,
    adaptador({}, [{ id: '573009998877@c.us', is_group: false, last_activity: T }]),
  );

  assert.strictEqual(salida.metrics.total, 2);
  assert.ok(salida.source.includes('getChats'));
  assert.ok(salida.source.includes('store'));
});

test('la misma conversacion por las dos vias se cuenta UNA vez', async () => {
  const cliente = clienteCon([chatDe('573001112233@c.us')]);
  const salida = await indice.inventarioCompleto(
    cliente,
    adaptador({}, [{ id: '573001112233@c.us', is_group: false, last_activity: T }]),
  );
  assert.strictEqual(salida.metrics.total, 1);
});

// ---------------------------------------------------------------------------
// Los tres intentos, en orden
// ---------------------------------------------------------------------------

test('1) el ultimo mensaje de la lista basta y no se pide nada mas', async () => {
  let pedidos = 0;
  const cliente = clienteCon([
    chatDe('573001112233@c.us', {
      lastMessage: mensaje(),
      fetchMessages: async () => {
        pedidos += 1;
        return [];
      },
    }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());

  assert.strictEqual(salida.metrics.seed_from_last_message, 1);
  assert.strictEqual(salida.chats[0].via, 'last_message');
  assert.strictEqual(pedidos, 0, 'no se pide a la red lo que ya se tiene');
});

test('el origen del candidato dice por QUE via salio', async () => {
  // Antes los tres se etiquetaban `web_store` y no habia forma de saber cual
  // funcionaba.
  const cliente = clienteCon([chatDe('573001112233@c.us', { lastMessage: mensaje() })]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());
  assert.strictEqual(salida.chats[0].candidate.source, 'web_last_message');
});

test('2) si no, lo que el Store ya materializo', async () => {
  let pedidos = 0;
  const cliente = clienteCon([
    chatDe('573001112233@c.us', {
      fetchMessages: async () => {
        pedidos += 1;
        return [];
      },
    }),
  ]);
  const salida = await indice.inventarioCompleto(
    cliente,
    adaptador({ '573001112233@c.us': [mensaje()] }),
  );

  assert.strictEqual(salida.metrics.seed_from_store, 1);
  assert.strictEqual(salida.chats[0].via, 'store_memory');
  assert.strictEqual(pedidos, 0);
});

test('3) y solo entonces se materializa UNO', async () => {
  let limite = null;
  const cliente = clienteCon([
    chatDe('573001112233@c.us', {
      fetchMessages: async (opciones) => {
        limite = opciones?.limit;
        return [mensaje()];
      },
    }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());

  assert.strictEqual(salida.metrics.seed_from_fetch1, 1);
  assert.strictEqual(salida.chats[0].via, 'fetch_limit_1');
  assert.strictEqual(limite, 1, 'UNO. Nunca mas.');
  assert.strictEqual(salida.chats[0].candidate.source, 'web_fetch1');
});

test('se ESPERA a la peticion: una promesa no es un mensaje', async () => {
  let resolver;
  const cliente = clienteCon([
    chatDe('573001112233@c.us', {
      fetchMessages: () =>
        new Promise((r) => {
          resolver = () => r([mensaje()]);
          setTimeout(() => resolver(), 5);
        }),
    }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());
  assert.strictEqual(salida.metrics.seed_from_fetch1, 1);
});

test('NUNCA se pide historial completo', async () => {
  const limites = [];
  const cliente = clienteCon(
    Array.from({ length: 5 }, (_, i) =>
      chatDe(`57300011${i}@c.us`, {
        fetchMessages: async (opciones) => {
          limites.push(opciones?.limit);
          return [];
        },
      }),
    ),
  );
  await indice.inventarioCompleto(cliente, adaptador());

  assert.ok(limites.length > 0);
  for (const limite of limites) {
    assert.strictEqual(limite, 1);
    assert.notStrictEqual(limite, Infinity);
  }
});

test('la peticion a la red se puede desactivar entera', async () => {
  let pedidos = 0;
  const cliente = clienteCon([
    chatDe('573001112233@c.us', {
      fetchMessages: async () => {
        pedidos += 1;
        return [mensaje()];
      },
    }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador(), {
    permitirFetch: false,
  });

  assert.strictEqual(pedidos, 0);
  assert.strictEqual(salida.metrics.sin_candidato, 1);
});

test('hay un tope de conversaciones a las que se les pide', async () => {
  // Chromium no puede quedarse pidiendo indefinidamente.
  let pedidos = 0;
  const cliente = clienteCon(
    Array.from({ length: indice.TOPE_FETCH + 25 }, (_, i) =>
      chatDe(`5730${String(i).padStart(6, '0')}@c.us`, {
        fetchMessages: async () => {
          pedidos += 1;
          return [];
        },
      }),
    ),
  );
  await indice.inventarioCompleto(cliente, adaptador());
  assert.strictEqual(pedidos, indice.TOPE_FETCH);
});

// ---------------------------------------------------------------------------
// El resultado de pedir uno
// ---------------------------------------------------------------------------

test('si la peticion vuelve vacia no se inventa nada', async () => {
  const cliente = clienteCon([
    chatDe('573001112233@c.us', { fetchMessages: async () => [] }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());

  assert.strictEqual(salida.metrics.fetch1_attempted, 1);
  assert.strictEqual(salida.metrics.fetch1_empty, 1);
  assert.strictEqual(salida.chats[0].candidate, null);
  assert.strictEqual(salida.chats[0].no_seed_reason, 'WEB_NO_MATERIALIZED_MESSAGE');
});

test('si la peticion falla NO es un error permanente del chat', async () => {
  const cliente = clienteCon([
    chatDe('573001112233@c.us', {
      fetchMessages: async () => {
        throw new Error('sin red');
      },
    }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());

  assert.strictEqual(salida.metrics.fetch1_error, 1);
  assert.strictEqual(salida.chats[0].no_seed_reason, 'WEB_FETCH1_FAILED');
});

test('si el primero no sirve se prueba el siguiente, y se para en el bueno', async () => {
  const cliente = clienteCon([
    chatDe('573001112233@c.us', {
      fetchMessages: async () => [
        // El mas reciente no sirve: identificador del cliente, no de WhatsApp.
        mensaje({ id: { id: 'temp-abc', fromMe: false }, t: T + 50 }),
        mensaje({ id: { id: OTRO_WAMID, fromMe: false }, t: T }),
      ],
    }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());

  assert.strictEqual(salida.chats[0].candidate.wa_msg_id, OTRO_WAMID);
});

test('mensaje encontrado y referencia valida son DOS cosas', async () => {
  // Si esto se mezcla, "0 referencias" no distingue entre "WhatsApp no tiene
  // nada" y "nuestro filtro lo rechaza", que se arreglan en sitios distintos.
  const cliente = clienteCon([
    chatDe('573001112233@c.us', {
      fetchMessages: async () => [mensaje({ id: { id: 'temp-abc', fromMe: false } })],
    }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());

  assert.strictEqual(salida.metrics.fetch1_success, 1, 'si vino un mensaje');
  assert.strictEqual(salida.metrics.messages_found, 1);
  assert.strictEqual(salida.metrics.valid_seeds, 0, 'pero no sirve como referencia');
  assert.ok(salida.metrics.seed_invalid > 0);
  assert.strictEqual(salida.chats[0].no_seed_reason, 'WEB_MESSAGE_NOT_USABLE');
});

// ---------------------------------------------------------------------------
// El fallo medido: getChats() devuelve cero
// ---------------------------------------------------------------------------

test('sin getChats se sigue intentando: el Store tambien tiene mensajes', async () => {
  // ESTE es el fallo. Antes esto devolvia la lista y se acababa ahi: ni
  // ultimo mensaje, ni Store, ni peticion. `chats=50 seeds=0`.
  const cliente = { pupPage: {}, getChats: async () => [] };
  const adaptadorConStore = {
    chatsDelStore: async () => [
      { id: '573001112233@c.us', is_group: false, last_activity: T, msgs_in_memory: 4 },
    ],
    mensajesEnMemoria: async () => ({ encontrado: true, mensajes: [mensaje()] }),
  };
  const salida = await indice.inventarioCompleto(cliente, adaptadorConStore);

  assert.strictEqual(salida.metrics.total, 1);
  assert.strictEqual(salida.metrics.seed_from_store, 1, 'se llego a preguntar');
  assert.strictEqual(salida.chats[0].candidate.wa_msg_id, WAMID);
});

test('sin getChats tambien se puede pedir UNO, por identificador', async () => {
  // `getChats()` mapea todos los modelos y basta que uno falle para quedarse
  // sin lista. `getChatById` falla —o no— conversacion a conversacion.
  let pedidoPara = null;
  const cliente = {
    pupPage: {},
    getChats: async () => [],
    getChatById: async (jid) => {
      pedidoPara = jid;
      return { fetchMessages: async () => [mensaje()] };
    },
  };
  const adaptadorConStore = {
    chatsDelStore: async () => [{ id: '573001112233@c.us', is_group: false }],
    mensajesEnMemoria: async () => ({ encontrado: true, mensajes: [] }),
  };
  const salida = await indice.inventarioCompleto(cliente, adaptadorConStore);

  assert.strictEqual(pedidoPara, '573001112233@c.us');
  assert.strictEqual(salida.metrics.seed_from_fetch1, 1);
});

test('si no hay forma de construir el chat se dice, no se calla', async () => {
  const cliente = { pupPage: {}, getChats: async () => [] };
  const adaptadorConStore = {
    chatsDelStore: async () => [{ id: '573001112233@c.us', is_group: false }],
    mensajesEnMemoria: async () => ({ encontrado: true, mensajes: [] }),
  };
  const salida = await indice.inventarioCompleto(cliente, adaptadorConStore);

  assert.strictEqual(salida.chats[0].no_seed_reason, 'WEB_FETCH1_FAILED');
  assert.ok(salida.rejections['fetch1:sin_objeto_chat']);
});

// ---------------------------------------------------------------------------
// Donde se gasta la cuota de red
// ---------------------------------------------------------------------------

test('primero las que esperan referencia', async () => {
  const pedidas = [];
  const chats = Array.from({ length: 3 }, (_, i) =>
    chatDe(`5730000${i}@c.us`, {
      // La ultima de la lista es la mas reciente: sin prioridad iria primera.
      timestamp: T + i * 100,
      fetchMessages: async function () {
        pedidas.push(this.id._serialized);
        return [];
      },
    }),
  );
  // `fetchMessages` necesita el `this` del chat.
  for (const chat of chats) chat.fetchMessages = chat.fetchMessages.bind(chat);

  await indice.inventarioCompleto(clienteCon(chats), adaptador(), {
    prioritarios: ['57300000@c.us'],
  });

  assert.strictEqual(pedidas[0], '57300000@c.us');
});

test('a una que ya tiene con que excavar no se le pide nada', async () => {
  let pedidos = 0;
  const cliente = clienteCon([
    chatDe('573001112233@c.us', {
      fetchMessages: async () => {
        pedidos += 1;
        return [mensaje()];
      },
    }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador(), {
    omitir: ['573001112233@c.us'],
  });

  assert.strictEqual(pedidos, 0, 'gastarla ahi se la quita a otra');
  assert.strictEqual(salida.metrics.fetch1_skipped, 1);
  assert.strictEqual(salida.chats[0].no_seed_reason, 'WEB_SKIPPED_ALREADY_RESOLVED');
});

test('las que solo ve Web van antes que las ya conocidas', async () => {
  const pedidas = [];
  const hacer = (jid) => {
    const chat = chatDe(jid, { timestamp: T });
    chat.fetchMessages = async () => {
      pedidas.push(jid);
      return [];
    };
    return chat;
  };
  const cliente = clienteCon([hacer('57300001@c.us'), hacer('57300002@c.us')]);

  await indice.inventarioCompleto(cliente, adaptador(), {
    conocidos: ['57300001@c.us'],
  });

  assert.strictEqual(pedidas[0], '57300002@c.us', 'la que Python no conoce');
});

// ---------------------------------------------------------------------------
// Lo que sale
// ---------------------------------------------------------------------------

test('la referencia lleva lo que la peticion necesita', async () => {
  const cliente = clienteCon([
    chatDe('573001112233@c.us', { lastMessage: mensaje({ id: { id: WAMID, fromMe: true } }) }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());
  const candidato = salida.chats[0].candidate;

  assert.strictEqual(candidato.wa_msg_id, WAMID);
  assert.strictEqual(candidato.timestamp, T);
  assert.strictEqual(candidato.from_me, true);
  assert.strictEqual(candidato.chat_jid, '573001112233@c.us');
});

test('no hace falta _serialized: el WAMID vive en id.id', async () => {
  const cliente = clienteCon([
    chatDe('573001112233@c.us', {
      lastMessage: { id: { id: WAMID, fromMe: false }, t: T },
    }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());
  assert.strictEqual(salida.chats[0].candidate.wa_msg_id, WAMID);
});

test('la direccion puede venir en id.fromMe', async () => {
  const cliente = clienteCon([
    chatDe('573001112233@c.us', {
      lastMessage: { id: { id: WAMID, fromMe: true }, t: T },
    }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());
  assert.strictEqual(salida.chats[0].candidate.from_me, true);
});

test('la marca de tiempo va en SEGUNDOS, y en milisegundos se rechaza', async () => {
  const cliente = clienteCon([
    chatDe('573001112233@c.us', {
      lastMessage: mensaje({ t: T * 1000 }),
      fetchMessages: async () => [],
    }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());
  // No se divide por mil: adivinar la unidad produce un cursor que el
  // servidor confirma y nunca responde.
  assert.strictEqual(salida.chats[0].candidate, null);
});

test('una imagen sirve de referencia igual que un texto', async () => {
  // Para una referencia no hace falta cuerpo: hace falta identificador,
  // marca, direccion y conversacion.
  const cliente = clienteCon([
    chatDe('573001112233@c.us', { lastMessage: mensaje({ type: 'image' }) }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());

  assert.strictEqual(salida.chats[0].candidate.wa_msg_id, WAMID);
  assert.strictEqual(salida.chats[0].candidate.message_type, 'image');
});

test('un mensaje sin identificador real no produce referencia', async () => {
  const cliente = clienteCon([
    chatDe('573001112233@c.us', {
      lastMessage: mensaje({ id: { id: 'temp-abc', fromMe: false } }),
    }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());

  assert.strictEqual(salida.chats[0].candidate, null);
  assert.ok(Object.keys(salida.rejections).length > 0, 'y se dice por que');
});

test('una conversacion sin nada se devuelve igual: existe', async () => {
  // Python tiene que saber que esta ahi aunque no se pueda excavar todavia.
  const cliente = clienteCon([chatDe('573001112233@c.us')]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());

  assert.strictEqual(salida.chats.length, 1);
  assert.strictEqual(salida.chats[0].candidate, null);
  assert.strictEqual(salida.metrics.sin_candidato, 1);
});

test('un fallo pidiendo a la red no tumba el inventario', async () => {
  const cliente = clienteCon([
    chatDe('573001112233@c.us', {
      fetchMessages: async () => {
        throw new Error('sin red');
      },
    }),
    chatDe('573004445566@c.us', { lastMessage: mensaje() }),
  ]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());

  assert.strictEqual(salida.metrics.total, 2);
  assert.strictEqual(salida.metrics.fetch1_error, 1);
  assert.strictEqual(salida.metrics.valid_seeds, 1);
});

// ---------------------------------------------------------------------------
// Privacidad del detalle
// ---------------------------------------------------------------------------

test('el detalle por conversacion no lleva un telefono entero', async () => {
  const cliente = clienteCon([chatDe('573001112233@c.us', { lastMessage: mensaje() })]);
  const salida = await indice.inventarioCompleto(cliente, adaptador(), { debug: true });

  assert.strictEqual(salida.per_chat.length, 1);
  assert.ok(!salida.per_chat[0].chat.includes('1112233'));
  assert.ok(salida.per_chat[0].chat.includes('***'));
});

test('sin pedirlo, no hay detalle por conversacion', async () => {
  const cliente = clienteCon([chatDe('573001112233@c.us', { lastMessage: mensaje() })]);
  const salida = await indice.inventarioCompleto(cliente, adaptador());
  assert.strictEqual(salida.per_chat, undefined);
});
