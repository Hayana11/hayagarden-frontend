// Bilingual calls: one remembered choice of gloss or original, shared by the call page
// and the call transcript sheet.
import { useCallback, useState } from 'react';
import { readSubtitleMode, writeSubtitleMode, type SubtitleMode } from './voiceCall';

export function useSubtitleMode(): [SubtitleMode, () => void] {
  const [mode, setMode] = useState<SubtitleMode>(readSubtitleMode);
  const toggle = useCallback(() => {
    setMode((current) => {
      const next: SubtitleMode = current === 'gloss' ? 'original' : 'gloss';
      writeSubtitleMode(next);
      return next;
    });
  }, []);
  return [mode, toggle];
}
