/**
 * P-CONTEXT-DAILY-SOFT-WINDOW-FE-R0
 *
 * Frontend Soft Window carryover picker — **preview-only** until backend R1.1
 * round contract lands.
 *
 * - `/dash/daily-soft-window` uses mock API + round semantics
 * - `/dash/chat` is intentionally unwired (no picker, no select-carryover, no auto-lock)
 * - Does not enable `DAILY_SOFT_WINDOW_ENABLED`, does not call resident / Wake
 */

import { HttpError, http } from './http';

export const CARRYOVER_COUNTS = [0, 3, 5, 10] as const;
export type CarryoverCount = (typeof CARRYOVER_COUNTS)[number];

export const CHAT_DAY_START_HOUR = 4;

export type DailyContextStatus =
  | 'ABSENT'
  | 'COMPACTING'
  | 'PROVISIONAL'
  | 'FINALIZED'
  | 'FAILED_RETRYABLE';

export type CarryoverCandidate = {
  message_id: number;
  role: 'user' | 'assistant';
  content_preview: string;
  created_at: string;
  author?: string;
};

/** One conversation round: a user turn + following assistants until the next user. */
export type CarryoverRound = {
  /** Stable id = leading user message_id. */
  round_id: number;
  user: CarryoverCandidate;
  assistants: CarryoverCandidate[];
};

export type DailyContextSummary = {
  ok?: boolean;
  local_day: string;
  context_epoch: number;
  status: DailyContextStatus | string;
  boundary_message_id: number;
  /** Selected round count (not raw message count). */
  carryover_count: number;
  selection_finalized: boolean;
  handoff_status?: string;
  resident_generation?: number;
  context_id: number;
};

export type CarryoverCandidatesResponse = {
  ok?: boolean;
  context_id: number;
  context_epoch: number;
  /** Flat formal messages (ordered). Prefer grouping via `groupIntoRounds`. */
  candidates: CarryoverCandidate[];
  /** Round view for FE preview (awaiting backend R1.1 contract). */
  rounds: CarryoverRound[];
};

export type SelectCarryoverResponse = {
  ok?: boolean;
  selected_message_ids: number[];
  finalized_at: string;
  context_epoch: number;
  /** Selected round count. */
  carryover_count: number;
};

export type SoftWindowUiState =
  | 'idle'
  | 'loading'
  | 'ready'
  | 'empty'
  | 'disabled' // 404
  | 'conflict' // 409
  | 'error'
  | 'locked';

export type SoftWindowMockScenario =
  | 'ready'
  | 'loading'
  | 'empty'
  | 'disabled'
  | 'conflict'
  | 'locked'
  | 'error';

const MOCK_SCENARIO_KEY = 'DAILY_SOFT_WINDOW_FE_MOCK';

/**
 * Formal chat Soft Window integration is paused until backend R1.1 round contract.
 * Preview page passes `enabled: true` explicitly — do not re-enable via URL/localStorage.
 */
export function isDailySoftWindowFeEnabled(
  _search = typeof location !== 'undefined' ? location.search : '',
): boolean {
  return false;
}

/** Preview always uses mock until live round contract is ready. */
export function preferMockDailySoftWindow(
  search = typeof location !== 'undefined' ? location.search : '',
): boolean {
  const params = new URLSearchParams(search);
  const q = params.get('dailySoftWindowMock');
  if (q === '0' || q === 'false') return false;
  return true;
}

export function getMockScenario(search = typeof location !== 'undefined' ? location.search : ''): SoftWindowMockScenario {
  const params = new URLSearchParams(search);
  const q = (params.get('mockScenario') || '').trim() as SoftWindowMockScenario;
  if (
    q === 'ready' ||
    q === 'loading' ||
    q === 'empty' ||
    q === 'disabled' ||
    q === 'conflict' ||
    q === 'locked' ||
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

/** Map Asia/Shanghai local timestamp string → chat day (04:00 half-open). */
export function chatDayKeyFromLocalTs(createdAt: string | null | undefined): string {
  const raw = String(createdAt || '').trim();
  if (!raw) return '';
  // Backend stores +8 local as "YYYY-MM-DD HH:MM:SS" (no TZ suffix).
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

/**
 * Group formal messages into conversation rounds:
 * one user message + following assistants until the next user.
 * Leading assistant-only rows (if any) are dropped for carryover preview.
 */
export function groupIntoRounds(messages: CarryoverCandidate[]): CarryoverRound[] {
  const rounds: CarryoverRound[] = [];
  let current: CarryoverRound | null = null;
  for (const msg of messages) {
    if (msg.role === 'user') {
      if (current) rounds.push(current);
      current = { round_id: msg.message_id, user: msg, assistants: [] };
      continue;
    }
    if (current) current.assistants.push(msg);
  }
  if (current) rounds.push(current);
  return rounds;
}

export function flattenRoundMessages(rounds: CarryoverRound[]): CarryoverCandidate[] {
  const out: CarryoverCandidate[] = [];
  for (const r of rounds) {
    out.push(r.user, ...r.assistants);
  }
  return out;
}

export function pickLastNRounds(rounds: CarryoverRound[], count: CarryoverCount): CarryoverRound[] {
  if (count === 0 || !rounds.length) return [];
  return rounds.slice(-count);
}

/** Exact last-N **rounds**, flattened to messages (preview / highlight). */
export function pickLastNCandidates(
  candidates: CarryoverCandidate[],
  count: CarryoverCount,
): CarryoverCandidate[] {
  return flattenRoundMessages(pickLastNRounds(groupIntoRounds(candidates), count));
}

export function selectedMessageIds(
  candidates: CarryoverCandidate[],
  count: CarryoverCount,
): number[] {
  return pickLastNCandidates(candidates, count).map((c) => c.message_id);
}

export function selectedRoundMessageIds(rounds: CarryoverRound[], count: CarryoverCount): number[] {
  return flattenRoundMessages(pickLastNRounds(rounds, count)).map((c) => c.message_id);
}

/** First/last fragment messages for the packing modal preview. */
export function roundSnippetMessages(rounds: CarryoverRound[]): CarryoverCandidate[] {
  if (!rounds.length) return [];
  if (rounds.length === 1) {
    const only = rounds[0];
    return only.assistants.length ? [only.user, only.assistants[only.assistants.length - 1]] : [only.user];
  }
  const first = rounds[0].user;
  const lastRound = rounds[rounds.length - 1];
  const last = lastRound.assistants[lastRound.assistants.length - 1] ?? lastRound.user;
  return [first, last];
}

export function countLabel(count: CarryoverCount): string {
  if (count === 0) return '不带';
  return `${count}轮`;
}

export function lockedSummaryText(count: number): string {
  if (count <= 0) return '今天没有带走昨天的话。';
  return `今天带来了 ${count} 轮昨天的话。`;
}

/**
 * Mock transcript organized as 10 full conversation rounds.
 * Some rounds include multiple assistant messages (still one round).
 */
const MOCK_CANDIDATES: CarryoverCandidate[] = [
  // 1
  {
    message_id: 9001,
    role: 'user',
    content_preview: '今天想先把 Soft Window 的界面定下来，数字用 Bodoni。',
    created_at: '2026-07-27 22:12:08',
    author: 'hayana',
  },
  {
    message_id: 9002,
    role: 'assistant',
    content_preview: '好。就做小猫每天会看见的那一层——卡片、弹窗、锁定。',
    created_at: '2026-07-27 22:13:41',
    author: 'fyodor',
  },
  // 2
  {
    message_id: 9003,
    role: 'user',
    content_preview: '选了就不能再悄悄改成十条。直接发消息就算不带。',
    created_at: '2026-07-27 22:40:02',
    author: 'hayana',
  },
  {
    message_id: 9004,
    role: 'assistant',
    content_preview: '锁定以后只读。预览页可以反复试，正式聊天先不接线。',
    created_at: '2026-07-27 22:41:18',
    author: 'fyodor',
  },
  // 3 — multi-assistant round
  {
    message_id: 9005,
    role: 'user',
    content_preview: '候选要按完整对话轮：一条 user 加上后面的 assistant。',
    created_at: '2026-07-27 23:05:33',
    author: 'hayana',
  },
  {
    message_id: 9006,
    role: 'assistant',
    content_preview: '对。中间如果还有工具回声，也算在同一轮里。',
    created_at: '2026-07-27 23:06:11',
    author: 'fyodor',
  },
  {
    message_id: 9007,
    role: 'assistant',
    content_preview: '（整理了一下昨天尾巴，十轮都齐了。）',
    created_at: '2026-07-27 23:06:40',
    author: 'fyodor',
  },
  // 4
  {
    message_id: 9008,
    role: 'user',
    content_preview: '视觉语义写成：不带 / 3轮 / 5轮 / 10轮。',
    created_at: '2026-07-27 23:18:44',
    author: 'hayana',
  },
  {
    message_id: 9009,
    role: 'assistant',
    content_preview: '收到。档位按轮数，不再按单条消息数。',
    created_at: '2026-07-27 23:19:20',
    author: 'fyodor',
  },
  // 5
  {
    message_id: 9010,
    role: 'user',
    content_preview: '手机验收只要打开 /dash/daily-soft-window 就行。',
    created_at: '2026-07-27 23:32:55',
    author: 'hayana',
  },
  {
    message_id: 9011,
    role: 'assistant',
    content_preview: '这条 preview route 自带 mock，不依赖 live Soft Window。',
    created_at: '2026-07-27 23:33:40',
    author: 'fyodor',
  },
  // 6
  {
    message_id: 9012,
    role: 'user',
    content_preview: '正式聊天先别自动锁 0，也别弹选择卡。',
    created_at: '2026-07-28 00:02:11',
    author: 'hayana',
  },
  {
    message_id: 9013,
    role: 'assistant',
    content_preview: '好。/dash/chat 保持原样，等 R1.1 round contract。',
    created_at: '2026-07-28 00:02:48',
    author: 'fyodor',
  },
  // 7
  {
    message_id: 9014,
    role: 'user',
    content_preview: 'DAILY_SOFT_WINDOW_ENABLED 继续保持 0。',
    created_at: '2026-07-28 00:40:02',
    author: 'hayana',
  },
  {
    message_id: 9015,
    role: 'assistant',
    content_preview: '不调用模型，不跑 Wake，不改 runtime flag。',
    created_at: '2026-07-28 00:40:33',
    author: 'fyodor',
  },
  // 8
  {
    message_id: 9016,
    role: 'user',
    content_preview: '弹窗去掉左边行李箱，单列就好。',
    created_at: '2026-07-28 01:15:09',
    author: 'hayana',
  },
  {
    message_id: 9017,
    role: 'assistant',
    content_preview: '已经改成「新的一天」单列 packing modal。',
    created_at: '2026-07-28 01:15:41',
    author: 'fyodor',
  },
  // 9
  {
    message_id: 9018,
    role: 'user',
    content_preview: '预览要按 round 高亮整轮，不要只高亮一条。',
    created_at: '2026-07-28 02:08:22',
    author: 'hayana',
  },
  {
    message_id: 9019,
    role: 'assistant',
    content_preview: '选中 N 轮时，这一轮里的 user 和 assistant 都会标出来。',
    created_at: '2026-07-28 02:08:55',
    author: 'fyodor',
  },
  // 10
  {
    message_id: 9020,
    role: 'user',
    content_preview: 'Draft 先留着，等后端 round contract 再接正式聊天。',
    created_at: '2026-07-28 03:02:55',
    author: 'hayana',
  },
  {
    message_id: 9021,
    role: 'assistant',
    content_preview: '收到。你先睡，preview 留在手机里验视觉就好。',
    created_at: '2026-07-28 03:03:40',
    author: 'fyodor',
  },
];

type MockStore = {
  scenario: SoftWindowMockScenario;
  summary: DailyContextSummary;
  candidates: CarryoverCandidate[];
  rounds: CarryoverRound[];
  selectedIds: number[];
};

let mockStore: MockStore | null = null;
/** Active scenario for mock client (avoids relying on browser location in tests). */
let activeMockScenario: SoftWindowMockScenario = 'ready';

function todayChatDay(): string {
  const now = new Date();
  // Approximate Asia/Shanghai as local for FE mock (agent/dev machines use +8).
  return chatDayKeyFromLocalTs(
    `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')} ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:00`,
  );
}

function resetMockStore(scenario: SoftWindowMockScenario = activeMockScenario): MockStore {
  activeMockScenario = scenario;
  const localDay = todayChatDay() || '2026-07-28';
  const locked = scenario === 'locked' || scenario === 'conflict';
  const candidates = scenario === 'empty' ? [] : MOCK_CANDIDATES.slice();
  const rounds = groupIntoRounds(candidates);
  const lockedRounds = locked ? pickLastNRounds(rounds, 5) : [];
  mockStore = {
    scenario,
    summary: {
      ok: true,
      local_day: localDay,
      context_epoch: 42,
      status: 'PROVISIONAL',
      boundary_message_id: candidates.length ? candidates[candidates.length - 1].message_id : 0,
      carryover_count: locked ? lockedRounds.length : 0,
      selection_finalized: locked,
      handoff_status: 'ABSENT',
      resident_generation: 1,
      context_id: 7,
    },
    candidates,
    rounds,
    selectedIds: flattenRoundMessages(lockedRounds).map((c) => c.message_id),
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

async function mockCurrent(): Promise<DailyContextSummary> {
  const store = ensureMockStore();
  if (store.scenario === 'loading') {
    await sleep(1200);
  }
  if (store.scenario === 'disabled') {
    throw new HttpError(404, 'disabled', 'GET /api/daily-context/current failed: 404 · disabled');
  }
  if (store.scenario === 'error') {
    throw new HttpError(500, 'mock boom', 'GET /api/daily-context/current failed: 500 · mock boom');
  }
  return { ...store.summary };
}

async function mockCandidates(): Promise<CarryoverCandidatesResponse> {
  const store = ensureMockStore();
  if (store.scenario === 'loading') await sleep(900);
  if (store.scenario === 'disabled') {
    throw new HttpError(404, 'disabled', 'GET /api/daily-context/carryover-candidates failed: 404 · disabled');
  }
  if (store.scenario === 'error') {
    throw new HttpError(500, 'mock boom', 'GET /api/daily-context/carryover-candidates failed: 500 · mock boom');
  }
  return {
    ok: true,
    context_id: store.summary.context_id,
    context_epoch: store.summary.context_epoch,
    candidates: store.candidates.slice(),
    rounds: store.rounds.map((r) => ({
      round_id: r.round_id,
      user: { ...r.user },
      assistants: r.assistants.map((a) => ({ ...a })),
    })),
  };
}

async function mockSelect(count: CarryoverCount): Promise<SelectCarryoverResponse> {
  const store = ensureMockStore();
  if (store.scenario === 'disabled') {
    throw new HttpError(404, 'disabled', 'POST /api/daily-context/select-carryover failed: 404 · disabled');
  }
  if (store.scenario === 'conflict' || (store.summary.selection_finalized && store.summary.carryover_count !== count)) {
    throw new HttpError(
      409,
      'carryover selection is locked',
      'POST /api/daily-context/select-carryover failed: 409 · carryover selection is locked',
    );
  }
  if (store.summary.selection_finalized && store.summary.carryover_count === count) {
    return {
      ok: true,
      selected_message_ids: store.selectedIds.slice(),
      finalized_at: '2026-07-28 04:10:00',
      context_epoch: store.summary.context_epoch,
      carryover_count: store.summary.carryover_count,
    };
  }
  const picked = pickLastNRounds(store.rounds, count);
  store.selectedIds = flattenRoundMessages(picked).map((c) => c.message_id);
  store.summary.carryover_count = picked.length;
  store.summary.selection_finalized = true;
  return {
    ok: true,
    selected_message_ids: store.selectedIds.slice(),
    finalized_at: '2026-07-28 04:10:00',
    context_epoch: store.summary.context_epoch,
    carryover_count: picked.length,
  };
}

export type DailySoftWindowClient = {
  mode: 'mock' | 'live';
  getCurrent: () => Promise<DailyContextSummary>;
  getCandidates: () => Promise<CarryoverCandidatesResponse>;
  selectCarryover: (count: CarryoverCount) => Promise<SelectCarryoverResponse>;
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
  // Live paths exist for a future Integration PR only. Chat is unwired in this Draft.
  return {
    mode: 'live',
    getCurrent: () => http.get<DailyContextSummary>('/api/daily-context/current'),
    getCandidates: () =>
      http.get<CarryoverCandidatesResponse>('/api/daily-context/carryover-candidates').then((res) => ({
        ...res,
        rounds: res.rounds?.length ? res.rounds : groupIntoRounds(res.candidates || []),
      })),
    selectCarryover: (count) =>
      http.post<SelectCarryoverResponse>('/api/daily-context/select-carryover', { count }),
  };
}

export function classifySoftWindowError(err: unknown): SoftWindowUiState {
  if (err instanceof HttpError) {
    if (err.status === 404) return 'disabled';
    if (err.status === 409) return 'conflict';
    return 'error';
  }
  return 'error';
}

export function softWindowErrorMessage(state: SoftWindowUiState, err?: unknown): string {
  if (state === 'disabled') return '今天的软换窗还没打开（接口未启用）。';
  if (state === 'conflict') return '选择已经锁定，不能再改。';
  if (state === 'empty') return '昨天没有可带走的正式对话。';
  if (state === 'error') {
    if (err instanceof HttpError && err.detail) return err.detail;
    if (err instanceof Error) return err.message;
    return '行李箱暂时打不开。';
  }
  return '';
}

/** Demo transcript for the isolated preview page (spans 04:00 boundary). */
export function mockPreviewTranscript(): Array<{
  id: number;
  role: 'user' | 'assistant';
  text: string;
  created_at: string;
  chatDay: string;
}> {
  const rows = [
    { id: 8998, role: 'user' as const, text: '有点困了，明天再把界面做漂亮一点。', created_at: '2026-07-27 21:40:11' },
    { id: 8999, role: 'assistant' as const, text: '去睡。四点以后我会还在原处等你。', created_at: '2026-07-27 21:41:03' },
    ...MOCK_CANDIDATES.map((c) => ({
      id: c.message_id,
      role: c.role,
      text: c.content_preview,
      created_at: c.created_at,
    })),
    { id: 9022, role: 'assistant' as const, text: '（清晨的光落进来，旧气泡还安静地排在上面。）', created_at: '2026-07-28 04:01:12' },
  ];
  return rows.map((r) => ({ ...r, chatDay: chatDayKeyFromLocalTs(r.created_at) }));
}
