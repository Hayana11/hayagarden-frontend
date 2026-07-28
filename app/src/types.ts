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

export type UsageAgentId = 'claude' | 'codex';

export interface UsageQuotaWindow {
  usedPct: number | null;
  resetAt: string;
  remainingMinutes: number | null;
}

export interface AgentUsageSummary {
  id: UsageAgentId;
  name: string;
  available: boolean;
  source: string;
  updatedAt: string;
  contextTokens: number | null;
  contextWindowTokens: number | null;
  effectiveLimit: {
    kind: string;
    exhausted: boolean;
    resetText: string;
    observedAt: string;
  } | null;
  fiveHour: UsageQuotaWindow;
  sevenDay: UsageQuotaWindow;
}

export interface UsageSummary {
  win5Pct: number;
  win5ResetAt: string;
  win7Pct: number;
  win7ResetAt: string;
  msgToday: number;
  tokenToday: number;
  bars: UsageBar[];
  agents: Record<UsageAgentId, AgentUsageSummary>;
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
  /** null when the month has no saved budget */
  budget: number | null;
  spent: number;
  categories: LedgerCategory[];
}

export type LedgerWho = 'fy' | 'haya' | 'both';

/** One ledger row, enriched with the 账本 page's meta fields. */
export interface LedgerEntry {
  id: number;
  /** YYYY-MM-DD */
  date: string;
  /** negative = expense, positive = income */
  amount: number;
  /** category id from lib/ledger CATS */
  catId: string;
  title: string;
  who: LedgerWho;
  reason: string;
  /** longer free-text detail (meta.note) */
  note?: string;
  /** linked memory snippet */
  mem?: string;
  /** linked reading reference */
  read?: string;
  /** what this income later became */
  later?: string;
}

export interface LedgerTrendPoint {
  /** YYYY-MM */
  month: string;
  expense: number;
}

export interface PeriodStats {
  lastPeriodStart: string;
  cycleLengthAvgDays: number;
  periodLengthAvgDays: number;
  recordsCount: number;
  nextPredicted: string;
}

export type PeriodFlow = '少量' | '中等' | '多';
export type PeriodPain = '无' | '轻微' | '明显' | '严重';

/** One day's tracked record; every field optional (only what was logged). */
export interface PeriodDayRecord {
  came?: boolean;
  flow?: PeriodFlow;
  pain?: PeriodPain;
  states?: string[];
  extras?: string[];
  sex?: boolean;
  note?: string;
}

/** 'YYYY-MM-DD' -> record */
export type PeriodDays = Record<string, PeriodDayRecord>;

export interface PeriodSettings {
  cycleLength: number;
  periodLength: number;
  lastStart: string;
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
  /** Short (~8 char) title for dense rows, paired with `preview`. */
  title: string;
  /** Fuller (~12 char) title for headings/tooltips with no row-width budget. */
  summaryTitle: string;
  /** Single-line content snippet sized to fit alongside `title` in a row. */
  preview: string;
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
