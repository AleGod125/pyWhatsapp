/**
 * Qué se le enseña al usuario mientras se excava el historial.
 *
 * POR QUÉ EXISTE
 * --------------
 * La excavación completa puede durar minutos, y durante ese rato la pantalla
 * tiene que responder a una sola pregunta: «¿sigue vivo?». Sin respuesta el
 * usuario recarga la página, y recargar en mitad de una excavación es
 * exactamente lo que había que evitar.
 *
 * NO ES UN PORCENTAJE, A PROPÓSITO
 * --------------------------------
 * Cuántos mensajes quedan por traer no se sabe hasta haberlos traído: es el
 * teléfono quien decide cuándo se acaba el historial de cada conversación. Una
 * barra que prometa «73 %» estaría inventando ese total, y al llegar al 100 %
 * sin terminar haría exactamente el daño que se quería evitar.
 *
 * Así que se cuentan CIFRAS de lo que ya pasó: conversaciones recorridas y
 * mensajes nuevos. Suben, no mienten, y se ven avanzar.
 */
import { SyncStatus } from '../../core/models/api.models';

/** El nombre interno de cada fase traducido a lo que de verdad está pasando. */
const FASES: Record<string, string> = {
  reconcile: 'Revisando lo que ya hay guardado',
  archive: 'Releyendo el historial que ya está en disco',
  seeds: 'Buscando por dónde empezar cada conversación',
  web: 'Pidiendo referencias que faltaban',
  revalidate: 'Comprobando qué se puede pedir ahora',
  backfill: 'Trayendo mensajes del teléfono',
  media: 'Recogiendo fotos y audios',
  storage: 'Preparando la copia de seguridad',
  finalize: 'Cerrando y contando el resultado',
};

/**
 * La línea de progreso de la excavación, o `undefined` si no hay nada que decir.
 *
 * Se devuelve `undefined` --y no una cadena de relleno-- para que la plantilla
 * pueda no pintar la línea en vez de pintar un hueco.
 */
export function lineaDeExcavacion(status: SyncStatus | undefined): string | undefined {
  if (!status) return undefined;
  const partes: string[] = [];

  const fase = status.phase ? (FASES[status.phase] ?? status.phase) : undefined;
  if (fase) partes.push(fase);

  // Las cifras solo cuando dicen algo. «0 de 0» no informa de nada.
  const total = status.chatsTotal ?? 0;
  const hechas = status.chatsProcessed ?? 0;
  if (total > 0) partes.push(`${hechas} de ${total} conversaciones`);

  const nuevos = status.messagesNew ?? 0;
  if (nuevos > 0) {
    partes.push(nuevos === 1 ? '1 mensaje nuevo' : `${nuevos} mensajes nuevos`);
  }

  return partes.length ? partes.join(' · ') : undefined;
}
