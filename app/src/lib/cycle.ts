// Cycle derivation for the 身体节律 (PeriodScreen) page and DashScreen card.
// Pure functions over (day records, settings, today). All YYYY-MM-DD math uses
// UTC epoch-days so DST / local timezone never shifts calendar dates.
import type { PeriodDays, PeriodSettings } from '../types';

export const CYCLE_STATES = ['正常', '腰酸', '情绪敏感', '困', '想吃甜'];
export const CYCLE_EXTRAS = ['血块', '头痛', '腹泻', '乳房胀痛'];
export const FLOW_LEVELS = ['少量', '中等', '多'] as const;
export const PAIN_LEVELS = ['无', '轻微', '明显', '严重'] as const;

const PRE_PERIOD_WINDOW = 5;

/** Local midnight Date for calendar UI (weekday / month grid). Not for day math. */
export function parseYmd(s: string): Date {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y, m - 1, d);
}

export function toYmd(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** Integer day index from a calendar YYYY-MM-DD (UTC, timezone-safe). */
export function ymdToEpochDay(s: string): number {
  const [y, m, d] = s.split('-').map(Number);
  return Math.floor(Date.UTC(y, m - 1, d) / 864e5);
}

export function epochDayToYmd(n: number): string {
  const dt = new Date(n * 864e5);
  return `${dt.getUTCFullYear()}-${String(dt.getUTCMonth() + 1).padStart(2, '0')}-${String(dt.getUTCDate()).padStart(2, '0')}`;
}

export function addDays(s: string, n: number): string {
  return epochDayToYmd(ymdToEpochDay(s) + n);
}

/** Whole days from b to a (positive when a is after b). */
export function diffDays(a: string, b: string): number {
  return ymdToEpochDay(a) - ymdToEpochDay(b);
}

/** '2026-07-10' -> '07.10' */
export function shortMd(s: string): string {
  return s.slice(5).replace('-', '.');
}

export interface FlowGroup {
  start: string;
  end: string;
}

/** Group consecutive came=true dates into period runs. */
export function groupFlowDates(days: PeriodDays): FlowGroup[] {
  const flowDates = Object.keys(days)
    .filter((k) => days[k]?.came === true)
    .sort();
  const groups: FlowGroup[] = [];
  for (const d of flowDates) {
    const g = groups[groups.length - 1];
    if (g && diffDays(d, g.end) <= 1) g.end = d;
    else groups.push({ start: d, end: d });
  }
  return groups;
}

export interface CycleDerived {
  currentStart: string;
  cycleDay: number;
  inPeriod: boolean;
  nextStart: string;
  daysUntil: number;
  soon: boolean;
  overdue: boolean;
  /** dates predicted to be inside a period (excluding logged flow days) */
  predicted: Set<string>;
  /** dates predicted to be ovulation days */
  ovulation: Set<string>;
  groups: FlowGroup[];
  /** '07.14 – 07.18' — last completed period (or projection off settings) */
  lastRange: string;
  /** most-frequent non-正常 pre-period states, for the observation line */
  topStates: string[];
}

function resolveCurrentStart(settings: PeriodSettings, today: string, groups: FlowGroup[]): string {
  let currentStart = settings.lastStart || '';
  for (const g of groups) {
    if (!currentStart || diffDays(g.start, currentStart) > 0) currentStart = g.start;
  }
  return currentStart || today;
}

/** Actual record beats prediction for inPeriod. */
export function resolveInPeriod(
  days: PeriodDays,
  today: string,
  cycleDay: number,
  periodLength: number,
): boolean {
  const rec = days[today];
  if (rec?.came === true) return true;
  if (rec?.came === false) return false;
  return cycleDay >= 1 && cycleDay <= periodLength;
}

/** States logged on non-bleeding days in the 1–5 days before each real period start. */
export function collectPrePeriodTopStates(days: PeriodDays, groups: FlowGroup[], limit = 2): string[] {
  const window = new Set<string>();
  for (const g of groups) {
    for (let i = 1; i <= PRE_PERIOD_WINDOW; i++) {
      window.add(addDays(g.start, -i));
    }
  }
  const stateCount: Record<string, number> = {};
  for (const k of window) {
    const r = days[k];
    if (!r || r.came === true || !r.states) continue;
    for (const st of r.states) {
      if (st !== '正常') stateCount[st] = (stateCount[st] || 0) + 1;
    }
  }
  return Object.entries(stateCount)
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit)
    .map(([k]) => k);
}

export function deriveCycle(days: PeriodDays, settings: PeriodSettings, today: string): CycleDerived {
  const cycleLength = settings.cycleLength || 28;
  const periodLength = settings.periodLength || 5;
  const groups = groupFlowDates(days);

  const currentStart = resolveCurrentStart(settings, today, groups);
  const cycleDay = diffDays(today, currentStart) + 1;
  const inPeriod = resolveInPeriod(days, today, cycleDay, periodLength);
  const nextStart = addDays(currentStart, cycleLength);
  const daysUntil = diffDays(nextStart, today);
  const soon = !inPeriod && daysUntil >= 0 && daysUntil <= 3;
  const overdue = !inPeriod && daysUntil < 0;

  const predicted = new Set<string>();
  const ovulation = new Set<string>();
  for (let k = 0; k <= 4; k++) {
    const st = addDays(currentStart, k * cycleLength);
    for (let i = 0; i < periodLength; i++) {
      const d = addDays(st, i);
      if (days[d]?.came !== true) predicted.add(d);
    }
    // Ovulation = next period start − 14 days
    ovulation.add(addDays(st, cycleLength - 14));
  }

  let lastGroup: FlowGroup | null = groups[groups.length - 1] || null;
  if (inPeriod && groups.length > 1 && lastGroup && lastGroup.start === currentStart) {
    lastGroup = groups[groups.length - 2];
  }
  const lastRange = lastGroup
    ? `${shortMd(lastGroup.start)} – ${shortMd(lastGroup.end === lastGroup.start && !inPeriod ? addDays(lastGroup.start, periodLength - 1) : lastGroup.end)}`
    : settings.lastStart
      ? `${shortMd(settings.lastStart)} – ${shortMd(addDays(settings.lastStart, periodLength - 1))}`
      : '—';

  const topStates = collectPrePeriodTopStates(days, groups);

  return {
    currentStart,
    cycleDay,
    inPeriod,
    nextStart,
    daysUntil,
    soon,
    overdue,
    predicted,
    ovulation,
    groups,
    lastRange,
    topStates,
  };
}

/** Home card phase label — same derivation as PeriodScreen. */
export function cyclePhaseLabel(c: CycleDerived): string {
  if (c.inPeriod) return `经期第${c.cycleDay}天`;
  if (c.overdue) return `逾期 ${-c.daysUntil} 天`;
  return `周期第${c.cycleDay}天`;
}
