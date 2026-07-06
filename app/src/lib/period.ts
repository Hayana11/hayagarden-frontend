import type { PeriodStats } from '../types';

export function derivePeriod(stats: PeriodStats, now: Date): { daysLeft: number; phase: string } {
  const nextPeriod = new Date(stats.nextPredicted);
  const daysLeft = Math.max(0, Math.ceil((nextPeriod.getTime() - now.getTime()) / 86400000));

  const lastPeriod = new Date(stats.lastPeriodStart);
  let cd = Math.floor((now.getTime() - lastPeriod.getTime()) / 86400000) + 1;
  if (cd > stats.cycleLengthAvgDays) cd = ((cd - 1) % stats.cycleLengthAvgDays) + 1;

  const ovulationDay = Math.round(stats.cycleLengthAvgDays / 2);
  let phase: string;
  if (cd <= stats.periodLengthAvgDays) phase = `经期第${cd}天`;
  else if (cd < ovulationDay) phase = `卵泡期第${cd - stats.periodLengthAvgDays}天`;
  else if (cd === ovulationDay) phase = '排卵日';
  else phase = `黄体期第${cd - ovulationDay}天`;

  return { daysLeft, phase };
}
