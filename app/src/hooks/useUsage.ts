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
    fetchUsageSummary(nowRef.current).then((u) => {
      if (alive) setUsage(u);
    });

    try {
      const es = new EventSource(sseUrl(USAGE_STREAM_PATH));
      sourceRef.current = es;
      es.onmessage = (ev) => {
        try {
          const patch = JSON.parse(ev.data) as Partial<UsageSummary>;
          setUsage((prev) => (prev ? { ...prev, ...patch } : (patch as UsageSummary)));
        } catch {
          // ignore malformed frame
        }
      };
      es.onerror = () => es.close();
    } catch {
      // SSE unavailable in this environment; the polled summary above still applies
    }

    return () => {
      alive = false;
      sourceRef.current?.close();
    };
  }, []);

  return usage;
}
