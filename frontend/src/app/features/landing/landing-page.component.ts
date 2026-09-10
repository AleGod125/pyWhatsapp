import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  inject,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { AuthService } from '../../core/services/auth.service';
import { rutaPara } from '../../core/guards/onboarding.guard';
import { I18nService, ModoDeIdioma } from '../../core/i18n/i18n.service';
import { TranslatePipe } from '../../core/i18n/translate.pipe';

/**
 * La primera pantalla. Lo que alguien ve antes de decidir si entra.
 *
 * POR QUÉ NO EMPIEZA EN EL LOGIN
 * ------------------------------
 * Un formulario de acceso no explica nada. Quien abre la aplicación por
 * primera vez —o quien vuelve después de meses— se encontraba con dos campos y
 * ninguna respuesta a la única pregunta que importa: qué hace esto y por qué
 * debería confiarle su WhatsApp.
 *
 * LO QUE SE PROMETE AQUÍ ES LO QUE HACE
 * -------------------------------------
 * Ni una afirmación que el producto no cumpla. Nada de cifras inventadas, ni
 * de «millones de usuarios», ni de funciones que no existan. Todo lo que se
 * dice en esta página está implementado y se puede comprobar:
 *
 * * se vincula como dispositivo complementario, igual que WhatsApp Web;
 * * pide el historial conversación por conversación, no solo lo que llega solo;
 * * guarda en PostgreSQL local y sube una copia cifrada al Drive DEL USUARIO;
 * * varias cuentas de WhatsApp, aisladas entre sí;
 * * es de SOLO LECTURA: no puede enviar mensajes, y eso es una decisión, no
 *   una carencia.
 *
 * Prometer de más en una página que habla de una copia de seguridad es
 * exactamente lo que haría que no se confiara en ella.
 */
@Component({
  selector: 'app-landing-page',
  standalone: true,
  imports: [RouterLink, TranslatePipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './landing-page.component.html',
  styleUrl: './landing-page.component.scss',
})
export class LandingPageComponent {
  private readonly auth = inject(AuthService);
  private readonly destroyRef = inject(DestroyRef);
  readonly i18n = inject(I18nService);

  /**
   * Los idiomas, con su nombre EN SU PROPIO idioma.
   *
   * Quien busca portugués busca «Português», no «Portugués». Escribir la lista
   * en castellano obliga a saber castellano para salir del castellano, que es
   * justo al revés de lo que hace falta.
   */
  readonly idiomas = [
    { codigo: 'es', nombre: 'Español' },
    { codigo: 'en', nombre: 'English' },
    { codigo: 'pt', nombre: 'Português' },
    { codigo: 'fr', nombre: 'Français' },
    { codigo: 'de', nombre: 'Deutsch' },
    { codigo: 'it', nombre: 'Italiano' },
  ] as const;

  cambiarIdioma(modo: string) {
    this.i18n.usar(modo as ModoDeIdioma);
  }

  /**
   * Las secciones repetidas salen de una lista, no de bloques copiados.
   *
   * Con seis tarjetas escritas a mano, traducir obligaba a tocar seis sitios y
   * bastaba olvidar uno para dejar media portada en otro idioma. Aquí solo hay
   * una clave por sección y el catálogo pone el texto.
   */
  readonly tarjetas = [
    { clave: 'c1', icono: '📜' },
    { clave: 'c2', icono: '🗄️' },
    { clave: 'c3', icono: '📎' },
    { clave: 'c4', icono: '👥' },
    { clave: 'c5', icono: '⚡' },
    { clave: 'c6', icono: '🔒' },
  ] as const;

  readonly pasos = ['s1', 's2', 's3', 's4'] as const;
  readonly limites = ['l1', 'l2', 'l3', 'l4'] as const;

  /**
   * A dónde lleva el botón principal.
   *
   * Quien ya entró no tiene que volver a presentarse: se le manda al paso en
   * el que se quedó. Quien no, al registro. Se resuelve en segundo plano y
   * mientras tanto el botón ya funciona con el valor por defecto: una página
   * de entrada cuyo botón no se puede pulsar durante dos segundos no sirve.
   */
  readonly destino = signal('/login');
  /** CLAVE del catálogo, no el texto: si fuera texto, cambiar de idioma no lo
      cambiaría —se resolvió una vez y se quedó en el de entonces. */
  readonly etiqueta = signal('landing.cta_start');
  readonly volviendo = signal(false);

  constructor() {
    this.auth
      .onboarding()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (estado) => {
          if (!estado.authenticated) return;
          this.destino.set(rutaPara(estado.nextStep));
          this.etiqueta.set(
            estado.nextStep === 'dashboard'
              ? 'landing.cta_open'
              : 'landing.cta_continue',
          );
          this.volviendo.set(true);
        },
        // Sin backend la página se lee igual y el botón sigue llevando al
        // acceso. Una portada que se cae porque la API no responde es peor
        // que una portada que no sabe quién eres.
        error: () => undefined,
      });
  }
}
