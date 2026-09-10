import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { LandingPageComponent } from './landing-page.component';
import { AuthService } from '../../core/services/auth.service';
import { routes } from '../../app.routes';
import { rutaPara } from '../../core/guards/onboarding.guard';
import { OnboardingStep } from '../../core/models/api.models';
// Con el prefijo `cat`, y NO con el código a secas: `it` es el italiano y
// también es la función de Vitest que declara cada prueba. Importarlo como
// `it` la tapaba, y el error que salía —«This expression is not callable»—
// no menciona el idioma por ninguna parte.
import catEs from '../../../assets/i18n/es.json';
import catEn from '../../../assets/i18n/en.json';
import catPt from '../../../assets/i18n/pt.json';
import catFr from '../../../assets/i18n/fr.json';
import catDe from '../../../assets/i18n/de.json';
import catIt from '../../../assets/i18n/it.json';

/**
 * La portada: lo primero que ve cualquiera.
 *
 * LO QUE SE PROTEGE
 * -----------------
 * 1. Que sea de verdad lo primero. Quien no ha entrado y escribe `/dashboard`
 *    tiene que acabar aquí, no en un formulario que no explica nada.
 * 2. Que el botón funcione desde el primer instante. Una portada cuyo botón
 *    no se puede pulsar hasta que responde el backend no es una portada.
 * 3. Que no prometa lo que el producto no hace. Es una copia de seguridad: si
 *    la página exagera, lo que se pierde es justo lo que hace falta para que
 *    alguien le confíe su WhatsApp.
 */

function montar(estado?: unknown, falla = false) {
  TestBed.configureTestingModule({
    imports: [LandingPageComponent],
    providers: [
      provideRouter([]),
      {
        provide: AuthService,
        useValue: {
          onboarding: () =>
            falla ? throwError(() => new Error('sin backend')) : of(estado ?? {
              authenticated: false,
              nextStep: 'login' as OnboardingStep,
            }),
        },
      },
    ],
  });
  const fixture = TestBed.createComponent(LandingPageComponent);
  fixture.detectChanges();
  return fixture;
}

// ---------------------------------------------------------------------------
// El botón
// ---------------------------------------------------------------------------

describe('El botón de la portada', () => {
  it('sin sesión lleva al acceso', () => {
    const fixture = montar();
    expect(fixture.componentInstance.destino()).toBe('/login');
    // Una CLAVE, no un texto. Si guardara el texto ya traducido, cambiar de
    // idioma no cambiaría el botón: se resolvió una vez y ahí se quedó.
    expect(fixture.componentInstance.etiqueta()).toBe('landing.cta_start');
  });

  it('con sesión completa lleva al panel', () => {
    // Quien ya entró no tiene que volver a presentarse.
    const fixture = montar({ authenticated: true, nextStep: 'dashboard' });
    expect(fixture.componentInstance.destino()).toBe('/dashboard');
    expect(fixture.componentInstance.etiqueta()).toBe('landing.cta_open');
  });

  it('a medio camino lleva al paso que toca', () => {
    const fixture = montar({ authenticated: true, nextStep: 'pairing' });
    expect(fixture.componentInstance.destino()).toBe('/pairing');
    expect(fixture.componentInstance.etiqueta()).toBe('landing.cta_continue');
  });

  it('sin backend la página se lee y el botón sigue sirviendo', () => {
    // Una portada que se cae porque la API no responde es peor que una que no
    // sabe quién eres: es lo único que se puede enseñar cuando algo va mal.
    const fixture = montar(undefined, true);
    expect(fixture.componentInstance.destino()).toBe('/login');
    expect(fixture.nativeElement.textContent).toContain('WhatsApp Backup');
  });
});

// ---------------------------------------------------------------------------
// Es la PRIMERA pantalla, y eso se comprueba en el enrutado
// ---------------------------------------------------------------------------

describe('La portada va primero', () => {
  it('la raíz es la portada, no el panel', () => {
    const raiz = routes.find((r) => r.path === '' && r.pathMatch === 'full');
    expect(raiz).toBeDefined();
    expect(raiz?.redirectTo).toBeUndefined();
    expect(raiz?.loadComponent).toBeDefined();
  });

  it('no tiene guard: se ve haya sesión o no', () => {
    const raiz = routes.find((r) => r.path === '' && r.pathMatch === 'full');
    expect(raiz?.canActivate).toBeUndefined();
  });

  it('una URL que no existe cae en la portada', () => {
    const comodin = routes.find((r) => r.path === '**');
    expect(comodin?.redirectTo).toBe('');
  });

  it('quien no ha entrado acaba en la portada, no en el formulario', () => {
    // Es la regla que hace que la portada sea obligatoria: los guards mandan
    // el paso `login` aquí, así que escribir `/dashboard` a mano no salta la
    // presentación.
    expect(rutaPara('login')).toBe('/');
  });

  it('el formulario sigue siendo accesible por su URL', () => {
    // Obligar a pasar por la portada a quien ya sabe lo que quiere sería
    // estorbar. La portada presenta; no es un peaje.
    expect(routes.some((r) => r.path === 'login')).toBe(true);
    expect(routes.some((r) => r.path === 'register')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Lo que dice
// ---------------------------------------------------------------------------

describe('Lo que promete la portada', () => {
  // Se comprueba en el CATÁLOGO, no en el DOM: el texto vive ahí desde que la
  // portada se traduce, y comprobarlo en el catálogo lo verifica además en los
  // seis idiomas en vez de solo en el que resuelva la prueba.

  it('dice que es de solo lectura', () => {
    // Es la garantía más fuerte del producto y la que más tranquiliza a quien
    // duda en vincular su WhatsApp. Si desapareciera, la página dejaría de
    // contar lo que de verdad la distingue.
    expect(catEs.landing.readonly).toContain('No envía mensajes');
    expect(catEn.landing.readonly).toContain('does not send messages');
  });

  it('dice dónde acaban los datos', () => {
    expect(catEs.landing.c2_p).toContain('Drive');
    expect(catEs.landing.c2_p).toContain('PostgreSQL');
  });

  it('admite sus límites en vez de esconderlos', () => {
    expect(catEs.landing.l3_b).toContain('No recupera lo que tu teléfono ya no tiene');
    expect(catEn.landing.l3_b).toContain('cannot recover');
  });

  it('deja claro que no es de WhatsApp', () => {
    expect(catEs.landing.foot_legal).toContain('No está afiliado a WhatsApp');
    expect(catEn.landing.foot_legal).toContain('Not affiliated with WhatsApp');
  });

  it('lleva al acceso desde la barra, sin tener que bajar', () => {
    const enlaces = montar().nativeElement.querySelectorAll('a');
    const destinos = Array.from(enlaces).map((a) =>
      (a as HTMLAnchorElement).getAttribute('href'),
    );
    expect(destinos).toContain('/login');
  });
});


// ---------------------------------------------------------------------------
// El idioma se cambia de verdad
// ---------------------------------------------------------------------------

describe('La portada traducida', () => {
  it('los seis catálogos tienen TODAS las claves', () => {
    // El fallo que cierra: el selector cambiaba el idioma y el texto seguía en
    // castellano, porque estaba escrito a mano en la plantilla. Ahora sale del
    // catálogo — y si a un idioma le faltara una clave, esa frase se caería al
    // castellano y quedaría media portada en dos idiomas.
    const claves = Object.keys(catEs.landing).sort();
    for (const [nombre, catalogo] of Object.entries({ catEn, catPt, catFr, catDe, catIt })) {
      expect(Object.keys(catalogo.landing).sort(), `faltan claves en ${nombre}`).toEqual(
        claves,
      );
    }
  });

  it('ningún idioma deja una clave vacía', () => {
    for (const [nombre, catalogo] of Object.entries({ catEs, catEn, catPt, catFr, catDe, catIt })) {
      for (const [clave, valor] of Object.entries(catalogo.landing)) {
        expect(String(valor).trim().length, `${nombre}.${clave} vacía`).toBeGreaterThan(0);
      }
    }
  });

  it('cada idioma tiene su propio botón', () => {
    // Si todos compartieran el mismo texto, el selector parecería roto aunque
    // funcionara: lo primero que se mira al cambiar de idioma es el botón.
    const botones = [catEs, catEn, catPt, catFr, catDe, catIt].map((c) => c.landing.cta_start);
    expect(new Set(botones).size).toBeGreaterThan(1);
  });
});
