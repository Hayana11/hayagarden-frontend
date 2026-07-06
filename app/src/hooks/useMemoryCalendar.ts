import { useEffect, useState } from 'react';
import { useMonthOffset } from './useMonthOffset';
import { fetchMemoryCalendar, fetchMemoryDayEntries } from '../lib/api';
import type { MemoryCalendar, MemoryDayEntry } from '../types';

export function useMemoryCalendar(now: Date) {
  const nav = useMonthOffset(now.getFullYear(), now.getMonth());
  const { base, isCurrent } = nav;
  const today = now.getDate();
  const [cal, setCal] = useState<MemoryCalendar | null>(null);
  const [selectedDay, setSelectedDay] = useState<number | null>(null);
  const [entries, setEntries] = useState<MemoryDayEntry[]>([]);

  useEffect(() => {
    setSelectedDay(null);
    let alive = true;
    fetchMemoryCalendar(base, isCurrent, today).then((c) => {
      if (alive) setCal(c);
    });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base.getFullYear(), base.getMonth(), isCurrent, today]);

  const lastMemDay = cal
    ? [...cal.days].reverse().find((d) => d.hasMemory)?.day ?? null
    : null;
  const selected = selectedDay ?? lastMemDay ?? 1;

  useEffect(() => {
    if (!cal) return;
    let alive = true;
    fetchMemoryDayEntries(selected, base, isCurrent, today).then((e) => {
      if (alive) setEntries(e);
    });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cal, selected, base.getFullYear(), base.getMonth(), isCurrent, today]);

  return { ...nav, cal, selected, setSelectedDay, entries };
}
