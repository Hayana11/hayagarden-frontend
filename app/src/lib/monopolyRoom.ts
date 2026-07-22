import { http, sseUrl } from './http';
import type {
  MonopolyActor,
  MonopolyGameEvent,
  MonopolyGameState,
  MonopolyMessage,
  MonopolyRoomStatus,
  MonopolySetupValues,
  MonopolySnapshot,
  ParsedBoard,
  ParsedBoardPlayer,
  PendingDecision,
  ProviderSnapshot,
} from './monopolyTypes';

export const PLAYER_NAMES = { haya: '哈娅', cc: 'CC', codex: 'Codex' } as const;

export const REDLINE_OPTIONS = [
  ['anal', '后庭'], ['toys', '玩具'], ['pain', '疼痛'], ['bondage', '束缚'],
  ['public', '公开'], ['degrade', '羞辱'], ['wet', '失禁'], ['foot', '足'],
  ['spit', '口水'], ['milk', '产乳'], ['estim', '电刺激'], ['dp', '双龙'],
  ['hypno', '催眠'], ['wax', '滴蜡'],
] as const;

const BOARD_EMOJI_TO_TYPE: Record<string, string> = {
  '🏁': 'start', '🎯': 'task', '💬': 'truth', '🛒': 'shop',
  '🔒': 'jail', '🎴': 'chance', '❓': 'mystery',
};

const EMPTY_PLAYER: ParsedBoardPlayer = { position: 0, coins: 0, lap: 0, hand: [], identity: '未发放' };

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function numberAt(record: Record<string, unknown> | undefined, names: string[], fallback = 0): number {
  if (!record) return fallback;
  for (const name of names) {
    const value = record[name];
    if (typeof value === 'number' && Number.isFinite(value)) return value;
  }
  return fallback;
}

function parseBoardLine(board: string, name: string): Partial<ParsedBoardPlayer> {
  const line = board.split('\n').find((candidate) => candidate.includes(`${name}@`));
  if (!line) return {};
  const position = Number(line.match(/@(\d+)/)?.[1]);
  const lapHuman = Number(line.match(/第(\d+)圈/)?.[1]);
  const coins = Number(line.match(/💰\s*(\d+)/)?.[1]);
  const handRaw = line.match(/🃏\[([^\]]*)\]/)?.[1] ?? '';
  const identity = line.match(/身份:([^\n]+)/)?.[1]?.trim() || '未发放';
  return {
    position: Number.isFinite(position) ? position : 0,
    lap: Number.isFinite(lapHuman) ? Math.max(0, lapHuman - 1) : 0,
    coins: Number.isFinite(coins) ? coins : 0,
    hand: !handRaw || handRaw === '空' ? [] : handRaw.split(',').map((item) => item.trim()).filter(Boolean),
    identity,
  };
}

export function parseBoard(state: MonopolyGameState): ParsedBoard {
  const board = typeof state.board === 'string' ? state.board : '';
  const tileTypes = [...board.matchAll(/\[.*?([🏁🎯💬🛒🔒🎴❓]).*?\]/gu)]
    .map((match) => BOARD_EMOJI_TO_TYPE[match[1]] || 'task');
  const progress = board.match(/回合\s*(\d+)\/(\d+)/);
  const positions = asRecord(state.positions);
  const coins = asRecord(state.coins);
  const laps = asRecord(state.laps);
  const shop = state.shop && typeof state.shop === 'object' ? state.shop : undefined;
  const hands = shop?.hands;

  const fromState = (actor: 'haya' | 'cc'): ParsedBoardPlayer => {
    const name = PLAYER_NAMES[actor];
    const parsed = parseBoardLine(board, name);
    const stateHand = hands?.[name];
    return {
      ...EMPTY_PLAYER,
      ...parsed,
      position: numberAt(positions, [name, actor, actor === 'haya' ? 'p1' : 'p2'], parsed.position ?? 0),
      coins: numberAt(coins, [name, actor, actor === 'haya' ? 'p1' : 'p2'], parsed.coins ?? 0),
      lap: numberAt(laps, [name, actor, actor === 'haya' ? 'p1' : 'p2'], parsed.lap ?? 0),
      hand: Array.isArray(stateHand) ? stateHand.map(String) : parsed.hand ?? [],
    };
  };

  return {
    tileTypes: tileTypes.length === 20 ? tileTypes : denseBoardTypes(12),
    round: progress ? Number(progress[1]) : 0,
    maxRound: progress ? Number(progress[2]) : 12,
    haya: fromState('haya'),
    cc: fromState('cc'),
  };
}

export function denseBoardTypes(gameLength: number): string[] {
  const dense: Record<number, string> = { 0: 'start', 5: 'chance', 8: 'mystery', 11: 'jail', 14: 'truth', 17: 'shop' };
  const normal: Record<number, string> = { 0: 'start', 4: 'truth', 5: 'chance', 8: 'mystery', 10: 'jail', 12: 'shop', 14: 'truth', 15: 'chance', 17: 'mystery', 19: 'shop' };
  const special = gameLength <= 12 ? dense : normal;
  return Array.from({ length: 20 }, (_, index) => special[index] || 'task');
}

export function pendingStatus(pending: PendingDecision | null): MonopolyRoomStatus {
  if (!pending) return 'idle';
  return pending.kind === 'task' || pending.kind === 'truth'
    ? 'task_pending'
    : pending.kind === 'duel'
      ? 'duel_pending'
      : pending.kind === 'toll'
        ? 'toll_pending'
        : 'super_pending';
}

const TERMINAL_ROOM_STATUSES: ReadonlySet<MonopolyRoomStatus> = new Set([
  'paused', 'engine_down', 'finished',
]);

export function statusAfterEmptyPending(current: MonopolyRoomStatus): MonopolyRoomStatus {
  return TERMINAL_ROOM_STATUSES.has(current) ? current : 'idle';
}

export function resolveRoomStatus(
  current: MonopolyRoomStatus,
  explicit: MonopolyRoomStatus | null | undefined,
  pending: PendingDecision | null,
): MonopolyRoomStatus {
  if (explicit) return explicit;
  if (pending) return pendingStatus(pending);
  return statusAfterEmptyPending(current);
}

export function reduceGameEventStatus(
  current: MonopolyRoomStatus,
  eventType: string,
  payload: Record<string, unknown>,
): MonopolyRoomStatus {
  if (eventType === 'game_paused') return 'paused';
  if (eventType === 'engine_down' || eventType === 'roll_outcome_unknown') return 'engine_down';
  if (eventType === 'game_over') return 'finished';
  if (eventType === 'game_resumed') {
    const restored = payload.status;
    if (typeof restored === 'string') return restored as MonopolyRoomStatus;
    return current;
  }
  return current;
}

export function pendingText(pending: PendingDecision | null, lastEvent: MonopolyGameEvent | null): string {
  const displayText = pending?.display?.text;
  if (typeof displayText === 'string' && displayText.trim()) return displayText;
  const payload = lastEvent?.payload ?? {};
  const task = asRecord(payload.task);
  const truth = asRecord(payload.truth);
  const duel = asRecord(payload.duel);
  const toll = asRecord(payload.toll);
  const candidates = [
    task['内容'], task.content, task.text,
    truth['内容'], truth.content, truth.text,
    duel['内容'], duel.content, duel.text,
    toll['内容'], toll.content, toll.text,
    payload.say, payload.hint,
  ];
  const text = candidates.find((value) => typeof value === 'string' && value.trim());
  if (typeof text === 'string') return text;
  if (!pending) return '';
  return {
    task: '任务内容正在从引擎同步…', truth: '真心话内容正在从引擎同步…',
    duel: '双方完成对决后，请选出胜者。', toll: '过路费已挂账：交钱或劳动抵债。',
    super: '超级任务已挂账：完成或花币买断。',
  }[pending.kind];
}

export function providerLabel(meta?: ProviderSnapshot): string {
  if (!meta) return '';
  return [meta.label || meta.kind, meta.model].filter(Boolean).join(' · ');
}

export function messageTime(value?: string): string {
  if (!value) return '';
  const match = value.match(/(\d{2}):(\d{2})/);
  return match ? `${match[1]}:${match[2]}` : '';
}

export function setupPayload(values: MonopolySetupValues): Record<string, unknown> {
  const openAnal = [values.openAnal.haya ? PLAYER_NAMES.haya : '', values.openAnal.cc ? PLAYER_NAMES.cc : ''].filter(Boolean);
  const noPenetration = [values.noPenetration.haya ? PLAYER_NAMES.haya : '', values.noPenetration.cc ? PLAYER_NAMES.cc : ''].filter(Boolean);
  return {
    p1_name: PLAYER_NAMES.haya,
    p2_name: PLAYER_NAMES.cc,
    first_player: PLAYER_NAMES[values.first],
    flavor: values.flavor,
    game_length: values.gameLength,
    identity_mode: values.identityMode,
    reverse_chance: values.reverseChance,
    redline: values.redline,
    open_anal: openAnal,
    no_penetration: noPenetration,
    reset_blocklist: values.resetBlocklist,
  };
}

export const monopolyApi = {
  createRoom: () => http.post<MonopolySnapshot>('/api/monopoly/rooms', { mode: 'two_player_three_chat' }),
  room: (roomId: string) => http.get<MonopolySnapshot>(`/api/monopoly/rooms/${encodeURIComponent(roomId)}`),
  setup: (roomId: string, values: MonopolySetupValues, expectedEventSeq: number) =>
    http.post<MonopolySnapshot>(`/api/monopoly/rooms/${encodeURIComponent(roomId)}/setup`, {
      ...setupPayload(values), expectedEventSeq,
    }),
  action: (roomId: string, action: string, args: Record<string, unknown>, expectedEventSeq: number) =>
    http.post<MonopolySnapshot>(`/api/monopoly/rooms/${encodeURIComponent(roomId)}/actions`, {
      action, args, expectedEventSeq,
    }),
  message: (roomId: string, content: string, targets: Array<'cc' | 'codex'>, safeWord = false) =>
    http.post<{ ok: boolean; snapshot: MonopolySnapshot; message: MonopolyMessage }>(
      `/api/monopoly/rooms/${encodeURIComponent(roomId)}/messages`,
      { content, targets, safeWord },
    ),
  speak: (roomId: string, targets: Array<'cc' | 'codex'>) =>
    http.post<{ ok: boolean }>(`/api/monopoly/rooms/${encodeURIComponent(roomId)}/speak`, { targets }),
  pause: (roomId: string, expectedEventSeq: number) =>
    http.post<MonopolySnapshot>(`/api/monopoly/rooms/${encodeURIComponent(roomId)}/pause`, { expectedEventSeq }),
  resume: (roomId: string, expectedEventSeq: number) =>
    http.post<MonopolySnapshot>(`/api/monopoly/rooms/${encodeURIComponent(roomId)}/resume`, { expectedEventSeq }),
  deleteRoom: (roomId: string) => http.del<{ ok: boolean }>(`/api/monopoly/rooms/${encodeURIComponent(roomId)}`),
  streamUrl: (roomId: string, after: number) => sseUrl(`/api/monopoly/rooms/${encodeURIComponent(roomId)}/stream?after=${after}`),
};

export function actorFromEngineName(value: unknown): MonopolyActor | null {
  if (value === '哈娅' || value === 'haya' || value === 'p1') return 'haya';
  if (value === 'CC' || value === 'cc' || value === 'p2') return 'cc';
  if (value === 'Codex' || value === 'codex') return 'codex';
  return null;
}
