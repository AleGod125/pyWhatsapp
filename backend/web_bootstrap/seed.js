/**
 * Busca claves de mensaje reales para varios chats, y se apaga.
 *
 * POR QUE EXISTE
 * --------------
 * Hay conversaciones que llegaron del pairing como pura metadata: nombre,
 * LID, telefono, contadores. Ni un identificador de mensaje. Y sin uno,
 * HISTORY_SYNC_ON_DEMAND no puede pedir nada: va anclado por definicion.
 *
 * QUE HACE
 * --------
 * Levanta la sesion auxiliar, escucha lo que WhatsApp entregue y, por cada
 * chat pedido, devuelve UNA clave de mensaje si aparece. Nada mas.
 *
 * UNA SOLA CONEXION PARA TODOS
 * ----------------------------
 * Recibe la lista entera de chats objetivo. Levantar un proceso por chat
 * significaria treinta vinculaciones seguidas contra el servidor, y el
 * historial llega de todas formas junto: se aprovecha esa unica entrega.
 *
 * QUE NO HACE, Y ES LO IMPORTANTE
 * -------------------------------
 * No es un segundo backup. No escribe en PostgreSQL, no guarda mensajes, no
 * descarga multimedia, no envia nada y no se queda escuchando indefinidamente.
 * Imprime un JSON por linea y termina. Su sesion vive aparte y se puede
 * borrar sin tocar nada de pywhats.
 *
 * SALIDA
 * ------
 *   {"event":"status","state":"connecting"}
 *   {"event":"qr","data":"2@..."}
 *   {"event":"seed","chat_id":9,"remote_jid":"...","message_id":"...",...}
 *   {"event":"done","found":3,"pending":27}
 */

"use strict";

const fs = require("fs");
const path = require("path");
const baileys = require("baileys");

const makeWASocket = baileys.default || baileys;
const { useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } =
  baileys;

// ---------------------------------------------------------------------------
// Argumentos
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const args = { targets: null, timeout: 180, auth: null };
  for (let i = 2; i < argv.length; i += 1) {
    const clave = argv[i];
    if (clave === "--targets") args.targets = argv[++i];
    else if (clave === "--timeout") args.timeout = parseInt(argv[++i], 10);
    else if (clave === "--auth") args.auth = argv[++i];
  }
  return args;
}

function emit(objeto) {
  process.stdout.write(JSON.stringify(objeto) + "\n");
}

const args = parseArgs(process.argv);
if (!args.targets) {
  emit({ event: "error", message: "hace falta --targets con un archivo JSON" });
  process.exit(2);
}

/** Parte de usuario de un JID, sin sufijo de dispositivo. */
function userOf(jid) {
  return String(jid || "").split("@")[0].split(":")[0].split(".")[0];
}

// [{chat_id, jids:[...]}, ...] -> indice por usuario, para resolver rapido.
// Un contacto aparece por telefono y por LID: los dos apuntan al mismo chat.
let objetivos = [];
try {
  objetivos = JSON.parse(fs.readFileSync(args.targets, "utf8"));
} catch (error) {
  emit({ event: "error", message: `no se pudo leer --targets: ${error.message}` });
  process.exit(2);
}

const PorUsuario = new Map();
for (const objetivo of objetivos) {
  for (const jid of objetivo.jids || []) {
    const usuario = userOf(jid);
    if (usuario) PorUsuario.set(usuario, objetivo.chat_id);
  }
}
const pendientes = new Set(objetivos.map((o) => o.chat_id));
const encontradas = new Map();

const AUTH_DIR = args.auth || path.join(__dirname, "..", "session", "web_bootstrap");

// Nunca sirven como ancla de una conversacion.
const SERVIDORES_EXCLUIDOS = new Set(["broadcast", "newsletter"]);

// Mismo criterio grueso que el lado Python; el fino lo aplica alli
// ``is_valid_history_cursor_id``, que es el que usa el backfill.
const PARECE_ID = /^[0-9A-Fa-f]{16,32}$/;

/**
 * Convierte un mensaje en semilla, SOLO si sirve de ancla para un chat
 * pedido. Devuelve null en cuanto algo no encaja: mas vale no traer nada que
 * traer una clave que apunte a otra conversacion.
 */
function comoSemilla(mensaje) {
  const clave = mensaje && mensaje.key;
  if (!clave || !clave.remoteJid || !clave.id) return null;

  const servidor = String(clave.remoteJid).split("@")[1] || "";
  if (SERVIDORES_EXCLUIDOS.has(servidor)) return null;

  const chatId = PorUsuario.get(userOf(clave.remoteJid));
  if (chatId === undefined || !pendientes.has(chatId)) return null;
  if (!PARECE_ID.test(clave.id)) return null;

  const marca = Number(mensaje.messageTimestamp || 0);
  if (!marca) return null;

  return {
    event: "seed",
    chat_id: chatId,
    remote_jid: clave.remoteJid,
    message_id: clave.id,
    from_me: Boolean(clave.fromMe),
    participant: clave.participant || null,
    timestamp: marca,
    source: "web_bootstrap",
  };
}

/** Anota una semilla y deja de buscar para ese chat. */
function anotar(semilla) {
  if (!semilla || encontradas.has(semilla.chat_id)) return false;
  encontradas.set(semilla.chat_id, semilla);
  pendientes.delete(semilla.chat_id);
  emit(semilla);
  if (pendientes.size === 0) terminar("todos los chats resueltos");
  return true;
}

function revisar(mensajes) {
  for (const mensaje of mensajes || []) {
    anotar(comoSemilla(mensaje));
    if (terminado) return;
  }
}

// ---------------------------------------------------------------------------
// Ciclo de vida
// ---------------------------------------------------------------------------

let terminado = false;
let socket = null;

function terminar(motivo) {
  if (terminado) return;
  terminado = true;
  emit({
    event: "done",
    found: encontradas.size,
    pending: pendientes.size,
    reason: motivo || null,
  });
  try {
    // ``undefined``: se cierra la conexion SIN desvincular el dispositivo,
    // para no tener que escanear otro QR la proxima vez.
    if (socket) socket.end(undefined);
  } catch (_) {
    /* cerrar no puede impedir salir */
  }
  setTimeout(() => process.exit(0), 200);
}

const temporizador = setTimeout(
  () => terminar("se agoto el tiempo de escucha"),
  Math.max(30, args.timeout) * 1000
);
temporizador.unref?.();

const silencioso = {
  level: "silent",
  child() {
    return silencioso;
  },
  trace() {}, debug() {}, info() {}, warn() {}, error() {}, fatal() {},
};

async function main() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();
  emit({
    event: "status",
    state: "starting",
    wa_version: version.join("."),
    targets: objetivos.length,
  });

  socket = makeWASocket({
    version,
    auth: state,
    // El QR lo publica el lado Python, que es quien habla con el usuario.
    printQRInTerminal: false,
    // Solo lectura: no marca nada como leido ni cambia ningun chat.
    markOnlineOnConnect: false,
    syncFullHistory: true,
    logger: silencioso,
  });

  socket.ev.on("creds.update", saveCreds);

  socket.ev.on("connection.update", (actualizacion) => {
    const { connection, lastDisconnect, qr } = actualizacion;
    if (qr) emit({ event: "qr", data: qr });
    if (connection === "connecting") emit({ event: "status", state: "connecting" });
    if (connection === "open") emit({ event: "status", state: "connected" });
    if (connection === "close") {
      const codigo = lastDisconnect?.error?.output?.statusCode;
      if (codigo === DisconnectReason.loggedOut) {
        emit({ event: "error", message: "la sesion auxiliar fue desvinculada" });
        terminar("logged_out");
        return;
      }
      if (!terminado) {
        emit({ event: "status", state: "reconnecting" });
        main().catch((error) => {
          emit({ event: "error", message: String(error && error.message) });
          terminar("reconnect_failed");
        });
      }
    }
  });

  // El historial llega POR TANDAS, cada una con su ``syncType``. Se registra
  // el detalle porque distingue "el servidor no conoce el chat" de "lo conoce
  // y no manda sus mensajes", que son diagnosticos muy distintos.
  socket.ev.on("messaging-history.set", (tanda) => {
    const { chats, messages, isLatest, progress, syncType } = tanda;
    const conocidos = (chats || []).filter((c) =>
      PorUsuario.has(userOf(c.id))
    ).length;
    emit({
      event: "status",
      state: "history",
      sync_type: syncType === undefined ? null : syncType,
      progress: progress === undefined ? null : progress,
      chats: (chats || []).length,
      messages: (messages || []).length,
      targets_present: conocidos,
      is_latest: Boolean(isLatest),
    });
    revisar(messages);
  });

  // Un mensaje en vivo de un chat pendiente tambien es una semilla valida.
  socket.ev.on("messages.upsert", ({ messages }) => revisar(messages));
  socket.ev.on("messages.update", (actualizaciones) => {
    revisar((actualizaciones || []).map((a) => ({ key: a.key, messageTimestamp: a.update?.messageTimestamp })));
  });
}

main().catch((error) => {
  emit({ event: "error", message: String((error && error.message) || error) });
  terminar("fatal");
});
