import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  HostListener,
  computed,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import {
  AccountService,
  WhatsAppAccountInfo,
  nombreDeCuenta,
} from '../../core/services/account.service';

/**
 * El selector de cuenta de WhatsApp, bajo el título.
 *
 * QUÉ RESUELVE
 * ------------
 * Un usuario puede tener varios WhatsApp vinculados —el personal, el del
 * trabajo, el de otra persona— y son contextos separados: sus chats, su
 * historial y su copia de seguridad no tienen nada que ver.
 *
 * Mostrarlos juntos en una sola lista no sería «ver más»: sería no poder
 * saber de cuál es cada conversación, porque nada en la fila lo dice.
 *
 * NO ES UN FILTRO
 * ---------------
 * Elegir otra cuenta cambia el contexto entero. Quien recibe `cambiar` tiene
 * que vaciar lo que tenía y volver a pedirlo, canal de eventos incluido; no
 * quedarse con la lista anterior escondiendo filas.
 *
 * CUANDO HAY UNA SOLA
 * -------------------
 * Se sigue enseñando, sin la flecha ni el menú. Da igual que no haya nada que
 * elegir: decir **cuál** WhatsApp se está viendo es información, y el día que
 * aparezca la segunda el sitio ya es familiar.
 */
@Component({
  selector: 'app-account-switcher',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="envoltorio">
      <button
        type="button"
        class="actual"
        [class.sola]="!hayVarias()"
        [attr.aria-expanded]="hayVarias() ? abierto() : null"
        [attr.aria-haspopup]="hayVarias() ? 'menu' : null"
        [disabled]="!hayVarias() && !permitirAgregar()"
        (click)="alternar()"
      >
        <span class="punto" [class.viva]="activa()?.linked"></span>
        <span class="nombre">{{ nombre(activa()) }}</span>
        @if (activa()?.accountType === 'business') {
          <span class="etiqueta">Business</span>
        }
        @if (hayVarias() || permitirAgregar()) {
          <span class="flecha" aria-hidden="true">▾</span>
        }
      </button>

      @if (abierto()) {
        <div class="menu" role="menu">
          @for (cuenta of cuentas(); track cuenta.id) {
            <button
              type="button"
              role="menuitemradio"
              [attr.aria-checked]="cuenta.id === activa()?.id"
              [class.elegida]="cuenta.id === activa()?.id"
              (click)="elegir(cuenta)"
            >
              <span
                class="punto"
                [class.viva]="cuenta.linked"
                [class.caida]="cuenta.disconnected"
              ></span>
              <span class="nombre">{{ nombre(cuenta) }}</span>
              <!-- Un corte de red y una desvinculación NO son lo mismo, y lo
                   que el usuario tiene que hacer tampoco: una vuelve sola, la
                   otra necesita que escanee. Decir «sin vincular» en las dos
                   hacía que un corte pareciera grave y que una desvinculación
                   de verdad pareciera pasajera. -->
              @if (cuenta.needsRelink) {
                <small class="fino">hay que vincular</small>
              } @else if (cuenta.disconnected) {
                <small class="fino">desconectada</small>
              }
            </button>
          }

          @if (permitirAgregar()) {
            <div class="separador" role="separator"></div>
            <button type="button" role="menuitem" class="agregar" (click)="agregar()">
              + Agregar cuenta de WhatsApp
            </button>
          }
        </div>
      }
    </div>
  `,
  styles: [
    `
      .envoltorio {
        position: relative;
        padding: 0 16px 10px;
      }
      .actual {
        display: flex;
        align-items: center;
        gap: 8px;
        width: 100%;
        font: inherit;
        color: inherit;
        cursor: pointer;
        padding: 7px 10px;
        border-radius: 8px;
        border: 1px solid var(--border);
        background: var(--surface, rgba(127, 127, 127, 0.08));
      }
      .actual:hover:not(:disabled) {
        background: var(--surface-hover, rgba(127, 127, 127, 0.14));
      }
      .actual:disabled {
        cursor: default;
      }
      .punto {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex: 0 0 auto;
        background: var(--text-muted);
      }
      .punto.viva {
        background: var(--accent, #25d366);
      }
      /* Ámbar: se cayó, pero vuelve sola. Ni verde (mentiría) ni gris (haría
         pensar que hay que escanear). */
      .punto.caida {
        background: var(--warning, #e0a800);
      }
      .nombre {
        flex: 1 1 auto;
        text-align: left;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .etiqueta {
        font-size: var(--font-size-xs);
        border: 1px solid var(--border);
        border-radius: 4px;
        padding: 0 5px;
        color: var(--text-muted);
      }
      .flecha {
        color: var(--text-muted);
        font-size: var(--font-size-sm);
      }
      .menu {
        position: absolute;
        top: calc(100% - 4px);
        left: 16px;
        right: 16px;
        z-index: 30;
        display: grid;
        gap: 2px;
        padding: 6px;
        border: 1px solid var(--border);
        border-radius: 10px;
        background: var(--surface, #1f2c33);
        box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
      }
      .menu button {
        display: flex;
        align-items: center;
        gap: 8px;
        text-align: left;
        background: none;
        border: 0;
        color: inherit;
        font: inherit;
        padding: 9px 10px;
        border-radius: 6px;
        cursor: pointer;
      }
      .menu button:hover {
        background: var(--surface-hover, rgba(127, 127, 127, 0.12));
      }
      .menu button.elegida {
        background: var(--surface-hover, rgba(127, 127, 127, 0.12));
        font-weight: 600;
      }
      .separador {
        height: 1px;
        background: var(--border);
        margin: 4px 2px;
      }
      .agregar {
        color: var(--accent, #25d366);
      }
      .fino {
        color: var(--text-muted);
        font-size: var(--font-size-xs);
      }
    `,
  ],
})
export class AccountSwitcherComponent {
  private readonly cuentasApi = inject(AccountService);
  private readonly host = inject(ElementRef<HTMLElement>);

  /** Si se ofrece añadir otra. Se apaga mientras hay una vinculación en curso. */
  readonly permitirAgregar = input(true);

  readonly cambiar = output<WhatsAppAccountInfo>();
  readonly agregarCuenta = output<void>();

  readonly cuentas = this.cuentasApi.cuentas;
  readonly activa = this.cuentasApi.activa;
  readonly hayVarias = computed(() => this.cuentas().length > 1);

  readonly abierto = signal(false);

  nombre(cuenta: WhatsAppAccountInfo | undefined) {
    return nombreDeCuenta(cuenta);
  }

  alternar() {
    if (!this.hayVarias() && !this.permitirAgregar()) return;
    this.abierto.update((v) => !v);
  }

  elegir(cuenta: WhatsAppAccountInfo) {
    this.abierto.set(false);
    // Elegir la que ya está no puede costar un recargado entero del contexto.
    if (cuenta.id === this.activa()?.id) return;
    this.cambiar.emit(cuenta);
  }

  agregar() {
    this.abierto.set(false);
    this.agregarCuenta.emit();
  }

  /** Un clic fuera cierra el menú; si no, se queda abierto por la página. */
  @HostListener('document:click', ['$event'])
  fuera(evento: MouseEvent) {
    if (!this.abierto()) return;
    if (!this.host.nativeElement.contains(evento.target as Node)) {
      this.abierto.set(false);
    }
  }

  @HostListener('document:keydown.escape')
  escapar() {
    this.abierto.set(false);
  }
}
