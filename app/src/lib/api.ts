// Backend contracts here are best-guess REST shapes inferred from the design
// mock data (no live API spec was available). Field names match what the UI
// needs; adjust the shapes below if the real backend differs — everything
// backend-facing lives in this one file.
//
// Every call falls back to the deterministic mock in ./mock if the request
// fails, so the UI keeps working while endpoints are still being stood up
// (same pattern the design used for the Open-Meteo weather call).
import { http } from './http';
import { monthKey } from './format';
import * as mock from './mock';
import { entryToPayload, rowToEntry } from './ledger';
import type {
  BookCurrent,
  Heatmap,
  LedgerBudget,
  LedgerEntry,
  LedgerTrendPoint,
  LedgerWho,
  MemoryCalendar,
  MemoryDayEntry,
  MemoryLibrary,
  MemorySummary,
  PeriodDayRecord,
  PeriodDays,
  PeriodSettings,
  PeriodStats,
  Todo,
  AgentUsageSummary,
  UsageAgentId,
  UsageBar,
  UsageSummary,
} from '../types';

async function withFallback<T>(fn: () => Promise<T>, fallback: () => T): Promise<T> {
  try {
    return await fn();
  } catch {
    return fallback();
  }
}

/** Ledger reads only fall back to demo mock under an explicit DEV mock flag. */
export function ledgerReadsAllowMock(): boolean {
  return Boolean(import.meta.env.DEV) && import.meta.env.VITE_LEDGER_USE_MOCK === '1';
}

async function withLedgerFallback<T>(fn: () => Promise<T>, fallback: () => T): Promise<T> {
  if (!ledgerReadsAllowMock()) return fn();
  return withFallback(fn, fallback);
}

// GET /api/todos -> Todo[]
export function fetchTodos(): Promise<Todo[]> {
  return withFallback(
    () =>
      http
        .get<{ todos: Array<{ id: number | string; content?: string; done?: number | boolean; author?: string | null }> }>('/api/todos')
        .then((r) =>
          (r.todos || []).map((t) => ({
            id: t.id,
            text: t.content ?? '',
            done: Boolean(t.done),
            who: t.author === 'haya' ? 'haya' : 'fy',
          })),
        ),
    mock.mockTodos,
  );
}

// PATCH /api/todos/:id  body: { done: boolean } -> Todo
export function toggleTodo(id: Todo['id'], done: boolean): Promise<Todo | undefined> {
  return http
    .patch<{ id: number | string; content?: string; done?: number | boolean; author?: string | null }>(`/api/todos/${id}`, { done })
    .then((r) => ({ id: r.id, text: r.content ?? '', done: Boolean(r.done), who: (r.author === 'haya' ? 'haya' : 'fy') as 'haya' | 'fy' }))
    .catch(() => undefined);
}

// GET /api/posts/summary -> MemorySummary (core/long/recent counts + 渐变脑 gradient + section items)
export function fetchMemorySummary(): Promise<MemorySummary> {
  return withFallback(() => http.get<MemorySummary>('/api/posts/summary'), mock.mockMemorySummary);
}

// GET /api/posts/calendar?month=YYYY-MM -> MemoryCalendar
export function fetchMemoryCalendar(base: Date, isCurrentMonth: boolean, todayDate: number): Promise<MemoryCalendar> {
  return withFallback(
    () => http.get<MemoryCalendar>('/api/posts/calendar', { month: monthKey(base) }),
    () => mock.mockMemoryCalendar(base, isCurrentMonth, todayDate),
  );
}

// GET /api/posts/calendar/day?date=YYYY-MM-DD -> { entries: MemoryDayEntry[] }
export function fetchMemoryDayEntries(
  day: number,
  base: Date,
  isCurrentMonth: boolean,
  todayDate: number,
): Promise<MemoryDayEntry[]> {
  const mm = String(base.getMonth() + 1).padStart(2, '0');
  const dateStr = `${base.getFullYear()}-${mm}-${String(day).padStart(2, '0')}`;
  return withFallback(
    () => http.get<{ entries: MemoryDayEntry[] }>('/api/posts/calendar/day', { date: dateStr }).then((r) => r.entries),
    () => mock.mockMemoryDayEntries(day, base, isCurrentMonth, todayDate),
  );
}

// GET /api/memories/library -> MemoryLibrary (all topics + entries, for search/timeline/topic/star views)
export function fetchMemoryLibrary(): Promise<MemoryLibrary> {
  return withFallback(() => http.get<MemoryLibrary>('/api/memories/library'), mock.mockMemoryLibrary);
}

// GET /api/messages/heatmap?month=YYYY-MM -> Heatmap (chat_messages counted per day)
export function fetchHeatmap(base: Date, isCurrentMonth: boolean, todayDate: number, msgToday: number): Promise<Heatmap> {
  return withFallback(
    () => http.get<Heatmap>('/api/messages/heatmap', { month: monthKey(base) }),
    () => mock.mockHeatmap(base, isCurrentMonth, todayDate, msgToday),
  );
}

interface LegacyUsageSummary {
  win5Pct: number;
  win5ResetAt: string;
  win7Pct: number;
  win7ResetAt: string;
  msgToday: number;
  tokenToday: number;
  bars: UsageBar[];
}

interface RawQuotaWindow {
  used_percent?: number | null;
  used_percentage?: number | null;
  remaining_percentage?: number | null;
  remaining_minutes?: number | null;
  resets_at?: string | null;
}

interface RawUsageAgent {
  id?: string;
  name?: string;
  quota_source?: string;
  quota?: {
    five_hour?: RawQuotaWindow;
    seven_day?: RawQuotaWindow;
    updated_at?: string;
    latest_tokens?: number;
    context_window_tokens?: number;
    effective_limit?: {
      kind?: string;
      exhausted?: boolean;
      reset_text?: string;
      observed_at?: string;
    };
  };
  active_sessions?: Array<{
    latest_context_tokens?: number;
    context_window_tokens?: number;
  }>;
}

interface ContextUsageSnapshot {
  generated_at?: string;
  agents?: RawUsageAgent[];
}

function clampPct(value: number): number {
  return Math.min(100, Math.max(0, value));
}

function usedPct(window: RawQuotaWindow | undefined): number | null {
  if (!window) return null;
  const direct = window.used_percentage ?? window.used_percent;
  if (typeof direct === 'number' && Number.isFinite(direct)) return clampPct(direct);
  if (typeof window.remaining_percentage === 'number' && Number.isFinite(window.remaining_percentage)) {
    return clampPct(100 - window.remaining_percentage);
  }
  return null;
}

function emptyAgent(id: UsageAgentId): AgentUsageSummary {
  return {
    id,
    name: id === 'claude' ? 'Claude Code' : 'Codex',
    available: false,
    source: 'unavailable',
    updatedAt: '',
    contextTokens: null,
    contextWindowTokens: null,
    effectiveLimit: null,
    fiveHour: { usedPct: null, resetAt: '', remainingMinutes: null },
    sevenDay: { usedPct: null, resetAt: '', remainingMinutes: null },
  };
}

function normalizeAgent(id: UsageAgentId, raw: RawUsageAgent, generatedAt: string): AgentUsageSummary {
  const quota = raw.quota || {};
  const active = raw.active_sessions?.[0];
  const fiveHourPct = usedPct(quota.five_hour);
  const sevenDayPct = usedPct(quota.seven_day);
  const source = raw.quota_source || 'unavailable';
  const fiveHour = {
    usedPct: fiveHourPct,
    resetAt: quota.five_hour?.resets_at || '',
    remainingMinutes: quota.five_hour?.remaining_minutes ?? null,
  };
  const sevenDay = {
    usedPct: sevenDayPct,
    resetAt: quota.seven_day?.resets_at || '',
    remainingMinutes: quota.seven_day?.remaining_minutes ?? null,
  };
  // ccusage fallback may only have reset/remaining minutes (no %); still show the card.
  const hasWindowMeta = Boolean(
    fiveHourPct !== null
    || sevenDayPct !== null
    || fiveHour.resetAt
    || sevenDay.resetAt
    || fiveHour.remainingMinutes !== null
    || sevenDay.remainingMinutes !== null,
  );
  return {
    id,
    name: raw.name || (id === 'claude' ? 'Claude Code' : 'Codex'),
    available: source !== 'unavailable' || hasWindowMeta,
    source,
    updatedAt: quota.updated_at || generatedAt,
    contextTokens: active?.latest_context_tokens ?? quota.latest_tokens ?? null,
    contextWindowTokens: active?.context_window_tokens ?? quota.context_window_tokens ?? null,
    effectiveLimit: quota.effective_limit ? {
      kind: quota.effective_limit.kind || 'rate_limit',
      exhausted: Boolean(quota.effective_limit.exhausted),
      resetText: quota.effective_limit.reset_text || '',
      observedAt: quota.effective_limit.observed_at || '',
    } : null,
    fiveHour,
    sevenDay,
  };
}

function legacyClaude(base: LegacyUsageSummary): AgentUsageSummary {
  return {
    ...emptyAgent('claude'),
    available: true,
    source: 'claude_legacy_usage',
    updatedAt: new Date().toISOString(),
    fiveHour: { usedPct: base.win5Pct, resetAt: base.win5ResetAt, remainingMinutes: null },
    sevenDay: { usedPct: base.win7Pct, resetAt: base.win7ResetAt, remainingMinutes: null },
  };
}

// Combines the existing app-activity summary with independently sourced
// Claude Code and Codex quota snapshots. Missing agent data stays unavailable.
export async function fetchUsageSummary(now: Date): Promise<UsageSummary> {
  let base: LegacyUsageSummary;
  try {
    base = await http.get<LegacyUsageSummary>('/api/usage/summary');
  } catch {
    return mock.mockUsageSummary(now);
  }

  let snapshot: ContextUsageSnapshot | null = null;
  try {
    snapshot = await http.get<ContextUsageSnapshot>('/api/context-usage');
  } catch {
    // The collector endpoint can be deployed after the UI. Claude keeps its
    // existing source; Codex explicitly remains unavailable in the meantime.
  }

  const rawClaude = snapshot?.agents?.find((agent) => agent.id === 'claude');
  const rawCodex = snapshot?.agents?.find((agent) => agent.id === 'codex');
  return {
    ...base,
    agents: {
      claude: rawClaude
        ? normalizeAgent('claude', rawClaude, snapshot?.generated_at || '')
        : snapshot
          ? emptyAgent('claude')
          : legacyClaude(base),
      codex: rawCodex ? normalizeAgent('codex', rawCodex, snapshot?.generated_at || '') : emptyAgent('codex'),
    },
  };
}

// SSE stream carrying live usage updates, path used by useUsage() via EventSource directly.
export const USAGE_STREAM_PATH = '/api/usage/stream';

// GET /api/books/current -> BookCurrent
export function fetchBookCurrent(): Promise<BookCurrent> {
  return withFallback(
    () =>
      http
        .get<{ book: { title?: string; author?: string; read?: number; total?: number } | null }>('/api/books/current')
        .then((r) => {
          const book = r.book;
          if (!book) throw new Error('no current book');
          return {
            title: book.title || '共读中',
            author: book.author || '—',
            volumeLabel: '当前进度',
            page: Number(book.read ?? 0),
            totalPages: Math.max(1, Number(book.total ?? 1)),
            notes: [],
          } satisfies BookCurrent;
        }),
    mock.mockBookCurrent,
  );
}

// GET /api/ledger/budget?month=YYYY-MM -> LedgerBudget
export function fetchLedgerBudget(now: Date): Promise<LedgerBudget> {
  const month = monthKey(now);
  return withLedgerFallback(async () => {
    const [budgetResp, ledgerResp] = await Promise.all([
      http.get<{ amount: number | null }>('/api/ledger/budget', { month }),
      http.get<{ records: Array<{ amount?: number; category?: string | null }> }>('/api/ledger', { month }),
    ]);
    const expenseRows = (ledgerResp.records || []).filter((r) => Number(r.amount ?? 0) < 0);
    const spent = Math.abs(
      expenseRows.reduce((sum, r) => sum + Number(r.amount ?? 0), 0),
    );
    const catMap = new Map<string, number>();
    for (const row of expenseRows) {
      const name = (row.category || '未分类').trim() || '未分类';
      const prev = catMap.get(name) || 0;
      catMap.set(name, prev + Math.abs(Number(row.amount ?? 0)));
    }
    const categories = [...catMap.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
      .map(([name, amount]) => ({ name, amount }));
    return {
      budget: budgetResp.amount === null || budgetResp.amount === undefined ? null : Number(budgetResp.amount),
      spent,
      categories,
    };
  }, mock.mockLedgerBudget);
}

// ── Chat (Fyodor Chat page) ──
import { rowToMsg, type ChatMessageRow, type ChatMsg } from './chat';

export interface ChatPage {
  messages: ChatMsg[];
  hasMoreBefore: boolean;
  hasMoreAfter: boolean;
}

// GET /api/chat/messages?limit=&before=&after= -> { messages, has_more_before, has_more_after }
export function fetchChatMessages(opts: { limit?: number; before?: number; after?: number } = {}): Promise<ChatPage> {
  return http
    .get<{ messages: ChatMessageRow[]; has_more_before: boolean; has_more_after: boolean }>('/api/chat/messages', {
      limit: opts.limit ?? 80,
      before: opts.before,
      after: opts.after,
    })
    .then((r) => ({
      messages: (r.messages || []).map(rowToMsg),
      hasMoreBefore: Boolean(r.has_more_before),
      hasMoreAfter: Boolean(r.has_more_after),
    }))
    .catch(() => ({ messages: [], hasMoreBefore: false, hasMoreAfter: false }));
}

// POST /api/chat/send -> { ok, message_id }. Images go as multipart (backend
// compresses); text files are pre-uploaded via uploadChatFile then referenced.
export function sendChatMessage(
  content: string,
  extra: { fileUrl?: string; fileName?: string; imageFile?: File } = {},
): Promise<number | null> {
  if (extra.imageFile) {
    const fd = new FormData();
    fd.append('author', 'hayana');
    fd.append('content', content);
    fd.append('image', extra.imageFile);
    return fetch('/api/chat/send', { method: 'POST', body: fd })
      .then((r) => (r.ok ? r.json() : null))
      .then((r) => (r?.ok ? (r.message_id ?? null) : null))
      .catch(() => null);
  }
  return http
    .post<{ ok: boolean; message_id?: number }>('/api/chat/send', {
      author: 'hayana',
      content,
      file_url: extra.fileUrl || '',
      file_name: extra.fileName || '',
    })
    .then((r) => (r.ok ? (r.message_id ?? null) : null))
    .catch(() => null);
}

// POST /api/chat/upload_file (multipart) -> { file_url, file_name } — text files ≤2MB
export function uploadChatFile(file: File): Promise<{ fileUrl: string; fileName: string } | null> {
  const fd = new FormData();
  fd.append('file', file);
  return fetch('/api/chat/upload_file', { method: 'POST', body: fd })
    .then((r) => r.json())
    .then((r) => (r?.ok ? { fileUrl: r.file_url, fileName: r.file_name } : null))
    .catch(() => null);
}

// POST /api/chat/edit — archives the tail as an edit branch, truncates after msg
export function editChatMessage(
  msgId: number,
  content: string,
): Promise<{ ok: boolean; messageId: number | null }> {
  return http
    .post<{ ok: boolean; message_id?: number }>('/api/chat/edit', { msg_id: msgId, content })
    .then((r) => ({ ok: Boolean(r.ok), messageId: r.ok && r.message_id ? Number(r.message_id) : null }))
    .catch(() => ({ ok: false, messageId: null }));
}

// POST /api/chat/branch/switch -> { branch_idx, total }
export function switchChatBranch(msgId: number, direction: 1 | -1): Promise<{ branchIdx: number; total: number } | null> {
  return http
    .post<{ ok: boolean; branch_idx: number; total: number }>('/api/chat/branch/switch', { msg_id: msgId, direction })
    .then((r) => (r.ok ? { branchIdx: r.branch_idx, total: r.total } : null))
    .catch(() => null);
}

// POST /api/chat/regen/prepare -> { old_branches, user_message_id } (deletes the assistant row)
export function regenPrepare(
  msgId: number,
): Promise<{ oldBranches: unknown[]; userMessageId: number | null } | null> {
  return http
    .post<{ ok: boolean; old_branches: unknown[]; user_message_id?: number }>(
      '/api/chat/regen/prepare',
      { msg_id: msgId },
    )
    .then((r) =>
      r.ok
        ? {
            oldBranches: r.old_branches,
            userMessageId: r.user_message_id != null ? Number(r.user_message_id) : null,
          }
        : null,
    )
    .catch(() => null);
}

// POST /api/chat/regen/finalize — stitches old branches onto the fresh reply
export function regenFinalize(oldBranches: unknown[]): Promise<{ branchIdx: number; total: number } | null> {
  return http
    .post<{ ok: boolean; branch_idx: number; total: number }>('/api/chat/regen/finalize', { old_branches: oldBranches })
    .then((r) => (r.ok ? { branchIdx: r.branch_idx, total: r.total } : null))
    .catch(() => null);
}

export interface ModelCatalogEntry {
  id: string;
  label?: string;
  desc?: string;
  thinking?: string;
  primary?: boolean;
  dot?: string;
}

export type ChatModelProvider = 'api_relay' | 'claude_code';
export type ChatModelMode = 'default' | 'explicit' | '';

export interface ChatModelCatalog {
  models: ModelCatalogEntry[];
  current: string;
  provider: ChatModelProvider | '';
  modelMode: ChatModelMode;
  configuredModel: string | null;
}

// GET /api/config/model-catalog -> provider-aware current model (MODEL-1A/1B)
export function fetchModelCatalog(): Promise<ChatModelCatalog> {
  return http
    .get<{
      models?: ModelCatalogEntry[];
      current?: string | null;
      provider?: string;
      model_mode?: string;
      configured_model?: string | null;
    }>('/api/config/model-catalog')
    .then((r): ChatModelCatalog => {
      const provider: ChatModelProvider | '' =
        r.provider === 'claude_code' || r.provider === 'api_relay' ? r.provider : '';
      const modelMode: ChatModelMode =
        r.model_mode === 'explicit' || r.model_mode === 'default' ? r.model_mode : '';
      const configured =
        r.configured_model === null || r.configured_model === undefined
          ? null
          : String(r.configured_model);
      return {
        models: r.models || [],
        current: configured || r.current || '',
        provider,
        modelMode: provider === 'claude_code' ? (modelMode || 'default') : modelMode,
        configuredModel: configured,
      };
    })
    .catch((): ChatModelCatalog => ({
      models: [],
      current: '',
      provider: '',
      modelMode: '',
      configuredModel: null,
    }));
}

export interface SetChatModelResult {
  ok: boolean;
  modelMode?: ChatModelMode;
  configuredModel?: string | null;
  effectiveFrom?: string;
}

// POST /api/config/model — CC accepts null for default
export function setChatModel(model: string | null): Promise<SetChatModelResult> {
  return http
    .post<{
      ok?: boolean;
      model_mode?: string;
      configured_model?: string | null;
      effective_from?: string;
    }>('/api/config/model', { model })
    .then((r): SetChatModelResult => {
      const modelMode: ChatModelMode | undefined =
        r.model_mode === 'explicit' || r.model_mode === 'default' ? r.model_mode : undefined;
      return {
        ok: r.ok !== false,
        modelMode,
        configuredModel:
          r.configured_model === undefined
            ? undefined
            : (r.configured_model === null ? null : String(r.configured_model)),
        effectiveFrom: r.effective_from || undefined,
      };
    })
    .catch((): SetChatModelResult => ({ ok: false }));
}

export interface LedgerEntryDraft {
  date: string;
  amount: number;
  catId: string;
  title: string;
  who: LedgerWho;
  reason: string;
  note?: string;
  mem?: string;
  read?: string;
  later?: string;
}

// GET /api/ledger?month=YYYY-MM -> rows with the JSON meta column, mapped to LedgerEntry
export function fetchLedgerEntries(month: string): Promise<LedgerEntry[]> {
  return withLedgerFallback(
    () =>
      http
        .get<{ records: Array<Parameters<typeof rowToEntry>[0]> }>('/api/ledger', { month })
        .then((r) => (r.records || []).map(rowToEntry)),
    () => mock.mockLedgerEntries().filter((e) => e.date.startsWith(month)),
  );
}

// POST /api/ledger -> { ok, id }
export function addLedgerEntry(draft: LedgerEntryDraft): Promise<number | null> {
  return http
    .post<{ ok: boolean; id?: number }>('/api/ledger', entryToPayload(draft))
    .then((r) => {
      if (!r.ok) return null;
      const id = r.id;
      if (typeof id !== 'number' || !Number.isFinite(id) || id <= 0) return null;
      return id;
    })
    .catch(() => null);
}

// PATCH /api/ledger/:id
export function updateLedgerEntry(id: number, draft: LedgerEntryDraft): Promise<boolean> {
  return http
    .patch<{ ok: boolean }>(`/api/ledger/${id}`, entryToPayload(draft))
    .then((r) => Boolean(r.ok))
    .catch(() => false);
}

// DELETE /api/ledger/:id
export function deleteLedgerEntry(id: number): Promise<boolean> {
  return http
    .del<{ ok: boolean }>(`/api/ledger/${id}`)
    .then((r) => Boolean(r.ok))
    .catch(() => false);
}

// GET /api/ledger/trend -> last six months' expenses
export function fetchLedgerTrend(now: Date): Promise<LedgerTrendPoint[]> {
  return withLedgerFallback(
    () => http.get<Array<{ month: string; expense: number }>>('/api/ledger/trend'),
    () => mock.mockLedgerTrend(now),
  );
}

// GET /api/ledger/budget?month= -> { amount } (null when unset)
export function fetchLedgerBudgetAmount(month: string): Promise<number | null> {
  return withLedgerFallback(
    () => http.get<{ amount: number | null }>('/api/ledger/budget', { month }).then((r) => r.amount),
    () => 3000,
  );
}

// POST /api/ledger/budget
export function setLedgerBudgetAmount(month: string, amount: number): Promise<boolean> {
  return http
    .post<{ ok: boolean }>('/api/ledger/budget', { month, amount })
    .then((r) => Boolean(r.ok))
    .catch(() => false);
}

// GET /api/period/days?month=YYYY-MM -> { days: { 'YYYY-MM-DD': PeriodDayRecord } }
// Fetched without a month filter so predictions/averages can look across months.
// Health data: never silently fall back to fixed mock dates on failure.
export function fetchPeriodDays(): Promise<PeriodDays> {
  if (typeof window !== 'undefined' && (window as unknown as { __HAYA_PERIOD_MOCK__?: boolean }).__HAYA_PERIOD_MOCK__) {
    return Promise.resolve(mock.mockPeriodDays());
  }
  return http.get<{ days: PeriodDays }>('/api/period/days').then((r) => r.days || {});
}

// PUT /api/period/day  body: { date, record } — upsert one day's record.
// Backend rebuilds legacy period_records cycle-start markers after write.
export function savePeriodDay(date: string, record: PeriodDayRecord): Promise<boolean> {
  return http
    .put<{ ok: boolean }>('/api/period/day', { date, record })
    .then((r) => Boolean(r.ok));
}

// GET /api/period/settings -> { cycle_length, period_length, last_start }
export function fetchPeriodSettings(): Promise<PeriodSettings> {
  if (typeof window !== 'undefined' && (window as unknown as { __HAYA_PERIOD_MOCK__?: boolean }).__HAYA_PERIOD_MOCK__) {
    return Promise.resolve(mock.mockPeriodSettings());
  }
  return (async () => {
    const [settings, stats] = await Promise.all([
      http.get<{ cycle_length: number | null; period_length: number | null; last_start: string | null }>('/api/period/settings'),
      http
        .get<{ last_period: string | null; cycle_length: number | null; period_length: number | null }>('/api/period/stats')
        .catch(() => ({ last_period: null, cycle_length: null, period_length: null })),
    ]);
    return {
      cycleLength: settings.cycle_length ?? stats.cycle_length ?? 28,
      periodLength: settings.period_length ?? stats.period_length ?? 5,
      lastStart: settings.last_start ?? stats.last_period ?? '',
    };
  })();
}

// PUT /api/period/settings  body: { cycle_length?, period_length?, last_start? }
export function savePeriodSettings(s: PeriodSettings): Promise<boolean> {
  return http
    .put<{ cycle_length: number | null }>('/api/period/settings', {
      cycle_length: s.cycleLength,
      period_length: s.periodLength,
      last_start: s.lastStart || undefined,
    })
    .then(() => true);
}

// GET /api/period/stats -> PeriodStats
// Prefer deriveCycle(days, settings) on the client; this remains for legacy callers.
export function fetchPeriodStats(): Promise<PeriodStats> {
  if (typeof window !== 'undefined' && (window as unknown as { __HAYA_PERIOD_MOCK__?: boolean }).__HAYA_PERIOD_MOCK__) {
    return Promise.resolve(mock.mockPeriodStats());
  }
  return http
    .get<{
      last_period: string | null;
      cycle_length: number | null;
      period_length: number | null;
      next_period: string | null;
    }>('/api/period/stats')
    .then((r) => {
      if (!r.last_period || !r.next_period || !r.cycle_length) throw new Error('missing period stats');
      return {
        lastPeriodStart: r.last_period,
        cycleLengthAvgDays: Number(r.cycle_length),
        periodLengthAvgDays: Number(r.period_length ?? 5),
        recordsCount: 1,
        nextPredicted: r.next_period,
      } satisfies PeriodStats;
    });
}

