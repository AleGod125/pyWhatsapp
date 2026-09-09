'use strict';
/**
 * La traduccion Baileys -> eventos del contrato.
 *
 * Estas pruebas son la red que sostiene la migracion entera: si una carga
 * cambia de forma, la ingesta de Python deja de encontrar lo que busca y no
 * da error -- da mensajes que faltan. Aqui se comprueba sin socket, sin red y
 * sin sesion, porque `traducir.js` no importa Baileys a proposito.
 */

const test = require('node:test');
const assert = require('node:assert');

const t = require('../traducir');

/** Serializador de mentira: devuelve bytes reconocibles. */
const serializar = (info) => Buffer.from(JSON.stringify(info || {}), 'utf8');

// ---------------------------------------------------------------------------
// JIDs
// ---------------------------------------------------------------------------

test('el dispositivo se quita del JID: no identifica la conversacion', () => {
  assert.equal(t.jidTexto('573001234567:12@s.whatsapp.net'), '573001234567@s.whatsapp.net');
});

test('un @lid NO se convierte en telefono', () => {
  // Son espacios de identificadores distintos; mezclarlos corrompe los datos.
  assert.equal(t.jidTexto('86531142340710@lid'), '86531142340710@lid');
});

test('un JID vacio es null, no una cadena vacia', () => {
  assert.equal(t.jidTexto(''), null);
  assert.equal(t.jidTexto(null), null);
  assert.equal(t.jidTexto(undefined), null);
});

// ---------------------------------------------------------------------------
// El par PN <-> LID (C-23): sin esto el 98% del panel muestra numeros
// ---------------------------------------------------------------------------

test('se cosecha el par LID<->PN de la clave del mensaje', () => {
  const par = t.cosecharLid({
    remoteJid: '86531142340710@lid',
    remoteJidAlt: '573001234567@s.whatsapp.net',
  });
  assert.deepEqual(par, { lid: '86531142340710@lid', pn: '573001234567@s.whatsapp.net' });
});

test('tambien desde el participante de un grupo', () => {
  const par = t.cosecharLid({
    remoteJid: '1203630@g.us',
    participant: '573009999999@s.whatsapp.net',
    participantAlt: '99887766@lid',
  });
  assert.deepEqual(par, { lid: '99887766@lid', pn: '573009999999@s.whatsapp.net' });
});

test('sin los dos lados no se inventa un par', () => {
  assert.equal(t.cosecharLid({ remoteJid: '573001234567@s.whatsapp.net' }), null);
  assert.equal(t.cosecharLid(null), null);
});

// ---------------------------------------------------------------------------
// Mensaje en vivo
// ---------------------------------------------------------------------------

test('el mensaje lleva el WebMessageInfo EN CRUDO', () => {
  // Es la decision que hace quirurgica la migracion: Python sigue usando su
  // propio parser, asi que la ingesta no cambia ni una linea.
  const carga = t.traducirMensaje(
    { key: { remoteJid: '573001234567@s.whatsapp.net', id: 'ABC', fromMe: false }, messageTimestamp: 1700000000 },
    serializar,
  );
  assert.ok(carga.raw_proto, 'sin los bytes Python no puede clasificar por protobuf');
  assert.equal(Buffer.from(carga.raw_proto, 'base64').length > 0, true);
});

test('los campos son los que consume la ingesta', () => {
  const carga = t.traducirMensaje(
    {
      key: { remoteJid: '573001234567@s.whatsapp.net', id: 'ABC', fromMe: true },
      messageTimestamp: 1700000000,
      pushName: 'Marta',
    },
    serializar,
  );
  assert.equal(carga.id, 'ABC');
  assert.equal(carga.chat, '573001234567@s.whatsapp.net');
  assert.equal(carga.from_me, true);
  assert.equal(carga.timestamp, 1700000000);
  assert.equal(carga.push_name, 'Marta');
});

test('en un grupo el remitente es el participante, no el chat', () => {
  const carga = t.traducirMensaje(
    {
      key: { remoteJid: '1203630@g.us', participant: '573009999999@s.whatsapp.net', id: 'X' },
      messageTimestamp: 1,
    },
    serializar,
  );
  assert.equal(carga.chat, '1203630@g.us');
  assert.equal(carga.sender, '573009999999@s.whatsapp.net');
});

test('un mensaje sin chat se descarta, no se inventa uno', () => {
  assert.equal(t.traducirMensaje({ key: { id: 'X' } }, serializar), null);
});

test('si no se puede serializar, el mensaje sigue viajando sin los bytes', () => {
  const roto = () => { throw new Error('no se puede'); };
  const carga = t.traducirMensaje(
    { key: { remoteJid: 'a@s.whatsapp.net', id: 'X' }, messageTimestamp: 1 },
    roto,
  );
  assert.equal(carga.raw_proto, null);
  assert.equal(carga.chat, 'a@s.whatsapp.net');
});

// ---------------------------------------------------------------------------
// El marcador de fin (C-10) — compuerta G2
// ---------------------------------------------------------------------------

test('el marcador de fin viaja como NUMERO, no como booleano', () => {
  // El 0 significa "completo PERO queda mas en el principal" y el 1 "no queda
  // nada". Colapsarlos fue lo que dio por terminadas conversaciones que aun
  // tenian historial.
  assert.deepEqual(t.marcadorDeFin({ endOfHistoryTransferType: 0 }), { tipo: 0, terminado: false });
  assert.deepEqual(t.marcadorDeFin({ endOfHistoryTransferType: 1 }), { tipo: 1, terminado: true });
});

test('se lee tambien el nombre alternativo del campo', () => {
  // Baileys lo ha movido entre versiones.
  assert.deepEqual(t.marcadorDeFin({ endOfHistoryTransfer: 1 }), { tipo: 1, terminado: true });
});

test('sin marcador es "no lo se", NUNCA "terminado"', () => {
  assert.deepEqual(t.marcadorDeFin({}), { tipo: null, terminado: false });
  assert.deepEqual(t.marcadorDeFin({ endOfHistoryTransferType: null }), { tipo: null, terminado: false });
});

// ---------------------------------------------------------------------------
// Lote de historial
// ---------------------------------------------------------------------------

test('el lote agrupa los mensajes por conversacion', () => {
  const carga = t.traducirHistorial(
    {
      syncType: 6,
      chats: [{ id: 'a@s.whatsapp.net', name: 'Marta', conversationTimestamp: 1700000000 }],
      messages: [
        { key: { remoteJid: 'a@s.whatsapp.net', id: '1' } },
        { key: { remoteJid: 'a@s.whatsapp.net', id: '2' } },
      ],
    },
    serializar,
  );
  assert.equal(carga.sync_type, 'ON_DEMAND');
  assert.equal(carga.conversations.length, 1);
  assert.equal(carga.conversations[0].messages.length, 2);
  assert.equal(carga.conversations[0].name, 'Marta');
});

test('una conversacion con mensajes pero sin metadatos NO se pierde', () => {
  const carga = t.traducirHistorial(
    { syncType: 2, chats: [], messages: [{ key: { remoteJid: 'b@s.whatsapp.net', id: '1' } }] },
    serializar,
  );
  assert.equal(carga.conversations.length, 1);
  assert.equal(carga.conversations[0].jid, 'b@s.whatsapp.net');
});

test('el tipo de sync se traduce al nombre que ya entiende Python', () => {
  assert.equal(t.nombreDeSync(0), 'INITIAL_BOOTSTRAP');
  assert.equal(t.nombreDeSync(6), 'ON_DEMAND');
  assert.equal(t.nombreDeSync('ON_DEMAND'), 'ON_DEMAND');
});

test('un tipo desconocido se dice, no se traga', () => {
  assert.equal(t.nombreDeSync(99), 'DESCONOCIDO_99');
});

test('los nombres publicos salen del lote', () => {
  assert.deepEqual(
    t.pushnames([{ id: 'a@s.whatsapp.net', notify: 'Marta' }, { id: 'b@s.whatsapp.net' }]),
    [['a@s.whatsapp.net', 'Marta']],
  );
});

// ---------------------------------------------------------------------------
// messages.update: editar y borrar llegan por el MISMO sitio
// ---------------------------------------------------------------------------

test('un borrado se distingue de una edicion', () => {
  assert.equal(t.clasificarActualizacion({ update: { messageStubType: 1 } }), 'message_revoke');
  assert.equal(t.clasificarActualizacion({ update: { message: {} } }), 'message_edit');
});

test('una actualizacion que no es ni una cosa ni la otra no emite nada', () => {
  // Baileys manda por aqui tambien cambios de estado de entrega.
  assert.equal(t.clasificarActualizacion({ update: { status: 3 } }), null);
});

// ---------------------------------------------------------------------------
// Contactos
// ---------------------------------------------------------------------------

test('el contacto separa nombre de agenda y nombre publico', () => {
  const c = t.traducirContacto({ id: 'a@s.whatsapp.net', name: 'Marta Ruiz', notify: 'Marta' });
  assert.equal(c.full_name, 'Marta Ruiz');
  assert.equal(c.push_name, 'Marta');
});

test('el LID del contacto se conserva', () => {
  const c = t.traducirContacto({ id: 'a@s.whatsapp.net', lid: '99887766@lid' });
  assert.equal(c.lid, '99887766@lid');
});

test('un contacto sin identificador se descarta', () => {
  assert.equal(t.traducirContacto({ name: 'Sin JID' }), null);
});

// ---------------------------------------------------------------------------
// La agenda: de donde salen los NOMBRES
// ---------------------------------------------------------------------------

test('el `id` de un contacto puede venir en LID: el par no se lee al reves', () => {
  // El tipo de Baileys lo dice: "ID either in lid or jid format". Leerlo
  // siempre como telefono dejaba el par vacio, y sin par no hay nombre.
  const c = t.traducirContacto({
    id: '82025587417265@lid',
    jid: '573001234567@s.whatsapp.net',
    name: 'Isaac',
  });
  assert.equal(c.jid, '573001234567@s.whatsapp.net');
  assert.equal(c.lid, '82025587417265@lid');
  assert.equal(c.full_name, 'Isaac');
});

test('y tambien en telefono, con el LID al otro lado', () => {
  const c = t.traducirContacto({
    id: '573001234567@s.whatsapp.net',
    lid: '82025587417265@lid',
    notify: 'Isa',
  });
  assert.equal(c.jid, '573001234567@s.whatsapp.net');
  assert.equal(c.lid, '82025587417265@lid');
  assert.equal(c.push_name, 'Isa');
});

test('el nombre de la AGENDA y el PUBLICO no se mezclan', () => {
  const c = t.traducirContacto({
    id: '573001234567@s.whatsapp.net',
    name: 'Mamá',
    notify: 'Rosa M.',
  });
  assert.equal(c.full_name, 'Mamá', 'el de la agenda manda para el nombre');
  assert.equal(c.push_name, 'Rosa M.', 'el publico se guarda aparte');
});

test('un contacto solo con LID se descarta: no habria con que emparejarlo', () => {
  assert.equal(t.traducirContacto({ id: '82025587417265@lid', name: 'X' }), null);
});

test('la agenda del lote se traduce entera, y se tiran los vacios', () => {
  const lista = t.contactosDelLote([
    { id: '573001234567@s.whatsapp.net', name: 'Ana' },
    { id: '573009999999@s.whatsapp.net' }, // sin nombre ni lid: no aporta
    { id: '82025587417265@lid', jid: '573007777777@s.whatsapp.net', notify: 'Beto' },
  ]);
  assert.equal(lista.length, 2);
  assert.deepEqual(
    lista.map((c) => c.jid),
    ['573001234567@s.whatsapp.net', '573007777777@s.whatsapp.net'],
  );
});

test('el historial cosecha los pares de SUS mensajes, no solo los de en vivo', () => {
  // 5452 mensajes de historial pasaron sin que se guardara un solo par: el
  // cosechador solo corria sobre los mensajes en vivo.
  const lote = {
    chats: [],
    contacts: [],
    messages: [
      {
        key: {
          remoteJid: '82025587417265@lid',
          remoteJidAlt: '573001234567@s.whatsapp.net',
          id: 'A1',
        },
      },
    ],
  };
  const carga = t.traducirHistorial(lote, serializar);
  assert.deepEqual(carga.lid_pares, [
    { lid: '82025587417265@lid', pn: '573001234567@s.whatsapp.net' },
  ]);
});
