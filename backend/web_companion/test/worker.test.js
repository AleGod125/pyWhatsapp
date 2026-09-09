'use strict';
/**
 * El ciclo de vida del worker, sin arrancar Chromium.
 *
 * Se lanza el proceso de verdad y se le habla por sus tuberias, que es como
 * lo hara Python. Lo unico que se desactiva es el cliente de WhatsApp: lo que
 * se esta comprobando es el canal, no el navegador.
 */

const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');
const { spawn } = require('node:child_process');

const WORKER = path.join(__dirname, '..', 'worker.js');

/** Lanza el worker, le manda unas lineas y devuelve los eventos de stdout. */
function conversar(lineas, { esperar = 1 } = {}) {
  return new Promise((resolve, reject) => {
    const hijo = spawn(process.execPath, [WORKER], {
      env: { ...process.env, WEB_COMPANION_NO_CLIENT: 'true' },
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    const eventos = [];
    let pendiente = '';
    const plazo = setTimeout(() => {
      hijo.kill();
      reject(new Error(`el worker no contesto a tiempo (${eventos.length} eventos)`));
    }, 10000);

    hijo.stdout.setEncoding('utf8');
    hijo.stdout.on('data', (trozo) => {
      pendiente += trozo;
      const partes = pendiente.split('\n');
      pendiente = partes.pop();
      for (const linea of partes) {
        if (!linea.trim()) continue;
        eventos.push(JSON.parse(linea));
      }
      if (eventos.length >= esperar) {
        clearTimeout(plazo);
        hijo.stdin.end();
        hijo.kill();
        resolve(eventos);
      }
    });
    hijo.on('error', reject);

    for (const linea of lineas) hijo.stdin.write(linea + '\n');
  });
}

test('lo PRIMERO que dice es que ha arrancado', async () => {
  const eventos = await conversar([], { esperar: 1 });
  assert.strictEqual(eventos[0].event, 'starting');
  assert.ok(Number.isInteger(eventos[0].pid));
});

test('stdout lleva SOLO JSON, una linea por evento', async () => {
  // Si algo escribiera texto suelto en stdout, JSON.parse habria reventado ya
  // dentro de `conversar`. Llegar aqui ya es la comprobacion.
  const eventos = await conversar(['{"cmd":"status"}'], { esperar: 3 });
  for (const evento of eventos) assert.strictEqual(typeof evento, 'object');
});

test('un comando desconocido se contesta, no tumba el worker', async () => {
  const eventos = await conversar(['{"cmd":"no_existe","id":9}'], { esperar: 3 });
  const respuesta = eventos.find((e) => e.error === 'comando_desconocido');
  assert.ok(respuesta, 'deberia contestar que no lo conoce');
  assert.strictEqual(respuesta.id, 9, 'y devolver el id para poder casarlo');
});

test('una linea rota se contesta y se sigue atendiendo', async () => {
  const eventos = await conversar(['{roto', '{"cmd":"status","id":1}'], { esperar: 4 });
  assert.ok(eventos.find((e) => e.error === 'json_invalido'));
  assert.ok(
    eventos.find((e) => e.event === 'status'),
    'el comando siguiente sigue funcionando',
  );
});

test('sin cliente, inventario y sondeo dicen que no estan listos', async () => {
  const eventos = await conversar(['{"cmd":"inventory","id":2}'], { esperar: 3 });
  const respuesta = eventos.find((e) => e.error === 'no_listo');
  assert.ok(respuesta, 'no se inventa un inventario vacio: se dice que no puede');
});

test('el estado se puede consultar en cualquier momento', async () => {
  const eventos = await conversar(['{"cmd":"status"}'], { esperar: 3 });
  const estado = eventos.find((e) => e.event === 'status');
  assert.ok(estado);
  assert.ok('state' in estado && 'settings' in estado);
});
