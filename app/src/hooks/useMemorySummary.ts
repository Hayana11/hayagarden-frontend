import { useEffect, useState } from 'react';
import { fetchMemorySummary } from '../lib/api';
import type { MemorySummary } from '../types';

export function useMemorySummary(): MemorySummary | null {
  const [summary, setSummary] = useState<MemorySummary | null>(null);
  useEffect(() => {
    fetchMemorySummary().then(setSummary);
  }, []);
  return summary;
}
