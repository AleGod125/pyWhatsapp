import { Injectable, inject } from '@angular/core';
import { map } from 'rxjs';
import { ApiClientService } from '../api/api-client.service';
import { Chat, ChatAvatar, ChatDetails } from '../models/api.models';
import { ChatListState } from './chat-list-state.service';

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
   */
  list(incluirVacias = false) {
    const url = incluirVacias ? '/chats?todos=1' : '/chats';
    return this.api.get<unknown>(url).pipe(
      map((raw) => {
        const r = (raw ?? {}) as Record<string, unknown>;
        const chats = extractArray(raw, 'chats')
          .map(normalizeChat)
          .sort((a, b) => dateValue(b.lastMessageAt) - dateValue(a.lastMessageAt));
        this.estado.anotar(
          Number(r['total_conversaciones'] ?? chats.length),
          Number(r['sin_mensajes'] ?? 0),
          incluirVacias,
        );
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
const normalizeHistoryStatus = (value: unknown): Chat['historyStatus'] => {
  const status = String(value ?? '').toLowerCase();
  return ['complete', 'exhausted', 'fetching', 'pending', 'timeout', 'waiting_seed'].includes(
    status,
  )
    ? (status as Chat['historyStatus'])
    : undefined;
};
