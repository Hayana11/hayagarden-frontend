import { daysInMonth, leadingBlanks } from './format';
import type { MemoryCalendarDay } from '../types';

export interface MemCell {
  key: string;
  d: string;
  color: string;
  weight: number;
  ring: string;
  day: number | null;
}

export function buildMemoryCells(base: Date, days: MemoryCalendarDay[], selected: number): MemCell[] {
  const memByDay = new Map(days.map((d) => [d.day, d.hasMemory]));
  const lead = leadingBlanks(base);
  const dim = daysInMonth(base);
  const cells: MemCell[] = [];
  for (let i = 0; i < lead; i++) {
    cells.push({ key: `lead-${i}`, d: '', color: 'transparent', weight: 400, ring: '2px solid transparent', day: null });
  }
  for (let d = 1; d <= dim; d++) {
    const mem = memByDay.get(d) ?? false;
    cells.push({
      key: `d-${d}`,
      d: String(d),
      color: mem ? '#9C3B4A' : '#C9BDB8',
      weight: mem ? 600 : 400,
      ring: d === selected ? '2px solid #9C3B4A' : '2px solid transparent',
      day: d,
    });
  }
  return cells;
}
