import { Injectable, signal } from '@angular/core';

/**
 * Qué cuenta de WhatsApp se está mirando, para toda la aplicación.
 *
 * POR QUÉ ESTÁ SOLA EN SU SERVICIO
 * --------------------------------
 * La lee `ApiClientService` para poner la cabecera `X-WhatsApp-Account` en
 * cada petición, y la escribe `AccountService`, que a su vez usa el cliente.
 * Si el identificador viviera en cualquiera de los dos habría un ciclo de
 * dependencias; aquí no depende de nada.
 *
 * Y así las pruebas que sustituyen `AccountService` por un doble mínimo no
 * tienen que declararlo: es estado de vista, sin red.
 *
 * NO ES LA AUTORIDAD
 * ------------------
 * El servidor guarda cuál es la cuenta activa de cada usuario. Esto es una
 * copia para que las peticiones puedan decirlo explícitamente —y para que dos
 * pestañas puedan mirar cuentas distintas—, pero si aquí hubiera un
 * identificador ajeno el backend lo ignora y usa el suyo.
 */
@Injectable({ providedIn: 'root' })
export class AccountState {
  /** `undefined` mientras no se sabe: entonces manda la activa del servidor. */
  readonly activaId = signal<string | undefined>(undefined);

  fijar(id: string | undefined): void {
    this.activaId.set(id || undefined);
  }
}
