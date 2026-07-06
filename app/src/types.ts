export type Who = 'fy' | 'haya';

export interface Todo {
  id: number | string;
  text: string;
  who: Who;
  done: boolean;
}

export interface MemoryItem {
  text: string;
  date: string;
  who: Who;
}

export interface MemorySection {
  key: 'core' | 'long' | 'recent';
  title: string;
  count: number;
  items: MemoryItem[];
}

export interface MemorySummary {
  core: number;
  long: number;
  recent: number;
  /** 0..1, weight toward amber (哈娅) side of 渐变脑 */
  gradient: number;
  sections: MemorySection[];
}

export interface MemoryCalendarDay {
  day: number;
  hasMemory: boolean;
}

export interface MemoryCalendar {
  count: number;
  days: MemoryCalendarDay[];
}

export interface MemoryDayEntry {
  cat: string;
  title: string;
  date: string;
}

export interface HeatmapDay {
  day: number;
  count: number;
}

export interface Heatmap {
  days: HeatmapDay[];
  todayCount: number;
  streakDays: number;
}

export interface UsageBar {
  date: string;
  fy: number;
  haya: number;
}

export interface UsageSummary {
  win5Pct: number;
  win5ResetAt: string;
  win7Pct: number;
  win7ResetAt: string;
  msgToday: number;
  tokenToday: number;
  bars: UsageBar[];
}

export interface BookNote {
  text: string;
  who: Who;
  at: string;
}

export interface BookCurrent {
  title: string;
  author: string;
  volumeLabel: string;
  page: number;
  totalPages: number;
  notes: BookNote[];
}

export interface LedgerCategory {
  name: string;
  amount: number;
}

export interface LedgerBudget {
  budget: number;
  spent: number;
  categories: LedgerCategory[];
}

export interface PeriodStats {
  lastPeriodStart: string;
  cycleLengthAvgDays: number;
  periodLengthAvgDays: number;
  recordsCount: number;
  nextPredicted: string;
}

export interface WeatherNow {
  temp: number;
  hum: number;
  code: number;
  mock?: boolean;
}
