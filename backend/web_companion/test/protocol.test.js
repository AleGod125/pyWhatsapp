'use strict';
/**
 * El canal entre Python y el worker.
 *
 * Todo lo que se prueba aqui es logica pura: no arranca Chromium ni necesita
 * una sesion. Si estas pruebas exigieran un navegador, nadie las correria.
 */

const test = require('node:test');
const assert = require('node:assert');
const { encode, LineReader, parseCommand } = require('../protocol');

test('un evento ocupa exactamente una linea', () => {
  const linea = encode({ event: 'ready', n: 1 });
  assert.strictEqual(linea.endsWith('\n'), true);
  assert.strictEqual(linea.trimEnd().includes('\n'), false);
});

test('un texto con salto de linea no parte el evento en dos', () => {
  // Si esto fallara, un nombre con salto de linea rompeeria el canal entero.
  const linea = encode({ event: 'x', name: 'hola\nadios' });
  assert.strictEqual(linea.split('\n').length, 2);
  assert.strictEqual(JSON.parse(linea).name, 'hola\nadios');
});

test('un comando partido en dos trozos se reconstruye', () => {
  // stdin llega troceado como quiere el sistema, no por lineas.
  const lector = new LineReader();
  assert.deepStrictEqual(lector.push('{"cmd":"sta'), []);
  assert.deepStrictEqual(lector.push('tus"}\n'), ['{"cmd":"status"}']);
});

test('dos comandos en el mismo trozo se atienden los dos', () => {
  const lector = new LineReader();
  const lineas = lector.push('{"cmd":"a"}\n{"cmd":"b"}\n');
  assert.deepStrictEqual(lineas, ['{"cmd":"a"}', '{"cmd":"b"}']);
});

test('una linea a medias espera al siguiente trozo', () => {
  const lector = new LineReader();
  assert.deepStrictEqual(lector.push('{"cmd":"a"}\n{"cmd":'), ['{"cmd":"a"}']);
  assert.deepStrictEqual(lector.push('"b"}\n'), ['{"cmd":"b"}']);
});

test('las lineas vacias se ignoran', () => {
  assert.deepStrictEqual(new LineReader().push('\n\n  \n'), []);
});

test('un JSON roto se rechaza sin lanzar', () => {
  assert.deepStrictEqual(parseCommand('{roto'), { error: 'json_invalido' });
});

test('un JSON valido que no es un objeto se rechaza', () => {
  assert.deepStrictEqual(parseCommand('[1,2]'), { error: 'no_es_un_objeto' });
  assert.deepStrictEqual(parseCommand('null'), { error: 'no_es_un_objeto' });
  assert.deepStrictEqual(parseCommand('"hola"'), { error: 'no_es_un_objeto' });
});

test('un objeto sin cmd se rechaza', () => {
  assert.deepStrictEqual(parseCommand('{"foo":1}'), { error: 'sin_cmd' });
});

test('un comando valido se devuelve entero', () => {
  const { command } = parseCommand('{"cmd":"inventory","id":7}');
  assert.strictEqual(command.cmd, 'inventory');
  assert.strictEqual(command.id, 7);
});
