'use strict';
/**
 * El canal entre Python y este worker: JSON Lines por stdin/stdout.
 *
 * Es EL MISMO protocolo que usa `web_companion/`, a proposito: ya esta
 * probado en produccion y el supervisor de alla sirve de plantilla directa.
 *
 * LA REGLA DE STDOUT
 * ------------------
 * stdout es SOLO protocolo: una linea, un JSON, un evento. Cualquier cosa
 * legible para humanos va a stderr. Un `console.log` de mas en stdout rompe el
 * canal, y por eso el worker redefine `console.log` al arrancar.
 *
 * QUE VIAJA POR AQUI
 * ------------------
 * A diferencia del companion, aqui SI viaja contenido: este worker es la capa
 * de WhatsApp entera, y los mensajes tienen que llegar a PostgreSQL. Lo que no
 * viaja son los binarios grandes: un adjunto se descarga bajo peticion y se
 * escribe a disco desde Node, no se mete en una linea JSON.
 */

/** Un evento no se parte en dos lineas ni lleva saltos dentro. */
function codificar(evento) {
  return JSON.stringify(evento) + '\n';
}

/**
 * Trocea un flujo en lineas completas.
 *
 * Una linea puede llegar partida en varios `data`, y dos lineas pueden llegar
 * en el mismo. Sin esto, un `JSON.parse` por trozo falla en cuanto el mensaje
 * pasa del tamano del buffer -- que con un lote de historial es siempre.
 */
function troceador(alRecibirLinea) {
  let resto = '';
  return (trozo) => {
    resto += trozo;
    let corte = resto.indexOf('\n');
    while (corte !== -1) {
      const linea = resto.slice(0, corte).trim();
      resto = resto.slice(corte + 1);
      if (linea) alRecibirLinea(linea);
      corte = resto.indexOf('\n');
    }
  };
}

/** Escribe un evento en stdout. La unica funcion que puede tocar stdout. */
function emitir(evento) {
  process.stdout.write(codificar(evento));
}

/** Lo legible para humanos. Va a stderr y el supervisor lo manda al log. */
function registrar(...partes) {
  process.stderr.write(partes.map(String).join(' ') + '\n');
}

/**
 * Protege stdout de cualquier `console.log` suelto -- propio o de una
 * dependencia. Baileys registra bastante, y una sola linea suya en stdout
 * dejaria al supervisor sin poder interpretar el canal.
 */
function blindarStdout() {
  console.log = registrar;
  console.info = registrar;
  console.warn = registrar;
  console.error = registrar;
  console.debug = () => {};
}

module.exports = { codificar, troceador, emitir, registrar, blindarStdout };
