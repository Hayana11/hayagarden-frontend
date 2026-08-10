import { useEffect, useRef, useState } from 'react';
import { fetchMemoryEntryContent } from '../lib/api';
import { markMemoryPerf } from '../lib/memoryColdStart';

const inflight = new Map<number, Promise<string>>();

function loadContent(id: number): Promise<string> {
  const existing = inflight.get(id);
  if (existing) return existing;

  markMemoryPerf('memory_detail_start');
  const promise = fetchMemoryEntryContent(id)
    .then((detail) => {
      if (!detail) throw new Error('memory entry not found');
      markMemoryPerf('memory_detail_ready');
      return detail.content;
    })
    .finally(() => {
      inflight.delete(id);
    });
  inflight.set(id, promise);
  return promise;
}

/** Test-only: reset in-flight detail requests between contract simulations. */
export function __resetMemoryEntryContentForTests(): void {
  inflight.clear();
}

export function useMemoryEntryContent(entryId: number | null) {
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(() => entryId != null);
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

    const gen = ++generationRef.current;
    setContent(null);
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

    return () => {
      mountedRef.current = false;
    };
  }, [entryId]);

  return { content, loading };
}
