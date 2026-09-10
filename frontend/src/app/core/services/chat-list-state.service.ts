import { Injectable, computed, signal } from '@angular/core';

/**
 * Cuántas conversaciones hay y cuántas se están ocultando.
 *
 * POR QUÉ ESTÁ SEPARADO DE `ChatService`
 * --------------------------------------
 * `ChatService` hace HTTP y las pruebas lo sustituyen por un doble mínimo
 * (`{ list, get }`). Si estas señales vivieran ahí, cada doble tendría que
 * declararlas o el panel reventaría con «no es una función» — un fallo del
 * andamiaje, no del producto.
 *
 * Aquí no las sustituye nadie: es estado de vista, sin dependencias.
 */
@Injectable({ providedIn: 'root' })
export class ChatListState {
  /** Conversaciones que existen, contando las que no tienen mensajes. */
  readonly total = signal(0);
  /** Cuántas quedaron fuera del listado por no tener ningún mensaje. */
  readonly sinMensajes = signal(0);
  /** Si se están pidiendo también las vacías. */
  readonly incluyeVacias = signal(false);
  /** Las que SÍ tienen mensajes, se estén mostrando todas o no. */
  readonly conMensajes = computed(() => this.total() - this.sinMensajes());

  /** Qué sección se está mirando: normal, archivados o restringidos. */
  readonly vista = signal<'normal' | 'archivados' | 'restringidos'>('normal');
  /**
   * Cuántas hay en las OTRAS secciones.
   *
   * Llegan en cada respuesta de `/chats`, también desde la lista normal: es lo
   * que permite pintar «Archivados 12» sin pedir la sección entera solo para
   * contar. Sin esto, la única forma de saber si la entrada debe aparecer
   * sería una petición más por cada carga.
   */
  readonly archivados = signal(0);
  readonly restringidos = signal(0);

  anotar(total: number, sinMensajes: number, incluyeVacias: boolean): void {
    this.total.set(total);
    this.sinMensajes.set(sinMensajes);
    this.incluyeVacias.set(incluyeVacias);
  }

  anotarSecciones(archivados: number, restringidos: number): void {
    this.archivados.set(archivados);
    this.restringidos.set(restringidos);
  }
}
