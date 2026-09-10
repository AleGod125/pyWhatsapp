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

/**
 * De que cuenta de WhatsApp es ESTE worker.
 *
 * POR QUE VIAJA EN CADA LINEA
 * ---------------------------
 * Antes ningun evento decia de quien era. El supervisor de Python atribuia lo
 * que llegara a `runtime_owner_account_id`, un campo EN MEMORIA del runtime
 * que posee el proceso. Mientras ese campo este bien, todo cuadra; cuando se
 * equivoca, no hay nada que lo detecte -- porque lo que acaba en PostgreSQL es
 * un `chat_id` perfectamente valido de la cuenta equivocada.
 *
 * Se midio: 195 conversaciones de un telefono entraron bajo la cuenta de otro,
 * catorce segundos despues de vincular el segundo movil. Ni una restriccion de
 * la base podia verlo, porque la base no sabe que socket escribio.
 *
 * Firmando cada linea, el dato dice de quien es. Si no coincide con lo que el
 * runtime cree, se descarta y se anota. El mismo fallo habria producido 195
 * lineas de aviso en vez de 195 conversaciones cruzadas.
 */
const WA_ACCOUNT_ID = process.env.WA_ACCOUNT_ID || '';

/** Escribe un evento en stdout. La unica funcion que puede tocar stdout. */
function emitir(evento) {
  // La firma se pone AQUI, en el unico sitio que escribe en stdout, y no en
  // cada `emitir(...)` repartido por el worker: asi no existe la posibilidad
  // de que alguien anada un evento nuevo y se le olvide firmarlo.
  process.stdout.write(codificar({ ...evento, wa_account_id: WA_ACCOUNT_ID }));
}

/**
 * Sin cuenta NO se arranca. Se sale con codigo 1 y se dice por que.
 *
 * Arrancar igual seria emitir eventos sin firmar, y el supervisor tendria que
 * elegir entre descartarlos --perder el historial-- o creerselos, que es
 * exactamente el agujero que esto cierra.
 */
function exigirCuenta() {
  if (!WA_ACCOUNT_ID) {
    process.stderr.write(
      '[FATAL] WA_ACCOUNT_ID no definido. El worker no puede firmar sus ' +
        'eventos y el supervisor no sabria de que cuenta son.\n',
    );
    process.exit(1);
  }
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

module.exports = {
  codificar,
  troceador,
  emitir,
  registrar,
  blindarStdout,
  exigirCuenta,
  WA_ACCOUNT_ID,
};
