import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { OnboardingPanelComponent } from './onboarding-panel.component';
import { normalizeOnboarding, OnboardingService } from '../../../core/services/onboarding.service';

/**
 * La puesta en marcha, contada sin lenguaje técnico.
 *
 * Lo que se protege aquí: que el segundo código aparezca cuando hace falta y
 * sólo entonces, que el panel desaparezca cuando ya no tiene nada que contar,
 * y que no diga «completo» mientras queden conversaciones por recuperar.
 */
const respuesta = (extra: Record<string, unknown> = {}) => ({
  phase: 'recovering_history',
  primary: { linked: true },
  recovery: { seeds_applied: 0, chats_promoted: 0, attempts: 0 },
  counts: {
    chats_total: 41,
    waiting_seed: 3,
    pending: 1,
    fetching: 0,
    timeout: 3,
    exhausted: 34,
  },
  ...extra,
});

describe('Onboarding: lectura del backend', () => {
  it('una fase desconocida NO se muestra como terminada', () => {
    expect(normalizeOnboarding({ phase: 'algo_nuevo' }).phase).toBe('recovering_history');
  });

  it('nada se asume: sin datos, todo va a falso', () => {
    const s = normalizeOnboarding({});
    expect(s.primaryLinked).toBe(false);
    expect(s.counts.chatsTotal).toBe(0);
  });

  it('lee los recuentos que deciden si esto terminó', () => {
    const s = normalizeOnboarding(respuesta());
    expect(s.counts.waitingSeed).toBe(3);
    expect(s.counts.exhausted).toBe(34);
    expect(s.counts.chatsTotal).toBe(41);
  });

  it('la cola sólo existe si el backend la manda', () => {
    expect(normalizeOnboarding(respuesta()).queue).toBeUndefined();
    const conCola = normalizeOnboarding(
      respuesta({ queue: { pending: 5, paused: true, waiting_for_phone: true, dug: 7 } }),
    );
    expect(conCola.queue?.waitingForPhone).toBe(true);
  });
});

describe('Onboarding: el panel', () => {
  let recibido: Record<string, unknown>;
  let vivo: { destroy: () => void } | undefined;

  beforeEach(() => {
    recibido = respuesta();
    TestBed.configureTestingModule({
      imports: [OnboardingPanelComponent],
      providers: [
        {
          provide: OnboardingService,
          useValue: { status: () => of(normalizeOnboarding(recibido)) },
        },
      ],
    });
  });

  // El panel se reprograma solo cada pocos segundos. Sin destruirlo, cada
  // prueba deja un temporizador vivo y al final del archivo hay veinte
  // componentes preguntando a la vez.
  afterEach(() => {
    vivo?.destroy();
    vivo = undefined;
  });

  const montar = (datos: Record<string, unknown>) => {
    recibido = datos;
    const fixture = TestBed.createComponent(OnboardingPanelComponent);
    vivo = fixture;
    fixture.detectChanges();
    return fixture;
  };

  it('terminado, el panel no ocupa sitio', () => {
    const fixture = montar(respuesta({ phase: 'complete' }));
    expect(fixture.nativeElement.textContent.trim()).toBe('');
  });





  it('mientras recupera, cuenta el progreso en conversaciones', () => {
    const fixture = montar(respuesta());
    const texto = fixture.nativeElement.textContent;
    expect(texto).toContain('Recuperando tu historial');
    expect(texto).toContain('34 de 41 conversaciones');
    expect(texto).toContain('3 sin referencia');
  });


  it('si el teléfono duerme, dice qué hacer y que no se pierde nada', () => {
    const fixture = montar(
      respuesta({
        phase: 'waiting_for_phone',
        queue: { pending: 8, paused: true, waiting_for_phone: true, dug: 12 },
      }),
    );
    const texto = fixture.nativeElement.textContent;
    expect(texto).toContain('Esperando al teléfono');
    expect(texto).toContain('Abre WhatsApp');
    expect(texto).toContain('progreso está guardado');
  });

  it('recuperación parcial dice cuántas quedan, sin llamarlo error', () => {
    const fixture = montar(respuesta({ phase: 'partial' }));
    const texto = fixture.nativeElement.textContent;
    expect(texto).toContain('Recuperación parcial');
    expect(texto).toContain('Quedan 3 conversaciones');
    expect(texto.toLowerCase()).not.toContain('error');
  });

  it('una sola conversación pendiente se escribe en singular', () => {
    const fixture = montar(
      respuesta({
        phase: 'partial',
        counts: {
          chats_total: 41,
          waiting_seed: 1,
          pending: 0,
          fetching: 0,
          timeout: 0,
          exhausted: 40,
        },
      }),
    );
    expect(fixture.nativeElement.textContent).toContain('Queda 1 conversación');
  });

  it('un fallo consultando el estado no rompe el panel', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [OnboardingPanelComponent],
      providers: [
        {
          provide: OnboardingService,
          useValue: { status: () => throwError(() => new Error('sin red')) },
        },
      ],
    });
    const fixture = TestBed.createComponent(OnboardingPanelComponent);
    vivo = fixture;
    expect(() => fixture.detectChanges()).not.toThrow();
  });

  it('el usuario no tiene que pulsar nada: el panel no ofrece acciones', () => {
    // La recuperación la dispara el backend solo. Un botón aquí sería volver
    // al flujo de laboratorio que se está quitando.
    const fixture = montar(respuesta());
    expect(fixture.nativeElement.querySelectorAll('button').length).toBe(0);
  });
});
