/** Memory Library cold-start diagnostics (DEV-only performance marks). */

export const MEMORY_SEARCH_DEBOUNCE_MS = 200;

/** True only when a search response still matches the active trimmed query and generation. */
export function canCommitSearchResults(
  genAtRequest: number,
  genNow: number,
  resultQuery: string,
  activeQuery: string,
): boolean {
  const q = activeQuery.trim();
  return genAtRequest === genNow && resultQuery === q;
}

export function markMemoryPerf(name: string): void {
  if (!import.meta.env.DEV) return;
  try {
    performance.mark(name);
  } catch {
    /* ignore */
  }
}
