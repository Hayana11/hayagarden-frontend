import { daysInMonth, leadingBlanks } from './format';
import type { HeatmapDay } from '../types';

export interface HeatmapCell {
  key: string;
  d: string;
  n: string;
  bg: string;
  dColor: string;
  nColor: string;
  border: string;
}

export function buildHeatmapCells(base: Date, days: HeatmapDay[], todayDate: number, isCurrent: boolean): HeatmapCell[] {
  const countByDay = new Map(days.map((d) => [d.day, d.count]));
  const lead = leadingBlanks(base);
  const dim = daysInMonth(base);
  const cells: HeatmapCell[] = [];
  for (let i = 0; i < lead; i++) {
    cells.push({ key: `lead-${i}`, d: '', n: '', bg: 'transparent', border: '2px solid transparent', dColor: 'transparent', nColor: 'transparent' });
  }
  for (let d = 1; d <= dim; d++) {
    const today = isCurrent && d === todayDate;
    const n = countByDay.get(d) ?? null;
    let bg = '#F9F5F3';
    let dColor = '#C9BDB8';
    let nColor = 'transparent';
    if (n !== null) {
      dColor = '#6B5A55';
      nColor = '#A18E88';
      if (n < 120) bg = '#F6EDEA';
      else if (n < 200) bg = '#F0DDD8';
      else if (n < 280) bg = '#E6C7C1';
      else if (n < 360) {
        bg = '#D6A5A1';
        dColor = '#FFFFFF';
        nColor = 'rgba(255,255,255,0.85)';
      } else {
        bg = '#BF8288';
        dColor = '#FFFFFF';
        nColor = 'rgba(255,255,255,0.85)';
      }
    }
    cells.push({
      key: `d-${d}`,
      d: String(d),
      n: n === null ? '' : String(n),
      bg,
      dColor,
      nColor,
      border: today ? '2px solid #B76E79' : '2px solid transparent',
    });
  }
  return cells;
}

export function heatmapStats(base: Date, days: HeatmapDay[]): { total: number; avg: number; maxLabel: string } {
  let total = 0;
  let max = 0;
  let maxD = base.getDate() || 1;
  for (const { day, count } of days) {
    total += count;
    if (count > max) {
      max = count;
      maxD = day;
    }
  }
  const avg = days.length ? Math.round(total / days.length) : 0;
  const maxLabel = days.length ? `${base.getMonth() + 1}/${maxD}（${max}条）` : '—';
  return { total, avg, maxLabel };
}
