export type MonopolyActor = 'haya' | 'cc' | 'codex';

export type MonopolyRoomStatus =
  | 'lobby'
  | 'setup'
  | 'idle'
  | 'task_pending'
  | 'duel_pending'
  | 'toll_pending'
  | 'super_pending'
  | 'jail_turn'
  | 'paused'
  | 'finished'
  | 'engine_down';

export type PendingKind = 'task' | 'truth' | 'duel' | 'toll' | 'super';

export interface PendingDecision {
  kind: PendingKind;
  actor: Exclude<MonopolyActor, 'codex'>;
  default: Record<string, unknown>;
  chosen: Record<string, unknown> | null;
  created_seq: number;
  display?: {
    text?: string;
    payload?: Record<string, unknown>;
  };
}

export interface ProviderSnapshot {
  kind?: 'official' | 'relay' | 'claude_code' | string;
  label?: string;
  model?: string;
}

export interface MonopolyRoom {
  id: string;
  mode: 'two_player_three_chat' | 'three_player';
  status: MonopolyRoomStatus;
  game_id?: string | null;
  pair_code?: string;
  seats: Record<string, MonopolyActor>;
  active_actor: Exclude<MonopolyActor, 'codex'> | null;
  pending: PendingDecision | Record<string, never>;
  state: MonopolyGameState;
  event_seq: number;
  created_at?: string;
  updated_at?: string;
}

export interface MonopolyGameState extends Record<string, unknown> {
  status?: string;
  board?: string;
  turn?: string | number;
  positions?: Record<string, number>;
  coins?: Record<string, number>;
  laps?: Record<string, number>;
  identity_reminder?: unknown;
  shop?: {
    price?: number;
    price_each?: Record<string, number>;
    hands?: Record<string, string[]>;
    items?: Array<{ name?: string; description?: string }>;
  };
}

export interface MonopolyMessage {
  id: number;
  room_id: string;
  author: MonopolyActor | 'system';
  content: string;
  thinking?: string;
  reply_to?: number | null;
  game_event_id?: number | null;
  provider_meta?: ProviderSnapshot;
  created_at?: string;
}

export interface MonopolySnapshot {
  room: MonopolyRoom;
  state: MonopolyGameState;
  pending: PendingDecision | null | Record<string, never>;
  messages: MonopolyMessage[];
}

export interface MonopolyGameEvent {
  id?: number;
  room_id?: string;
  seq: number;
  type: string;
  actor?: MonopolyActor | null;
  payload: Record<string, unknown>;
  created_at?: string;
}

export interface AgentStatus {
  ready?: boolean;
  state?: string;
  detail?: string;
  provider?: ProviderSnapshot;
  [key: string]: unknown;
}

export type StreamEnvelope =
  | { type: 'room.snapshot'; data: MonopolySnapshot }
  | { type: 'game.event'; seq: number; data: MonopolyGameEvent }
  | { type: 'game.state'; seq: number; data: MonopolyGameState }
  | { type: 'game.pending'; seq?: number; status?: MonopolyRoomStatus; data: PendingDecision | null }
  | { type: 'chat.message'; data: MonopolyMessage }
  | { type: 'chat.start' | 'chat.delta' | 'chat.done'; actor: 'cc' | 'codex'; delta?: string; content?: string; message_id?: number }
  | { type: 'agent.status'; actor: 'cc' | 'codex'; data: AgentStatus }
  | { type: 'room.error'; seq: number; data: { code?: string; detail?: string } };

export interface MonopolySetupValues {
  first: 'haya' | 'cc';
  flavor: 'light' | 'medium' | 'heavy';
  gameLength: 12 | 18 | 24;
  identityMode: 'off' | 'mixed' | 'nsfw_only';
  reverseChance: number;
  redline: string[];
  openAnal: { haya: boolean; cc: boolean };
  noPenetration: { haya: boolean; cc: boolean };
  resetBlocklist: boolean;
}

export interface SetupConfirmation {
  activeLimits: unknown;
  historyNote: string;
  intensityNote?: string;
}

export interface ParsedBoardPlayer {
  position: number;
  coins: number;
  lap: number;
  hand: string[];
  identity: string;
}

export interface ParsedBoard {
  tileTypes: string[];
  round: number;
  maxRound: number;
  haya: ParsedBoardPlayer;
  cc: ParsedBoardPlayer;
}
