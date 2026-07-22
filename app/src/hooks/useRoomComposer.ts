import { useMemo, useState } from 'react';

export type ComposerTarget = 'all' | 'cc' | 'codex';

export function useRoomComposer() {
  const [content, setContent] = useState('');
  const [target, setTarget] = useState<ComposerTarget>('all');
  const targets = useMemo<Array<'cc' | 'codex'>>(
    () => target === 'all' ? ['cc', 'codex'] : [target],
    [target],
  );

  const cycleTarget = () => {
    setTarget((current) => current === 'all' ? 'cc' : current === 'cc' ? 'codex' : 'all');
  };

  return { content, setContent, target, targets, cycleTarget };
}
