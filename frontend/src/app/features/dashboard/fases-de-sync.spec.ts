/**
 * La línea que se lee mientras se excava.
 *
 * Su única obligación es responder «¿sigue vivo?». Si no la responde, el
 * usuario recarga la página — y recargar en mitad de una excavación es lo que
 * había que evitar.
 */
import { lineaDeExcavacion } from './fases-de-sync';

describe('lineaDeExcavacion', () => {
  it('traduce la fase interna a algo que se entienda', () => {
    expect(lineaDeExcavacion({ phase: 'backfill' })).toBe('Trayendo mensajes del teléfono');
  });

  it('una fase desconocida se enseña tal cual, no se traga', () => {
    // Preferible leer un nombre técnico que no leer nada: si el backend añade
    // una fase, el usuario sigue viendo que algo cambia.
    expect(lineaDeExcavacion({ phase: 'inventada' })).toBe('inventada');
  });

  it('junta fase, conversaciones y mensajes', () => {
    const linea = lineaDeExcavacion({
      phase: 'backfill',
      chatsProcessed: 12,
      chatsTotal: 340,
      messagesNew: 1500,
    });

    expect(linea).toBe('Trayendo mensajes del teléfono · 12 de 340 conversaciones · 1500 mensajes nuevos');
  });

  it('«0 de 0» no se dice: no informa de nada', () => {
    expect(lineaDeExcavacion({ phase: 'seeds', chatsProcessed: 0, chatsTotal: 0 })).toBe(
      'Buscando por dónde empezar cada conversación',
    );
  });

  it('cero mensajes nuevos tampoco se dice', () => {
    expect(lineaDeExcavacion({ phase: 'seeds', messagesNew: 0 })).toBe(
      'Buscando por dónde empezar cada conversación',
    );
  });

  it('el singular se escribe en singular', () => {
    expect(lineaDeExcavacion({ messagesNew: 1 })).toBe('1 mensaje nuevo');
  });

  it('sin nada que decir se devuelve undefined, no una cadena de relleno', () => {
    // Para que la plantilla pueda NO pintar la línea, en vez de pintar un hueco.
    expect(lineaDeExcavacion({})).toBeUndefined();
    expect(lineaDeExcavacion(undefined)).toBeUndefined();
  });
});

describe('lineaDeExcavacion: la fase de archivo', () => {
  it('se dice en términos de lo que pasa, no del nombre interno', () => {
    expect(lineaDeExcavacion({ phase: 'archive' })).toBe(
      'Releyendo el historial que ya está en disco',
    );
  });
});
