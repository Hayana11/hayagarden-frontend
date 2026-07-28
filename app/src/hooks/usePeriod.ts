import { useCallback, useEffect, useState } from 'react';
import { fetchPeriodDays, fetchPeriodSettings } from '../lib/api';
import { cyclePhaseLabel, deriveCycle, toYmd, type CycleDerived } from '../lib/cycle';
import type { PeriodDays, PeriodSettings } from '../types';

export type PeriodHookState =
  | { status: 'loading'; reload: () => void }
  | { status: 'error'; message: string; reload: () => void }
  | {
      status: 'ready';
      days: PeriodDays;
      settings: PeriodSettings;
      cycle: CycleDerived;
      phase: string;
      reload: () => void;
    };

export function usePeriod(now: Date = new Date()): PeriodHookState {
  const [days, setDays] = useState<PeriodDays | null>(null);
  const [settings, setSettings] = useState<PeriodSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  const reload = useCallback(() => {
    setError(null);
    setDays(null);
    setSettings(null);
    setTick((t) => t + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([fetchPeriodDays(), fetchPeriodSettings()])
      .then(([d, s]) => {
        if (cancelled) return;
        setDays(d);
        setSettings(s);
        setError(null);
      })
      .catch(() => {
        if (cancelled) return;
        setDays(null);
        setSettings(null);
        setError('经期数据暂时没有连接成功');
      });
    return () => {
      cancelled = true;
    };
  }, [tick]);

  if (error) return { status: 'error', message: error, reload };
  if (!days || !settings) return { status: 'loading', reload };

  const today = toYmd(now);
  const cycle = deriveCycle(days, settings, today);
  return {
    status: 'ready',
    days,
    settings,
    cycle,
    phase: cyclePhaseLabel(cycle),
    reload,
  };
}
