import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchMemoryEntryContent } from '../lib/api';
import { markMemoryPerf } from '../lib/memoryColdStart';

const contentCache = new Map<number, string>();
const inflight = new Map<number, Promise<string>>();

function loadContent(id: number): Promise<string> {
  const cached = contentCache.get(id);
  if (cached !== undefined) return Promise.resolve(cached);

  const existing = inflight.get(id);
  if (existing) return existing;

  markMemoryPerf('memory_detail_start');
  const promise = fetchMemoryEntryContent(id)
    .then((detail) => {
      if (!detail) throw new Error('memory entry not found');
      contentCache.set(id, detail.content);
      markMemoryPerf('memory_detail_ready');
      return detail.content;
    })
    .finally(() => {
      inflight.delete(id);
    });
  inflight.set(id, promise);
  return promise;
}

/** Test-only: reset per-mount detail cache. */
export function __resetMemoryEntryContentForTests(): void {
  contentCache.clear();
  inflight.clear();
}

export function useMemoryEntryContent(entryId: number | null) {
  const [content, setContent] = useState<string | null>(() =>
    entryId != null ? contentCache.get(entryId) ?? null : null,
  );
  const [loading, setLoading] = useState(
    () => entryId != null && !contentCache.has(entryId),
  );
  const mountedRef = useRef(true);
  const generationRef = useRef(0);

  const reload = useCallback(() => {
    if (entryId == null) return;
    const gen = ++generationRef.current;
    const cached = contentCache.get(entryId);
    if (cached !== undefined) {
      setContent(cached);
      setLoading(false);
      return;
    }
    setLoading(true);
    loadContent(entryId)
      .then((text) => {
        if (!mountedRef.current || generationRef.current !== gen) return;
        setContent(text);
        setLoading(false);
      })
      .catch(() => {
        if (!mountedRef.current || generationRef.current !== gen) return;
        setContent(null);
        setLoading(false);
      });
  }, [entryId]);

  useEffect(() => {
    mountedRef.current = true;
    if (entryId == null) {
      setContent(null);
      setLoading(false);
      return () => {
        mountedRef.current = false;
      };
    }
    reload();
    return () => {
      mountedRef.current = false;
    };
  }, [entryId, reload]);

  return { content, loading };
}
