import { useEffect, useState } from 'react';
import { fetchMemoryLibrary } from '../lib/api';
import type { MemoryLibrary } from '../types';

export function useMemoryLibrary(): MemoryLibrary | null {
  const [library, setLibrary] = useState<MemoryLibrary | null>(null);
  useEffect(() => {
    fetchMemoryLibrary().then(setLibrary);
  }, []);
  return library;
}
