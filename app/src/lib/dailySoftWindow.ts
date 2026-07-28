/**
 * P-CONTEXT-DAILY-SOFT-WINDOW-FE-R0
 *
 * Frontend-only Soft Window carryover picker.
 * Default OFF — does not change formal chat until explicitly gated on.
 * Development uses mock API; live routes stay behind DAILY_SOFT_WINDOW_ENABLED=0.
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

export type DailyContextSummary = {
  ok?: boolean;
  local_day: string;
  context_epoch: number;
  status: DailyContextStatus | string;
  boundary_message_id: number;
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
  candidates: CarryoverCandidate[];
};

export type SelectCarryoverResponse = {
  ok?: boolean;
  selected_message_ids: number[];
  finalized_at: string;
  context_epoch: number;
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

const FE_FLAG_KEY = 'DAILY_SOFT_WINDOW_FE';
const MOCK_SCENARIO_KEY = 'DAILY_SOFT_WINDOW_FE_MOCK';

/** Explicit opt-in only. Production chat stays unchanged by default. */
export function isDailySoftWindowFeEnabled(search = typeof location !== 'undefined' ? location.search : ''): boolean {
  const params = new URLSearchParams(search);
  const q = params.get('dailySoftWindowFe');
  if (q === '1' || q === 'true') return true;
  if (q === '0' || q === 'false') return false;
  try {
    return localStorage.getItem(FE_FLAG_KEY) === '1';
  } catch {
    return false;
  }
}

export function preferMockDailySoftWindow(search = typeof location !== 'undefined' ? location.search : ''): boolean {
  const params = new URLSearchParams(search);
  const q = params.get('dailySoftWindowMock');
  if (q === '1' || q === 'true') return true;
  if (q === '0' || q === 'false') return false;
  // Preview route / explicit FE flag without live backend → mock by default.
  if (params.get('dailySoftWindowFe') === '1') return true;
  try {
    return localStorage.getItem(MOCK_SCENARIO_KEY) !== 'live';
  } catch {
    return true;
  }
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

/** Exact last-N by message_id order (backend stores candidates ascending). */
export function pickLastNCandidates(
  candidates: CarryoverCandidate[],
  count: CarryoverCount,
): CarryoverCandidate[] {
  if (count === 0) return [];
  if (!candidates.length) return [];
  return candidates.slice(-count);
}

export function selectedMessageIds(
  candidates: CarryoverCandidate[],
  count: CarryoverCount,
): number[] {
  return pickLastNCandidates(candidates, count).map((c) => c.message_id);
}

export function countLabel(count: CarryoverCount): string {
  if (count === 0) return '不带';
  return `${count}句`;
}

export function lockedSummaryText(count: number): string {
  if (count <= 0) return '今天没有带走昨天的话。';
  return `今天带来了 ${count} 句昨天的话。`;
}

const MOCK_CANDIDATES: CarryoverCandidate[] = [
  {
    message_id: 9001,
    role: 'user',
    content_preview: '今天想先把 Soft Window 的界面定下来，数字用 Bodoni。',
    created_at: '2026-07-27 23:12:08',
    author: 'hayana',
  },
  {
    message_id: 9002,
    role: 'assistant',
    content_preview: '好。那就只做小猫每天会看见的那一层——卡片、抽屉、锁定。',
    created_at: '2026-07-27 23:13:41',
    author: 'fyodor',
  },
  {
    message_id: 9003,
    role: 'user',
    content_preview: '选了就不能再悄悄改成十条。直接发消息就算不带。',
    created_at: '2026-07-27 23:40:02',
    author: 'hayana',
  },
  {
    message_id: 9004,
    role: 'assistant',
    content_preview: '那条后台任务失败不用管——是我想临时再开一眼状态。',
    created_at: '2026-07-27 23:41:18',
    author: 'fyodor',
  },
  {
    message_id: 9005,
    role: 'user',
    content_preview: '明天四点以后旧消息还留在原处，中间只放一条很轻的分隔。',
    created_at: '2026-07-28 01:05:33',
    author: 'hayana',
  },
  {
    message_id: 9006,
    role: 'assistant',
    content_preview: '☾ 新的一天。昨天的话还留在身后——你选要带几句。',
    created_at: '2026-07-28 01:06:11',
    author: 'fyodor',
  },
  {
    message_id: 9007,
    role: 'user',
    content_preview: '预览要按 message id 精确高亮最后 N 条，不要猜文本。',
    created_at: '2026-07-28 02:18:44',
    author: 'hayana',
  },
  {
    message_id: 9008,
    role: 'assistant',
    content_preview: '可以。候选来自昨天的正式聊天尾巴，选中就锁进行李箱。',
    created_at: '2026-07-28 02:19:20',
    author: 'fyodor',
  },
  {
    message_id: 9009,
    role: 'user',
    content_preview: '手机从底部抽屉，桌面用窄侧栏。别做全屏阻断。',
    created_at: '2026-07-28 03:02:55',
    author: 'hayana',
  },
  {
    message_id: 9010,
    role: 'assistant',
    content_preview: '收到。你先睡，四点以后卡片会等你——不选也可以直接说话。',
    created_at: '2026-07-28 03:03:40',
    author: 'fyodor',
  },
];

type MockStore = {
  scenario: SoftWindowMockScenario;
  summary: DailyContextSummary;
  candidates: CarryoverCandidate[];
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
  mockStore = {
    scenario,
    summary: {
      ok: true,
      local_day: localDay,
      context_epoch: 42,
      status: 'PROVISIONAL',
      boundary_message_id: 9010,
      carryover_count: locked ? 5 : 0,
      selection_finalized: locked,
      handoff_status: 'ABSENT',
      resident_generation: 1,
      context_id: 7,
    },
    candidates: scenario === 'empty' ? [] : MOCK_CANDIDATES.slice(),
    selectedIds: locked ? MOCK_CANDIDATES.slice(-5).map((c) => c.message_id) : [],
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
  const picked = pickLastNCandidates(store.candidates, count);
  store.selectedIds = picked.map((c) => c.message_id);
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
  return {
    mode: 'live',
    getCurrent: () => http.get<DailyContextSummary>('/api/daily-context/current'),
    getCandidates: () => http.get<CarryoverCandidatesResponse>('/api/daily-context/carryover-candidates'),
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
    { id: 8998, role: 'user' as const, text: '有点困了，明天再把界面做漂亮一点。', created_at: '2026-07-27 22:40:11' },
    { id: 8999, role: 'assistant' as const, text: '去睡。四点以后我会还在原处等你。', created_at: '2026-07-27 22:41:03' },
    ...MOCK_CANDIDATES.map((c) => ({
      id: c.message_id,
      role: c.role,
      text: c.content_preview,
      created_at: c.created_at,
    })),
    { id: 9011, role: 'assistant' as const, text: '（清晨的光落进来，旧气泡还安静地排在上面。）', created_at: '2026-07-28 04:01:12' },
  ];
  return rows.map((r) => ({ ...r, chatDay: chatDayKeyFromLocalTs(r.created_at) }));
}
