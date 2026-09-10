import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormsModule } from '@angular/forms';
import { ChatLockService } from '../../../core/services/chat-lock.service';
import { Vista } from '../../../core/services/chat.service';

/**
 * Las entradas de «Archivados» y «Chats bloqueados», y la puerta de estos.
 *
 * DE DÓNDE SALE LA FORMA
 * ----------------------
 * De WhatsApp Web, que es lo que el usuario ya sabe usar: dos filas encima de
 * la lista, cada una con su contador, y al entrar una cabecera con la flecha
 * de volver. Nada de un menú escondido.
 *
 * LA ADVERTENCIA, QUE ES LO PRIMERO QUE PIDIÓ EL USUARIO
 * ------------------------------------------------------
 * Un chat solo llega marcado como restringido si el bloqueo está puesto **en
 * el teléfono**. `lockChatAction` es la única acción de app-state que Baileys
 * no procesa, así que el dato solo viaja dentro del historial, y solo si allí
 * ya venía marcado. Sin esa explicación, la sección aparece vacía y parece que
 * la función está rota.
 *
 * EL CÓDIGO NO ES EL DE WHATSAPP
 * ------------------------------
 * Y se dice con esas palabras. `chatLockSettings.secretCode` llega como bytes
 * opacos —WhatsApp manda un derivado, no el código—, y aunque lo mandara no
 * serviría: los mensajes de un chat restringido no están cifrados con él, se
 * guardan como los de cualquier otro. Prometer que es el mismo código sería
 * mentir sobre lo que protege.
 */
@Component({
  selector: 'app-secciones-de-chats',
  standalone: true,
  imports: [FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (vista() === 'normal') {
      <!-- «Archivados» se enseña SIEMPRE, como en WhatsApp Web.
           Esconderla cuando no hay ninguno parecía que la función no existía:
           el usuario la buscaba y no encontraba nada que pulsar. Vacía dice
           «0», que es información; ausente no dice nada. -->
      <button type="button" class="seccion" (click)="ir.emit('archivados')">
        <span class="icono" aria-hidden="true">🗄️</span>
        <span class="etiqueta">Archivados</span>
        <span class="cuenta" [class.ninguno]="archivados() === 0">{{
          archivados()
        }}</span>
      </button>
      @if (restringidos() > 0) {
        <button
          type="button"
          class="seccion restringida"
          (click)="pedirEntrada()"
        >
          <span class="icono" aria-hidden="true">🔒</span>
          <span class="etiqueta">Chats bloqueados</span>
          <span class="cuenta">{{ restringidos() }}</span>
        </button>
      } @else {
        <!-- Sin ninguno marcado, la explicación es lo único útil que se puede
             enseñar: si no, el usuario busca una sección que nunca aparece. -->
        <button type="button" class="ayuda" (click)="explicar.set(true)">
          ¿Y los chats bloqueados?
        </button>
      }
    } @else {
      <div class="cabecera">
        <button type="button" class="volver" (click)="ir.emit('normal')">
          ← <span>{{ vista() === 'archivados' ? 'Archivados' : 'Chats bloqueados' }}</span>
        </button>
        @if (vista() === 'restringidos') {
          <button type="button" class="cerrar-pestillo" (click)="cerrar()">
            Bloquear
          </button>
        }
      </div>
    }

    @if (explicar()) {
      <div class="aviso" role="note">
        <p>
          <b>Antes tienes que bloquearlos en tu teléfono.</b>
        </p>
        <p>
          Un chat aparece aquí solo si en WhatsApp lo pusiste como
          <b>chat bloqueado</b> y creaste tu <b>código de acceso secreto</b>. Se
          hace desde el móvil: abre el chat › <b>Ajustes del chat</b> ›
          <b>Bloqueo de chat</b>.
        </p>
        <p class="matiz">
          WhatsApp no nos entrega ese código secreto —manda un dato derivado del
          que no se puede volver atrás—, así que aquí usarás un
          <b>código propio de esta aplicación</b> para destapar la sección.
        </p>
        <button type="button" class="entendido" (click)="explicar.set(false)">
          Entendido
        </button>
      </div>
    }

    @if (pidiendo()) {
      <div class="puerta" role="dialog" aria-label="Código de acceso">
        @if (bloqueo.configurado()) {
          <p class="titulo">Escribe tu código para ver los chats bloqueados</p>
          <input
            type="password"
            inputmode="numeric"
            autocomplete="off"
            aria-label="Código de acceso"
            [(ngModel)]="codigo"
            (keyup.enter)="entrar()"
          />
          @if (error()) {
            <p class="mal">{{ error() }}</p>
          }
          <div class="acciones">
            <button type="button" class="secundario" (click)="cancelar()">
              Cancelar
            </button>
            <button type="button" class="principal" (click)="entrar()">
              Abrir
            </button>
          </div>
        } @else {
          <p class="titulo">Crea un código para esta sección</p>
          <p class="matiz">
            No es tu código secreto de WhatsApp: ese no nos lo entrega WhatsApp.
            Este es de esta aplicación y solo tapa esta sección en este equipo.
          </p>
          <input
            type="password"
            inputmode="numeric"
            autocomplete="off"
            aria-label="Código nuevo"
            placeholder="Al menos 6 caracteres"
            [(ngModel)]="codigo"
            (keyup.enter)="crear()"
          />
          @if (error()) {
            <p class="mal">{{ error() }}</p>
          }
          <div class="acciones">
            <button type="button" class="secundario" (click)="cancelar()">
              Cancelar
            </button>
            <button type="button" class="principal" (click)="crear()">
              Crear y abrir
            </button>
          </div>
        }
      </div>
    }
  `,
  styles: [
    `
      :host {
        display: block;
      }
      .seccion,
      .ayuda,
      .volver,
      .cerrar-pestillo {
        font: inherit;
        color: inherit;
        background: none;
        border: 0;
        cursor: pointer;
      }
      .seccion {
        width: 100%;
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 10px 14px;
        text-align: left;
        border-bottom: 1px solid var(--border);
      }
      .seccion:hover {
        background: var(--surface-hover, rgba(127, 127, 127, 0.1));
      }
      .icono {
        width: 22px;
        text-align: center;
      }
      .etiqueta {
        flex: 1;
        font-size: var(--font-size-sm);
      }
      .cuenta {
        font-size: var(--font-size-xs);
        color: var(--accent, #25d366);
      }
      /* Cero no es una novedad: se dice, pero sin llamar la atención. */
      .cuenta.ninguno {
        color: var(--text-muted);
      }
      .ayuda {
        width: 100%;
        padding: 8px 14px;
        text-align: left;
        font-size: var(--font-size-xs);
        color: var(--text-muted);
        text-decoration: underline;
      }
      .cabecera {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        padding: 10px 14px;
        border-bottom: 1px solid var(--border);
      }
      .volver {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: var(--font-size-sm);
      }
      .cerrar-pestillo {
        font-size: var(--font-size-xs);
        color: var(--text-muted);
        border: 1px solid var(--border);
        border-radius: 6px;
        padding: 4px 8px;
      }
      .aviso,
      .puerta {
        display: grid;
        gap: 8px;
        padding: 12px 14px;
        border-bottom: 1px solid var(--border);
        background: var(--surface-hover, rgba(127, 127, 127, 0.06));
      }
      .aviso p,
      .puerta p {
        margin: 0;
        font-size: var(--font-size-xs);
        line-height: 1.45;
      }
      .titulo {
        font-size: var(--font-size-sm) !important;
      }
      .matiz {
        color: var(--text-muted);
      }
      .mal {
        color: var(--danger, #d9534f);
      }
      input {
        font: inherit;
        color: inherit;
        padding: 8px 10px;
        border-radius: 8px;
        border: 1px solid var(--border);
        background: var(--surface, rgba(127, 127, 127, 0.12));
      }
      .acciones {
        display: flex;
        gap: 8px;
        justify-content: flex-end;
      }
      .acciones button,
      .entendido {
        font: inherit;
        font-size: var(--font-size-xs);
        padding: 6px 12px;
        border-radius: 8px;
        cursor: pointer;
        border: 1px solid var(--border);
        background: transparent;
        color: inherit;
      }
      .entendido {
        justify-self: start;
      }
      .acciones .principal {
        border-color: var(--accent, #25d366);
        color: var(--accent, #25d366);
      }
    `,
  ],
})
export class SeccionesDeChatsComponent {
  readonly bloqueo = inject(ChatLockService);
  private readonly destroyRef = inject(DestroyRef);

  constructor() {
    // Si el pestillo sigue abierto de antes, entrar no tiene que volver a
    // pedir el código —igual que WhatsApp Web—, y para saberlo hay que
    // preguntárselo al servidor: la duración la cuenta él.
    //
    // Que falle no rompe nada: se queda en «cerrado», que es el lado seguro.
    this.bloqueo
      .refrescar()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({ error: () => this.bloqueo.marcarCerrado() });
  }

  vista = input<Vista>('normal');
  archivados = input(0);
  restringidos = input(0);

  /** Cambiar de sección. Quien recarga la lista es el panel. */
  readonly ir = output<Vista>();

  readonly explicar = signal(false);
  readonly pidiendo = signal(false);
  readonly error = signal('');
  codigo = '';

  /** Ya está abierto: se entra sin preguntar, como WhatsApp Web. */
  readonly yaAbierto = computed(() => this.bloqueo.abierto());

  pedirEntrada() {
    if (this.yaAbierto()) {
      this.ir.emit('restringidos');
      return;
    }
    this.codigo = '';
    this.error.set('');
    this.pidiendo.set(true);
  }

  entrar() {
    if (!this.codigo) return;
    this.bloqueo.abrir(this.codigo).subscribe({
      next: () => {
        this.pidiendo.set(false);
        this.codigo = '';
        this.ir.emit('restringidos');
      },
      // No se distingue «por poco» de «nada que ver»: cualquier detalle de más
      // es una pista para quien esté probando.
      error: () => this.error.set('Código incorrecto.'),
    });
  }

  crear() {
    if (this.codigo.length < 6) {
      this.error.set('Usa al menos 6 caracteres.');
      return;
    }
    this.bloqueo.poner(this.codigo).subscribe({
      next: () => {
        this.pidiendo.set(false);
        this.codigo = '';
        this.ir.emit('restringidos');
      },
      error: () => this.error.set('No se pudo guardar el código.'),
    });
  }

  cancelar() {
    this.pidiendo.set(false);
    this.codigo = '';
    this.error.set('');
  }

  cerrar() {
    this.bloqueo.cerrar().subscribe({
      next: () => this.ir.emit('normal'),
      // Aunque falle la petición se sale de la sección: dejar al usuario
      // dentro después de pedir «Bloquear» es lo contrario de lo que pidió.
      error: () => {
        this.bloqueo.marcarCerrado();
        this.ir.emit('normal');
      },
    });
  }
}
