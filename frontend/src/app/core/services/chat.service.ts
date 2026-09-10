import { Injectable, inject } from '@angular/core';
import { map } from 'rxjs';
import { ApiClientService } from '../api/api-client.service';
import { Chat, ChatAvatar, ChatDetails } from '../models/api.models';
import { ChatListState } from './chat-list-state.service';

/** Qué sección del sidebar se pide. */
export type Vista = 'normal' | 'archivados' | 'restringidos';

@Injectable({ providedIn: 'root' })
export class ChatService {
  private readonly api = inject(ApiClientService);
  private readonly estado = inject(ChatListState);

  /**
   * El listado del panel.
   *
   * Por defecto solo las conversaciones donde se ha escrito algo: de 205
   * traídas, 164 no tenían ni un mensaje real —contactos a los que WhatsApp
   * reparte un aviso de cifrado, y chats aún sin excavar— y tapaban las 41
   * de verdad. Las que quedan fuera se CUENTAN, no se esconden.
   *
   * `vista` elige la sección, como en WhatsApp Web: la lista normal deja fuera
   * archivados y restringidos, y cada una de esas es su propia sección. Los
   * restringidos exigen el pestillo: sin él el servidor responde 423 y aquí no
   * se disimula, porque el panel tiene que poder pedir el código.
   */
  list(incluirVacias = false, vista: Vista = 'normal') {
    const params: string[] = [];
    if (incluirVacias) params.push('todos=1');
    if (vista !== 'normal') params.push(`vista=${vista}`);
    const url = params.length ? `/chats?${params.join('&')}` : '/chats';
    return this.api.get<unknown>(url).pipe(
      map((raw) => {
        const r = (raw ?? {}) as Record<string, unknown>;
        const chats = extractArray(raw, 'chats').map(normalizeChat).sort(porOrdenDeLista);
        this.estado.anotar(
          Number(r['total_conversaciones'] ?? chats.length),
          Number(r['sin_mensajes'] ?? 0),
          incluirVacias,
        );
        const secciones = (r['secciones'] ?? {}) as Record<string, unknown>;
        this.estado.anotarSecciones(
          Number(secciones['archivados'] ?? 0),
          Number(secciones['restringidos'] ?? 0),
        );
        this.estado.vista.set(vista);
        return chats;
      }),
    );
  }
  get(id: string) {
    return this.api
      .get<Record<string, unknown>>(`/chats/${encodeURIComponent(id)}`)
      .pipe(map(normalizeChatDetails));
  }
}
export function normalizeChat(value: unknown): Chat {
  const r = (value ?? {}) as Record<string, unknown>;
  const history =
    r['history'] && typeof r['history'] === 'object'
      ? (r['history'] as Record<string, unknown>)
      : undefined;
  const historyStatusValue =
    r['history_status'] ?? r['history_state'] ?? r['backfill_status'] ?? history?.['status'];
  const avatarRaw =
    r['avatar'] && typeof r['avatar'] === 'object'
      ? (r['avatar'] as Record<string, unknown>)
      : undefined;
  const avatar: ChatAvatar | undefined = avatarRaw
    ? {
        initials: String(avatarRaw['initials'] ?? '?'),
        color: String(avatarRaw['color'] ?? '#607d78'),
        url: optionalString(avatarRaw['url']),
      }
    : undefined;
  return {
    id: String(r['id'] ?? r['chat_id'] ?? r['jid'] ?? ''),
    jid: optionalString(r['jid']),
    displayName: String(
      r['display_name'] ?? r['displayName'] ?? r['name'] ?? r['id'] ?? 'Conversación',
    ),
    avatar,
    avatarUrl: optionalString(r['avatar_url'] ?? r['avatarUrl'] ?? avatar?.url),
    preview: optionalString(r['preview'] ?? r['last_message']),
    lastMessageAt: optionalString(r['last_message_at'] ?? r['lastMessageAt']),
    lastMessageTimestamp: optionalNumber(r['last_message_timestamp']),
    messageCount: optionalNumber(r['message_count'] ?? r['messageCount']),
    historyStatus: normalizeHistoryStatus(historyStatusValue),
    waitingSeed:
      r['waiting_seed'] === true ||
      history?.['waiting_seed'] === true ||
      String(historyStatusValue ?? '').toLowerCase() === 'waiting_seed',
    historyComplete:
      r['history_complete'] === true ||
      history?.['complete'] === true ||
      ['complete', 'exhausted'].includes(String(historyStatusValue ?? '').toLowerCase()),
    selfChat: r['self_chat'] === true,
    archived: r['archived'] === true,
    locked: r['locked'] === true,
    pinned: r['pinned'] === true,
    pinnedAt: optionalNumber(r['pinned_at']),
    // `muted` viene ya resuelto del servidor. No se deduce de `mute_until`:
    // 0 significa «silenciado para siempre» y sería falsy aquí.
    muted: r['muted'] === true,
    muteUntil: optionalNumber(r['mute_until']),
    type: optionalString(r['chat_type'] ?? r['type']),
    unreadCount: optionalNumber(r['unread_count']),
    favorite: r['favorite'] === true,
    firstMessageAt: optionalString(r['first_message_at']),
  };
}
export function normalizeChatDetails(value: unknown): ChatDetails {
  const r = (value ?? {}) as Record<string, unknown>;
  const chat = normalizeChat(r);
  const s =
    r['stats'] && typeof r['stats'] === 'object'
      ? (r['stats'] as Record<string, unknown>)
      : undefined;
  return {
    ...chat,
    firstMessageAt: optionalString(s?.['oldest_at']) ?? chat.firstMessageAt,
    stats: s
      ? {
          total: optionalNumber(s['total']) ?? chat.messageCount ?? 0,
          oldestTimestamp: optionalNumber(s['oldest_timestamp']),
          newestTimestamp: optionalNumber(s['newest_timestamp']),
          oldestAt: optionalString(s['oldest_at']),
          newestAt: optionalString(s['newest_at']),
        }
      : undefined,
  };
}
export function extractArray(raw: unknown, key: string): unknown[] {
  if (Array.isArray(raw)) return raw;
  if (raw && typeof raw === 'object') {
    const value = (raw as Record<string, unknown>)[key];
    return Array.isArray(value) ? value : [];
  }
  return [];
}
export const optionalString = (value: unknown): string | undefined =>
  typeof value === 'string' && value ? value : undefined;
export const optionalNumber = (value: unknown): number | undefined =>
  typeof value === 'number' ? value : undefined;
const dateValue = (value?: string) => (value ? new Date(value).getTime() || 0 : 0);

/**
 * El orden del sidebar: primero las fijadas, después por fecha.
 *
 * El servidor ya las devuelve así. Ordenar aquí SOLO por fecha —que es lo que
 * se hacía— deshacía ese orden y las fijadas volvían a caer entre las demás:
 * una conversación fijada con el último mensaje de hace un mes acababa al
 * final de la lista, que es exactamente lo contrario de fijarla.
 *
 * Entre dos fijadas manda CUÁNDO se fijaron, no cuándo se escribió: es lo que
 * hace WhatsApp y lo que espera quien las ha ordenado a mano.
 */
export function porOrdenDeLista(a: Chat, b: Chat): number {
  const fijadaA = a.pinnedAt ?? (a.pinned ? 1 : 0);
  const fijadaB = b.pinnedAt ?? (b.pinned ? 1 : 0);
  if (fijadaA !== fijadaB) return fijadaB - fijadaA;
  return actividad(b) - actividad(a);
}

/**
 * Cuándo fue lo último de esta conversación, venga como venga.
 *
 * Las dos formas existen de verdad y por caminos distintos: la carga inicial
 * trae `lastMessageAt` (ISO) y los avisos en vivo traen
 * `lastMessageTimestamp` (epoch en segundos). Mirar solo una deja la mitad de
 * las filas con valor 0 y el orden se decide por dónde estaban antes.
 */
function actividad(chat: Chat): number {
  if (typeof chat.lastMessageTimestamp === 'number') {
    return chat.lastMessageTimestamp;
  }
  // A segundos, para que las dos escalas se puedan comparar entre sí.
  return Math.floor(dateValue(chat.lastMessageAt) / 1000);
}
const normalizeHistoryStatus = (value: unknown): Chat['historyStatus'] => {
  const status = String(value ?? '').toLowerCase();
  return ['complete', 'exhausted', 'fetching', 'pending', 'timeout', 'waiting_seed'].includes(
    status,
  )
    ? (status as Chat['historyStatus'])
    : undefined;
};
