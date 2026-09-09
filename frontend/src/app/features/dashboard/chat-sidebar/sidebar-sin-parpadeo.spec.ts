/**
 * La lista no puede desaparecer mientras se extrae.
 *
 * EL FALLO, TAL Y COMO SE VEIA
 * ----------------------------
 * Durante la extraccion la barra lateral parpadeaba varias veces por segundo:
 * la lista entera se sustituia por seis rectangulos grises y volvia. Parecia
 * la pagina recargandose sola.
 *
 * La causa era una sola condicion: `@if (loading())` mostraba los esqueletos
 * SIEMPRE que hubiera una carga en curso, y cada aviso de historial disparaba
 * una. Los esqueletos son para cuando no hay NADA que ensenar, no para tapar
 * lo que ya esta en pantalla.
 */
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { ChatSidebarComponent } from './chat-sidebar.component';
import type { Chat } from '../../../core/models/api.models';

function chat(id: string, displayName = `Chat ${id}`): Chat {
  return {
    id,
    jid: `${id}@s.whatsapp.net`,
    displayName,
    type: 'individual',
    messageCount: 1,
  };
}

describe('ChatSidebar: sin parpadeo', () => {
  let fixture: ComponentFixture<ChatSidebarComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ChatSidebarComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(ChatSidebarComponent);
  });

  function render(props: Record<string, unknown>) {
    for (const [clave, valor] of Object.entries(props)) {
      fixture.componentRef.setInput(clave, valor);
    }
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('con la lista puesta, una recarga NO la sustituye por esqueletos', () => {
    const el = render({ chats: [chat('1'), chat('2')], loading: true });

    // Lo que se comprueba es que la rama de esqueletos NO gana y que sigue
    // siendo la lista la que se pinta. Las filas en si no se cuentan: el
    // scroll virtual no materializa nada sin altura real, y en un entorno de
    // prueba no la hay. Contarlas seria medir JSDOM, no el componente.
    expect(el.querySelector('.skeletons')).toBeNull();
    expect(el.querySelector('cdk-virtual-scroll-viewport')).not.toBeNull();
  });

  it('sin nada que ensenar, los esqueletos SI aparecen', () => {
    const el = render({ chats: [], loading: true });

    expect(el.querySelector('.skeletons')).not.toBeNull();
  });

  it('una recarga con lista se anuncia con una barra, no vaciando', () => {
    const el = render({ chats: [chat('1')], loading: true });

    expect(el.querySelector('.recargando')).not.toBeNull();
  });

  it('sin recarga en curso no hay barra', () => {
    const el = render({ chats: [chat('1')], loading: false });

    expect(el.querySelector('.recargando')).toBeNull();
  });

  it('el componente recibe que conversaciones acaban de llegar', () => {
    render({ chats: [chat('1'), chat('2')], loading: false, recientes: new Set(['2']) });

    // La marca llega calculada desde el panel: el sidebar no decide cual es
    // nueva. Se comprueba que la recibe y la distingue; que se pinte con
    // animacion es cosa del CSS y del scroll virtual, que en un entorno de
    // prueba no materializa filas por no haber altura real.
    expect(fixture.componentInstance.recientes().has('2')).toBe(true);
    expect(fixture.componentInstance.recientes().has('1')).toBe(false);
  });

  it('por defecto no hay ninguna marcada', () => {
    render({ chats: [chat('1')], loading: false });

    expect(fixture.componentInstance.recientes().size).toBe(0);
  });
});
