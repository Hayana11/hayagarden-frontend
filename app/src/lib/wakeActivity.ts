import { useEffect, useState } from 'react';
import { http } from './http';

export interface WakeActivity {
  wake_run_id: string;
  woke_at: string;
  spoke: boolean;
  title: string;
  summary: string;
  tools: string[];
}

// The bounds follow the actual message window, including when Chrome 78
// paginates already-loaded history. Silent wakes do not occupy message slots.
export function useWakeActivity(ready: boolean, since: string, before: string) {
  const [rows, setRows] = useState<WakeActivity[]>([]);
  useEffect(() => {
    if (!ready) return;
    const controller = new AbortController();
    const cache = new Map<string, WakeActivity>();
    let busy = false;
    let newest = since;
    async function refresh() {
      if (busy || document.hidden) return;
      busy = true;
      let cursor = before;
      let cursorId = '';
      const fresh: WakeActivity[] = [];
      try {
        while (!controller.signal.aborted) {
          const page = await http.get<{ items: WakeActivity[] }>('/api/wake/activity', {
            silent: 1, since: newest, before: cursor, before_id: cursorId, limit: 100,
          }, { signal: controller.signal });
          for (const row of page.items) {
            if (!row.spoke && !cache.has(row.wake_run_id)) {
              fresh.push(row);
            }
          }
          if (page.items.length < 100) break;
          const last = page.items[page.items.length - 1];
          cursor = last.woke_at;
          cursorId = last.wake_run_id;
        }
        if (!controller.signal.aborted && fresh.length) {
          for (const row of fresh) cache.set(row.wake_run_id, row);
          const sorted = Array.from(cache.values()).sort((a, b) =>
            a.woke_at.localeCompare(b.woke_at) || a.wake_run_id.localeCompare(b.wake_run_id));
          newest = sorted[sorted.length - 1].woke_at;
          setRows(sorted);
        }
      } catch {
        // Retry on the next poll. Activity availability must not block chat.
      } finally {
        busy = false;
      }
    }
    setRows([]);
    void refresh();
    const timer = window.setInterval(refresh, 30000);
    document.addEventListener('visibilitychange', refresh);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, [ready, since, before]);
  return rows.filter(row => row.woke_at >= since && (!before || row.woke_at < before));
}
