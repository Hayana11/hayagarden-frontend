import { useEffect, useState } from 'react';
import { useMonthOffset } from './useMonthOffset';
import { fetchHeatmap } from '../lib/api';
import type { Heatmap } from '../types';

export function useHeatmap(now: Date, msgToday: number) {
  const nav = useMonthOffset(now.getFullYear(), now.getMonth());
  const { base, isCurrent } = nav;
  const today = now.getDate();
  const [data, setData] = useState<Heatmap | null>(null);

  useEffect(() => {
    let alive = true;
    fetchHeatmap(base, isCurrent, today, msgToday).then((h) => {
      if (alive) setData(h);
    });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base.getFullYear(), base.getMonth(), isCurrent, today, msgToday]);

  return { ...nav, data };
}
