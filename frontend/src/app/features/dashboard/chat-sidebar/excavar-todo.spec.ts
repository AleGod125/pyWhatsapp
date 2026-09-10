/**
 * El botón de excavar todo el historial.
 *
 * LO QUE PIDIÓ EL USUARIO, LITERALMENTE
 * -------------------------------------
 * «Un botón en el panel lateral que haga una excavación desde 0 […] que traiga
 * todos los mensajes nuevos sin necesidad de borrar la bd […] pon algo visual
 * que le diga al usuario que espere y que no se ponga a refrescar […] haz que
 * solo funcione una vez hasta que se acabe la extracción.»
 *
 * Las tres cosas se prueban aquí. La tercera es la que más fácil se rompe: no
 * basta con deshabilitar el botón, porque un botón gris invita a insistir. Se
 * quita de en medio y en su sitio va el aviso.
 */
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { ChatSidebarComponent } from './chat-sidebar.component';
import type { Chat } from '../../../core/models/api.models';

function chat(id: string): Chat {
  return {
    id,
    jid: `${id}@s.whatsapp.net`,
    displayName: `Chat ${id}`,
    type: 'individual',
    messageCount: 1,
  };
}

describe('ChatSidebar: excavar todo el historial', () => {
  let fixture: ComponentFixture<ChatSidebarComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ChatSidebarComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(ChatSidebarComponent);
  });

  function render(props: Record<string, unknown> = {}) {
    fixture.componentRef.setInput('chats', [chat('1')]);
    for (const [clave, valor] of Object.entries(props)) {
      fixture.componentRef.setInput(clave, valor);
    }
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const boton = (el: HTMLElement) => el.querySelector<HTMLButtonElement>('button.excavar');

  it('en reposo se ofrece el botón', () => {
    const el = render();

    expect(boton(el)).not.toBeNull();
    expect(el.querySelector('.excavando')).toBeNull();
  });

  it('al pulsarlo pide la excavación', () => {
    const el = render();
    let pedidas = 0;
    fixture.componentInstance.excavarTodo.subscribe(() => (pedidas += 1));

    boton(el)!.click();

    expect(pedidas).toBe(1);
  });

  it('mientras excava el botón NO existe: no se puede pulsar veinte veces', () => {
    // Deshabilitarlo no basta. Un botón gris sigue invitando a insistir, y lo
    // que el usuario pidió es que «solo funcione una vez hasta que se acabe».
    const el = render({ excavando: true });

    expect(boton(el)).toBeNull();
  });

  it('mientras excava se dice, en una línea y al fondo', () => {
    // Era un bloque de cuatro líneas ENCIMA de la lista: ocupaba media
    // pantalla y empujaba hacia abajo las conversaciones justo cuando el
    // usuario quiere verlas aparecer. El aviso tapaba aquello de lo que
    // avisaba. Dice lo mismo sin quitarle sitio a nada.
    const el = render({ excavando: true });
    const aviso = el.querySelector('.excavando-pie');

    expect(aviso).not.toBeNull();
    expect(aviso!.textContent).toContain('Excavando');
    // Y que sea un estado, no decoración: quien use lector de pantalla también
    // tiene que enterarse de que hay algo en marcha.
    expect(aviso!.getAttribute('role')).toBe('status');
  });

  it('el progreso se pinta cuando lo hay', () => {
    const el = render({ excavando: true, excavacionProgreso: '12 de 340 conversaciones' });

    expect(el.querySelector('.excavando-pie .cuanto')!.textContent).toContain(
      '12 de 340',
    );
  });

  it('sin progreso no se pinta un hueco', () => {
    const el = render({ excavando: true });

    expect(el.querySelector('.excavando-pie .cuanto')).toBeNull();
  });

  it('bloqueado se deshabilita y se explica por qué', () => {
    const el = render({
      excavarBloqueado: true,
      excavarMotivo: 'WhatsApp no está conectado.',
    });

    expect(boton(el)!.disabled).toBe(true);
    expect(boton(el)!.title).toBe('WhatsApp no está conectado.');
  });

  it('un botón bloqueado no emite nada aunque le llegue un clic', () => {
    const el = render({ excavarBloqueado: true });
    let pedidas = 0;
    fixture.componentInstance.excavarTodo.subscribe(() => (pedidas += 1));

    boton(el)!.click();

    expect(pedidas).toBe(0);
  });

  it('excavar y recargar la lista son cosas distintas', () => {
    // `loading` es la lista refrescándose; `excavando` es la extracción. Que
    // una recarga de fondo tapara el botón sería confundir dos cosas que duran
    // órdenes de magnitud distintas.
    const el = render({ loading: true, excavando: false });

    expect(boton(el)).not.toBeNull();
  });
});
