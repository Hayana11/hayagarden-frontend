// Cycle derivation for the 身体节律 (PeriodScreen) page, ported from the
// Cycle.dc.html design prototype. Pure functions over (day records, settings,
// today) so the screen component stays declarative.
import type { PeriodDays, PeriodSettings } from '../types';

export const CYCLE_STATES = ['正常', '腰酸', '情绪敏感', '困', '想吃甜'];
export const CYCLE_EXTRAS = ['血块', '头痛', '腹泻', '乳房胀痛'];
export const FLOW_LEVELS = ['少量', '中等', '多'] as const;
export const PAIN_LEVELS = ['无', '轻微', '明显', '严重'] as const;

export function parseYmd(s: string): Date {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y, m - 1, d);
}

export function toYmd(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function addDays(s: string, n: number): string {
  const d = parseYmd(s);
  d.setDate(d.getDate() + n);
  return toYmd(d);
}

/** Whole days from b to a (positive when a is after b). */
export function diffDays(a: string, b: string): number {
  return Math.round((parseYmd(a).getTime() - parseYmd(b).getTime()) / 864e5);
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

export function deriveCycle(days: PeriodDays, settings: PeriodSettings, today: string): CycleDerived {
  const { cycleLength, periodLength } = settings;
  const groups = groupFlowDates(days);

  // Current cycle start: the later of the settings value and the latest logged run.
  let currentStart = settings.lastStart;
  for (const g of groups) if (diffDays(g.start, currentStart) > 0) currentStart = g.start;

  const cycleDay = diffDays(today, currentStart) + 1;
  const inPeriod = cycleDay >= 1 && cycleDay <= periodLength;
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
    ovulation.add(addDays(st, cycleLength - 14));
  }

  let lastGroup: FlowGroup | null = groups[groups.length - 1] || null;
  if (inPeriod && groups.length > 1 && lastGroup && lastGroup.start === currentStart) {
    lastGroup = groups[groups.length - 2];
  }
  const lastRange = lastGroup
    ? `${shortMd(lastGroup.start)} – ${shortMd(lastGroup.end === lastGroup.start && !inPeriod ? addDays(lastGroup.start, periodLength - 1) : lastGroup.end)}`
    : `${shortMd(settings.lastStart)} – ${shortMd(addDays(settings.lastStart, periodLength - 1))}`;

  const stateCount: Record<string, number> = {};
  for (const k of Object.keys(days)) {
    const r = days[k];
    if (r?.came === false && r.states) {
      for (const st of r.states) if (st !== '正常') stateCount[st] = (stateCount[st] || 0) + 1;
    }
  }
  const topStates = Object.entries(stateCount)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 2)
    .map(([k]) => k);

  return { currentStart, cycleDay, inPeriod, nextStart, daysUntil, soon, overdue, predicted, ovulation, groups, lastRange, topStates };
}
