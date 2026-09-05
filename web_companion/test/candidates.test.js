'use strict';
/**
 * Que se propone como referencia y que no.
 *
 * LA REGLA QUE NO SE ROMPE
 * ------------------------
 * No se fabrica un identificador ni una marca de tiempo. Una referencia
 * inventada recibe confirmacion del servidor y despues silencio, y eso es lo
 * mas caro de diagnosticar que tiene este proyecto. Casi todas estas pruebas
 * comprueban RECHAZOS.
 */

const test = require('node:test');
const assert = require('node:assert');
const {
  serializar,
  normalizarJid,
  esConversacion,
  segundos,
  desdeMensaje,
  masAntiguo,
  unirInventario,
  compararInventario,
} = require('../candidates');

const CHAT = '573001112233@c.us';

// --- Identificadores --------------------------------------------------------

test('un Wid con _serialized se lee', () => {
  assert.strictEqual(serializar({ _serialized: CHAT }), CHAT);
});

test('un objeto sin nada util no se convierte en un JID falso', () => {
  // `String({})` da "[object Object]": eso NO puede pasar por un chat.
  assert.strictEqual(serializar({}), null);
  assert.strictEqual(normalizarJid({}), null);
});

test('el sufijo de dispositivo no crea una conversacion nueva', () => {
  // `573001112233:12@c.us` es el mismo chat visto desde otro aparato.
  assert.strictEqual(normalizarJid('573001112233:12@c.us'), CHAT);
});

test('los estados y las difusiones no son conversaciones', () => {
  assert.strictEqual(esConversacion('status@broadcast'), false);
  assert.strictEqual(esConversacion('x@broadcast'), false);
  assert.strictEqual(esConversacion('x@newsletter'), false);
  assert.strictEqual(esConversacion(CHAT), true);
  assert.strictEqual(esConversacion('120363@g.us'), true);
});

// --- Marcas de tiempo -------------------------------------------------------

test('una marca en segundos se acepta', () => {
  assert.strictEqual(segundos(1760000000), 1760000000);
});

test('una marca en MILISEGUNDOS se rechaza, no se convierte', () => {
  // Dividir por mil es adivinar la unidad. Equivocarse produce un cursor que
  // el servidor confirma y nunca responde.
  assert.strictEqual(segundos(1760000000000), null);
});

test('cero, negativo y basura se rechazan', () => {
  for (const valor of [0, -1, null, undefined, '', 'ayer', NaN]) {
    assert.strictEqual(segundos(valor), null, `${valor} deberia rechazarse`);
  }
});

// --- Candidatos -------------------------------------------------------------

const MENSAJE = { id: { id: '3A1F8BDD4678EB6DE395', fromMe: false }, t: 1760000000 };

test('un mensaje real se acepta', () => {
  const { candidato } = desdeMensaje(MENSAJE, { chatJid: CHAT, source: 'web_store' });
  assert.deepStrictEqual(candidato, {
    chat_jid: CHAT,
    wa_msg_id: '3A1F8BDD4678EB6DE395',
    timestamp: 1760000000,
    from_me: false,
    source: 'web_store',
    // El tipo viaja para que Python aplique SU filtro de protocolo.
    message_type: null,
  });
});

test('se usa id.id, NO id._serialized', () => {
  // `_serialized` es `false_<chat>_<id>`: lleva pegados el chat y la
  // direccion, y no es lo que ON_DEMAND espera como ancla.
  const { candidato } = desdeMensaje(
    {
      // `fromMe` hace falta: sin el no se puede saber la direccion, y esa
      // viaja en la peticion ON_DEMAND.
      id: { id: 'ABCDEF0123456789', fromMe: false, _serialized: `false_${CHAT}_ABCDEF0123456789` },
      t: 1760000000,
    },
    { chatJid: CHAT, source: 'web_store' },
  );
  assert.strictEqual(candidato.wa_msg_id, 'ABCDEF0123456789');
});

test('un mensaje sin identificador NO es candidato', () => {
  const r = desdeMensaje({ id: {}, t: 1760000000 }, { chatJid: CHAT, source: 'web_store' });
  assert.strictEqual(r.rechazado, 'sin_id');
});

test('un mensaje sin marca de tiempo NO es candidato', () => {
  const r = desdeMensaje(
    { id: { id: 'ABCDEF0123456789', fromMe: false } },
    { chatJid: CHAT, source: 'web_store' },
  );
  assert.strictEqual(r.rechazado, 'sin_timestamp');
});

test('un mensaje de un estado NO es candidato', () => {
  const r = desdeMensaje(MENSAJE, { chatJid: 'status@broadcast', source: 'web_store' });
  assert.strictEqual(r.rechazado, 'chat_no_conversacion');
});

test('un modelo vacio no revienta', () => {
  assert.strictEqual(desdeMensaje(null, { chatJid: CHAT }).rechazado, 'sin_modelo');
});

test('un origen desconocido no se propaga tal cual', () => {
  const { candidato } = desdeMensaje(MENSAJE, { chatJid: CHAT, source: 'inventado' });
  assert.strictEqual(candidato.source, 'web_store');
});

test('se elige el candidato MAS ANTIGUO', () => {
  // Se excava hacia atras: lo que falta esta antes del mas antiguo conocido.
  const elegido = masAntiguo([
    { timestamp: 1760002800 },
    { timestamp: 1760000100 },
    { timestamp: 1760002000 },
  ]);
  assert.strictEqual(elegido.timestamp, 1760000100);
});

test('sin candidatos no se inventa uno', () => {
  assert.strictEqual(masAntiguo([]), null);
  assert.strictEqual(masAntiguo([null, undefined]), null);
});

// --- Inventario -------------------------------------------------------------

test('el mismo chat por las dos vias se cuenta UNA vez', () => {
  const union = unirInventario([{ id: CHAT, name: 'A' }], [{ id: CHAT, msgs_in_memory: 5 }]);
  assert.strictEqual(union.length, 1);
  assert.deepStrictEqual(union[0].sources, ['get_chats', 'store']);
});

test('un chat que solo ve el Store se conserva y se marca', () => {
  const otro = '120363000000000000@g.us';
  const union = unirInventario([{ id: CHAT }], [{ id: otro, msgs_in_memory: 2 }]);
  assert.strictEqual(union.length, 2);
  const soloStore = union.find((c) => c.chat_jid === otro);
  assert.deepStrictEqual(soloStore.sources, ['store']);
  assert.strictEqual(soloStore.is_group, true);
});

test('los estados no entran en el inventario', () => {
  const union = unirInventario([{ id: 'status@broadcast' }, { id: CHAT }], []);
  assert.deepStrictEqual(
    union.map((c) => c.chat_jid),
    [CHAT],
  );
});

test('las metricas dicen que aporta Web y que le falta', () => {
  const otro = '120363000000000000@g.us';
  const union = unirInventario([{ id: CHAT }], [{ id: otro }]);
  const m = compararInventario(union, [CHAT, '573009998877@c.us']);

  assert.strictEqual(m.python_chats, 2);
  assert.strictEqual(m.union_chats, 2);
  assert.strictEqual(m.extra_vs_python, 1, 'el grupo que Python no conoce');
  assert.strictEqual(m.missing_vs_python, 1, 'el chat que Web no ve');
  assert.strictEqual(m.individual, 1);
  assert.strictEqual(m.group, 1);
});

test('sin nada por ninguna via las metricas son ceros, no errores', () => {
  const m = compararInventario([], []);
  assert.strictEqual(m.union_chats, 0);
  assert.strictEqual(m.extra_vs_python, 0);
  assert.strictEqual(m.missing_vs_python, 0);
});

test('getChats=10 Store.Chat=15 overlap=8 produce union=17', () => {
  const getChats = Array.from({ length: 10 }, (_, i) => ({ id: `5730000000${i}@c.us` }));
  const store = Array.from({ length: 15 }, (_, i) => ({
    id: i < 8 ? `5730000000${i}@c.us` : `1203630000000000${i}@g.us`,
  }));
  const union = unirInventario(getChats, store);
  const metrics = compararInventario(union, []);
  assert.strictEqual(metrics.web_get_chats, 10);
  assert.strictEqual(metrics.web_store_chats, 15);
  assert.strictEqual(metrics.union_chats, 17);
});
