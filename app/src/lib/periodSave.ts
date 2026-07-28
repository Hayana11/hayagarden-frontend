/** Per-date serial save queue: same-day edits merge and flush in order. */
import type { PeriodDayRecord } from '../types';

type QueueState = {
  tail: Promise<boolean>;
  pending: PeriodDayRecord | null;
};

const queues = new Map<string, QueueState>();

function getQueue(date: string): QueueState {
  let q = queues.get(date);
  if (!q) {
    q = { tail: Promise.resolve(true), pending: null };
    queues.set(date, q);
  }
  return q;
}

async function flushDate(
  date: string,
  saveFn: (date: string, record: PeriodDayRecord) => Promise<boolean>,
): Promise<boolean> {
  const q = getQueue(date);
  let lastOk = true;
  while (q.pending) {
    const snapshot = q.pending;
    q.pending = null;
    lastOk = await saveFn(date, snapshot);
    if (!lastOk) break;
  }
  return lastOk;
}

/** Merge patch and enqueue; returns when this date's latest snapshot has been saved. */
export function schedulePeriodDaySave(
  date: string,
  patch: PeriodDayRecord,
  saveFn: (date: string, record: PeriodDayRecord) => Promise<boolean>,
): Promise<boolean> {
  const q = getQueue(date);
  q.pending = { ...(q.pending || {}), ...patch };
  q.tail = q.tail.then(() => flushDate(date, saveFn));
  return q.tail;
}

/** Test helper — reset module state between cases. */
export function resetPeriodSaveQueues(): void {
  queues.clear();
}
