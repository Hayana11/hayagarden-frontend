import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  fetchPendingTaskTimers,
  markTaskTimerDone,
  markTaskTimerStarted,
  type TaskTimerPendingCommand,
} from './api';

export const TASK_TIMER_POLL_MS = 4000;

export type TaskTimerMode = 'countdown' | 'elapsed';
export type TaskTimerPhase = 'active' | 'overtime' | 'completed';

export interface TaskTimerSnapshot {
  taskId: string;
  title?: string;
  mode: TaskTimerMode;
  startedAtMs: number;
  countdownSeconds?: number;
  completed?: boolean;
}

export interface TaskTimerDisplay {
  phase: TaskTimerPhase;
  primaryLabel: string;
  statusText: string;
  progressRatio: number | null;
}

function pad2(n: number): string {
  return String(Math.max(0, Math.floor(n))).padStart(2, '0');
}

function formatMMSS(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return String(pad2(m)) + ':' + String(pad2(sec));
}

function formatHHMMSS(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h > 0 ? pad2(h) + ':' + pad2(m) + ':' + pad2(sec) : pad2(m) + ':' + pad2(sec);
}

export function computeTaskTimerDisplay(snapshot: TaskTimerSnapshot, nowMs: number): TaskTimerDisplay {
  const elapsedSec = Math.max(0, (nowMs - snapshot.startedAtMs) / 1000);

  if (snapshot.completed) {
    const label =
      snapshot.mode === 'countdown'
        ? formatMMSS(Math.max(0, (snapshot.countdownSeconds ?? 0) - elapsedSec))
        : formatHHMMSS(elapsedSec);
    return { phase: 'completed', primaryLabel: label, statusText: '已完成', progressRatio: null };
  }

  if (snapshot.mode === 'elapsed' || snapshot.countdownSeconds == null) {
    return {
      phase: 'active',
      primaryLabel: formatHHMMSS(elapsedSec),
      statusText: '正在计时',
      progressRatio: null,
    };
  }

  const remaining = snapshot.countdownSeconds - elapsedSec;
  if (remaining >= 0) {
    return {
      phase: 'active',
      primaryLabel: formatMMSS(remaining),
      statusText: '正在计时',
      progressRatio: snapshot.countdownSeconds > 0
        ? Math.min(1, Math.max(0, remaining / snapshot.countdownSeconds))
        : null,
    };
  }

  return {
    phase: 'overtime',
    primaryLabel: '+' + formatMMSS(-remaining),
    statusText: '已超时',
    progressRatio: null,
  };
}

export function selectActiveTask(tasks: TaskTimerPendingCommand[]): TaskTimerPendingCommand | null {
  return tasks.length > 0 ? tasks[0] : null;
}

export function pendingTaskToSnapshot(task: TaskTimerPendingCommand | null): TaskTimerSnapshot | null {
  if (!task || task.started_at == null) return null;

  const startedAtMs = Number(task.started_at);
  if (!Number.isFinite(startedAtMs)) return null;

  const countdownSeconds = task.countdown_seconds == null ? null : Number(task.countdown_seconds);
  const hasCountdown = countdownSeconds != null && Number.isFinite(countdownSeconds) && countdownSeconds > 0;

  return {
    taskId: String(task.id),
    title: task.title,
    mode: hasCountdown ? 'countdown' : 'elapsed',
    startedAtMs,
    countdownSeconds: hasCountdown ? countdownSeconds : undefined,
  };
}

/** Ticks from Date.now(); it does not decrement a local counter. */
export function useTaskTimerClock(snapshot: TaskTimerSnapshot | null, intervalMs = 1000): TaskTimerDisplay | null {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!snapshot) return;
    let id: ReturnType<typeof setInterval> | null = null;

    const start = () => {
      if (id != null) return;
      setNow(Date.now());
      id = setInterval(() => setNow(Date.now()), intervalMs);
    };
    const stop = () => {
      if (id != null) {
        clearInterval(id);
        id = null;
      }
    };
    const onVisibility = () => {
      if (document.hidden) stop();
      else start();
    };

    if (document.hidden) setNow(Date.now());
    else start();

    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [snapshot, intervalMs]);

  return useMemo(() => (snapshot ? computeTaskTimerDisplay(snapshot, now) : null), [snapshot, now]);
}

function visibleNow(): boolean {
  return typeof document === 'undefined' || document.visibilityState === 'visible';
}

export interface TaskTimerController {
  snapshot: TaskTimerSnapshot | null;
  onComplete: () => void;
  completing: boolean;
}

export function useTaskTimerController(fixtureEnabled: boolean): TaskTimerController {
  const [tasks, setTasks] = useState<TaskTimerPendingCommand[]>([]);
  const [visible, setVisible] = useState(visibleNow);
  const [visibilityRefresh, setVisibilityRefresh] = useState(0);
  const [completing, setCompleting] = useState(false);

  const mountedRef = useRef(false);
  const pendingRequestRef = useRef(0);
  const mutationEpochRef = useRef(0);
  const mutationPhaseRef = useRef<'idle' | 'reconciling'>('idle');
  const mutationBusyRef = useRef(false);
  const startInFlightRef = useRef<number | null>(null);
  const failedStartIdRef = useRef<number | null>(null);
  const startedPostSucceededRef = useRef<number | null>(null);
  const reconcileRetryTimerRef = useRef<number | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (reconcileRetryTimerRef.current !== null) {
        window.clearTimeout(reconcileRetryTimerRef.current);
        reconcileRetryTimerRef.current = null;
      }
    };
  }, []);

  const refreshPending = useCallback(async (reason: 'visible' | 'poll' | 'start') => {
    if (mutationPhaseRef.current !== 'idle') return false;

    const requestId = ++pendingRequestRef.current;
    const epoch = mutationEpochRef.current;
    try {
      const response = await fetchPendingTaskTimers();
      if (!mountedRef.current || requestId !== pendingRequestRef.current || epoch !== mutationEpochRef.current) {
        return false;
      }
      const nextTasks = Array.isArray(response.commands) ? response.commands : [];
      setTasks(nextTasks);
      if (reason === 'visible' || reason === 'poll') {
        failedStartIdRef.current = null;
      }
      return true;
    } catch {
      return false;
    }
  }, []);

  useEffect(() => {
    const onVisibilityChange = () => {
      const nextVisible = visibleNow();
      setVisible(nextVisible);
      if (nextVisible) setVisibilityRefresh((value) => value + 1);
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => document.removeEventListener('visibilitychange', onVisibilityChange);
  }, []);

  useEffect(() => {
    if (!visible) return;
    void refreshPending('visible');
    const intervalId = window.setInterval(() => {
      void refreshPending('poll');
    }, TASK_TIMER_POLL_MS);
    return () => window.clearInterval(intervalId);
  }, [visible, visibilityRefresh, refreshPending]);

  const activeTask = selectActiveTask(tasks);
  const realSnapshot = useMemo(() => pendingTaskToSnapshot(activeTask), [activeTask]);

  useEffect(() => {
    if (!visible || !activeTask || activeTask.started_at != null) return;

    const id = activeTask.id;
    if (
      startInFlightRef.current === id
      || failedStartIdRef.current === id
      || startedPostSucceededRef.current === id
    ) {
      return;
    }

    startInFlightRef.current = id;
    markTaskTimerStarted(id)
      .then((response) => {
        if (!response.ok) throw new Error('task timer start rejected');
        if (!mountedRef.current || startInFlightRef.current !== id) return;
        startedPostSucceededRef.current = id;
        // The server-persisted started_at is the only clock authority.
        void refreshPending('start');
      })
      .catch(() => {
        if (mountedRef.current) failedStartIdRef.current = id;
      })
      .finally(() => {
        if (startInFlightRef.current === id) startInFlightRef.current = null;
      });
  }, [activeTask, visible, visibilityRefresh, refreshPending]);

  const reconcileAfterMutation = useCallback(async (epoch: number, id: number) => {
    while (mountedRef.current && epoch === mutationEpochRef.current) {
      try {
        const response = await fetchPendingTaskTimers();
        if (!mountedRef.current || epoch !== mutationEpochRef.current) return false;
        const nextTasks = Array.isArray(response.commands) ? response.commands : [];
        setTasks(nextTasks);
        return true;
      } catch {
        if (!mountedRef.current || epoch !== mutationEpochRef.current) return false;
        await new Promise<void>((resolve) => {
          reconcileRetryTimerRef.current = window.setTimeout(() => {
            reconcileRetryTimerRef.current = null;
            resolve();
          }, TASK_TIMER_POLL_MS);
        });
      }
    }
    return false;
  }, []);

  const activeTaskForMutation = activeTask;
  const runComplete = useCallback(async () => {
    if (!activeTaskForMutation || mutationBusyRef.current || mutationPhaseRef.current !== 'idle') return;

    mutationBusyRef.current = true;
    mutationPhaseRef.current = 'reconciling';
    setCompleting(true);
    const id = activeTaskForMutation.id;
    const epoch = ++mutationEpochRef.current;
    pendingRequestRef.current += 1;

    try {
      try {
        const response = await markTaskTimerDone(id);
        if (!response.ok) throw new Error('task timer done rejected');
      } catch {
        // The POST outcome is intentionally not retried; only pending GET reconciliation follows.
      }

      await reconcileAfterMutation(epoch, id);
      if (!mountedRef.current || epoch !== mutationEpochRef.current) return;
      mutationPhaseRef.current = 'idle';
      mutationBusyRef.current = false;
      setCompleting(false);
    } finally {
      // A component unmount or an endless reconciliation leaves the mutation locked.
    }
  }, [activeTaskForMutation, reconcileAfterMutation]);

  const fixtureSnapshot = useTaskTimerFixtureSnapshot(fixtureEnabled);
  const snapshot = realSnapshot || (!activeTask ? fixtureSnapshot : null);

  return { snapshot, onComplete: runComplete, completing };
}

export function isTaskTimerFixtureEnabled(): boolean {
  if (!import.meta.env.DEV) return false;
  try {
    if (new URLSearchParams(window.location.search).get('timerFixture') === '1') return true;
  } catch {
    // ignore — window/location unavailable
  }
  return import.meta.env.VITE_TASK_TIMER_FIXTURE === '1';
}

export function useTaskTimerFixtureSnapshot(enabled: boolean): TaskTimerSnapshot | null {
  const cycleStartRef = useRef(Date.now());
  const [snapshot, setSnapshot] = useState<TaskTimerSnapshot | null>(null);

  useEffect(() => {
    if (!enabled) {
      setSnapshot(null);
      return;
    }

    const FIXTURE_COUNTDOWN_SECONDS = 8;
    const OVERTIME_HOLD_SECONDS = 6;
    const COMPLETED_HOLD_SECONDS = 4;
    const CYCLE_SECONDS = FIXTURE_COUNTDOWN_SECONDS + OVERTIME_HOLD_SECONDS + COMPLETED_HOLD_SECONDS;

    const tick = () => {
      const elapsed = (Date.now() - cycleStartRef.current) / 1000;
      const phaseElapsed = elapsed % CYCLE_SECONDS;
      if (phaseElapsed >= CYCLE_SECONDS - COMPLETED_HOLD_SECONDS) {
        const completedAt = cycleStartRef.current
          + Math.floor(elapsed / CYCLE_SECONDS) * CYCLE_SECONDS * 1000
          + FIXTURE_COUNTDOWN_SECONDS * 1000;
        setSnapshot({
          taskId: 'fixture-task',
          title: '整理今天的对话摘要',
          mode: 'countdown',
          startedAtMs: completedAt - FIXTURE_COUNTDOWN_SECONDS * 1000,
          countdownSeconds: FIXTURE_COUNTDOWN_SECONDS,
          completed: true,
        });
        return;
      }
      const segmentStart = cycleStartRef.current
        + Math.floor(elapsed / CYCLE_SECONDS) * CYCLE_SECONDS * 1000;
      setSnapshot({
        taskId: 'fixture-task',
        title: '整理今天的对话摘要',
        mode: 'countdown',
        startedAtMs: segmentStart,
        countdownSeconds: FIXTURE_COUNTDOWN_SECONDS,
        completed: false,
      });
    };

    tick();
    const id = setInterval(tick, 500);
    return () => clearInterval(id);
  }, [enabled]);

  return snapshot;
}
