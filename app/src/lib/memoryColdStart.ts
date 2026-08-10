/** Memory Library cold-start diagnostics (DEV-only performance marks). */

export const MEMORY_SEARCH_DEBOUNCE_MS = 200;

export function markMemoryPerf(name: string): void {
  if (!import.meta.env.DEV) return;
  try {
    performance.mark(name);
  } catch {
    /* ignore */
  }
}
