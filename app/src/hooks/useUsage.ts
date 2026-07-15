import { useEffect, useRef, useState } from 'react';
import { fetchUsageSummary, USAGE_STREAM_PATH } from '../lib/api';
import { sseUrl } from '../lib/http';
import type { UsageSummary } from '../types';

export function useUsage(now: Date): UsageSummary | null {
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const sourceRef = useRef<EventSource | null>(null);
  const nowRef = useRef(now);
  nowRef.current = now;

  useEffect(() => {
    let alive = true;
    const refresh = () => {
      fetchUsageSummary(nowRef.current).then((u) => {
        if (alive) setUsage(u);
      });
    };
    refresh();
    const timer = window.setInterval(refresh, 15_000);

    try {
      const es = new EventSource(sseUrl(USAGE_STREAM_PATH));
      sourceRef.current = es;
      es.onmessage = refresh;
      es.onerror = () => es.close();
    } catch {
      // SSE unavailable in this environment; the polled summary above still applies
    }

    return () => {
      alive = false;
      window.clearInterval(timer);
      sourceRef.current?.close();
    };
  }, []);

  return usage;
}
