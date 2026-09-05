'use strict';
/**
 * De un mensaje real del Store a una referencia utilizable.
 *
 * EL FALLO QUE FIJAN ESTAS PRUEBAS
 * --------------------------------
 * 22 conversaciones con mensajes materializados daban CERO candidatos. No era
 * que WhatsApp no tuviera datos: el adaptador aplanaba `id.id` a una cadena y
 * el normalizador esperaba el objeto `{id: {...}}` del Store, asi que
 * `typeof clave === 'object'` era falso y TODO se rechazaba por `sin_id`.
 *
 * Un desajuste de contrato entre dos modulos, invisible en los numeros. Por
 * eso aqui se prueban LAS DOS formas: si una se rompe, se ve.
 *
 * LAS FORMAS SON REALES
 * ---------------------
 * Medidas en la sesion vinculada, no inventadas:
 *   msg.id        -> object {fromMe, remote, id, participant?}
 *   msg.id.id     -> string hexadecimal de 20 o 32 caracteres
 *   msg.id._serialized -> NO EXISTE (null)
 *   msg.t         -> number de 10 digitos (segundos)
 *   msg.timestamp -> undefined
 *   msg.fromMe    -> undefined (solo vive en id.fromMe)
 */

const test = require('node:test');
const assert = require('node:assert');
const { RECHAZOS, extractSeedCandidate, clasificarJid, contarPorClase } = require('../candidates');

const DIRECTO = '573001112233@c.us';
const GRUPO = '120363000000000000@g.us';
const WAMID_32 = 'AC7B0102030405060708090A0B0C24EB';
const WAMID_20 = '3A4901020304050607FA';
const T = 1760000000;

/** El modelo tal y como lo da el Store, con `id` como OBJETO. */
const delStore = (extra = {}, clave = {}) => ({
  id: { id: WAMID_32, fromMe: false, remote: { _serialized: DIRECTO }, ...clave },
  t: T,
  type: 'chat',
  ...extra,
});

/** Lo que devuelve el adaptador, ya aplanado. */
const delAdaptador = (extra = {}) => ({
  wa_msg_id: WAMID_32,
  t: T,
  from_me: false,
  remote: DIRECTO,
  type: 'chat',
  ...extra,
});

const opciones = { chatJid: DIRECTO, source: 'web_store' };

// ---------------------------------------------------------------------------
// Las dos formas, que es el fallo que hubo
// ---------------------------------------------------------------------------

test('el modelo crudo del Store da candidato', () => {
  const { candidato } = extractSeedCandidate(delStore(), opciones);
  assert.strictEqual(candidato.wa_msg_id, WAMID_32);
  assert.strictEqual(candidato.timestamp, T);
  assert.strictEqual(candidato.from_me, false);
});

test('la forma aplanada del adaptador da EL MISMO candidato', () => {
  // Si estas dos divergen, vuelve el bug de los 0 candidatos.
  const a = extractSeedCandidate(delStore(), opciones).candidato;
  const b = extractSeedCandidate(delAdaptador(), opciones).candidato;
  assert.deepStrictEqual(a, b);
});

test('un `id` que ya viene como cadena tambien vale', () => {
  const { candidato } = extractSeedCandidate({ id: WAMID_32, t: T, fromMe: true }, opciones);
  assert.strictEqual(candidato.wa_msg_id, WAMID_32);
});

// ---------------------------------------------------------------------------
// El identificador
// ---------------------------------------------------------------------------

test('se acepta el WAMID de 20 y el de 32 caracteres', () => {
  // Los dos se midieron en la sesion real.
  for (const wamid of [WAMID_20, WAMID_32]) {
    const { candidato } = extractSeedCandidate(delAdaptador({ wa_msg_id: wamid }), opciones);
    assert.strictEqual(candidato.wa_msg_id, wamid, `${wamid} deberia valer`);
  }
});

test('sin identificador se dice sin_id, no se inventa uno', () => {
  const r = extractSeedCandidate(delAdaptador({ wa_msg_id: null }), opciones);
  assert.strictEqual(r.rechazado, RECHAZOS.SIN_ID);
});

test('la forma serializada NO es el WAMID', () => {
  // `false_<chat>_<id>` lleva pegados el chat y la direccion; el servidor no
  // la reconoce como ancla.
  const r = extractSeedCandidate(
    delAdaptador({ wa_msg_id: `false_${DIRECTO}_${WAMID_32}` }),
    opciones,
  );
  assert.strictEqual(r.rechazado, RECHAZOS.ID_NO_REAL);
});

test('un identificador local del cliente no sirve de ancla', () => {
  for (const falso of ['opaque-1', 'temp-abc', 'local-9', 'fake-x']) {
    const r = extractSeedCandidate(delAdaptador({ wa_msg_id: falso }), opciones);
    assert.strictEqual(r.rechazado, RECHAZOS.ID_NO_REAL, falso);
  }
});

test('algo que no es hexadecimal no es un WAMID', () => {
  const r = extractSeedCandidate(delAdaptador({ wa_msg_id: 'no-es-un-wamid-zzz' }), opciones);
  assert.strictEqual(r.rechazado, RECHAZOS.ID_NO_REAL);
});

// ---------------------------------------------------------------------------
// La marca de tiempo
// ---------------------------------------------------------------------------

test('t en segundos se acepta', () => {
  assert.strictEqual(extractSeedCandidate(delStore(), opciones).candidato.timestamp, T);
});

test('timestamp tambien vale cuando es el que trae el modelo', () => {
  const { candidato } = extractSeedCandidate(
    { wa_msg_id: WAMID_32, timestamp: T, from_me: false },
    opciones,
  );
  assert.strictEqual(candidato.timestamp, T);
});

test('una marca en MILISEGUNDOS se rechaza, no se divide por mil', () => {
  // Adivinar la unidad produce un cursor que el servidor confirma y nunca
  // responde: el fallo mas caro de diagnosticar de este proyecto.
  const r = extractSeedCandidate(delAdaptador({ t: T * 1000 }), opciones);
  assert.strictEqual(r.rechazado, RECHAZOS.TIMESTAMP_UNIDAD_INVALIDA);
});

test('sin marca de tiempo se dice sin_timestamp', () => {
  const r = extractSeedCandidate({ wa_msg_id: WAMID_32, from_me: false }, opciones);
  assert.strictEqual(r.rechazado, RECHAZOS.SIN_TIMESTAMP);
});

// ---------------------------------------------------------------------------
// La direccion
// ---------------------------------------------------------------------------

test('from_me sale de id.fromMe, que es donde vive de verdad', () => {
  // Medido: `msg.fromMe` es undefined en el modelo real.
  const propio = extractSeedCandidate(delStore({}, { fromMe: true }), opciones).candidato;
  const ajeno = extractSeedCandidate(delStore({}, { fromMe: false }), opciones).candidato;
  assert.strictEqual(propio.from_me, true);
  assert.strictEqual(ajeno.from_me, false);
});

test('si no se puede saber la direccion NO se supone false', () => {
  // `oldestMsgFromMe` viaja en la peticion ON_DEMAND: equivocarse ahi cuesta
  // una peticion que no responde.
  const r = extractSeedCandidate({ wa_msg_id: WAMID_32, t: T }, opciones);
  assert.strictEqual(r.rechazado, RECHAZOS.FROM_ME_INDETERMINADO);
});

// ---------------------------------------------------------------------------
// La conversacion
// ---------------------------------------------------------------------------

test('en un grupo el ancla es del GRUPO, no del participante', () => {
  const mensaje = delStore({}, {
    remote: { _serialized: GRUPO },
    participant: { _serialized: '573009998877@c.us' },
  });
  const { candidato } = extractSeedCandidate(mensaje, { chatJid: GRUPO, source: 'web_store' });
  assert.strictEqual(candidato.chat_jid, GRUPO);
});

test('manda el chat contenedor sobre el remote del mensaje', () => {
  // Es el que Python pidio y el que ya resolvio a una conversacion suya.
  const mensaje = delStore({}, { remote: { _serialized: '999@lid' } });
  const { candidato } = extractSeedCandidate(mensaje, opciones);
  assert.strictEqual(candidato.chat_jid, DIRECTO);
});

test('sin contenedor se usa el remote del propio mensaje', () => {
  const mensaje = delStore({}, { remote: { _serialized: GRUPO } });
  const { candidato } = extractSeedCandidate(mensaje, { source: 'web_store' });
  assert.strictEqual(candidato.chat_jid, GRUPO);
});

test('un JID con forma de alias no se rechaza: lo resuelve Python', () => {
  // El Store puede usar LID donde Python usa telefono. Node propone.
  const { candidato } = extractSeedCandidate(delAdaptador(), {
    chatJid: '64940106866902@lid',
    source: 'web_store',
  });
  assert.strictEqual(candidato.chat_jid, '64940106866902@lid');
});

test('un estado o una difusion no es una conversacion', () => {
  for (const jid of ['status@broadcast', 'x@broadcast', 'y@newsletter']) {
    const r = extractSeedCandidate(delAdaptador(), { chatJid: jid, source: 'web_store' });
    assert.strictEqual(r.rechazado, RECHAZOS.CHAT_NO_CONVERSACION, jid);
  }
});

test('sin conversacion ninguna se dice sin_chat', () => {
  const r = extractSeedCandidate({ wa_msg_id: WAMID_32, t: T, from_me: false }, { source: 'web_store' });
  assert.strictEqual(r.rechazado, RECHAZOS.SIN_CHAT);
});

// ---------------------------------------------------------------------------
// Tipos de mensaje
// ---------------------------------------------------------------------------

test('sticker, imagen y audio SI sirven de ancla', () => {
  // Lo que descarta un ancla es ser senalizacion, no llevar contenido.
  for (const tipo of ['sticker', 'image', 'audio', 'video', 'document']) {
    const { candidato } = extractSeedCandidate(delAdaptador({ type: tipo }), opciones);
    assert.ok(candidato, tipo);
    assert.strictEqual(candidato.message_type, tipo);
  }
});

test('el tipo viaja para que lo filtre Python, no Node', () => {
  const { candidato } = extractSeedCandidate(delAdaptador({ type: 'protocol' }), opciones);
  // Node NO lo rechaza: manda el tipo y Python aplica su regla.
  assert.strictEqual(candidato.message_type, 'protocol');
});

test('un modelo vacio no revienta', () => {
  assert.strictEqual(extractSeedCandidate(null, opciones).rechazado, RECHAZOS.SIN_MODELO);
});

// ---------------------------------------------------------------------------
// Clasificacion de conversaciones
// ---------------------------------------------------------------------------

test('cada JID se clasifica por su servidor', () => {
  assert.strictEqual(clasificarJid(GRUPO), 'group');
  assert.strictEqual(clasificarJid(DIRECTO), 'individual');
  assert.strictEqual(clasificarJid('1@lid'), 'individual');
  assert.strictEqual(clasificarJid('1@newsletter'), 'newsletter');
  assert.strictEqual(clasificarJid('status@broadcast'), 'status');
  assert.strictEqual(clasificarJid('1@broadcast'), 'broadcast');
  assert.strictEqual(clasificarJid('sin-arroba'), 'unknown');
});

test('se cuenta cuantas hay de cada clase, sin exponer cuales', () => {
  const conteo = contarPorClase([GRUPO, DIRECTO, '2@lid', '3@newsletter']);
  assert.deepStrictEqual(conteo, { group: 1, individual: 2, newsletter: 1 });
});
