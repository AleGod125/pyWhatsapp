import { Injectable, computed, inject, signal } from '@angular/core';
import { Observable, tap } from 'rxjs';
import { ApiClientService } from '../api/api-client.service';

/**
 * El código que tapa los chats restringidos.
 *
 * NO ES EL CÓDIGO DE WHATSAPP, Y NO PUEDE SERLO
 * ---------------------------------------------
 * Se investigó y la respuesta es firme:
 *
 * - `chatLockSettings.secretCode` llega como bytes opacos: WhatsApp manda un
 *   derivado, no el código;
 * - Baileys ni llega ahí, porque `lockChatAction` es la única acción de
 *   app-state que no procesa;
 * - y sobre todo, los mensajes de un chat restringido **no** están cifrados
 *   con ese código. Llegan y se guardan como los de cualquier otro; el bloqueo
 *   de WhatsApp es un control de acceso de su interfaz, no una capa de
 *   cifrado.
 *
 * Así que este código es de esta aplicación. La pantalla lo dice con esas
 * palabras: prometer que es el de WhatsApp sería mentir sobre lo que protege.
 *
 * EL PESTILLO VIVE EN EL SERVIDOR
 * -------------------------------
 * Aquí solo se guarda lo que hace falta para pintar. Quien decide si se puede
 * ver la sección es el backend, y lo comprueba también al abrir un chat y al
 * pedir sus mensajes: una sección tapada solo en el navegador no está tapada.
 */
export interface EstadoDelCodigo {
  configurado: boolean;
  abierto: boolean;
  minutos: number;
}

@Injectable({ providedIn: 'root' })
export class ChatLockService {
  private readonly api = inject(ApiClientService);

  private readonly estado = signal<EstadoDelCodigo>({
    configurado: false,
    abierto: false,
    minutos: 15,
  });

  readonly configurado = computed(() => this.estado().configurado);
  readonly abierto = computed(() => this.estado().abierto);
  readonly minutos = computed(() => this.estado().minutos);

  /** Lo que hay ahora mismo, según el servidor. */
  refrescar(): Observable<EstadoDelCodigo> {
    return this.api
      .get<EstadoDelCodigo>('/chat-lock')
      .pipe(tap((e) => this.estado.set(this.normalizar(e))));
  }

  /**
   * Pone o cambia el código.
   *
   * `codigoActual` es obligatorio cuando ya había uno: sin eso, quien se
   * siente delante de una sesión abierta lo sustituye por el suyo.
   */
  poner(codigo: string, codigoActual?: string): Observable<unknown> {
    return this.api
      .post('/chat-lock', {
        codigo,
        codigo_actual: codigoActual ?? '',
      })
      .pipe(
        tap(() =>
          this.estado.update((e) => ({ ...e, configurado: true, abierto: true })),
        ),
      );
  }

  abrir(codigo: string): Observable<unknown> {
    return this.api
      .post('/chat-lock/abrir', { codigo })
      .pipe(tap(() => this.estado.update((e) => ({ ...e, abierto: true }))));
  }

  cerrar(): Observable<unknown> {
    return this.api
      .post('/chat-lock/cerrar', {})
      .pipe(tap(() => this.estado.update((e) => ({ ...e, abierto: false }))));
  }

  /** El servidor puede haber cerrado el pestillo por su cuenta (caducó). */
  marcarCerrado() {
    this.estado.update((e) => ({ ...e, abierto: false }));
  }

  private normalizar(crudo: unknown): EstadoDelCodigo {
    const r = (crudo ?? {}) as Record<string, unknown>;
    return {
      configurado: r['configurado'] === true,
      abierto: r['abierto'] === true,
      minutos: typeof r['minutos'] === 'number' ? r['minutos'] : 15,
    };
  }
}
