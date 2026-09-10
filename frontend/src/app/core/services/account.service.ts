import { Injectable, computed, inject, signal } from '@angular/core';
import { map, tap } from 'rxjs';
import { ApiClientService } from '../api/api-client.service';
import { AccountState } from './account-state.service';

/** Una cuenta de WhatsApp vinculada. Nunca lleva material de sesión. */
export interface WhatsAppAccountInfo {
  id: string;
  /** Lo que el usuario le puso, o el nombre del perfil, o el número. */
  displayName?: string;
  phoneNumber?: string;
  accountType: 'personal' | 'business' | 'unknown';
  avatarUrl?: string;
  sessionStatus: string;
  linked: boolean;
  /** El teléfono la desvinculó (o nunca se vinculó): hay que escanear otra vez. */
  needsRelink: boolean;
  /** Solo se cayó el socket. Vuelve sola; no hay nada que hacer. */
  disconnected: boolean;
  linkedAt?: string;
  lastConnectedAt?: string;
  active: boolean;
}

function normalizar(raw: unknown): WhatsAppAccountInfo {
  const r = (raw ?? {}) as Record<string, unknown>;
  const tipo = String(r['account_type'] ?? 'unknown');
  return {
    id: String(r['id'] ?? ''),
    displayName: (r['display_name'] as string) || undefined,
    phoneNumber: (r['phone_number'] as string) || undefined,
    accountType:
      tipo === 'personal' || tipo === 'business' ? tipo : 'unknown',
    avatarUrl: (r['avatar_url'] as string) || undefined,
    sessionStatus: String(r['session_status'] ?? 'never_linked'),
    linked: r['linked'] === true,
    needsRelink: r['needs_relink'] === true,
    disconnected: r['disconnected'] === true,
    linkedAt: (r['linked_at'] as string) || undefined,
    lastConnectedAt: (r['last_connected_at'] as string) || undefined,
    active: r['active'] === true,
  };
}

/**
 * Las cuentas de WhatsApp del usuario: cuáles hay y cuál se está mirando.
 *
 * CAMBIAR DE CUENTA NO ES FILTRAR
 * -------------------------------
 * Es cambiar de contexto entero: otros chats, otro historial, otra sesión y
 * otra copia en Drive. Quien llame a `activar` tiene que vaciar lo que tenía
 * y volver a pedirlo —incluido el canal de eventos—, no quedarse con la lista
 * anterior filtrada. Un evento tardío de la cuenta anterior no puede tocar lo
 * que se está viendo ahora.
 */
@Injectable({ providedIn: 'root' })
export class AccountService {
  private readonly api = inject(ApiClientService);
  private readonly estado = inject(AccountState);

  readonly cuentas = signal<WhatsAppAccountInfo[]>([]);
  readonly cargando = signal(false);

  /** La que se está mirando, ya resuelta. */
  readonly activa = computed(() => {
    const id = this.estado.activaId();
    const todas = this.cuentas();
    return todas.find((c) => c.id === id) ?? todas.find((c) => c.active);
  });

  /** Si hay más de una, el selector tiene sentido. */
  readonly hayVarias = computed(() => this.cuentas().length > 1);

  listar() {
    this.cargando.set(true);
    return this.api.get<Record<string, unknown>>('/accounts').pipe(
      map((raw) => {
        const filas = Array.isArray(raw?.['accounts'])
          ? (raw['accounts'] as unknown[]).map(normalizar)
          : [];
        this.cuentas.set(filas);
        // El servidor manda: es él quien guarda cuál es la activa, así que
        // si aquí había otra —una pestaña vieja, por ejemplo— se corrige.
        const activaId = (raw?.['active_id'] as string) || undefined;
        this.estado.fijar(activaId ?? filas.find((c) => c.active)?.id);
        this.cargando.set(false);
        return filas;
      }),
    );
  }

  /** Prepara otra cuenta para vincular. NO la activa: todavía no tiene sesión. */
  crear(displayName?: string) {
    return this.api
      .post<Record<string, unknown>>('/accounts', {
        display_name: displayName ?? null,
      })
      .pipe(
        map((raw) => normalizar(raw?.['account'])),
        tap((nueva) => this.cuentas.update((todas) => [...todas, nueva])),
      );
  }

  renombrar(id: string, displayName: string) {
    return this.api
      .patch<Record<string, unknown>>(`/accounts/${encodeURIComponent(id)}`, {
        display_name: displayName,
      })
      .pipe(
        map((raw) => normalizar(raw?.['account'])),
        tap((fila) =>
          this.cuentas.update((todas) =>
            todas.map((c) => (c.id === fila.id ? { ...c, ...fila } : c)),
          ),
        ),
      );
  }

  /**
   * Cambia la cuenta activa EN EL SERVIDOR y aquí.
   *
   * El orden importa: primero el servidor. Si se cambiara aquí primero y la
   * llamada fallara, las peticiones siguientes irían con una cuenta que el
   * backend no considera activa, y el usuario vería una lista que no
   * corresponde a lo que dice el selector.
   */
  activar(id: string) {
    return this.api
      .post<Record<string, unknown>>(
        `/accounts/${encodeURIComponent(id)}/activate`,
      )
      .pipe(
        map((raw) => normalizar(raw?.['account'])),
        tap((fila) => {
          this.estado.fijar(fila.id);
          this.cuentas.update((todas) =>
            todas.map((c) => ({ ...c, active: c.id === fila.id })),
          );
        }),
      );
  }
}

/** El nombre que se pinta. Nunca un identificador interno. */
export function nombreDeCuenta(cuenta: WhatsAppAccountInfo | undefined): string {
  if (!cuenta) return 'WhatsApp';
  if (cuenta.displayName) return cuenta.displayName;
  if (cuenta.phoneNumber) return `+${cuenta.phoneNumber}`;
  // Sin nombre y sin número es una cuenta que todavía no se ha vinculado.
  return 'Cuenta sin vincular';
}
