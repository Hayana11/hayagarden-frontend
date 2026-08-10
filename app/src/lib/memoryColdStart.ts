/** Memory Library cold-start diagnostics (DEV-only performance marks). */

export const MEMORY_SEARCH_DEBOUNCE_MS = 200;

export type MemoryDetailCaches = {
  content: Map<number, string>;
  inflight: Map<number, Promise<string>>;
};

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

/** Prevent stale detail responses from committing after id/generation drift. */
export function canCommitDetailContent(
  genAtRequest: number,
  genNow: number,
  idAtRequest: number,
  idNow: number,
): boolean {
  return genAtRequest === genNow && idAtRequest === idNow;
}

export function createMemoryDetailCaches(): MemoryDetailCaches {
  return { content: new Map(), inflight: new Map() };
}

/** Load detail body with MemoryScreen-scoped cache + in-flight dedupe. */
export async function loadMemoryDetailContent(
  caches: MemoryDetailCaches,
  id: number,
  fetchContent: (entryId: number) => Promise<string>,
): Promise<string> {
  const cached = caches.content.get(id);
  if (cached !== undefined) return cached;

  const existing = caches.inflight.get(id);
  if (existing) return existing;

  const promise = fetchContent(id)
    .then((text) => {
      caches.content.set(id, text);
      return text;
    })
    .finally(() => {
      caches.inflight.delete(id);
    });
  caches.inflight.set(id, promise);
  return promise;
}

export function markMemoryPerf(name: string): void {
  if (!import.meta.env.DEV) return;
  try {
    performance.mark(name);
  } catch {
    /* ignore */
  }
}
