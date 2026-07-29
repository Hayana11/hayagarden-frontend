/**
 * P-CONTEXT-DAILY-SOFT-WINDOW-FE-R1
 *
 * Soft Window carryover picker — formal chat probes live BFF (flag-off = 404 hide);
 * `/dash/daily-soft-window` stays mock preview. Token never leaves the server BFF.
 */

import { HttpError, http } from './http';

export const CARRYOVER_COUNTS = [0, 3, 5, 10] as const;
export type CarryoverCount = (typeof CARRYOVER_COUNTS)[number];

export const CHAT_DAY_START_HOUR = 4;

/** Browser → nginx `/api/gw` → gateway BFF. Never call `/api/daily-context/*` from FE. */
export const LIVE_DAILY_CONTEXT_CURRENT = '/api/gw/daily-context/current';
export const LIVE_DAILY_CONTEXT_CANDIDATES = '/api/gw/daily-context/carryover-candidates';
export const LIVE_DAILY_CONTEXT_SELECT = '/api/gw/daily-context/select-carryover';

export type CarryoverMessage = {
  message_id: number;
  role: 'user' | 'assistant';
  author: string;
  content_preview: string;
  created_at: string;
};

export type CarryoverRound = {
  round_id: number;
  message_ids: number[];
  messages: CarryoverMessage[];
};

export type DailyContextCurrent = {
  context_id: number;
  context_epoch: number;
  local_day: string;
  boundary_message_id: number;
  carryover_unit: 'round';
  requested_round_count: 0 | 3 | 5 | 10 | null;
  selected_round_count: number;
  selected_message_count: number;
  selected_message_ids: number[];
  carryover_count: number;
  selection_finalized: boolean;
  handoff_status: string;
  resident_generation: number;
  status?: string;
  ok?: boolean;
};

export type CarryoverCandidatesResponse = {
  context_id: number;
  context_epoch: number;
  carryover_unit: 'round';
  available_round_count: number;
  rounds: CarryoverRound[];
  /** Flattened compat only — do not groupIntoRounds for live. */
  candidates: CarryoverMessage[];
  ok?: boolean;
};

export type SelectCarryoverResponse = {
  context_id: number;
  context_epoch: number;
  carryover_unit: 'round';
  requested_round_count: 0 | 3 | 5 | 10;
  selected_round_count: number;
  selected_message_count: number;
  selected_message_ids: number[];
  carryover_count: number;
  finalized_at: string;
  ok?: boolean;
};

/** Formal chat controller states. */
export type SoftWindowUiState =
  | 'probing'
  | 'disabled'
  | 'deferred'
  | 'ready'
  | 'empty'
  | 'submitting'
  | 'locked'
  | 'unavailable'
  /** Preview / classify leftovers mapped into UI copy. */
  | 'loading'
  | 'conflict'
  | 'error'
  | 'auth_error'
  | 'idle'
  /** Manual context window (P-CONTEXT-MANUAL-WINDOW-R1). */
  | 'busy'
  | 'stale'
  | 'no_open_context';

export type SoftWindowErrorKind =
  | 'disabled'
  | 'deferred'
  | 'conflict'
  | 'auth_error'
  | 'error';

export type SoftWindowMockScenario =
  | 'ready'
  | 'loading'
  | 'empty'
  | 'disabled'
  | 'conflict'
  | 'locked'
  | 'deferred'
  | 'error';

const MOCK_SCENARIO_KEY = 'DAILY_SOFT_WINDOW_FE_MOCK';

/**
 * Formal Soft Window is never gated by URL/localStorage.
 * Chat mounts with `{ live: true }`; flag-off truth is upstream 404.
 */
export function isDailySoftWindowFeEnabled(
  _search = typeof location !== 'undefined' ? location.search : '',
): boolean {
  return false;
}

/** Preview page prefers mock unless explicitly opted out. */
export function preferMockDailySoftWindow(
  search = typeof location !== 'undefined' ? location.search : '',
): boolean {
  const params = new URLSearchParams(search);
  const q = params.get('dailySoftWindowMock');
  if (q === '0' || q === 'false') return false;
  return true;
}

export function getMockScenario(
  search = typeof location !== 'undefined' ? location.search : '',
): SoftWindowMockScenario {
  const params = new URLSearchParams(search);
  const q = (params.get('mockScenario') || '').trim() as SoftWindowMockScenario;
  if (
    q === 'ready' ||
    q === 'loading' ||
    q === 'empty' ||
    q === 'disabled' ||
    q === 'conflict' ||
    q === 'locked' ||
    q === 'deferred' ||
    q === 'error'
  ) {
    return q;
  }
  try {
    const stored = localStorage.getItem(MOCK_SCENARIO_KEY);
    if (
      stored === 'ready' ||
      stored === 'loading' ||
      stored === 'empty' ||
      stored === 'disabled' ||
      stored === 'conflict' ||
      stored === 'locked' ||
      stored === 'deferred' ||
      stored === 'error'
    ) {
      return stored;
    }
  } catch {
    /* ignore */
  }
  return 'ready';
}

export function setMockScenario(scenario: SoftWindowMockScenario): void {
  try {
    localStorage.setItem(MOCK_SCENARIO_KEY, scenario);
  } catch {
    /* ignore */
  }
}

/** Map Asia/Shanghai local timestamp string → chat day (04:00 half-open). Preview transcript only. */
export function chatDayKeyFromLocalTs(createdAt: string | null | undefined): string {
  const raw = String(createdAt || '').trim();
  if (!raw) return '';
  const m = raw.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if (!m) return raw.slice(0, 10);
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const d = Number(m[3]);
  const h = Number(m[4]);
  const dt = new Date(y, mo - 1, d);
  if (h < CHAT_DAY_START_HOUR) dt.setDate(dt.getDate() - 1);
  const yy = dt.getFullYear();
  const mm = String(dt.getMonth() + 1).padStart(2, '0');
  const dd = String(dt.getDate()).padStart(2, '0');
  return `${yy}-${mm}-${dd}`;
}

export function isCarryoverCount(n: number): n is CarryoverCount {
  return (CARRYOVER_COUNTS as readonly number[]).includes(n);
}

export function normalizeMessageId(value: unknown): number | null {
  if (typeof value === 'number') {
    if (!Number.isInteger(value) || value <= 0) return null;
    return value;
  }
  if (typeof value === 'string') {
    const s = value.trim();
    if (/^[1-9]\d*$/.test(s)) return Number(s);
  }
  return null;
}

/** Positive integers only — no decimal truncation, no string patching for formal JSON. */
export function normalizePositiveInt(value: unknown): number | null {
  if (typeof value === 'number' && Number.isInteger(value) && value > 0) return value;
  return null;
}

/** Non-negative integers — number type only (formal JSON contract). */
export function normalizeNonNegativeInt(value: unknown): number | null {
  if (typeof value === 'number' && Number.isInteger(value) && value >= 0) return value;
  return null;
}

function parseRequestedRoundCount(value: unknown): 0 | 3 | 5 | 10 | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== 'number' || !Number.isInteger(value)) return null;
  return isCarryoverCount(value) ? value : null;
}

function isRole(v: unknown): v is 'user' | 'assistant' {
  return v === 'user' || v === 'assistant';
}

function parseMessage(raw: unknown): CarryoverMessage | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const message_id = normalizeMessageId(r.message_id);
  if (message_id === null || !isRole(r.role)) return null;
  return {
    message_id,
    role: r.role,
    author: typeof r.author === 'string' ? r.author : '',
    content_preview: typeof r.content_preview === 'string' ? r.content_preview : '',
    created_at: typeof r.created_at === 'string' ? r.created_at : '',
  };
}

/**
 * Validate canonical backend rounds. Fail-soft → null when shape is wrong.
 * Requires carryover_unit === "round" (missing also malformed).
 * message_ids must be present (never synthesized from messages).
 * messages[0] = user; messages[1:] all assistant; no duplicate IDs in response.
 */
export function validateCanonicalRounds(
  rounds: unknown,
  carryoverUnit?: string | null,
): CarryoverRound[] | null {
  if (carryoverUnit !== 'round') return null;
  if (!Array.isArray(rounds)) return null;
  const out: CarryoverRound[] = [];
  const seenIds = new Set<number>();
  for (const raw of rounds) {
    if (!raw || typeof raw !== 'object') return null;
    const r = raw as Record<string, unknown>;
    const round_id = normalizeMessageId(r.round_id);
    if (round_id === null) return null;
    if (!Array.isArray(r.messages) || r.messages.length === 0) return null;
    if (!Array.isArray(r.message_ids)) return null;
    const messages: CarryoverMessage[] = [];
    for (const m of r.messages) {
      const parsed = parseMessage(m);
      if (!parsed) return null;
      messages.push(parsed);
    }
    if (messages[0].role !== 'user') return null;
    if (messages[0].message_id !== round_id) return null;
    for (let i = 1; i < messages.length; i++) {
      if (messages[i].role !== 'assistant') return null;
    }
    const message_ids: number[] = [];
    for (const id of r.message_ids) {
      const n = normalizeMessageId(id);
      if (n === null) return null;
      message_ids.push(n);
    }
    if (message_ids.length !== messages.length) return null;
    for (let i = 0; i < messages.length; i++) {
      if (message_ids[i] !== messages[i].message_id) return null;
      if (seenIds.has(message_ids[i])) return null;
      seenIds.add(message_ids[i]);
    }
    out.push({ round_id, message_ids, messages });
  }
  return out;
}

export function parseCanonicalRounds(
  rounds: unknown,
  carryoverUnit?: string | null,
): CarryoverRound[] | null {
  return validateCanonicalRounds(rounds, carryoverUnit);
}

/** Last N rounds; fewer available → take all; count 0 → []. */
export function pickLastNRounds(rounds: CarryoverRound[], count: number): CarryoverRound[] {
  if (count === 0 || !rounds.length) return [];
  if (count < 0) return [];
  return rounds.slice(-count);
}

export function flattenRoundMessageIds(rounds: CarryoverRound[]): number[] {
  const out: number[] = [];
  for (const r of rounds) {
    for (const id of r.message_ids) out.push(id);
  }
  return out;
}

export function flattenRoundMessages(rounds: CarryoverRound[]): CarryoverMessage[] {
  const out: CarryoverMessage[] = [];
  for (const r of rounds) out.push(...r.messages);
  return out;
}

/** First user of first round + last message of last round. */
export function roundSnippetMessages(rounds: CarryoverRound[]): CarryoverMessage[] {
  if (!rounds.length) return [];
  const first = rounds[0].messages[0];
  if (rounds.length === 1) {
    const msgs = rounds[0].messages;
    return msgs.length > 1 ? [first, msgs[msgs.length - 1]] : [first];
  }
  const lastRound = rounds[rounds.length - 1];
  const last = lastRound.messages[lastRound.messages.length - 1];
  return [first, last];
}

export function selectedRoundMessageIds(rounds: CarryoverRound[], count: number): number[] {
  return flattenRoundMessageIds(pickLastNRounds(rounds, count));
}

export function countLabel(count: CarryoverCount): string {
  if (count === 0) return '不带';
  return `${count}轮`;
}

export function lockedSummaryText(selectedRoundCount: number): string {
  if (selectedRoundCount <= 0) return '今天没有带走昨天的话。';
  return `今天带来了 ${selectedRoundCount} 轮昨天的话。`;
}

/**
 * Locked draft: prefer requested_round_count when it is a valid tier,
 * else fall back to selected_round_count / carryover_count.
 */
export function draftCountFromCurrent(cur: DailyContextCurrent): CarryoverCount {
  if (cur.selection_finalized) {
    const requested = cur.requested_round_count;
    if (requested != null && isCarryoverCount(requested)) return requested;
    if (isCarryoverCount(cur.selected_round_count)) return cur.selected_round_count;
    if (isCarryoverCount(cur.carryover_count)) return cur.carryover_count;
  }
  return 10;
}

/**
 * Insert index for DaySoftBoundary among message ids.
 * Requires BOTH sides of the boundary in the loaded page:
 *   some id ≤ boundary AND some id > boundary.
 * Only old-side or only new-side → null (wait for load-more).
 */
export function boundaryInsertIndex(
  messageIds: number[],
  boundaryMessageId: number,
): number | null {
  const bid = normalizeMessageId(boundaryMessageId);
  if (bid === null || !messageIds.length) return null;
  const hasAtOrBefore = messageIds.some((id) => id <= bid);
  const hasAfter = messageIds.some((id) => id > bid);
  if (!hasAtOrBefore || !hasAfter) return null;
  return messageIds.findIndex((id) => id > bid);
}

export function classifySoftWindowError(err: unknown): SoftWindowErrorKind {
  if (err instanceof HttpError) {
    if (err.status === 404) return 'disabled';
    if (err.status === 423) return 'deferred';
    if (err.status === 409) return 'conflict';
    if (err.status === 401 || err.status === 403) return 'auth_error';
  }
  return 'error';
}

export function softWindowErrorMessage(state: SoftWindowUiState | SoftWindowErrorKind, err?: unknown): string {
  if (state === 'disabled') return '今天的软换窗还没打开（接口未启用）。';
  if (state === 'deferred') return '日界换窗还在路上，稍后再看。';
  if (state === 'conflict') return '选择已经锁定，不能再改。';
  if (state === 'auth_error') return '鉴权桥接失败。';
  if (state === 'empty') return '昨天没有可带走的正式对话。';
  if (state === 'unavailable' || state === 'error') {
    if (err instanceof HttpError && err.detail) return err.detail;
    if (err instanceof Error) return err.message;
    return '行李箱暂时打不开。';
  }
  return '';
}

/** Strict current parser — missing carryover_unit is malformed. */
export function parseCurrent(raw: unknown): DailyContextCurrent | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const context_id = normalizeMessageId(r.context_id);
  const context_epoch = normalizePositiveInt(r.context_epoch);
  if (context_id === null || context_epoch === null) return null;
  if (r.carryover_unit !== 'round') return null;

  const boundaryRaw = r.boundary_message_id;
  let boundary_message_id = 0;
  if (boundaryRaw !== undefined && boundaryRaw !== null) {
    if (boundaryRaw === 0) {
      boundary_message_id = 0;
    } else {
      const bid = normalizeMessageId(boundaryRaw);
      if (bid === null) return null;
      boundary_message_id = bid;
    }
  }

  if (!Array.isArray(r.selected_message_ids)) return null;
  const selected_message_ids: number[] = [];
  const seenIds = new Set<number>();
  for (const id of r.selected_message_ids) {
    const n = normalizeMessageId(id);
    if (n === null) return null;
    if (seenIds.has(n)) return null;
    seenIds.add(n);
    selected_message_ids.push(n);
  }

  const selected_round_count = normalizeNonNegativeInt(r.selected_round_count);
  const selected_message_count = normalizeNonNegativeInt(r.selected_message_count);
  const carryover_count = normalizeNonNegativeInt(r.carryover_count);
  if (
    selected_round_count === null ||
    selected_message_count === null ||
    carryover_count === null
  ) {
    return null;
  }
  if (selected_message_count !== selected_message_ids.length) return null;
  if (carryover_count !== selected_round_count) return null;

  const selection_finalized = r.selection_finalized === true;
  let requested_round_count: 0 | 3 | 5 | 10 | null;
  if (selection_finalized) {
    const tier = parseRequestedRoundCount(r.requested_round_count);
    if (tier === null) return null;
    requested_round_count = tier;
  } else {
    if (r.requested_round_count !== null && r.requested_round_count !== undefined) return null;
    requested_round_count = null;
  }

  const resident_generation = normalizePositiveInt(r.resident_generation) ?? 1;

  return {
    context_id,
    context_epoch,
    local_day: typeof r.local_day === 'string' ? r.local_day : '',
    boundary_message_id,
    carryover_unit: 'round',
    requested_round_count,
    selected_round_count,
    selected_message_count,
    selected_message_ids,
    carryover_count,
    selection_finalized,
    handoff_status: typeof r.handoff_status === 'string' ? r.handoff_status : 'ABSENT',
    resident_generation,
    status: typeof r.status === 'string' ? r.status : undefined,
    ok: r.ok === undefined ? undefined : Boolean(r.ok),
  };
}

/**
 * Strict carryover-candidates response.
 */
export function parseCarryoverCandidatesResponse(raw: unknown): CarryoverCandidatesResponse | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  if (r.carryover_unit !== 'round') return null;
  const context_id = normalizeMessageId(r.context_id);
  const context_epoch = normalizePositiveInt(r.context_epoch);
  if (context_id === null || context_epoch === null) return null;
  const rounds = parseCanonicalRounds(r.rounds, 'round');
  if (!rounds) return null;
  const available_round_count = normalizeNonNegativeInt(r.available_round_count);
  if (available_round_count === null) return null;
  if (available_round_count < rounds.length) return null;
  return {
    ok: r.ok === undefined ? undefined : Boolean(r.ok),
    context_id,
    context_epoch,
    carryover_unit: 'round',
    available_round_count,
    rounds,
    candidates: flattenRoundMessages(rounds),
  };
}

/**
 * Strict select-carryover response. Never patch with local draftCount.
 * Malformed → null (caller fail-soft + GET current).
 */
export function parseSelectCarryoverResponse(raw: unknown): SelectCarryoverResponse | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const context_id = normalizeMessageId(r.context_id);
  const context_epoch = normalizePositiveInt(r.context_epoch);
  if (context_id === null || context_epoch === null) return null;
  if (r.carryover_unit !== 'round') return null;
  const requested_round_count = parseRequestedRoundCount(r.requested_round_count);
  if (requested_round_count === null) return null;
  const selected_round_count = normalizeNonNegativeInt(r.selected_round_count);
  const selected_message_count = normalizeNonNegativeInt(r.selected_message_count);
  const carryover_count = normalizeNonNegativeInt(r.carryover_count);
  if (
    selected_round_count === null ||
    selected_message_count === null ||
    carryover_count === null
  ) {
    return null;
  }
  if (selected_round_count > requested_round_count) return null;
  if (carryover_count !== selected_round_count) return null;
  if (!Array.isArray(r.selected_message_ids)) return null;
  const selected_message_ids: number[] = [];
  const seenIds = new Set<number>();
  for (const id of r.selected_message_ids) {
    const n = normalizeMessageId(id);
    if (n === null) return null;
    if (seenIds.has(n)) return null;
    seenIds.add(n);
    selected_message_ids.push(n);
  }
  if (selected_message_ids.length !== selected_message_count) return null;
  if (requested_round_count === 0) {
    if (selected_round_count !== 0 || selected_message_count !== 0 || selected_message_ids.length !== 0) {
      return null;
    }
  }
  return {
    context_id,
    context_epoch,
    carryover_unit: 'round',
    requested_round_count,
    selected_round_count,
    selected_message_count,
    selected_message_ids,
    carryover_count,
    finalized_at: typeof r.finalized_at === 'string' ? r.finalized_at : '',
    ok: r.ok === undefined ? undefined : Boolean(r.ok),
  };
}

// ── Mock (canonical rounds) ───────────────────────────────────────────

function msg(
  id: number,
  role: 'user' | 'assistant',
  preview: string,
  created_at: string,
  author?: string,
): CarryoverMessage {
  return {
    message_id: id,
    role,
    author: author ?? (role === 'user' ? 'hayana' : 'fyodor'),
    content_preview: preview,
    created_at,
  };
}

function roundOf(...messages: CarryoverMessage[]): CarryoverRound {
  return {
    round_id: messages[0].message_id,
    message_ids: messages.map((m) => m.message_id),
    messages,
  };
}

/** 10 canonical rounds; round 3 is multi-assistant; round 10 is user-only. */
const MOCK_ROUNDS: CarryoverRound[] = [
  roundOf(
    msg(9001, 'user', '今天想先把 Soft Window 的界面定下来，数字用 Bodoni。', '2026-07-27 22:12:08'),
    msg(9002, 'assistant', '好。就做小猫每天会看见的那一层——卡片、弹窗、锁定。', '2026-07-27 22:13:41'),
  ),
  roundOf(
    msg(9003, 'user', '选了就不能再悄悄改成十条。直接发消息就算不带。', '2026-07-27 22:40:02'),
    msg(9004, 'assistant', '锁定以后只读。预览页可以反复试，正式聊天走 BFF。', '2026-07-27 22:41:18'),
  ),
  roundOf(
    msg(9005, 'user', '候选要按完整对话轮：一条 user 加上后面的 assistant。', '2026-07-27 23:05:33'),
    msg(9006, 'assistant', '对。中间如果还有工具回声，也算在同一轮里。', '2026-07-27 23:06:11'),
    msg(9007, 'assistant', '（整理了一下昨天尾巴，十轮都齐了。）', '2026-07-27 23:06:40'),
  ),
  roundOf(
    msg(9008, 'user', '视觉语义写成：不带 / 3轮 / 5轮 / 10轮。', '2026-07-27 23:18:44'),
    msg(9009, 'assistant', '收到。档位按轮数，不再按单条消息数。', '2026-07-27 23:19:20'),
  ),
  roundOf(
    msg(9010, 'user', '手机验收只要打开 /dash/daily-soft-window 就行。', '2026-07-27 23:32:55'),
    msg(9011, 'assistant', '这条 preview route 自带 mock，不依赖 live Soft Window。', '2026-07-27 23:33:40'),
  ),
  roundOf(
    msg(9012, 'user', '正式聊天走 /api/gw/daily-context，token 只在 BFF。', '2026-07-28 00:02:11'),
    msg(9013, 'assistant', '好。浏览器从不带 Authorization，404 就整卡隐藏。', '2026-07-28 00:02:48'),
  ),
  roundOf(
    msg(9014, 'user', 'DAILY_SOFT_WINDOW_ENABLED 继续保持 0。', '2026-07-28 00:40:02'),
    msg(9015, 'assistant', '不调用模型，不跑 Wake，不改 runtime flag。', '2026-07-28 00:40:33'),
  ),
  roundOf(
    msg(9016, 'user', '弹窗去掉左边行李箱，单列就好。', '2026-07-28 01:15:09'),
    msg(9017, 'assistant', '已经改成「新的一天」单列 packing modal。', '2026-07-28 01:15:41'),
  ),
  roundOf(
    msg(9018, 'user', '预览要按 round 高亮整轮，不要只高亮一条。', '2026-07-28 02:08:22'),
    msg(9019, 'assistant', '选中 N 轮时，这一轮里的 user 和 assistant 都会标出来。', '2026-07-28 02:08:55'),
  ),
  // user-only tail
  roundOf(
    msg(9020, 'user', 'Draft 先留着，FE-R1 接正式聊天的 BFF 探针。', '2026-07-28 03:02:55'),
  ),
];

type MockStore = {
  scenario: SoftWindowMockScenario;
  current: DailyContextCurrent;
  rounds: CarryoverRound[];
};

let mockStore: MockStore | null = null;
let activeMockScenario: SoftWindowMockScenario = 'ready';

function todayChatDay(): string {
  const now = new Date();
  return (
    chatDayKeyFromLocalTs(
      `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')} ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:00`,
    ) || '2026-07-28'
  );
}

function cloneRounds(rounds: CarryoverRound[]): CarryoverRound[] {
  return rounds.map((r) => ({
    round_id: r.round_id,
    message_ids: r.message_ids.slice(),
    messages: r.messages.map((m) => ({ ...m })),
  }));
}

function resetMockStore(scenario: SoftWindowMockScenario = activeMockScenario): MockStore {
  activeMockScenario = scenario;
  const localDay = todayChatDay();
  const rounds = scenario === 'empty' ? [] : cloneRounds(MOCK_ROUNDS);
  const locked = scenario === 'locked' || scenario === 'conflict';
  // locked: requested=5, selected=2 (fewer available simulation with fixed ids)
  const lockedPicked = locked ? pickLastNRounds(rounds, 2) : [];
  const selectedIds = flattenRoundMessageIds(lockedPicked);
  const lastId = rounds.length
    ? rounds[rounds.length - 1].message_ids[rounds[rounds.length - 1].message_ids.length - 1]
    : 0;
  mockStore = {
    scenario,
    current: {
      ok: true,
      context_id: 7,
      context_epoch: 42,
      local_day: localDay,
      boundary_message_id: lastId,
      carryover_unit: 'round',
      requested_round_count: locked ? 5 : null,
      selected_round_count: locked ? lockedPicked.length : 0,
      selected_message_count: selectedIds.length,
      selected_message_ids: selectedIds,
      carryover_count: locked ? lockedPicked.length : 0,
      selection_finalized: locked,
      handoff_status: 'ABSENT',
      resident_generation: 1,
      status: 'PROVISIONAL',
    },
    rounds,
  };
  return mockStore;
}

function ensureMockStore(): MockStore {
  if (!mockStore || mockStore.scenario !== activeMockScenario) {
    return resetMockStore(activeMockScenario);
  }
  return mockStore;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function mockCurrent(): Promise<DailyContextCurrent> {
  const store = ensureMockStore();
  if (store.scenario === 'loading') await sleep(1200);
  if (store.scenario === 'disabled') {
    throw new HttpError(404, 'disabled', `GET ${LIVE_DAILY_CONTEXT_CURRENT} failed: 404 · disabled`);
  }
  if (store.scenario === 'deferred') {
    throw new HttpError(
      423,
      'rollover deferred',
      `GET ${LIVE_DAILY_CONTEXT_CURRENT} failed: 423 · rollover deferred`,
      { code: 'rollover_deferred', payload: { ok: false, code: 'rollover_deferred' } },
    );
  }
  if (store.scenario === 'error') {
    throw new HttpError(500, 'mock boom', `GET ${LIVE_DAILY_CONTEXT_CURRENT} failed: 500 · mock boom`);
  }
  return { ...store.current, selected_message_ids: store.current.selected_message_ids.slice() };
}

async function mockCandidates(): Promise<CarryoverCandidatesResponse> {
  const store = ensureMockStore();
  if (store.scenario === 'loading') await sleep(900);
  if (store.scenario === 'disabled') {
    throw new HttpError(404, 'disabled', `GET ${LIVE_DAILY_CONTEXT_CANDIDATES} failed: 404 · disabled`);
  }
  if (store.scenario === 'deferred') {
    throw new HttpError(
      423,
      'rollover deferred',
      `GET ${LIVE_DAILY_CONTEXT_CANDIDATES} failed: 423 · rollover deferred`,
      { code: 'rollover_deferred', payload: { ok: false, code: 'rollover_deferred' } },
    );
  }
  if (store.scenario === 'error') {
    throw new HttpError(500, 'mock boom', `GET ${LIVE_DAILY_CONTEXT_CANDIDATES} failed: 500 · mock boom`);
  }
  const rounds = cloneRounds(store.rounds);
  return {
    ok: true,
    context_id: store.current.context_id,
    context_epoch: store.current.context_epoch,
    carryover_unit: 'round',
    available_round_count: rounds.length,
    rounds,
    candidates: flattenRoundMessages(rounds),
  };
}

async function mockSelect(count: CarryoverCount): Promise<SelectCarryoverResponse> {
  const store = ensureMockStore();
  if (store.scenario === 'disabled') {
    throw new HttpError(404, 'disabled', `POST ${LIVE_DAILY_CONTEXT_SELECT} failed: 404 · disabled`);
  }
  if (store.scenario === 'deferred') {
    throw new HttpError(
      423,
      'rollover deferred',
      `POST ${LIVE_DAILY_CONTEXT_SELECT} failed: 423 · rollover deferred`,
      { code: 'rollover_deferred', payload: { ok: false, code: 'rollover_deferred' } },
    );
  }
  if (
    store.scenario === 'conflict' ||
    (store.current.selection_finalized && store.current.requested_round_count !== count)
  ) {
    throw new HttpError(
      409,
      'carryover selection is locked',
      `POST ${LIVE_DAILY_CONTEXT_SELECT} failed: 409 · carryover selection is locked`,
    );
  }
  if (store.current.selection_finalized && store.current.requested_round_count === count) {
    return {
      ok: true,
      context_id: store.current.context_id,
      context_epoch: store.current.context_epoch,
      carryover_unit: 'round',
      requested_round_count: count,
      selected_round_count: store.current.selected_round_count,
      selected_message_count: store.current.selected_message_count,
      selected_message_ids: store.current.selected_message_ids.slice(),
      carryover_count: store.current.carryover_count,
      finalized_at: '2026-07-28 04:10:00',
    };
  }
  const picked = pickLastNRounds(store.rounds, count);
  const ids = flattenRoundMessageIds(picked);
  store.current.selection_finalized = true;
  store.current.requested_round_count = count;
  store.current.selected_round_count = picked.length;
  store.current.selected_message_count = ids.length;
  store.current.selected_message_ids = ids;
  store.current.carryover_count = picked.length;
  return {
    ok: true,
    context_id: store.current.context_id,
    context_epoch: store.current.context_epoch,
    carryover_unit: 'round',
    requested_round_count: count,
    selected_round_count: picked.length,
    selected_message_count: ids.length,
    selected_message_ids: ids,
    carryover_count: picked.length,
    finalized_at: '2026-07-28 04:10:00',
  };
}

export type DailySoftWindowClient = {
  mode: 'mock' | 'live';
  getCurrent: (init?: RequestInit) => Promise<DailyContextCurrent>;
  getCandidates: (init?: RequestInit) => Promise<CarryoverCandidatesResponse>;
  selectCarryover: (count: CarryoverCount, init?: RequestInit) => Promise<SelectCarryoverResponse>;
  resetMock?: (scenario?: SoftWindowMockScenario) => void;
};

export function createDailySoftWindowClient(opts?: {
  search?: string;
  forceMock?: boolean;
}): DailySoftWindowClient {
  const search = opts?.search ?? (typeof location !== 'undefined' ? location.search : '');
  const useMock = opts?.forceMock ?? preferMockDailySoftWindow(search);
  if (useMock) {
    activeMockScenario = getMockScenario(search);
    resetMockStore(activeMockScenario);
    return {
      mode: 'mock',
      getCurrent: mockCurrent,
      getCandidates: mockCandidates,
      selectCarryover: mockSelect,
      resetMock: (scenario) => {
        const next = scenario ?? getMockScenario(search);
        if (scenario) setMockScenario(scenario);
        resetMockStore(next);
      },
    };
  }
  return {
    mode: 'live',
    getCurrent: async (init) => {
      const raw = await http.get<unknown>(LIVE_DAILY_CONTEXT_CURRENT, undefined, init);
      const parsed = parseCurrent(raw);
      if (!parsed) throw new HttpError(500, 'malformed current', 'malformed daily-context/current');
      return parsed;
    },
    getCandidates: async (init) => {
      const raw = await http.get<unknown>(LIVE_DAILY_CONTEXT_CANDIDATES, undefined, init);
      const parsed = parseCarryoverCandidatesResponse(raw);
      if (!parsed) {
        throw new HttpError(500, 'malformed candidates', 'malformed daily-context/carryover-candidates');
      }
      return parsed;
    },
    selectCarryover: async (count, init) => {
      const raw = await http.post<unknown>(LIVE_DAILY_CONTEXT_SELECT, { count }, init);
      const parsed = parseSelectCarryoverResponse(raw);
      if (!parsed) {
        throw new HttpError(500, 'malformed select', 'malformed daily-context/select-carryover');
      }
      return parsed;
    },
  };
}

/** Demo transcript for the isolated preview page (spans 04:00 boundary). */
export function mockPreviewTranscript(): Array<{
  id: number;
  role: 'user' | 'assistant';
  text: string;
  created_at: string;
  chatDay: string;
}> {
  const flat = flattenRoundMessages(MOCK_ROUNDS);
  const rows = [
    {
      id: 8998,
      role: 'user' as const,
      text: '有点困了，明天再把界面做漂亮一点。',
      created_at: '2026-07-27 21:40:11',
    },
    {
      id: 8999,
      role: 'assistant' as const,
      text: '去睡。四点以后我会还在原处等你。',
      created_at: '2026-07-27 21:41:03',
    },
    ...flat.map((c) => ({
      id: c.message_id,
      role: c.role,
      text: c.content_preview,
      created_at: c.created_at,
    })),
    {
      id: 9022,
      role: 'assistant' as const,
      text: '（清晨的光落进来，旧气泡还安静地排在上面。）',
      created_at: '2026-07-28 04:01:12',
    },
  ];
  return rows.map((r) => ({ ...r, chatDay: chatDayKeyFromLocalTs(r.created_at) }));
}
