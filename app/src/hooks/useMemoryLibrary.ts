import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchMemoryLibraryIndex } from '../lib/api';
import { markMemoryPerf } from '../lib/memoryColdStart';
import type { MemoryLibraryIndex } from '../types';

let cachedMemoryIndex: MemoryLibraryIndex | null = null;
let memoryIndexInflight: Promise<MemoryLibraryIndex> | null = null;

function loadMemoryIndex(): Promise<MemoryLibraryIndex> {
  if (memoryIndexInflight) return memoryIndexInflight;
  markMemoryPerf('memory_index_start');
  memoryIndexInflight = fetchMemoryLibraryIndex()
    .then((data) => {
      cachedMemoryIndex = data;
      markMemoryPerf('memory_index_ready');
      return data;
    })
    .finally(() => {
      memoryIndexInflight = null;
    });
  return memoryIndexInflight;
}

/** Test-only: reset module cache between contract simulations. */
export function __resetMemoryLibraryCacheForTests(): void {
  cachedMemoryIndex = null;
  memoryIndexInflight = null;
}

export function useMemoryLibrary() {
  const [library, setLibrary] = useState<MemoryLibraryIndex | null>(() => cachedMemoryIndex);
  const [loading, setLoading] = useState(() => cachedMemoryIndex === null);
  const mountedRef = useRef(true);
  const generationRef = useRef(0);

  const applyIndex = useCallback((data: MemoryLibraryIndex) => {
    if (!mountedRef.current) return;
    setLibrary(data);
    setLoading(false);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    markMemoryPerf('memory_mount');

    if (cachedMemoryIndex) {
      markMemoryPerf('memory_cache_hit');
      setLibrary(cachedMemoryIndex);
      setLoading(false);

      const gen = ++generationRef.current;
      markMemoryPerf('memory_revalidate_start');
      loadMemoryIndex()
        .then((data) => {
          if (!mountedRef.current || generationRef.current !== gen) return;
          markMemoryPerf('memory_revalidate_ready');
          applyIndex(data);
        })
        .catch(() => {
          /* keep stale cache on revalidate failure */
        });
      return () => {
        mountedRef.current = false;
      };
    }

    const gen = ++generationRef.current;
    setLoading(true);
    loadMemoryIndex()
      .then((data) => {
        if (!mountedRef.current || generationRef.current !== gen) return;
        applyIndex(data);
      })
      .catch(() => {
        if (!mountedRef.current || generationRef.current !== gen) return;
        setLoading(false);
      });

    return () => {
      mountedRef.current = false;
    };
  }, [applyIndex]);

  return { library, loading };
}
