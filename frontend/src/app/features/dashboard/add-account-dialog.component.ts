import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  OnDestroy,
  computed,
  inject,
  output,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { AccountService, WhatsAppAccountInfo } from '../../core/services/account.service';
import { SessionService } from '../../core/services/session.service';

type Paso = 'nombre' | 'esperando_qr' | 'qr' | 'conectando' | 'listo' | 'error';

/**
 * Añadir OTRA cuenta de WhatsApp, sin tocar la que ya está funcionando.
 *
 * LO QUE NO PUEDE PASAR
 * ---------------------
 * Mientras esto está abierto, la cuenta que el usuario ya tenía sigue
 * conectada, con su sesión, su historial y su copia en Drive intactos. Por eso
 * la cuenta nueva se crea ANTES de pedir el código y todo el emparejamiento va
 * dirigido a ella con `account_id`: sin eso, el QR saldría de la sesión activa
 * y escanearlo la habría re-vinculado —perdiendo la que estaba.
 *
 * NO HAY UN SEGUNDO EXTRACTOR
 * ---------------------------
 * Se reutiliza el emparejamiento de siempre. Lo único distinto es a qué cuenta
 * va dirigido; de ahí en adelante —sesión, Signal, historial, semillas, media,
 * reintentos— es exactamente el mismo camino, en el runtime propio de la
 * cuenta nueva.
 *
 * SE ACTIVA AL FINAL, NO AL EMPEZAR
 * ---------------------------------
 * Cambiar el contexto al crearla dejaría al usuario mirando una lista vacía
 * mientras escanea, y sin forma evidente de volver. Se activa cuando ya tiene
 * sesión y algo que enseñar.
 */
@Component({
  selector: 'app-add-account-dialog',
  standalone: true,
  imports: [FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="fondo" (click)="intentarCerrar()">
      <div
        class="dialogo"
        role="dialog"
        aria-modal="true"
        aria-labelledby="titulo-agregar"
        (click)="$event.stopPropagation()"
      >
        <header>
          <h2 id="titulo-agregar">Agregar cuenta de WhatsApp</h2>
          <button
            type="button"
            class="cerrar"
            aria-label="Cerrar"
            (click)="intentarCerrar()"
          >
            ✕
          </button>
        </header>

        @switch (paso()) {
          @case ('nombre') {
            <p class="explica">
              Vas a vincular <b>otro</b> número. La cuenta que ya tienes sigue
              conectada y sus mensajes no se tocan.
            </p>
            <label class="campo">
              <span>Nombre de la cuenta <small>(opcional)</small></span>
              <input
                type="text"
                maxlength="60"
                placeholder="WhatsApp de trabajo"
                [(ngModel)]="nombre"
                (keyup.enter)="empezar()"
              />
            </label>
            <p class="fino">
              Si lo dejas vacío usaremos el nombre del perfil de ese WhatsApp.
              Puedes cambiarlo después.
            </p>
            <div class="acciones">
              <button type="button" class="secundario" (click)="cancelar()">
                Cancelar
              </button>
              <button type="button" class="principal" (click)="empezar()">
                Continuar
              </button>
            </div>
          }

          @case ('esperando_qr') {
            <p class="estado"><span class="girando"></span> Preparando el código…</p>
            <p class="fino">Esto tarda unos segundos.</p>
          }

          @case ('qr') {
            <p class="explica">Escanea este código con el WhatsApp que quieres agregar.</p>
            <div class="qr">
              <img [src]="qrUrl()" alt="Código QR para vincular la cuenta nueva" />
            </div>
            <ol class="pasos">
              <li>Abre <b>WhatsApp</b> en ese teléfono.</li>
              <li>Menú › <b>Dispositivos vinculados</b>.</li>
              <li><b>Vincular un dispositivo</b> y apunta la cámara aquí.</li>
            </ol>
            <p class="fino">El código se renueva solo si caduca.</p>
          }

          @case ('conectando') {
            <p class="estado"><span class="girando"></span> Conectando…</p>
            <p class="fino">
              Ya está vinculado. Estamos abriendo la sesión y empezando a traer
              las conversaciones.
            </p>
          }

          @case ('listo') {
            <p class="estado bien">✓ Cuenta agregada</p>
            <p class="fino">
              Ya puedes cambiar entre tus cuentas desde el selector. La
              extracción sigue en segundo plano.
            </p>
            <div class="acciones">
              <button type="button" class="principal" (click)="terminar()">
                Ver esta cuenta
              </button>
            </div>
          }

          @case ('error') {
            <p class="estado mal">{{ error() }}</p>
            <div class="acciones">
              <button type="button" class="secundario" (click)="cancelar()">
                Cerrar
              </button>
              <button type="button" class="principal" (click)="empezar()">
                Reintentar
              </button>
            </div>
          }
        }
      </div>
    </div>
  `,
  styles: [
    `
      .fondo {
        position: fixed;
        inset: 0;
        z-index: 100;
        display: grid;
        place-items: center;
        padding: 16px;
        background: rgba(0, 0, 0, 0.55);
      }
      .dialogo {
        width: min(420px, 100%);
        display: grid;
        gap: 12px;
        padding: 18px;
        border-radius: 12px;
        border: 1px solid var(--border);
        background: var(--surface, #1f2c33);
        box-shadow: 0 20px 60px rgba(0, 0, 0, 0.45);
      }
      header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
      }
      h2 {
        margin: 0;
        font-size: var(--font-size-md, 1rem);
      }
      .cerrar {
        background: none;
        border: 0;
        color: var(--text-muted);
        font: inherit;
        cursor: pointer;
        padding: 4px 8px;
        border-radius: 6px;
      }
      .cerrar:hover {
        background: var(--surface-hover, rgba(127, 127, 127, 0.12));
      }
      .explica {
        margin: 0;
        font-size: var(--font-size-sm);
      }
      .fino {
        margin: 0;
        font-size: var(--font-size-xs);
        color: var(--text-muted);
      }
      .campo {
        display: grid;
        gap: 5px;
        font-size: var(--font-size-sm);
      }
      .campo input {
        font: inherit;
        color: inherit;
        padding: 8px 10px;
        border-radius: 8px;
        border: 1px solid var(--border);
        background: var(--surface-hover, rgba(127, 127, 127, 0.08));
      }
      .qr {
        display: grid;
        place-items: center;
        padding: 12px;
        border-radius: 10px;
        background: #fff;
      }
      .qr img {
        width: 100%;
        max-width: 260px;
        image-rendering: pixelated;
      }
      .pasos {
        margin: 0;
        padding-left: 18px;
        font-size: var(--font-size-xs);
        color: var(--text-muted);
        display: grid;
        gap: 3px;
      }
      .estado {
        margin: 0;
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: var(--font-size-sm);
      }
      .estado.bien {
        color: var(--accent, #25d366);
      }
      .estado.mal {
        color: var(--danger, #d9534f);
      }
      .girando {
        width: 12px;
        height: 12px;
        border-radius: 50%;
        border: 2px solid var(--border);
        border-top-color: var(--accent, #25d366);
        animation: gira 0.8s linear infinite;
      }
      @keyframes gira {
        to {
          transform: rotate(360deg);
        }
      }
      .acciones {
        display: flex;
        gap: 8px;
        justify-content: flex-end;
      }
      .acciones button {
        font: inherit;
        padding: 8px 14px;
        border-radius: 8px;
        cursor: pointer;
        border: 1px solid var(--border);
        background: transparent;
        color: inherit;
      }
      .acciones .principal {
        border-color: var(--accent, #25d366);
        color: var(--accent, #25d366);
      }
    `,
  ],
})
export class AddAccountDialogComponent implements OnDestroy {
  private readonly cuentasApi = inject(AccountService);
  private readonly sesionApi = inject(SessionService);
  private readonly destroyRef = inject(DestroyRef);

  /** Se cerró sin terminar. La cuenta creada se queda, sin vincular. */
  readonly cerrar = output<void>();
  /** Terminó: la cuenta nueva está lista y hay que cambiar a ella. */
  readonly listo = output<WhatsAppAccountInfo>();

  readonly paso = signal<Paso>('nombre');
  readonly error = signal<string>('');
  readonly qrUrl = signal<string>('');
  nombre = '';

  private cuentaNueva?: WhatsAppAccountInfo;
  private temporizador?: ReturnType<typeof setInterval>;

  /** Mientras se está escaneando, cerrar por accidente cuesta repetirlo todo. */
  readonly enMarcha = computed(() =>
    ['esperando_qr', 'qr', 'conectando'].includes(this.paso()),
  );

  empezar() {
    this.error.set('');
    this.paso.set('esperando_qr');

    // La cuenta ANTES que el código. El emparejamiento va dirigido a ella; sin
    // crearla primero, el QR saldría de la sesión activa y escanearlo la
    // re-vincularía, perdiendo la que ya estaba funcionando.
    const crear = this.cuentaNueva
      ? null
      : this.cuentasApi.crear(this.nombre.trim() || undefined);

    if (!crear) {
      this.pedirCodigo();
      return;
    }
    crear.pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (cuenta) => {
        this.cuentaNueva = cuenta;
        this.pedirCodigo();
      },
      error: () => this.fallar('No se pudo preparar la cuenta nueva.'),
    });
  }

  private pedirCodigo() {
    const id = this.cuentaNueva?.id;
    if (!id) return this.fallar('No se pudo preparar la cuenta nueva.');

    this.sesionApi
      .pair(id)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: () => this.vigilar(id),
        error: () =>
          this.fallar('No se pudo pedir el código. Inténtalo otra vez.'),
      });
  }

  /**
   * Sondeo, no eventos.
   *
   * El canal de tiempo real pertenece a la cuenta que se está MIRANDO, y esta
   * todavía no lo es: sus eventos no llegarían por ahí. Preguntar por su
   * estado cada dos segundos es lo correcto mientras dura el alta, y se para
   * en cuanto termina.
   */
  private vigilar(id: string) {
    this.parar();
    this.temporizador = setInterval(() => {
      this.sesionApi
        .session(id)
        .pipe(takeUntilDestroyed(this.destroyRef))
        .subscribe({
          next: (estado) => {
            if (estado.connected) {
              this.parar();
              this.paso.set('listo');
              return;
            }
            if (estado.state === 'CONNECTING') {
              this.paso.set('conectando');
              return;
            }
            if (estado.qrAvailable) {
              this.qrUrl.set(this.sesionApi.qrImageUrl(estado.generation, id));
              this.paso.set('qr');
            }
          },
          error: () => {
            /* Un sondeo que falla no rompe el alta: se reintenta al siguiente. */
          },
        });
    }, 2000);
  }

  private fallar(mensaje: string) {
    this.parar();
    this.error.set(mensaje);
    this.paso.set('error');
  }

  private parar() {
    if (this.temporizador) clearInterval(this.temporizador);
    this.temporizador = undefined;
  }

  intentarCerrar() {
    // Cerrar a mitad del escaneo obligaría a repetirlo entero. Se pregunta.
    if (this.enMarcha()) {
      const seguro = confirm(
        '¿Cancelar? La cuenta quedará creada pero sin vincular, y tendrás que volver a escanear.',
      );
      if (!seguro) return;
    }
    this.cancelar();
  }

  cancelar() {
    this.parar();
    this.cerrar.emit();
  }

  terminar() {
    this.parar();
    if (this.cuentaNueva) this.listo.emit(this.cuentaNueva);
    else this.cerrar.emit();
  }

  ngOnDestroy() {
    this.parar();
  }
}
