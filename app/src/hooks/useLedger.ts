import { useEffect, useState } from 'react';
import { fetchLedgerBudget } from '../lib/api';
import type { LedgerBudget } from '../types';

export function useLedger(now: Date): LedgerBudget | null {
  const [ledger, setLedger] = useState<LedgerBudget | null>(null);
  useEffect(() => {
    fetchLedgerBudget(now).then(setLedger);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [now.getFullYear(), now.getMonth()]);
  return ledger;
}
