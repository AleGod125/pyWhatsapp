'use strict';

const test = require('node:test');
const assert = require('node:assert');
const adapter = require('../store_adapter');

function pageWith(windowObject) {
  return {
    async evaluate(fn, ...args) {
      const previous = global.window;
      global.window = windowObject;
      try { return await fn(...args); } finally { global.window = previous; }
    },
  };
}

function chat(id, messages = []) {
  return {
    id: { _serialized: id },
    msgs: { getModelsArray: () => messages },
    timestamp: 1760000000,
  };
}

test('Store ausente intenta WAWebCollections', async () => {
  const chats = [chat('573001112233@c.us')];
  const page = pageWith({
    WWebJS: { getChats() {} },
    require: (name) => name === 'WAWebCollections' ? { Chat: { getModelsArray: () => chats } } : {},
  });
  const result = await adapter.descubrir(page);
  assert.strictEqual(result.capabilities.window_store, false);
  assert.strictEqual(result.capabilities.store_chat_models, true);
  assert.strictEqual(result.diagnostics.strategy, 'window.require(WAWebCollections)');
});

test('Store retrasado cambia store_ready sin confundir client ready', async () => {
  let attempts = 0;
  const page = pageWith({
    WWebJS: { getChats() {} },
    require: (name) => {
      if (name !== 'WAWebCollections') return {};
      attempts += 1;
      return attempts < 2 ? {} : { Chat: { getModelsArray: () => [] } };
    },
  });
  const result = await adapter.esperarStore(page, { timeoutMs: 100, pollMs: 1 });
  assert.strictEqual(result.capabilities.window_wwebjs, true);
  assert.strictEqual(result.store_ready, true);
});

test('client ready no implica Store ready', async () => {
  const result = await adapter.descubrir(pageWith({ WWebJS: { getChats() {} } }));
  assert.strictEqual(result.capabilities.client_get_chats, true);
  assert.strictEqual(result.capabilities.store_chat_models, false);
});

test('runtime webpack desconocido no rompe', async () => {
  const result = await adapter.descubrir(pageWith({ webpackChunkNuevo: [] }));
  assert.strictEqual(result.capabilities.store_chat, false);
  assert.deepStrictEqual(result.diagnostics.webpack_runtime_keys, ['webpackChunkNuevo']);
});

test('inventario directo usa WAWebCollections y no devuelve cuerpos', async () => {
  const models = [chat('573001112233@c.us', [{ body: 'SECRETO' }])];
  const rows = await adapter.chatsDelStore(pageWith({
    require: (name) => name === 'WAWebCollections' ? { Chat: { getModelsArray: () => models } } : {},
  }));
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].msgs_in_memory, 1);
  assert.strictEqual(JSON.stringify(rows).includes('SECRETO'), false);
});

test('probe devuelve sólo metadata mínima y no ejecuta loaders', async () => {
  let loaderCalls = 0;
  const model = chat('573001112233@c.us', [{
    id: { id: 'ABCDEF0123456789', fromMe: false }, t: 1760000000, body: 'SECRETO',
  }]);
  model.loadEarlierMsgs = () => { loaderCalls += 1; };
  const collection = { getModelsArray: () => [model], get: () => model };
  const result = await adapter.mensajesEnMemoria(pageWith({
    require: (name) => name === 'WAWebCollections' ? { Chat: collection } : {},
  }), model.id._serialized, 1);
  assert.strictEqual(result.mensajes.length, 1);
  assert.strictEqual(JSON.stringify(result).includes('SECRETO'), false);
  assert.strictEqual(loaderCalls, 0);
  assert.deepStrictEqual(await adapter.cargarAnteriores(), { ok: false, motivo: 'read_only_loader_disabled' });
});
