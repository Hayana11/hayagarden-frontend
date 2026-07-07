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

/** 1 = 新月 (passing mention) .. 5 = 满月 (core memory). */
export type MemoryWeight = 1 | 2 | 3 | 4 | 5;

export interface MemoryTopicRelation {
  key: string;
  pct: number;
}

export interface MemoryTopic {
  key: string;
  emoji: string;
  name: string;
  desc: string;
  /** Short AI-generated reflection shown at the top of the topic detail view. */
  ai: string;
  related: MemoryTopicRelation[];
}

export interface MemoryEntry {
  id: number;
  date: string;
  time: string;
  weight: MemoryWeight;
  /** List title: up to 12 chars + ··· */
  title: string;
  /** Full generated title (≤12 chars) for detail drawer */
  summaryTitle?: string;
  /** Content preview; length scales with title length */
  preview?: string;
  who: string;
  topics: string[];
  tags: string[];
  content: string;
  /** Ids of other entries this one is semantically linked to. */
  links: number[];
}

export interface MemoryLibrary {
  topics: MemoryTopic[];
  entries: MemoryEntry[];
}
