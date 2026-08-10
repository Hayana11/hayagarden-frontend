import { useEffect, useRef, useState } from 'react';
import { fetchMemoryEntryContent } from '../lib/api';
import {
  canCommitDetailContent,
  loadMemoryDetailContent,
  markMemoryPerf,
  type MemoryDetailCaches,
} from '../lib/memoryColdStart';

export function useMemoryEntryContent(entryId: number | null, caches: MemoryDetailCaches) {
  const [content, setContent] = useState<string | null>(() =>
    entryId != null ? caches.content.get(entryId) ?? null : null,
  );
  const [loading, setLoading] = useState(
    () => entryId != null && !caches.content.has(entryId),
  );
  const mountedRef = useRef(true);
  const generationRef = useRef(0);

  useEffect(() => {
    mountedRef.current = true;
    if (entryId == null) {
      setContent(null);
      setLoading(false);
      return () => {
        mountedRef.current = false;
      };
    }

    const cached = caches.content.get(entryId);
    if (cached !== undefined) {
      setContent(cached);
      setLoading(false);
      return () => {
        mountedRef.current = false;
      };
    }

    const gen = ++generationRef.current;
    const requestId = entryId;
    setContent(null);
    setLoading(true);
    loadMemoryDetailContent(caches, entryId, async (id) => {
      markMemoryPerf('memory_detail_start');
      const detail = await fetchMemoryEntryContent(id);
      if (!detail) throw new Error('memory entry not found');
      markMemoryPerf('memory_detail_ready');
      return detail.content;
    })
      .then((text) => {
        if (!mountedRef.current || !canCommitDetailContent(gen, generationRef.current, requestId, entryId)) return;
        setContent(text);
        setLoading(false);
      })
      .catch(() => {
        if (!mountedRef.current || !canCommitDetailContent(gen, generationRef.current, requestId, entryId)) return;
        setContent(null);
        setLoading(false);
      });

    return () => {
      mountedRef.current = false;
    };
  }, [entryId, caches]);

  return { content, loading };
}
