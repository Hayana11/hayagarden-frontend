// Thin wrapper kept for callers that still import derivePeriod.
// Authoritative cycle math lives in ./cycle (deriveCycle).
import { cyclePhaseLabel, deriveCycle, toYmd } from './cycle';
import type { PeriodDays, PeriodSettings, PeriodStats } from '../types';

/** @deprecated Prefer deriveCycle(days, settings, today). */
export function derivePeriod(stats: PeriodStats, now: Date): { daysLeft: number | null; phase: string } {
  const today = toYmd(now);
  const days: PeriodDays = stats.lastPeriodStart
    ? { [stats.lastPeriodStart]: { came: true } }
    : {};
  const settings: PeriodSettings = {
    cycleLength: stats.cycleLengthAvgDays,
    periodLength: stats.periodLengthAvgDays,
    lastStart: stats.lastPeriodStart || '',
  };
  const c = deriveCycle(days, settings, today);
  return { daysLeft: c.daysUntil, phase: cyclePhaseLabel(c) };
}
