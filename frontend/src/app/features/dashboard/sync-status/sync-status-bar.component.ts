import { DecimalPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  effect,
  input,
  signal,
} from '@angular/core';
import { SyncStatus } from '../../../core/models/api.models';

/**
 * Barra fija de estado, siempre visible pero discreta.
 *
 * POR QUÉ EXISTE APARTE DE `app-sync-indicator`
 * ----------------------------------------------
 * El indicador de siempre vive DENTRO del cajón «Recuperación avanzada»: solo
 * se ve si el usuario lo abre. Esta barra es la vista de un vistazo, sin
 * abrir nada — mismo `SyncStatus` de `dashboard-page`, ningún fetch ni
 * suscripción propia.
 *
 * EL CRONÓMETRO SE ANCLA AL RELOJ DEL SERVIDOR
 * ---------------------------------------------
 * `status().startedAt` es la hora en que el BACKEND arrancó este ciclo. Con
 * eso, quien recarga la página a mitad de una excavación ve el tiempo
 * transcurrido real al instante, no un cronómetro que vuelve a cero. El
 * `setInterval` de aquí solo REPINTA cada segundo; no cuenta nada por su
 * cuenta.
 */
@Component({
  selector: 'app-sync-status-bar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DecimalPipe],
  template: `
    <div class="status-bar" [class.status-bar--active]="excavando()">
      @if (excavando()) {
        <span class="status-bar__dot" aria-hidden="true"></span>
        <span>
          Excavando
          @if (chatsTotal(); as total) {
            <strong>{{ chatsProcesados() }} / {{ total }}</strong>
            chats
          } @else {
            <strong>{{ chatsProcesados() }}</strong>
            chats
          }
          <span class="status-bar__sep">·</span>
          <strong>{{ mensajes() | number }}</strong> mensajes
          <span class="status-bar__sep">·</span>
          {{ tiempoTranscurrido() }}
        </span>
      } @else if (mostrarCompletado()) {
        <span class="status-bar__dot status-bar__dot--fijo" aria-hidden="true"></span>
        <span>Sincronización completada @if (finalizadaEn(); as fecha) { · {{ fecha }} }</span>
      } @else if (mostrarError()) {
        <span class="status-bar__dot status-bar__dot--error" aria-hidden="true"></span>
        <span>Sincronización detenida</span>
      } @else if (finalizadaEn(); as fecha) {
        <span>Última sincronización: {{ fecha }}</span>
      } @else {
        <span class="status-bar__vacio">Sin sincronizar todavía</span>
      }
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
      }
      .status-bar {
        position: fixed;
        bottom: 0;
        left: 0;
        right: 0;
        height: 28px;
        display: flex;
        align-items: center;
        padding: 0 16px;
        gap: 8px;
        background: var(--bg-panel);
        border-top: 1px solid var(--border);
        font-size: var(--font-size-xs, 12px);
        color: var(--text-secondary);
        z-index: 100;
        transition: color 0.2s ease;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .status-bar--active {
        color: var(--accent);
      }
      .status-bar__dot {
        flex: none;
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: currentColor;
        animation: status-bar-pulse 1.5s infinite;
      }
      .status-bar__dot--fijo {
        animation: none;
      }
      .status-bar__dot--error {
        background: var(--color-danger);
        animation: none;
      }
      .status-bar__sep {
        opacity: 0.4;
      }
      .status-bar__vacio {
        opacity: 0.6;
      }
      strong {
        color: var(--text-primary);
        font-weight: 600;
      }
      @keyframes status-bar-pulse {
        0%,
        100% {
          opacity: 1;
        }
        50% {
          opacity: 0.3;
        }
      }
      @media (prefers-reduced-motion: reduce) {
        .status-bar__dot {
          animation: none;
        }
      }
    `,
  ],
})
export class SyncStatusBarComponent implements OnDestroy {
  readonly status = input<SyncStatus>();

  /** Se repinta cada segundo mientras excava; no cuenta nada por sí mismo. */
  private readonly ahora = signal(Date.now());
  private temporizador?: ReturnType<typeof setInterval>;

  readonly excavando = computed(() => this.status()?.state === 'running');
  readonly chatsProcesados = computed(() => this.status()?.chatsProcessed ?? 0);
  readonly chatsTotal = computed(() => this.status()?.chatsTotal || undefined);
  readonly mensajes = computed(() => this.status()?.messagesNew ?? 0);

  /** "Sincronización completada" / "detenida", solo unos segundos. */
  private readonly mostrarCompletadoHasta = signal(0);
  private readonly mostrarErrorHasta = signal(0);
  readonly mostrarCompletado = computed(() => this.ahora() < this.mostrarCompletadoHasta());
  readonly mostrarError = computed(() => this.ahora() < this.mostrarErrorHasta());

  readonly tiempoTranscurrido = computed(() => {
    const inicio = this.status()?.startedAt;
    if (!inicio) return '0m 0s';
    const ms = Math.max(0, this.ahora() - Date.parse(inicio));
    const totalSegundos = Math.floor(ms / 1000);
    const horas = Math.floor(totalSegundos / 3600);
    const minutos = Math.floor((totalSegundos % 3600) / 60);
    const segundos = totalSegundos % 60;
    return horas > 0 ? `${horas}h ${minutos}m ${segundos}s` : `${minutos}m ${segundos}s`;
  });

  readonly finalizadaEn = computed(() => {
    const fecha = this.status()?.finishedAt;
    if (!fecha) return undefined;
    try {
      return new Intl.DateTimeFormat('es', {
        day: 'numeric',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
      }).format(new Date(fecha));
    } catch {
      return undefined;
    }
  });

  constructor() {
    // Un solo reloj para las tres cosas que dependen del tiempo: el
    // cronómetro en marcha y las dos ventanas de "acaba de terminar". Vive
    // siempre encendido (28px no cuesta nada) y no hace ningún trabajo salvo
    // repintar cuando de verdad hay algo que mostrar.
    this.temporizador = setInterval(() => this.ahora.set(Date.now()), 1000);

    let estadoAnterior: SyncStatus['state'] | undefined;
    effect(() => {
      const actual = this.status()?.state;
      if (actual === estadoAnterior) return;
      const previo = estadoAnterior;
      estadoAnterior = actual;

      // Solo se anuncia la TRANSICION hacia completado/error, no cada vez
      // que llega un snapshot con ese mismo estado (una reconexión SSE no
      // puede reabrir el cartel de "acaba de terminar").
      if (actual === 'complete' && previo === 'running') {
        this.mostrarCompletadoHasta.set(Date.now() + 10_000);
      } else if (actual === 'error' && previo === 'running') {
        this.mostrarErrorHasta.set(Date.now() + 5_000);
      }
    });
  }

  ngOnDestroy(): void {
    clearInterval(this.temporizador);
  }
}
