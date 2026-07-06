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
  return withFallback(() => http.get<Todo[]>('/api/todos'), mock.mockTodos);
}

// PATCH /api/todos/:id  body: { done: boolean } -> Todo
export function toggleTodo(id: Todo['id'], done: boolean): Promise<Todo | undefined> {
  return http.patch<Todo>(`/api/todos/${id}`, { done }).catch(() => undefined);
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
  return withFallback(() => http.get<BookCurrent>('/api/books/current'), mock.mockBookCurrent);
}

// GET /api/ledger/budget?month=YYYY-MM -> LedgerBudget
export function fetchLedgerBudget(now: Date): Promise<LedgerBudget> {
  return withFallback(
    () => http.get<LedgerBudget>('/api/ledger/budget', { month: monthKey(now) }),
    mock.mockLedgerBudget,
  );
}

// GET /api/period/stats -> PeriodStats
export function fetchPeriodStats(): Promise<PeriodStats> {
  return withFallback(() => http.get<PeriodStats>('/api/period/stats'), mock.mockPeriodStats);
}
