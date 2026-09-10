import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { SeccionesDeChatsComponent } from './secciones-de-chats.component';
import { ChatLockService } from '../../../core/services/chat-lock.service';
import { porOrdenDeLista } from '../../../core/services/chat.service';
import { Chat } from '../../../core/models/api.models';

/**
 * Archivados y chats bloqueados: las dos secciones del sidebar.
 *
 * LO QUE SE PROTEGE
 * -----------------
 * Sobre todo, que la sección de bloqueados no se abra sola. Aquí el fallo
 * tiene consecuencia para el usuario: alguien mirando la pantalla vería una
 * conversación que él había escondido a propósito.
 *
 * Y la advertencia, que fue lo primero que pidió: un chat solo llega marcado
 * como restringido si el bloqueo está puesto EN EL TELÉFONO. Sin decirlo, la
 * sección aparece vacía y parece que la función está rota.
 */

function bloqueoFalso(inicial: { configurado?: boolean; abierto?: boolean } = {}) {
  const estado = {
    configurado: inicial.configurado ?? false,
    abierto: inicial.abierto ?? false,
  };
  const llamadas: string[] = [];
  return {
    llamadas,
    servicio: {
      configurado: () => estado.configurado,
      abierto: () => estado.abierto,
      minutos: () => 15,
      refrescar: () => of(estado),
      poner: (codigo: string) => {
        llamadas.push(`poner:${codigo}`);
        estado.configurado = true;
        estado.abierto = true;
        return of({});
      },
      abrir: (codigo: string) => {
        llamadas.push(`abrir:${codigo}`);
        if (codigo !== '135790') return throwError(() => new Error('mal'));
        estado.abierto = true;
        return of({});
      },
      cerrar: () => {
        llamadas.push('cerrar');
        estado.abierto = false;
        return of({});
      },
      marcarCerrado: () => {
        estado.abierto = false;
      },
    },
  };
}

function montar(
  entradas: { vista?: string; archivados?: number; restringidos?: number } = {},
  bloqueo = bloqueoFalso(),
) {
  TestBed.configureTestingModule({
    imports: [SeccionesDeChatsComponent],
    providers: [{ provide: ChatLockService, useValue: bloqueo.servicio }],
  });
  const fixture = TestBed.createComponent(SeccionesDeChatsComponent);
  fixture.componentRef.setInput('vista', entradas.vista ?? 'normal');
  fixture.componentRef.setInput('archivados', entradas.archivados ?? 0);
  fixture.componentRef.setInput('restringidos', entradas.restringidos ?? 0);
  fixture.detectChanges();
  return { fixture, bloqueo };
}

// ---------------------------------------------------------------------------
// Las entradas
// ---------------------------------------------------------------------------

describe('Las entradas de sección', () => {
  it('«Archivados» se enseña siempre, aunque no haya ninguno', () => {
    // Esconderla cuando está vacía hacía que pareciera que la función no
    // existe: el usuario la busca y no encuentra nada que pulsar. Es lo que
    // reportó. WhatsApp Web también la enseña siempre.
    const { fixture } = montar({ archivados: 0 });
    const texto = fixture.nativeElement.textContent;
    expect(texto).toContain('Archivados');
    expect(texto).toContain('0');
  });

  it('con archivados la ofrece, con su contador', () => {
    const { fixture } = montar({ archivados: 12 });
    const texto = fixture.nativeElement.textContent;
    expect(texto).toContain('Archivados');
    expect(texto).toContain('12');
  });

  it('sin restringidos ofrece la explicación en vez de la sección', () => {
    // Si no, el usuario busca una sección que nunca aparece y no hay forma de
    // que se entere de que primero tiene que bloquearlos en el teléfono.
    const { fixture } = montar({ restringidos: 0 });
    const texto = fixture.nativeElement.textContent;
    expect(texto).not.toContain('Chats bloqueados');
    expect(texto).toContain('¿Y los chats bloqueados?');
  });

  it('la explicación dice que hay que bloquearlos en el teléfono', () => {
    const { fixture } = montar({ restringidos: 0 });
    fixture.componentInstance.explicar.set(true);
    fixture.detectChanges();

    const texto = fixture.nativeElement.textContent;
    expect(texto).toContain('teléfono');
    expect(texto).toContain('código de acceso secreto');
  });

  it('la explicación NO promete que sea el código de WhatsApp', () => {
    // Prometerlo sería mentir sobre lo que protege: WhatsApp manda un derivado
    // opaco, no el código, y los mensajes no están cifrados con él.
    const { fixture } = montar({ restringidos: 0 });
    fixture.componentInstance.explicar.set(true);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain(
      'código propio de esta aplicación',
    );
  });
});

// ---------------------------------------------------------------------------
// La puerta
// ---------------------------------------------------------------------------

describe('Entrar en los chats bloqueados', () => {
  it('sin código puesto, pide crearlo', () => {
    const { fixture } = montar({ restringidos: 3 });
    fixture.componentInstance.pedirEntrada();
    fixture.detectChanges();

    const texto = fixture.nativeElement.textContent;
    expect(texto).toContain('Crea un código');
    expect(texto).toContain('No es tu código secreto de WhatsApp');
  });

  it('NO entra solo por pulsar', () => {
    // Es el fallo con consecuencia: destaparía la sección a quien esté mirando.
    const { fixture } = montar({ restringidos: 3 });
    const idas: string[] = [];
    fixture.componentInstance.ir.subscribe((v) => idas.push(v));

    fixture.componentInstance.pedirEntrada();

    expect(idas).toEqual([]);
    expect(fixture.componentInstance.pidiendo()).toBe(true);
  });

  it('con el código correcto entra', () => {
    const bloqueo = bloqueoFalso({ configurado: true });
    const { fixture } = montar({ restringidos: 3 }, bloqueo);
    const idas: string[] = [];
    fixture.componentInstance.ir.subscribe((v) => idas.push(v));

    fixture.componentInstance.pedirEntrada();
    fixture.componentInstance.codigo = '135790';
    fixture.componentInstance.entrar();

    expect(idas).toEqual(['restringidos']);
    expect(fixture.componentInstance.pidiendo()).toBe(false);
  });

  it('con el código equivocado NO entra', () => {
    const bloqueo = bloqueoFalso({ configurado: true });
    const { fixture } = montar({ restringidos: 3 }, bloqueo);
    const idas: string[] = [];
    fixture.componentInstance.ir.subscribe((v) => idas.push(v));

    fixture.componentInstance.pedirEntrada();
    fixture.componentInstance.codigo = '000000';
    fixture.componentInstance.entrar();

    expect(idas).toEqual([]);
    expect(fixture.componentInstance.error()).toBe('Código incorrecto.');
  });

  it('el error no dice nada de más', () => {
    // Cualquier detalle —«por poco», «te quedan dos»— es una pista.
    const bloqueo = bloqueoFalso({ configurado: true });
    const { fixture } = montar({ restringidos: 3 }, bloqueo);
    fixture.componentInstance.pedirEntrada();
    fixture.componentInstance.codigo = '000000';
    fixture.componentInstance.entrar();

    expect(fixture.componentInstance.error()).toBe('Código incorrecto.');
  });

  it('si ya está abierto entra sin preguntar', () => {
    // Como WhatsApp Web: dentro de la ventana no vuelve a pedirlo.
    const bloqueo = bloqueoFalso({ configurado: true, abierto: true });
    const { fixture } = montar({ restringidos: 3 }, bloqueo);
    const idas: string[] = [];
    fixture.componentInstance.ir.subscribe((v) => idas.push(v));

    fixture.componentInstance.pedirEntrada();

    expect(idas).toEqual(['restringidos']);
    expect(fixture.componentInstance.pidiendo()).toBe(false);
  });

  it('un código nuevo demasiado corto no se manda', () => {
    const bloqueo = bloqueoFalso();
    const { fixture } = montar({ restringidos: 3 }, bloqueo);
    fixture.componentInstance.pedirEntrada();
    fixture.componentInstance.codigo = '12';
    fixture.componentInstance.crear();

    expect(bloqueo.llamadas).toEqual([]);
    expect(fixture.componentInstance.error()).toContain('6 caracteres');
  });
});

// ---------------------------------------------------------------------------
// Salir
// ---------------------------------------------------------------------------

describe('Volver a bloquear', () => {
  it('cierra el pestillo y sale de la sección', () => {
    const bloqueo = bloqueoFalso({ configurado: true, abierto: true });
    const { fixture } = montar({ vista: 'restringidos', restringidos: 3 }, bloqueo);
    const idas: string[] = [];
    fixture.componentInstance.ir.subscribe((v) => idas.push(v));

    fixture.componentInstance.cerrar();

    expect(bloqueo.llamadas).toContain('cerrar');
    expect(idas).toEqual(['normal']);
  });

  it('dentro de una sección se ofrece volver', () => {
    const { fixture } = montar({ vista: 'archivados', archivados: 4 });
    expect(fixture.nativeElement.textContent).toContain('Archivados');
  });
});

// ---------------------------------------------------------------------------
// El orden de la lista
// ---------------------------------------------------------------------------

describe('El orden del sidebar', () => {
  function chat(parcial: Partial<Chat>): Chat {
    return { id: 'x', displayName: 'x', ...parcial } as Chat;
  }

  it('las fijadas van primero aunque sean más viejas', () => {
    // El fallo que cierra: el cliente ordenaba SOLO por fecha y deshacía el
    // orden que ya mandaba el servidor. Una conversación fijada con el último
    // mensaje de hace un mes acababa al final.
    const lista = [
      chat({ id: 'reciente', lastMessageAt: '2026-09-09T10:00:00Z' }),
      chat({
        id: 'fijada',
        pinned: true,
        pinnedAt: 1725900000,
        lastMessageAt: '2026-08-01T10:00:00Z',
      }),
    ];

    expect([...lista].sort(porOrdenDeLista).map((c) => c.id)).toEqual([
      'fijada',
      'reciente',
    ]);
  });

  it('entre dos fijadas manda cuándo se fijaron', () => {
    const lista = [
      chat({ id: 'antes', pinned: true, pinnedAt: 1000, lastMessageAt: '2026-09-09T10:00:00Z' }),
      chat({ id: 'despues', pinned: true, pinnedAt: 2000, lastMessageAt: '2026-09-09T09:00:00Z' }),
    ];

    expect([...lista].sort(porOrdenDeLista).map((c) => c.id)).toEqual([
      'despues',
      'antes',
    ]);
  });

  it('sin fijar ninguna, manda la fecha de siempre', () => {
    const lista = [
      chat({ id: 'vieja', lastMessageAt: '2026-08-01T10:00:00Z' }),
      chat({ id: 'nueva', lastMessageAt: '2026-09-09T10:00:00Z' }),
    ];

    expect([...lista].sort(porOrdenDeLista).map((c) => c.id)).toEqual([
      'nueva',
      'vieja',
    ]);
  });
});
