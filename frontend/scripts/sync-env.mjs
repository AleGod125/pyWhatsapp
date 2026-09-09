/**
 * Genera `src/environments/*.ts` a partir del `.env` de la raiz del repositorio.
 *
 * POR QUE
 * -------
 * Backend y frontend son dos proyectos separados en el mismo repositorio y
 * comparten un unico `.env` en la raiz. La direccion de la API estaba escrita
 * A MANO en `environment.ts`, y el backend la construye desde `API_HOST` y
 * `API_PORT`. Dos sitios para el mismo dato: cambias el puerto en `.env`,
 * arrancas, y el navegador sigue llamando al viejo. El sintoma es un error de
 * red que no menciona la configuracion por ningun lado.
 *
 * Angular no lee `.env`: no hay forma de que el navegador acceda a el. Asi que
 * el valor se traslada en tiempo de compilacion, que es cuando se puede.
 *
 * CUANDO CORRE
 * ------------
 * Solo antes de `npm start` y `npm run build` (ganchos `prestart` y
 * `prebuild`). Los archivos generados SIGUEN VERSIONADOS a proposito: asi un
 * clon recien hecho compila sin tener que ejecutar nada primero.
 *
 * SIN `.env` NO FALLA
 * -------------------
 * Se avisa y se deja lo que hubiera. Alguien que solo quiere compilar el
 * frontend no tiene por que tener la configuracion del backend.
 */

import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const AQUI = dirname(fileURLToPath(import.meta.url));
const FRONTEND = resolve(AQUI, '..');
const ENTORNOS = join(FRONTEND, 'src', 'environments');

/** El primer `.env` subiendo desde el frontend. `null` si no hay ninguno. */
function buscarEnv(desde = FRONTEND) {
  let carpeta = desde;
  for (;;) {
    const candidato = join(carpeta, '.env');
    if (existsSync(candidato)) return candidato;
    const padre = dirname(carpeta);
    if (padre === carpeta) return null;
    carpeta = padre;
  }
}

/** Pares clave/valor de un `.env`. Sin dependencias: el formato es trivial. */
function leerEnv(ruta) {
  const salida = {};
  for (const linea of readFileSync(ruta, 'utf8').split(/\r?\n/)) {
    const limpia = linea.trim();
    if (!limpia || limpia.startsWith('#')) continue;
    const corte = limpia.indexOf('=');
    if (corte < 0) continue;
    const clave = limpia.slice(0, corte).trim();
    let valor = limpia.slice(corte + 1).trim();
    if (
      (valor.startsWith('"') && valor.endsWith('"')) ||
      (valor.startsWith("'") && valor.endsWith("'"))
    ) {
      valor = valor.slice(1, -1);
    }
    salida[clave] = valor;
  }
  return salida;
}

/**
 * La direccion con la que el NAVEGADOR llega a la API.
 *
 * `API_HOST` es la interfaz donde ESCUCHA el backend, y no siempre sirve como
 * destino: `0.0.0.0` significa "todas" y no se puede navegar a el. Con una
 * direccion de bucle se usa `localhost`, que es el origen para el que esta
 * configurado CORS.
 */
function urlDeLaApi(env) {
  const host = (env.API_HOST || '127.0.0.1').trim();
  const puerto = (env.API_PORT || '5000').trim();
  const bucle = ['127.0.0.1', '0.0.0.0', '::1', 'localhost', ''];
  const destino = bucle.includes(host) ? 'localhost' : host;
  return `http://${destino}:${puerto}/api/v1`;
}

function contenido({ production, apiBaseUrl }) {
  return `// GENERADO por scripts/sync-env.mjs desde el .env de la raiz.
// No lo edites a mano: se reescribe en cada \`npm start\` y \`npm run build\`.
// Para cambiar la direccion de la API, edita API_HOST / API_PORT en el .env.
export const environment = {
  production: ${production},
  apiBaseUrl: '${apiBaseUrl}',
} as const;
`;
}

function main() {
  const env = buscarEnv();
  if (!env) {
    console.log('[sync-env] sin .env: se deja environment.ts como esta');
    return;
  }

  const apiBaseUrl = urlDeLaApi(leerEnv(env));
  let escritos = 0;
  for (const [archivo, production] of [
    ['environment.ts', false],
    ['environment.production.ts', true],
  ]) {
    const destino = join(ENTORNOS, archivo);
    const nuevo = contenido({ production, apiBaseUrl });
    const previo = existsSync(destino) ? readFileSync(destino, 'utf8') : null;
    // Solo se escribe si cambia: reescribirlo siempre invalidaria la cache de
    // compilacion y el modo `watch` recompilaria sin motivo.
    if (previo !== nuevo) {
      writeFileSync(destino, nuevo, 'utf8');
      escritos += 1;
    }
  }

  console.log(
    `[sync-env] ${apiBaseUrl}  (desde ${env})` +
      (escritos ? `  -> ${escritos} archivo(s) actualizado(s)` : '  -> sin cambios')
  );
}

main();
