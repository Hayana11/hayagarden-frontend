/** Per-date serial save queue: same-day edits flush in order by generation. */
import type { PeriodDayRecord } from '../types';

type QueueState = {
  tail: Promise<SaveResult>;
  snapshots: Map<number, PeriodDayRecord>;
};

export type SaveResult = { ok: boolean; generation: number };

const queues = new Map<string, QueueState>();
const editGenerations = new Map<string, number>();

export function bumpEditGeneration(date: string): number {
  const next = (editGenerations.get(date) || 0) + 1;
  editGenerations.set(date, next);
  return next;
}

export function getEditGeneration(date: string): number {
  return editGenerations.get(date) || 0;
}

/** Only reload from server when no newer local edit happened after this save began. */
export function shouldReloadAfterFailedSave(date: string, saveGeneration: number): boolean {
  return saveGeneration === getEditGeneration(date);
}

function getQueue(date: string): QueueState {
  let q = queues.get(date);
  if (!q) {
    q = { tail: Promise.resolve({ ok: true, generation: 0 }), snapshots: new Map() };
    queues.set(date, q);
  }
  return q;
}

function maybeCleanupQueue(date: string) {
  const q = queues.get(date);
  if (q && q.snapshots.size === 0) {
    queues.delete(date);
  }
}

async function flushGeneration(
  date: string,
  saveGeneration: number,
  saveFn: (date: string, record: PeriodDayRecord) => Promise<boolean>,
): Promise<boolean> {
  const q = getQueue(date);
  const snapshot = q.snapshots.get(saveGeneration);
  if (!snapshot) {
    maybeCleanupQueue(date);
    return false;
  }
  q.snapshots.delete(saveGeneration);
  let lastOk = false;
  try {
    lastOk = await saveFn(date, snapshot);
  } catch {
    lastOk = false;
  }
  maybeCleanupQueue(date);
  return lastOk;
}

/** Enqueue a full day record; resolves when this generation's flush turn completes. */
export function schedulePeriodDaySave(
  date: string,
  record: PeriodDayRecord,
  saveGeneration: number,
  saveFn: (date: string, record: PeriodDayRecord) => Promise<boolean>,
): Promise<SaveResult> {
  const q = getQueue(date);
  q.snapshots.set(saveGeneration, { ...record });
  const promise = q.tail
    .catch(() => ({ ok: false, generation: saveGeneration }))
    .then(async (): Promise<SaveResult> => {
      const ok = await flushGeneration(date, saveGeneration, saveFn);
      return { ok, generation: saveGeneration };
    });
  q.tail = promise;
  return promise;
}

/** Test helper — number of dates with active queue state. */
export function getActiveQueueCountForTests(): number {
  return queues.size;
}

/** Test helper — reset module state between cases. */
export function resetPeriodSaveQueues(): void {
  queues.clear();
  editGenerations.clear();
}
