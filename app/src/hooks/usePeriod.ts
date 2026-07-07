import { useEffect, useState } from 'react';
import { fetchPeriodStats } from '../lib/api';
import type { PeriodStats } from '../types';

export function usePeriod(): PeriodStats | null {
  const [period, setPeriod] = useState<PeriodStats | null>(null);
  useEffect(() => {
    fetchPeriodStats().then(setPeriod);
  }, []);
  return period;
}
