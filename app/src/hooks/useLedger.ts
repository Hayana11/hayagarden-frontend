import { useEffect, useState } from 'react';
import { fetchLedgerBudget } from '../lib/api';
import type { LedgerBudget } from '../types';

export interface LedgerLoadState {
  data: LedgerBudget | null;
  loading: boolean;
  error: boolean;
}

export function useLedger(now: Date): LedgerLoadState {
  const [data, setData] = useState<LedgerBudget | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);
    fetchLedgerBudget(now)
      .then((next) => {
        if (!cancelled) {
          setData(next);
          setLoading(false);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setData(null);
          setError(true);
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [now.getFullYear(), now.getMonth()]);

  return { data, loading, error };
}
