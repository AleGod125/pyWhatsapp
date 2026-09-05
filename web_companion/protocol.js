'use strict';
/**
 * El protocolo entre Python y este worker: JSON Lines por stdin/stdout.
 *
 * POR QUE NO OTRO SERVIDOR HTTP
 * -----------------------------
 * Un puerto mas es un puerto mas que asegurar, que documentar y que chocar con
 * algo. Aqui el padre ya es dueno del proceso hijo, asi que sus tuberias son
 * el canal natural: se cierran solas cuando el proceso muere y nadie mas puede
 * hablar por ellas.
 *
 * LA REGLA DE STDOUT
 * ------------------
 * stdout es SOLO protocolo: una linea, un JSON, un evento. Cualquier cosa
 * legible para humanos va a stderr. Un `console.log` de mas en stdout rompe el
 * canal, y por eso el worker redefine `console.log` al arrancar.
 *
 * NADA DE CONTENIDO
 * -----------------
 * Por aqui no viaja texto de mensajes ni multimedia. Solo metadatos: a que
 * conversacion pertenece, que identificador tiene y de cuando es.
 */

/** Un evento no se parte en dos lineas ni lleva saltos dentro. */
function encode(event) {
  return JSON.stringify(event) + '\n';
}

/**
 * Trocea un flujo en lineas completas.
 *
 * Existe porque stdin llega en trozos arbitrarios: un comando puede partirse
 * en dos `data` y dos comandos pueden llegar juntos. Interpretar cada trozo
 * como si fuera una linea entera es un fallo silencioso, y aparece justo
 * cuando el mensaje crece.
 */
class LineReader {
  constructor() {
    this.buffer = '';
  }

  /** Devuelve las lineas COMPLETAS que haya en el trozo. */
  push(chunk) {
    this.buffer += String(chunk);
    const lines = this.buffer.split('\n');
    // La ultima puede estar a medias: se queda para el proximo trozo.
    this.buffer = lines.pop() ?? '';
    return lines.map((line) => line.trim()).filter(Boolean);
  }
}

/**
 * Interpreta una linea como comando.
 *
 * Nunca lanza: una linea rota no puede tumbar el worker. Devuelve un error
 * descriptivo para poder contestarlo por el mismo canal.
 */
function parseCommand(line) {
  let parsed;
  try {
    parsed = JSON.parse(line);
  } catch {
    return { error: 'json_invalido' };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { error: 'no_es_un_objeto' };
  }
  if (typeof parsed.cmd !== 'string' || !parsed.cmd) {
    return { error: 'sin_cmd' };
  }
  return { command: parsed };
}

module.exports = { encode, LineReader, parseCommand };
