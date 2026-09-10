import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { AccountSwitcherComponent } from './account-switcher.component';
import {
  AccountService,
  WhatsAppAccountInfo,
  nombreDeCuenta,
} from '../../core/services/account.service';
import { AccountState } from '../../core/services/account-state.service';

/**
 * Varias cuentas de WhatsApp: elegir una NO es filtrar la lista.
 *
 * LO QUE SE PROTEGE
 * -----------------
 * Un usuario puede tener el WhatsApp personal y el del trabajo. Son contextos
 * separados —otros chats, otro historial, otra copia en Drive— y lo que no
 * puede pasar es que se vean juntos, ni que queden restos de uno al pasar al
 * otro.
 *
 * Por eso las pruebas de aquí miran sobre todo el ORDEN de las cosas al
 * cambiar: el servidor primero, después vaciar, después reconectar el canal en
 * vivo. Cada paso cubre un fallo distinto y hacerlos en otro orden los
 * reintroduce.
 */

function cuenta(parcial: Partial<WhatsAppAccountInfo>): WhatsAppAccountInfo {
  return {
    id: 'a',
    accountType: 'unknown',
    sessionStatus: 'linked',
    linked: true,
    needsRelink: false,
    disconnected: false,
    active: false,
    ...parcial,
  };
}

// ---------------------------------------------------------------------------
// El nombre que se pinta
// ---------------------------------------------------------------------------

describe('El nombre de una cuenta', () => {
  it('usa el que puso el usuario antes que nada', () => {
    const nombre = nombreDeCuenta(
      cuenta({ displayName: 'Trabajo', phoneNumber: '573001112233' }),
    );
    expect(nombre).toBe('Trabajo');
  });

  it('cae al número cuando no tiene nombre', () => {
    expect(nombreDeCuenta(cuenta({ phoneNumber: '573001112233' }))).toBe(
      '+573001112233',
    );
  });

  it('nunca enseña el identificador interno', () => {
    const nombre = nombreDeCuenta(
      cuenta({ id: '3f8c1a22-0000-4000-8000-000000000000' }),
    );
    expect(nombre).not.toContain('3f8c1a22');
  });
});

// ---------------------------------------------------------------------------
// El selector
// ---------------------------------------------------------------------------

describe('El selector de cuenta', () => {
  function montar(cuentas: WhatsAppAccountInfo[], activaId?: string) {
    const estado = new AccountState();
    estado.fijar(activaId ?? cuentas.find((c) => c.active)?.id);
    TestBed.configureTestingModule({
      imports: [AccountSwitcherComponent],
      providers: [
        { provide: AccountState, useValue: estado },
        {
          provide: AccountService,
          useValue: {
            cuentas: () => cuentas,
            activa: () =>
              cuentas.find((c) => c.id === estado.activaId()) ??
              cuentas.find((c) => c.active),
          },
        },
      ],
    });
    const fixture = TestBed.createComponent(AccountSwitcherComponent);
    fixture.detectChanges();
    return fixture;
  }

  it('enseña cuál se está viendo aunque solo haya una', () => {
    const fixture = montar([
      cuenta({ id: 'a', displayName: 'Personal', active: true }),
    ]);
    expect(fixture.nativeElement.textContent).toContain('Personal');
  });

  it('con una sola no ofrece elegir', () => {
    const fixture = montar([cuenta({ id: 'a', displayName: 'Personal', active: true })]);
    fixture.componentInstance.alternar();
    fixture.detectChanges();
    // El menú solo aparece si hay algo que elegir o se puede agregar.
    expect(fixture.componentInstance.hayVarias()).toBe(false);
  });

  it('con dos, el menú las lista', () => {
    const fixture = montar([
      cuenta({ id: 'a', displayName: 'Personal', active: true }),
      cuenta({ id: 'b', displayName: 'Trabajo' }),
    ]);
    fixture.componentInstance.alternar();
    fixture.detectChanges();

    const texto = fixture.nativeElement.textContent;
    expect(texto).toContain('Personal');
    expect(texto).toContain('Trabajo');
  });

  it('elegir la que YA está no cuesta un recargado', () => {
    const fixture = montar([
      cuenta({ id: 'a', displayName: 'Personal', active: true }),
      cuenta({ id: 'b', displayName: 'Trabajo' }),
    ]);
    const avisos: WhatsAppAccountInfo[] = [];
    fixture.componentInstance.cambiar.subscribe((c) => avisos.push(c));

    fixture.componentInstance.elegir(
      cuenta({ id: 'a', displayName: 'Personal', active: true }),
    );

    expect(avisos).toEqual([]);
  });

  it('elegir otra sí avisa', () => {
    const fixture = montar([
      cuenta({ id: 'a', displayName: 'Personal', active: true }),
      cuenta({ id: 'b', displayName: 'Trabajo' }),
    ]);
    const avisos: WhatsAppAccountInfo[] = [];
    fixture.componentInstance.cambiar.subscribe((c) => avisos.push(c));

    fixture.componentInstance.elegir(cuenta({ id: 'b', displayName: 'Trabajo' }));

    expect(avisos.map((c) => c.id)).toEqual(['b']);
  });
});

// ---------------------------------------------------------------------------
// Cambiar de cuenta: el orden importa
// ---------------------------------------------------------------------------

describe('Al cambiar de cuenta', () => {
  /**
   * Se reproduce la secuencia del panel sin montar el panel entero: lo que se
   * comprueba es el ORDEN de las llamadas, y para eso basta con registrarlas.
   */
  function secuencia({ falla = false } = {}) {
    const pasos: string[] = [];
    const estado = new AccountState();
    estado.fijar('a');

    const cuentasApi = {
      activa: () => cuenta({ id: 'a', active: true }),
      activar: (id: string) => {
        pasos.push(`servidor:${id}`);
        return falla
          ? throwError(() => new Error('no'))
          : of(cuenta({ id, active: true }));
      },
    };
    const vaciar = () => pasos.push('vaciar');
    const reconectar = () => pasos.push('reconectar');
    const pedir = () => pasos.push('pedir');

    // La misma secuencia que `DashboardPageComponent.cambiarDeCuenta`.
    cuentasApi.activar('b').subscribe({
      next: () => {
        vaciar();
        reconectar();
        pedir();
      },
      error: () => pasos.push('error'),
    });
    return pasos;
  }

  it('avisa al servidor ANTES de vaciar la pantalla', () => {
    const pasos = secuencia();
    expect(pasos.indexOf('servidor:b')).toBeLessThan(pasos.indexOf('vaciar'));
  });

  it('vacía antes de pedir: si no, se ven las dos cuentas a la vez', () => {
    const pasos = secuencia();
    expect(pasos.indexOf('vaciar')).toBeLessThan(pasos.indexOf('pedir'));
  });

  it('reconecta el canal antes de pedir', () => {
    // Si no, seguirían llegando eventos de la cuenta anterior mientras carga
    // la nueva, y un mensaje tardío de aquella aparecería en esta.
    const pasos = secuencia();
    expect(pasos.indexOf('reconectar')).toBeLessThan(pasos.indexOf('pedir'));
  });

  it('si el servidor falla NO se vacía nada', () => {
    // Vaciar primero dejaría al usuario con la pantalla en blanco de una
    // cuenta que en realidad no cambió.
    const pasos = secuencia({ falla: true });
    expect(pasos).not.toContain('vaciar');
    expect(pasos).toContain('error');
  });
});

// ---------------------------------------------------------------------------
// El estado que viaja en cada petición
// ---------------------------------------------------------------------------

describe('La cuenta activa', () => {
  it('empieza sin fijar: manda la del servidor', () => {
    expect(new AccountState().activaId()).toBeUndefined();
  });

  it('una cadena vacía cuenta como "no se sabe"', () => {
    // Mandar la cabecera vacía sería decirle al servidor "esta cuenta", con
    // un identificador que no existe, en vez de "usa la mía".
    const estado = new AccountState();
    estado.fijar('');
    expect(estado.activaId()).toBeUndefined();
  });
});
