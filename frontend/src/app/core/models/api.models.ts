export type SessionStateCode =
  | 'STARTING'
  | 'NO_SESSION'
  | 'PAIRING_REQUIRED'
  | 'PAIRING'
  | 'QR_READY'
  | 'CONNECTING'
  | 'CONNECTED'
  | 'DISCONNECTED'
  | 'SESSION_INVALID'
  | 'ERROR';
/**
 * En que punto del camino a la vinculacion estamos.
 *
 * `state` solo no basta: con `SESSION_INVALID` no se puede distinguir "se
 * esta comprobando la sesion guardada" de "hace falta un QR y no llega", y la
 * pantalla acaba diciendo "Preparando tu codigo QR" durante los tres rechazos.
 */
export type PairingPhase =
  'idle' | 'verifying_session' | 'pairing_required' | 'qr_ready' | 'connecting' | 'connected';

export interface SessionState {
  state: SessionStateCode;
  connected: boolean;
  viewerAllowed?: boolean;
  whatsappEnabled?: boolean;
  generation?: number;
  message?: string;
  qrAvailable?: boolean;
  pairingPhase?: PairingPhase;
  /** Rechazos 401 seguidos de la sesion guardada; al tercero se archiva. */
  sessionRejections?: number;
  sessionRejectionsMax?: number;
}
export interface QrStatus {
  available: boolean;
  imageUrl?: string;
  generation?: number;
  expiresAt?: string;
  expiresInSeconds?: number;
}
export interface HealthStatus {
  status: string;
  state?: SessionStateCode;
  database: boolean;
  whatsappEnabled: boolean;
  sessionFilePresent?: boolean;
  apiVersion?: string;
}
export type MessageType =
  | 'text'
  | 'image'
  | 'video'
  | 'audio'
  | 'voice_note'
  | 'document'
  | 'sticker'
  | 'gif'
  | 'location'
  | 'contact'
  | 'poll'
  | 'reaction'
  | 'call'
  | 'missed_voice_call'
  | 'missed_video_call'
  | 'voice_call'
  | 'video_call'
  | 'encryption_notice'
  | 'system'
  | 'unknown_system'
  | 'unknown';
export interface ChatAvatar {
  initials: string;
  color: string;
  url?: string;
}
/**
 * Los estados que el backend escribe de verdad en `chat_history_state`.
 *
 * La lista estaba incompleta: faltaban `error`, `no_valid_cursor` y
 * `server_limited`, que el motor de excavación sí escribe. Un estado que el
 * tipo no contempla se cuela sin que nadie lo mire, y la pantalla acaba
 * enseñando el caso por defecto sobre algo que sí tenía nombre.
 */
export type ChatHistoryStatus =
  | 'waiting_seed'
  | 'pending'
  | 'fetching'
  | 'timeout'
  | 'complete'
  | 'exhausted'
  /** El motor falló pidiendo este chat. */
  | 'error'
  /** No hay ancla real con la que pedir. */
  | 'no_valid_cursor'
  /** Se alcanzó el tope de rondas de una pasada, no el final del historial. */
  | 'server_limited';
export interface Chat {
  id: string;
  jid?: string;
  displayName: string;
  avatar?: ChatAvatar;
  avatarUrl?: string;
  preview?: string;
  lastMessageAt?: string;
  lastMessageTimestamp?: number;
  messageCount?: number;
  historyStatus?: ChatHistoryStatus;
  waitingSeed?: boolean;
  historyComplete?: boolean;
  /** La conversación de uno consigo mismo. Se guarda igual; solo no se lista. */
  selfChat?: boolean;
  /** Archivada. No sale en la lista normal; tiene su propia sección. */
  archived?: boolean;
  /** «Chat bloqueado» de WhatsApp. Su sección pide el código de acceso. */
  locked?: boolean;
  /** Fijada arriba del todo. */
  pinned?: boolean;
  /** CUÁNDO se fijó: entre varias fijadas, ese es el orden. */
  pinnedAt?: number;
  /** Silenciada. Ojo: `muteUntil === 0` es «para siempre», no «no lo está». */
  muted?: boolean;
  muteUntil?: number;
  type?: string;
  unreadCount?: number;
  favorite?: boolean;
  firstMessageAt?: string;
}
export interface ChatDetails extends Chat {
  stats?: {
    total: number;
    oldestTimestamp?: number;
    newestTimestamp?: number;
    oldestAt?: string;
    newestAt?: string;
  };
}
export type MediaStatus =
  'pending' | 'downloading' | 'downloaded' | 'failed' | 'unavailable' | 'expired' | 'missing';
export interface Media {
  id: string;
  status: MediaStatus;
  mimeType?: string;
  filename?: string;
  size?: number;
  duration?: number;
  thumbnailUrl?: string;
  fileUrl?: string;
  width?: number;
  height?: number;
}
export interface MessageReply {
  senderName?: string;
  text?: string;
  messageId?: string;
}
export interface Message {
  id: string;
  chatId: string;
  type: MessageType;
  /**
   * Etiqueta corta ya resuelta por el backend.
   *
   * El backend conoce el protobuf entero —el `stub_type` de un evento de
   * sistema, los subtipos de encuesta, el contacto— y aquí sólo llega el tipo
   * normalizado. Recalcular la etiqueta en Angular era mantener un segundo
   * mapa, siempre incompleto: `system`, `contact` y `unknown` no estaban, y
   * los tres acababan en "Mensaje no compatible".
   */
  preview?: string;
  text?: string;
  timestamp: string;
  fromMe: boolean;
  senderName?: string;
  media?: Media;
  reply?: MessageReply;
  latitude?: number;
  longitude?: number;
  pollQuestion?: string;
  pollOptions?: string[];
}
export interface MessageCursor {
  beforeTimestamp: string | number;
  beforeId: string | number;
}
export interface PagedMessages {
  items: Message[];
  hasMore: boolean;
  nextCursor?: MessageCursor;
}
/**
 * Lo que un ciclo pudo hacer, y lo que no pudo.
 *
 * Existe porque "sincronizacion completada" no distingue "no habia nada
 * nuevo" de "hay 27 conversaciones que no se pueden ni intentar". Son cosas
 * distintas y el usuario merece saber cual es.
 */
export interface SyncSummary {
  chatsTotal?: number;
  /** Conversaciones a las que SI se les puede pedir historial. */
  withCursor?: number;
  waitingSeed?: number;
  retried?: number;
  /** Tienen ancla, pero su espera de reintento no habia vencido. */
  retryPending?: number;
  recoveredMessages?: number;
  newSeeds?: number;
  drivePending?: number;
}
/**
 * Lo que CAMBIO por haber pulsado, que es otra pregunta que "como esta todo".
 *
 * Nace de un numero enganoso: el ciclo decia «3410 referencias nuevas» con
 * cero conversaciones desatascadas. Las 3410 eran reales, pero salian de
 * excavar ocho conversaciones que ya funcionaban. Lo que contesta si sirvio
 * de algo es `promoted`, y el par `waitingBefore`/`waitingAfter`.
 */
export interface SyncRecovery {
  waitingBefore?: number;
  waitingAfter?: number;
  /** Conversaciones que pasaron de esperar a poder pedir su historial. */
  promoted?: number;
  seedsFound?: number;
  newChats?: number;
  messagesAdded?: number;
  backfillStarted?: number;
}
export interface SyncStatus {
  connected?: boolean;
  state?: 'idle' | 'running' | 'complete' | 'error';
  /** En qué va el ciclo: reconcile, seeds, web, backfill… */
  phase?: string;
  /**
   * Qué ciclo es: la búsqueda rápida o la excavación completa.
   *
   * Importa porque las dos ponen `state: 'running'` y no se avisan igual: la
   * rápida dura segundos y no merece ni un cartel; la completa puede durar
   * minutos y el usuario tiene que saber que espere.
   *
   * Viene del backend, así que sobrevive a un F5: es lo que impide que
   * recargar la página vuelva a habilitar el botón con la excavación a medias.
   */
  mode?: 'incremental' | 'full';
  /** Conversaciones que estaban atascadas y esta excavación devolvió a la cola. */
  chatsReopened?: number;
  /**
   * Mensajes rescatados de los blobs ya archivados en disco.
   *
   * Va aparte de los recuperados porque responde a otra pregunta: no «cuánto
   * trajo WhatsApp» sino «cuánto teníamos ya y no habíamos guardado».
   */
  messagesFromArchive?: number;
  /** Conversaciones ya recorridas y totales del ciclo, para poder decir «12 de 340». */
  chatsProcessed?: number;
  chatsTotal?: number;
  history?: 'idle' | 'syncing' | 'complete' | 'error';
  mediaPending?: number;
  backfillCurrent?: number;
  backfillTotal?: number;
  messagesNew?: number;
  /**
   * Los que ha traído ESTA corrida, contados sobre la base.
   *
   * Distinto de `messagesNew`, que se apoya en un observador que se cierra
   * antes de que llegue el blob y por eso se quedaba en 0 mientras la base
   * crecía. Este sale de una resta de totales y no se puede desincronizar.
   */
  messagesInRun?: number;
  /** Mensajes que todavía no están confirmados en Drive. */
  drivePending?: number;
  /** Los que ya llegaron. `drivePending + driveDone` es el total. */
  driveDone?: number;
  synced?: number;
  waitingSeed?: number;
  timeouts?: number;
  errors?: number;
  pending?: number;
  summary?: SyncSummary;
  recovery?: SyncRecovery;
  /**
   * Cuando arrancó y cuando terminó ESTE ciclo, tal cual las manda el backend.
   *
   * Con ellas el cronómetro de la barra de estado se ancla al reloj del
   * servidor: quien recarga la página a mitad de una excavación ve el tiempo
   * transcurrido real, no cero. Y son las mismas dos fechas con las que el
   * backend decide `phase`/`state`, así que no pueden discrepar.
   */
  startedAt?: string;
  finishedAt?: string;
}
// --- Revision de historiales pendientes (ruta normal del producto) --------
//
// Todo local: no vincula ningun dispositivo ni pide un segundo QR.
export type HistoryRecheckState = 'starting' | 'running' | 'completed' | 'failed';

export type RecheckChatState =
  'waiting_seed' | 'rechecking' | 'seed_found' | 'fetching_history' | 'error';

export interface RecheckChatProgress {
  id: number;
  name?: string;
  state: RecheckChatState;
}

export interface RecheckJob {
  jobId: string;
  state: HistoryRecheckState;
  total: number;
  processed: number;
  /** Chats en los que aparecio un ancla y que ya se estan excavando. */
  recovered: number;
  /** Siguen sin ancla. NO es un error: es reintentable. */
  stillWaiting: number;
  errors: number;
  messagesRecovered: number;
  /** La automatica se omitio porque acababa de correr. No es un fallo. */
  skipped?: boolean;
  currentChat?: RecheckChatProgress;
  error?: string;
  elapsedSeconds?: number;
}

// --- Cuentas ---------------------------------------------------------------

export interface AuthUser {
  id: string;
  email: string;
  display_name?: string | null;
  avatar_url?: string | null;
  auth_provider?: 'local' | 'google' | 'both';
  email_verified?: boolean;
}

/** Por donde va el usuario. Lo decide el backend, no el enrutador. */
export type OnboardingStep = 'login' | 'connect_google' | 'pairing' | 'dashboard';

export interface OnboardingStatus {
  authenticated: boolean;
  googleConnected: boolean;
  driveAuthorized: boolean;
  whatsappLinked: boolean;
  nextStep: OnboardingStep;
  user?: AuthUser;
}

export interface GoogleStatus {
  googleConnected: boolean;
  driveAuthorized: boolean;
  tokenValid: boolean;
  scopes: string[];
  email?: string;
}

// --- Almacenamiento --------------------------------------------------------

/**
 * En qué punto está la copia. El orden de la lista es de más urgente a menos:
 * `reauthorization_required` y `blocked` piden algo al usuario; el resto no.
 */
export type StorageState =
  | 'disabled'
  | 'up_to_date'
  | 'syncing'
  | 'paused'
  | 'error'
  | 'blocked'
  | 'reauthorization_required';

export interface StorageStatus {
  enabled: boolean;
  connected: boolean;
  authorized: boolean;
  rootReady: boolean;
  encrypted: boolean;
  pendingJobs: number;
  failedJobs: number;
  pausedJobs: number;
  pendingBytes: number;
  bytesUploaded: number;
  filesUploaded: number;
  lastUploadAt?: string;
  state: StorageState;
}

export interface ApiErrorBody {
  error: { code: string; message: string };
}
export interface AppError {
  code: string;
  message: string;
  status?: number;
  offline?: boolean;
}
export interface RealtimeEnvelope<T = unknown> {
  type: string;
  data: T;
}
export interface RealtimeEvent<T = unknown> extends RealtimeEnvelope<T> {}
export interface SystemEvent {
  type:
    | 'missed_voice_call'
    | 'missed_video_call'
    | 'voice_call'
    | 'video_call'
    | 'encryption_notice'
    | 'system'
    | 'unknown_system';
  text?: string;
}
