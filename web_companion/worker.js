'use strict';
/**
 * Web Companion: mide que ve WhatsApp Web. NO extrae historial.
 *
 * QUE ES
 * ------
 * Un dispositivo vinculado MAS de la misma cuenta, con su propia sesion y su
 * propio QR, que sirve para responder a UNA pregunta:
 *
 *   de las conversaciones que estan esperando una referencia,
 *   ¿cuantas ve WhatsApp Web, y de cuantas puede dar un mensaje real?
 *
 * QUE NO ES
 * ---------
 * No es un segundo backup. No guarda mensajes, no descarga multimedia, no
 * escribe en PostgreSQL, no habla con Drive y no toca la sesion de pywhats.
 * Devuelve metadatos por stdout y se calla.
 *
 * SU SESION ES OTRA
 * -----------------
 * Vive en ``session/web_companion/`` y es una vinculacion INDEPENDIENTE: otro
 * aparato en la lista del telefono. No se copia ni una clave entre las dos —
 * son sesiones Signal distintas y mezclarlas corrompe ambas.
 *
 * QUIEN MANDA
 * -----------
 * Este proceso no decide nada. Propone candidatos; Python los valida con las
 * reglas que ya funcionan y decide que hacer. En esta fase Python solo
 * MIDE: no escribe anclas, no cambia estados y no pide historial.
 */

const path = require('path');
const { encode, LineReader, parseCommand } = require('./protocol');
const candidatos = require('./candidates');
const indice = require('./inventory');
const store = require('./store_adapter');

// -- stdout es SOLO protocolo -----------------------------------------------
//
// whatsapp-web.js y puppeteer escriben por consola cuando les apetece. Una
// linea suya en stdout rompe el canal, asi que se desvia todo a stderr ANTES
// de cargar nada. La unica via a stdout es `emitir`.
const salidaReal = process.stdout.write.bind(process.stdout);
console.log = (...args) => process.stderr.write(args.join(' ') + '\n');
console.info = console.log;
console.warn = console.log;
console.debug = console.log;

function emitir(evento) {
  salidaReal(encode(evento));
}

function registrar(mensaje) {
  process.stderr.write(`[web-companion] ${mensaje}\n`);
}

// -- Estado -----------------------------------------------------------------

const ESTADOS = {
  STARTING: 'starting',
  QR_REQUIRED: 'qr_required',
  CONNECTED: 'connected',
  READY: 'ready',
  ERROR: 'error',
  STOPPED: 'stopped',
};

const estado = {
  fase: ESTADOS.STARTING,
  qr: null,
  error: null,
  capacidades: null,
  diagnostico: null,
  authenticated: false,
  webClientReady: false,
  storeReady: false,
  probeRunning: false,
};

function cambiarFase(fase, extra = {}) {
  estado.fase = fase;
  // El motivo se GUARDA, no solo se anuncia: si no, un `status` posterior
  // diria "error" sin poder decir de que.
  if ('error' in extra) estado.error = extra.error ?? null;
  if ('capabilities' in extra) estado.capacidades = extra.capabilities ?? null;
  if ('diagnostics' in extra) estado.diagnostico = extra.diagnostics ?? null;
  if ('authenticated' in extra) estado.authenticated = extra.authenticated === true;
  if ('web_client_ready' in extra) estado.webClientReady = extra.web_client_ready === true;
  if ('store_ready' in extra) estado.storeReady = extra.store_ready === true;
  emitir({ event: 'state', state: fase, ...extra });
}

// -- Ajustes ----------------------------------------------------------------
//
// Todo llega por variables de entorno, que las pone el supervisor de Python.
// Asi los interruptores viven en un solo sitio: el .env del proyecto.
const AJUSTES = {
  sessionDir: process.env.WEB_COMPANION_SESSION_DIR || path.join(__dirname, 'session'),
  chrome: process.env.WEB_COMPANION_CHROME || null,
  cargarAnteriores: process.env.WEB_STORE_LOAD_EARLIER === 'true',
  descubrirConScroll: process.env.WEB_STORE_DISCOVERY_SCROLL === 'true',
  // Tope de mensajes que se miran por chat. No queremos descargar miles:
  // queremos UNA referencia y parar.
  topeMensajes: Number(process.env.WEB_STORE_PROBE_LIMIT || 25),
};

let cliente = null;

// -- Arranque del cliente ---------------------------------------------------

async function arrancarCliente() {
  if (process.env.WEB_COMPANION_NO_CLIENT === 'true') {
    // Solo para las pruebas del canal: se comprueba que el worker atiende
    // comandos sin arrancar Chromium. Nunca se pone en produccion.
    registrar('cliente desactivado (WEB_COMPANION_NO_CLIENT)');
    cambiarFase(ESTADOS.ERROR, { error: 'cliente_desactivado' });
    return;
  }

  let Client;
  let LocalAuth;
  try {
    ({ Client, LocalAuth } = require('whatsapp-web.js'));
  } catch (e) {
    estado.error = 'faltan_dependencias';
    cambiarFase(ESTADOS.ERROR, {
      error: 'faltan_dependencias',
      detail: "ejecuta 'py tools/setup_web_companion.py'",
    });
    return;
  }

  cliente = new Client({
    authStrategy: new LocalAuth({
      clientId: 'web-companion',
      dataPath: AJUSTES.sessionDir,
    }),
    puppeteer: {
      headless: true,
      ...(AJUSTES.chrome ? { executablePath: AJUSTES.chrome } : {}),
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--no-first-run',
        '--disable-extensions',
        '--window-size=1280,800',
      ],
    },
  });

  cliente.on('qr', (qr) => {
    // El QR viaja para poder pintarlo, pero NO se guarda en disco ni se
    // registra entero: es una credencial de vinculacion.
    estado.qr = qr;
    registrar(`QR requerido (${qr.length} caracteres)`);
    cambiarFase(ESTADOS.QR_REQUIRED, { qr });
  });

  cliente.on('authenticated', () => {
    estado.qr = null;
    cambiarFase(ESTADOS.CONNECTED, { authenticated: true });
    registrar('autenticado');
    confirmarReadyPorRuntime().catch((error) =>
      registrar(`ready runtime fallo error_name=${error?.name || 'Error'} error_message=${String(error?.message || error).slice(0, 160)}`),
    );
  });

  let readyConfirmado = false;
  let startupTimeout;

  async function marcarClienteReady(origen) {
    if (readyConfirmado) return;
    readyConfirmado = true;
    if (startupTimeout) clearTimeout(startupTimeout);
    estado.qr = null;
    cambiarFase(ESTADOS.CONNECTED, { authenticated: estado.authenticated, web_client_ready: true, store_ready: false });
    registrar(`client ready origen=${origen}; esperando Store`);
    const discovery = await store.esperarStore(cliente.pupPage);
    estado.capacidades = discovery.capabilities;
    estado.diagnostico = discovery.diagnostics;
    estado.storeReady = discovery.store_ready;
    for (const error of discovery.diagnostics?.errors || []) {
      registrar(`strategy fallo method=${error.method} error_name=${error.error_name} error_message=${error.error_message}`);
    }
    cambiarFase(ESTADOS.READY, {
      authenticated: estado.authenticated,
      web_client_ready: true,
      store_ready: estado.storeReady,
      capabilities: estado.capacidades,
      diagnostics: estado.diagnostico,
    });
  }

  async function confirmarReadyPorRuntime() {
    const start = Date.now();
    let lastError = null;
    while (!readyConfirmado && Date.now() - start < 60000) {
      try {
        const available = await cliente.pupPage?.evaluate(
          () => typeof window.WWebJS?.getChats === 'function',
        );
        if (available) {
          await marcarClienteReady('runtime_WWebJS');
          return;
        }
      } catch (error) {
        lastError = error;
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    if (!readyConfirmado && lastError) {
      registrar(`ready runtime timeout error_name=${lastError?.name || 'Error'} error_message=${String(lastError?.message || lastError).slice(0, 160)}`);
    }
  }

  cliente.on('ready', async () => {
    await marcarClienteReady('client_event');
  });

  startupTimeout = setTimeout(() => {
    if (readyConfirmado) return;
    registrar('worker activo pero WhatsApp Web no llego a ready en 60s');
    cambiarFase(estado.fase === ESTADOS.STARTING ? ESTADOS.STARTING : ESTADOS.CONNECTED, {
      authenticated: estado.authenticated,
      web_client_ready: false,
      store_ready: false,
      startup_timeout: true,
    });
  }, 60000);
  startupTimeout.unref?.();

  cliente.on('auth_failure', (motivo) => {
    estado.error = String(motivo || 'auth_failure').slice(0, 200);
    cambiarFase(ESTADOS.ERROR, { error: estado.error });
  });

  cliente.on('disconnected', (motivo) => {
    estado.error = String(motivo || 'disconnected').slice(0, 200);
    cambiarFase(ESTADOS.STOPPED, { error: estado.error });
  });

  try {
    await cliente.initialize();
  } catch (e) {
    estado.error = String(e?.message || e).slice(0, 200);
    registrar(`no se pudo inicializar: ${estado.error}`);
    cambiarFase(ESTADOS.ERROR, { error: estado.error });
  }
}

// -- Comandos ---------------------------------------------------------------

function exigirListo() {
  if (!estado.webClientReady || !cliente?.pupPage) {
    return { error: 'no_listo', state: estado.fase };
  }
  return null;
}

/**
 * El inventario: las dos vias y su union.
 *
 * `getChats()` es la lista de siempre. `Store.Chat.getModelsArray()` puede
 * traer conversaciones que aquella no da —el proyecto anterior lo usaba justo
 * para eso—. Se piden las dos y se comparan, que es lo unico que permite
 * decir si la segunda aporta algo.
 */
async function inventario(comando) {
  const problema = exigirListo();
  if (problema) return problema;

  if (AJUSTES.descubrirConScroll) {
    // El panel lateral carga en diferido. El proyecto anterior hacia 80
    // desplazamientos por defecto; aqui va detras de un interruptor y no
    // se hace salvo que se pida.
    try {
      await cliente.pupPage.evaluate(async () => {
        const esperar = (ms) => new Promise((r) => setTimeout(r, ms));
        const panel =
          document.querySelector('#pane-side') ||
          document.querySelector('[data-testid="chat-list"]');
        if (!panel) return;
        for (let i = 0; i < 20; i++) {
          panel.scrollTop = panel.scrollHeight;
          await esperar(300);
        }
        panel.scrollTop = 0;
      });
    } catch (e) {
      registrar(`el descubrimiento por desplazamiento fallo: ${e?.message}`);
    }
  }

  let deGetChats = [];
  try {
    const chats = await cliente.getChats();
    deGetChats = (chats || []).map((c) => ({
      id: candidatos.serializar(c?.id),
      is_group: Boolean(c?.isGroup),
      last_activity: Number(c?.timestamp || c?.lastMessage?.timestamp || 0) || null,
      msgs_in_memory: 0,
    }));
  } catch (e) {
    registrar(`getChats() fallo: ${e?.message}`);
  }

  let deStore = [];
  try {
    deStore = await store.chatsDelStore(cliente.pupPage);
  } catch (e) {
    registrar(`Store.Chat fallo: ${e?.message}`);
  }

  const union = candidatos.unirInventario(deGetChats, deStore);
  const metricas = candidatos.compararInventario(union, comando.python_chat_jids || []);

  return {
    event: 'inventory_result',
    metrics: metricas,
    capabilities: estado.capacidades,
    // Solo los que Python no conoce: mandar los 40 que ya tiene no aporta
    // nada y son datos personales viajando sin motivo.
    // De que clase son las que Web ve y Python no. Numeros, no identificadores.
    extra_por_clase: candidatos.contarPorClase(
      union
        .map((c) => c.chat_jid)
        .filter((jid) => {
          const conocidos = new Set(
            (comando.python_chat_jids || []).map(candidatos.normalizarJid).filter(Boolean),
          );
          return !conocidos.has(jid);
        }),
    ),
    // Y al reves: las que Python tiene y Web no llega a ver.
    faltan_por_clase: candidatos.contarPorClase(
      (comando.python_chat_jids || [])
        .map(candidatos.normalizarJid)
        .filter(Boolean)
        .filter((jid) => !union.some((c) => c.chat_jid === jid)),
    ),
    unknown_to_python: union
      .filter((c) => {
        const conocidos = new Set(
          (comando.python_chat_jids || []).map(candidatos.normalizarJid).filter(Boolean),
        );
        return !conocidos.has(c.chat_jid);
      })
      .map((c) => ({
        chat_jid: c.chat_jid,
        is_group: c.is_group,
        msgs_in_memory: c.msgs_in_memory,
        sources: c.sources,
      })),
  };
}

/**
 * El sondeo: por cada conversacion que espera, ¿hay una referencia real?
 *
 * Se para EN CUANTO aparece una. No queremos descargar miles de mensajes;
 * queremos una semilla, y la mas antigua que este a mano.
 */
async function sondearSemillas(comando) {
  const problema = exigirListo();
  if (problema) return problema;

  const jids = (comando.chat_jids || []).map(candidatos.normalizarJid).filter(Boolean);
  const resultados = [];
  const resumen = {
    waiting: jids.length,
    visible_store: 0,
    with_messages: 0,
    seed_usable: 0,
    candidates: 0,
    sin_seed: 0,
    rejections: {},
    load_earlier_intentos: 0,
    load_earlier_exitos: 0,
  };

  for (const jid of jids) {
    const fila = { chat_jid: jid, visible: false, msgs_in_memory: 0, candidate: null };

    let enMemoria;
    try {
      enMemoria = await store.mensajesEnMemoria(cliente.pupPage, jid, AJUSTES.topeMensajes);
    } catch (e) {
      fila.error = String(e?.message || e).slice(0, 120);
      resultados.push(fila);
      resumen.sin_seed += 1;
      continue;
    }

    fila.visible = Boolean(enMemoria?.encontrado);
    if (fila.visible) resumen.visible_store += 1;

    const deLosCargados = [];
    for (const mensaje of enMemoria?.mensajes || []) {
      const propuesta = candidatos.extractSeedCandidate(mensaje, {
        chatJid: jid,
        source: 'web_store',
      });
      if (propuesta.candidato) {
        deLosCargados.push(propuesta.candidato);
        resumen.candidates += 1;
      } else {
        const reason = propuesta.rechazado || 'desconocido';
        resumen.rejections[reason] = (resumen.rejections[reason] || 0) + 1;
      }
    }
    fila.msgs_in_memory = (enMemoria?.mensajes || []).length;
    if (fila.msgs_in_memory > 0) resumen.with_messages += 1;

    let mejor = candidatos.masAntiguo(deLosCargados);

    // Nivel 2: solo si se pide, y solo si lo que ya habia no bastaba.
    if (!mejor && AJUSTES.cargarAnteriores && fila.visible) {
      resumen.load_earlier_intentos += 1;
      try {
        const carga = await store.cargarAnteriores(cliente.pupPage, jid);
        fila.load_earlier = carga;
        if (carga?.ok) {
          resumen.load_earlier_exitos += 1;
          const otra = await store.mensajesEnMemoria(
            cliente.pupPage,
            jid,
            AJUSTES.topeMensajes,
          );
          const nuevos = (otra?.mensajes || [])
            .map(
              (m) =>
                candidatos.extractSeedCandidate(m, {
                  chatJid: jid,
                  source: 'web_load_earlier',
                }).candidato,
            )
            .filter(Boolean);
          mejor = candidatos.masAntiguo(nuevos);
        }
      } catch (e) {
        fila.load_earlier = { ok: false, motivo: String(e?.message || e).slice(0, 80) };
      }
    }

    if (mejor) {
      fila.candidate = mejor;
      resumen.seed_usable += 1;
    } else {
      resumen.sin_seed += 1;
    }
    resultados.push(fila);
  }

  return { event: 'seed_probe_result', summary: resumen, chats: resultados };
}

async function atender(comando) {
  switch (comando.cmd) {
    case 'status':
      return {
        event: 'status',
        state: estado.fase,
        qr: estado.qr,
        error: estado.error,
        capabilities: estado.capacidades,
        diagnostics: estado.diagnostico,
        authenticated: estado.authenticated,
        web_client_ready: estado.webClientReady,
        store_ready: estado.storeReady,
        probe_running: estado.probeRunning,
        settings: {
          load_earlier: AJUSTES.cargarAnteriores,
          discovery_scroll: AJUSTES.descubrirConScroll,
        },
      };
    case 'inspect_models':
      // Radiografia estructural. Solo nombres y tipos: es lo que permite
      // dejar de adivinar donde vive el identificador de un mensaje.
      {
        const problema = exigirListo();
        if (problema) return problema;
        try {
          const forma = await store.inspeccionarModelos(cliente.pupPage, {
            chats: Number(comando.chats || 3),
            mensajes: Number(comando.mensajes || 2),
          });
          return { event: 'model_shapes', shapes: forma };
        } catch (e) {
          return { event: 'error', error: 'inspeccion_fallo', detail: String(e?.message || e).slice(0, 200) };
        }
      }
    case 'inventory':
      estado.probeRunning = true;
      try { return await inventario(comando); } finally { estado.probeRunning = false; }
    case 'web_inventory':
      // El indice completo: QUE conversaciones existen y cual es el ultimo
      // mensaje real de cada una. Es la pregunta que 'inventory' no hacia:
      // aquel comparaba contra lo que Python ya conocia, asi que lo que
      // Python nunca descubrio no aparecia por ningun lado.
      {
        const problema = exigirListo();
        if (problema) return problema;
        estado.probeRunning = true;
        try {
          return await indice.inventarioCompleto(cliente, store, {
            permitirFetch: comando.allow_fetch !== false,
            // Python sabe cuales esperan referencia, cuales ya tienen cursor
            // y cuales no conoce. Node no puede saberlo, y sin eso la cuota
            // de red se gasta en conversaciones que no desbloquean nada.
            prioritarios: comando.priority_chat_jids || [],
            omitir: comando.skip_chat_jids || [],
            conocidos: comando.known_chat_jids || [],
            topeMensajes: AJUSTES.topeMensajes,
            debug: comando.debug === true,
          });
        } catch (e) {
          return {
            event: 'error',
            error: 'inventario_fallo',
            detail: String(e?.message || e).slice(0, 200),
          };
        } finally {
          estado.probeRunning = false;
        }
      }
    case 'j31_store_snapshot':
      // Plan J3.1. LEE el almacen, no le pide nada: ni fetchMessages ni
      // loadEarlierMsgs. Es la unica forma de medir que trae el arranque del
      // navegador por si solo, que es lo que nunca se habia medido aparte.
      {
        const problema = exigirListo();
        if (problema) return problema;
        const comenzo = Date.now();
        try {
          const foto = await store.instantaneaJ31(cliente.pupPage, {
            topeMensajes: Number(comando.limit || 1),
          });
          return { event: 'j31_store_snapshot', elapsed_ms: Date.now() - comenzo, ...foto };
        } catch (e) {
          return { event: 'error', error: 'instantanea_fallo', detail: String(e?.message || e).slice(0, 200) };
        }
      }
    case 'probe_waiting_seeds':
      estado.probeRunning = true;
      try { return await sondearSemillas(comando); } finally { estado.probeRunning = false; }
    case 'shutdown':
      cerrar(0);
      return { event: 'stopping' };
    default:
      return { error: 'comando_desconocido', cmd: comando.cmd };
  }
}

// -- Bucle de entrada -------------------------------------------------------

const lector = new LineReader();

process.stdin.setEncoding('utf8');
process.stdin.on('data', (trozo) => {
  for (const linea of lector.push(trozo)) {
    const { command, error } = parseCommand(linea);
    if (error) {
      // Una linea rota se contesta y se sigue: no puede tumbar el worker.
      emitir({ event: 'error', error });
      continue;
    }
    Promise.resolve()
      .then(() => atender(command))
      .then((respuesta) => {
        if (respuesta) emitir({ id: command.id ?? null, ...respuesta });
      })
      .catch((e) => {
        emitir({
          id: command.id ?? null,
          event: 'error',
          error: 'fallo_al_atender',
          detail: String(e?.message || e).slice(0, 200),
        });
      });
  }
});

process.stdin.on('end', () => cerrar(0));

let cerrando = false;
function cerrar(codigo) {
  if (cerrando) return;
  cerrando = true;
  const salir = () => process.exit(codigo);
  // Chromium tarda en soltar sus manejadores. Se le da un margen corto y se
  // sale igualmente: colgarse al cerrar seria peor.
  const plazo = setTimeout(salir, 5000);
  plazo.unref?.();
  Promise.resolve()
    .then(() => cliente?.destroy())
    .catch(() => undefined)
    .finally(salir);
}

process.on('SIGTERM', () => cerrar(0));
process.on('SIGINT', () => cerrar(0));

emitir({ event: 'starting', pid: process.pid });
arrancarCliente();
