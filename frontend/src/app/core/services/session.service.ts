import { Injectable, inject } from '@angular/core';
import { map } from 'rxjs';
import { ApiClientService } from '../api/api-client.service';
import {
  HealthStatus,
  PairingPhase,
  QrStatus,
  SessionState,
  SessionStateCode,
} from '../models/api.models';

@Injectable({ providedIn: 'root' })
export class SessionService {
  private readonly api = inject(ApiClientService);
  health() {
    return this.api.get<Record<string, unknown>>('/health').pipe(map(normalizeHealth));
  }
  getSession() {
    return this.api.get<Record<string, unknown>>('/session').pipe(map(normalizeSession));
  }
  /**
   * Pide el código de vinculación.
   *
   * `accountId` sirve para vincular una cuenta que NO es la activa —añadir un
   * segundo WhatsApp sin dejar de ver el primero—. Sin él va la activa, que
   * es lo que hace la pantalla de alta inicial.
   *
   * Va como parámetro y no por la cabecera de siempre a propósito: la
   * cabecera lleva la cuenta que se está *mirando*, y aquí hace falta decir
   * otra cosa —la que se está *vinculando*— sin cambiar el contexto.
   */
  pair(accountId?: string) {
    return this.api
      .post<Record<string, unknown>>(this.conCuenta('/session/pair', accountId))
      .pipe(map(normalizeSession));
  }
  session(accountId?: string) {
    return this.api
      .get<Record<string, unknown>>(this.conCuenta('/session', accountId))
      .pipe(map(normalizeSession));
  }
  qr(accountId?: string) {
    return this.api.get<Record<string, unknown>>(this.conCuenta('/session/qr', accountId)).pipe(
      map((value): QrStatus => ({
        available: value['available'] === true,
        imageUrl: typeof value['image_url'] === 'string' ? value['image_url'] : undefined,
        generation: typeof value['generation'] === 'number' ? value['generation'] : undefined,
        expiresAt: typeof value['expires_at'] === 'string' ? value['expires_at'] : undefined,
        expiresInSeconds:
          typeof value['expires_in_seconds'] === 'number' ? value['expires_in_seconds'] : undefined,
      })),
    );
  }
  qrImageUrl(generation?: number, accountId?: string) {
    const base =
      generation === undefined
        ? `/session/qr/image?size=560&v=${Date.now()}`
        : `/session/qr/image?generation=${generation}&size=560`;
    // Una `<img>` no puede llevar cabeceras, así que la cuenta viaja en la
    // URL. Es el mismo dato por el otro camino que acepta el servidor.
    return this.api.url(this.conCuenta(base, accountId), {
      conCuenta: accountId === undefined,
    });
  }

  private conCuenta(ruta: string, accountId?: string): string {
    if (!accountId) return ruta;
    const union = ruta.includes('?') ? '&' : '?';
    return `${ruta}${union}account_id=${encodeURIComponent(accountId)}`;
  }
}

export function normalizeSession(raw: Record<string, unknown>): SessionState {
  const value =
    raw['session'] && typeof raw['session'] === 'object'
      ? (raw['session'] as Record<string, unknown>)
      : raw;
  const text = String(value['state'] ?? value['status'] ?? '').toUpperCase();
  const connected = value['connected'] === true || text === 'CONNECTED';
  const allowed: SessionStateCode[] = [
    'STARTING',
    'NO_SESSION',
    'PAIRING_REQUIRED',
    'PAIRING',
    'QR_READY',
    'CONNECTING',
    'CONNECTED',
    'DISCONNECTED',
    'SESSION_INVALID',
    'ERROR',
  ];
  return {
    state: connected
      ? 'CONNECTED'
      : allowed.includes(text as SessionStateCode)
        ? (text as SessionStateCode)
        : 'NO_SESSION',
    connected,
    viewerAllowed: value['viewer_allowed'] === true,
    whatsappEnabled: value['whatsapp_enabled'] !== false,
    generation: typeof value['generation'] === 'number' ? value['generation'] : undefined,
    message: typeof value['message'] === 'string' ? value['message'] : undefined,
    qrAvailable: value['qr_available'] === true || value['qrAvailable'] === true,
    pairingPhase: normalizePairingPhase(value),
    sessionRejections:
      typeof value['session_rejections'] === 'number'
        ? (value['session_rejections'] as number)
        : undefined,
    sessionRejectionsMax:
      typeof value['session_rejections_max'] === 'number'
        ? (value['session_rejections_max'] as number)
        : undefined,
  };
}

const FASES: PairingPhase[] = [
  'idle',
  'verifying_session',
  'pairing_required',
  'qr_ready',
  'connecting',
  'connected',
];

/**
 * Backends anteriores no mandan `pairing_phase`. Se deduce del resto en vez de
 * dejarla vacia: sin ella la pantalla vuelve al mensaje generico de siempre.
 */
function normalizePairingPhase(value: Record<string, unknown>): PairingPhase | undefined {
  const crudo = String(value['pairing_phase'] ?? '');
  if (FASES.includes(crudo as PairingPhase)) return crudo as PairingPhase;
  if (value['connected'] === true) return 'connected';
  if (value['qr_available'] === true) return 'qr_ready';
  const estado = String(value['state'] ?? '').toUpperCase();
  if (estado === 'SESSION_INVALID' && value['session_file_present'] === true)
    return 'verifying_session';
  return undefined;
}
export function normalizeHealth(value: Record<string, unknown>): HealthStatus {
  return {
    status: String(value['status'] ?? 'error'),
    state: typeof value['state'] === 'string' ? (value['state'] as SessionStateCode) : undefined,
    database: value['database'] === true,
    whatsappEnabled: value['whatsapp_enabled'] !== false,
    sessionFilePresent: value['session_file_present'] === true,
    apiVersion: typeof value['api_version'] === 'string' ? value['api_version'] : undefined,
  };
}
