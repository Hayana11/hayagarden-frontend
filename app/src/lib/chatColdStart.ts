/** Chat cold-start limits — INITIAL vs AUTHORITATIVE vs legacy warm-up. */
export const CHAT_LEGACY_INITIAL_LIMIT = 24;
export const CHAT_AUTHORITATIVE_LIMIT = 80;
export const CHAT_LEGACY_WARMUP_LIMIT = 56;

export type ChatColdStartMark =
  | 'chat_mount'
  | 'initial_history_start'
  | 'initial_history_ready'
  | 'first_history_paint_scheduled'
  | 'background_warm_start'
  | 'background_warm_ready'
  | 'catalog_start'
  | 'catalog_ready';

/** DEV-only performance marks — no console spam in production. */
export function markChatColdStart(phase: ChatColdStartMark): void {
  if (!import.meta.env.DEV) return;
  if (typeof performance === 'undefined' || typeof performance.mark !== 'function') return;
  performance.mark(`chat_cold_start:${phase}`);
}

/** Chrome78-safe: rAF then setTimeout(0). */
export function scheduleAfterFirstPaint(fn: () => void): void {
  const run = () => {
    setTimeout(fn, 0);
  };
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(run);
  } else {
    run();
  }
}

/** Prepend older page with id dedupe; preserves ascending id order. */
export function mergeOlderChatMessages<T extends { id: number }>(
  current: T[],
  older: T[],
): T[] {
  if (!older.length) return current;
  const known = new Set(current.map((m) => m.id));
  const fresh = older.filter((m) => !known.has(m.id));
  if (!fresh.length) return current;
  const curEarliest = current[0]?.id;
  if (curEarliest !== undefined && fresh.some((m) => m.id >= curEarliest)) return current;
  return [...fresh, ...current];
}
