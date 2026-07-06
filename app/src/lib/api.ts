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
import type {
  BookCurrent,
  Heatmap,
  LedgerBudget,
  MemoryCalendar,
  MemoryDayEntry,
  MemorySummary,
  PeriodStats,
  Todo,
  UsageSummary,
} from '../types';

async function withFallback<T>(fn: () => Promise<T>, fallback: () => T): Promise<T> {
  try {
    return await fn();
  } catch {
    return fallback();
  }
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

// GET /api/messages/heatmap?month=YYYY-MM -> Heatmap (chat_messages counted per day)
export function fetchHeatmap(base: Date, isCurrentMonth: boolean, todayDate: number, msgToday: number): Promise<Heatmap> {
  return withFallback(
    () => http.get<Heatmap>('/api/messages/heatmap', { month: monthKey(base) }),
    () => mock.mockHeatmap(base, isCurrentMonth, todayDate, msgToday),
  );
}

// GET /api/usage/summary -> UsageSummary (today msg count is real already; tokens come from SSE `usage` field)
export function fetchUsageSummary(now: Date): Promise<UsageSummary> {
  return withFallback(() => http.get<UsageSummary>('/api/usage/summary'), () => mock.mockUsageSummary(now));
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
  return withFallback(async () => {
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
      budget: Number(budgetResp.amount ?? 0),
      spent,
      categories,
    };
  }, mock.mockLedgerBudget);
}

// GET /api/period/stats -> PeriodStats
export function fetchPeriodStats(): Promise<PeriodStats> {
  return withFallback(
    () =>
      http
        .get<{
          last_period: string | null;
          cycle_length: number | null;
          next_period: string | null;
        }>('/api/period/stats')
        .then((r) => {
          if (!r.last_period || !r.next_period || !r.cycle_length) throw new Error('missing period stats');
          return {
            lastPeriodStart: r.last_period,
            cycleLengthAvgDays: Number(r.cycle_length),
            periodLengthAvgDays: 5,
            recordsCount: 1,
            nextPredicted: r.next_period,
          } satisfies PeriodStats;
        }),
    mock.mockPeriodStats,
  );
}
