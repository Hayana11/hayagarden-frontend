import { useMemo, useState } from 'react';

/** Month navigation clamped so you can never page past the current month. */
export function useMonthOffset(year: number, month: number) {
  const [offset, setOffset] = useState(0);
  const base = useMemo(() => new Date(year, month + offset, 1), [year, month, offset]);
  const isCurrent = offset === 0;
  const prev = () => setOffset((o) => o - 1);
  const next = () => setOffset((o) => Math.min(0, o + 1));
  return { base, isCurrent, offset, prev, next };
}
