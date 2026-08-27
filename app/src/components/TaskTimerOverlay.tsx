import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  fetchPendingTaskTimers,
  markTaskTimerCanceled,
  markTaskTimerDone,
  markTaskTimerStarted,
  type TaskTimerPendingCommand,
} from '../lib/api';

export const TASK_TIMER_POLL_MS = 4000;
export const TASK_TIMER_CANCEL_HOLD_MS = 1100;

export type TaskTimerView = {
  state: 'unstarted' | 'countdown' | 'countup' | 'overtime';
  label: string;
  elapsedMs: number;
  remainingMs: number | null;
};

export function selectActiveTask(tasks: TaskTimerPendingCommand[]): TaskTimerPendingCommand | null {
  return tasks.length > 0 ? tasks[0] : null;
}

export function formatTaskTimerClock(ms: number): string {
  const totalSeconds = Math.max(0, Math.round(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

export function getTaskTimerView(task: TaskTimerPendingCommand, nowMs: number): TaskTimerView {
  if (task.started_at == null) {
    return { state: 'unstarted', label: '开始未确认', elapsedMs: 0, remainingMs: null };
  }

  const elapsedMs = Math.max(0, nowMs - task.started_at);
  const countdownMs = Number(task.countdown_seconds || 0) * 1000;
  if (countdownMs > 0) {
    const remainingMs = countdownMs - elapsedMs;
    if (remainingMs >= 0) {
      return { state: 'countdown', label: formatTaskTimerClock(remainingMs), elapsedMs, remainingMs };
    }
    return { state: 'overtime', label: `+${formatTaskTimerClock(-remainingMs)}`, elapsedMs, remainingMs };
  }

  return { state: 'countup', label: formatTaskTimerClock(elapsedMs), elapsedMs, remainingMs: null };
}

function visibleNow(): boolean {
  return typeof document === 'undefined' || document.visibilityState === 'visible';
}

function errorText(error: unknown): string {
  return error instanceof Error && error.message ? error.message : '网络暂时不可用';
}

export function TaskTimerOverlay() {
  const [tasks, setTasks] = useState<TaskTimerPendingCommand[]>([]);
  const [visible, setVisible] = useState(visibleNow);
  const [clockNow, setClockNow] = useState(() => Date.now());
  const [expanded, setExpanded] = useState(true);
  const [startingId, setStartingId] = useState<number | null>(null);
  const [startError, setStartError] = useState(false);
  const [mutationBusy, setMutationBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [visibilityRefresh, setVisibilityRefresh] = useState(0);

  const mountedRef = useRef(true);
  const pendingRequestRef = useRef(0);
  const mutationEpochRef = useRef(0);
  const mutationBusyRef = useRef(false);
  const startingIdRef = useRef<number | null>(null);
  const failedStartIdRef = useRef<number | null>(null);
  const cancelHoldRef = useRef<number | null>(null);

  useEffect(() => () => {
    mountedRef.current = false;
    if (cancelHoldRef.current !== null) {
      window.clearTimeout(cancelHoldRef.current);
      cancelHoldRef.current = null;
    }
  }, []);

  const refreshPending = useCallback(async (reason: 'visible' | 'poll' | 'mutation') => {
    const requestId = ++pendingRequestRef.current;
    const epoch = mutationEpochRef.current;
    try {
      const response = await fetchPendingTaskTimers();
      if (!mountedRef.current || requestId !== pendingRequestRef.current || epoch !== mutationEpochRef.current) {
        return false;
      }
      setTasks(Array.isArray(response.commands) ? response.commands : []);
      setError(null);
      if (reason === 'visible') {
        failedStartIdRef.current = null;
        setStartError(false);
      }
      return true;
    } catch (requestError) {
      if (mountedRef.current && requestId === pendingRequestRef.current && epoch === mutationEpochRef.current) {
        setError(errorText(requestError));
      }
      throw requestError;
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
    void refreshPending('visible').catch(() => undefined);
    const intervalId = window.setInterval(() => {
      void refreshPending('poll').catch(() => undefined);
    }, TASK_TIMER_POLL_MS);
    return () => window.clearInterval(intervalId);
  }, [visible, visibilityRefresh, refreshPending]);

  const activeTask = selectActiveTask(tasks);
  const activeId = activeTask?.id ?? null;

  useEffect(() => {
    if (!visible || !activeTask || activeTask.started_at != null) return;
    if (startingIdRef.current === activeTask.id || failedStartIdRef.current === activeTask.id) return;

    const id = activeTask.id;
    startingIdRef.current = id;
    setStartingId(id);
    setStartError(false);

    markTaskTimerStarted(id)
      .then(() => {
        if (!mountedRef.current || startingIdRef.current !== id) return;
        // Never invent a local start time: persisted started_at is authoritative.
        void refreshPending('mutation').catch(() => undefined);
      })
      .catch(() => {
        if (!mountedRef.current) return;
        failedStartIdRef.current = id;
        setStartError(true);
      })
      .finally(() => {
        if (startingIdRef.current === id) {
          startingIdRef.current = null;
          if (mountedRef.current) setStartingId(null);
        }
      });
  }, [activeTask, visible, visibilityRefresh, refreshPending]);

  useEffect(() => {
    if (!visible || !activeTask?.started_at) return;
    setClockNow(Date.now());
    const tickId = window.setInterval(() => setClockNow(Date.now()), 250);
    return () => window.clearInterval(tickId);
  }, [visible, activeTask?.id, activeTask?.started_at]);

  const activeView = useMemo(
    () => (activeTask ? getTaskTimerView(activeTask, clockNow) : null),
    [activeTask, clockNow],
  );

  const reconcileAfterMutation = useCallback(async (epoch: number, id: number) => {
    const requestId = ++pendingRequestRef.current;
    const response = await fetchPendingTaskTimers();
    if (!mountedRef.current || requestId !== pendingRequestRef.current || epoch !== mutationEpochRef.current) {
      return null;
    }
    const nextTasks = Array.isArray(response.commands) ? response.commands : [];
    setTasks(nextTasks);
    setError(null);
    return nextTasks.some((task) => task.id === id);
  }, []);

  const runMutation = useCallback(async (kind: 'done' | 'cancel') => {
    const task = selectActiveTask(tasks);
    if (!task || mutationBusyRef.current) return;

    mutationBusyRef.current = true;
    setMutationBusy(true);
    const id = task.id;
    const epoch = ++mutationEpochRef.current;
    // Invalidate every older poll so it cannot resurrect this task.
    pendingRequestRef.current += 1;

    try {
      if (kind === 'done') await markTaskTimerDone(id);
      else await markTaskTimerCanceled(id);
      if (mountedRef.current) setTasks((current) => current.filter((item) => item.id !== id));
      await reconcileAfterMutation(epoch, id);
    } catch (mutationError) {
      // The mutation result is uncertain: reconcile once, never auto-POST again.
      try {
        const stillPending = await reconcileAfterMutation(epoch, id);
        if (mountedRef.current && stillPending === true) setError(errorText(mutationError));
      } catch (reconcileError) {
        if (mountedRef.current) setError(errorText(reconcileError));
      }
    } finally {
      mutationBusyRef.current = false;
      if (mountedRef.current) setMutationBusy(false);
    }
  }, [tasks, reconcileAfterMutation]);

  const clearCancelHold = useCallback(() => {
    if (cancelHoldRef.current !== null) {
      window.clearTimeout(cancelHoldRef.current);
      cancelHoldRef.current = null;
    }
  }, []);

  const startCancelHold = useCallback((event: React.PointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    clearCancelHold();
    if (!activeTask || mutationBusyRef.current) return;
    cancelHoldRef.current = window.setTimeout(() => {
      cancelHoldRef.current = null;
      void runMutation('cancel');
    }, TASK_TIMER_CANCEL_HOLD_MS);
  }, [activeTask, clearCancelHold, runMutation]);

  if (!activeTask) return null;

  return (
    <aside
      className={`task-timer-overlay task-timer-overlay--${activeView?.state ?? 'unstarted'}`}
      data-task-timer-state={activeView?.state ?? 'unstarted'}
      style={{
        position: 'fixed',
        right: 10,
        bottom: 'calc(92px + env(safe-area-inset-bottom, 0px))',
        zIndex: 60,
        width: 'min(212px, calc(100vw - 20px))',
        pointerEvents: 'auto',
      }}
      aria-label="行动任务计时器"
    >
      <div className={`task-timer-card task-timer-card--${expanded ? 'expanded' : 'collapsed'}`}>
        <div className="task-timer-header">
          <span className="task-timer-title" title={activeTask.title}>{activeTask.title}</span>
          {tasks.length > 1 ? <span className="task-timer-queue">+{tasks.length - 1}</span> : null}
          <button
            type="button"
            className="task-timer-toggle"
            aria-expanded={expanded}
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? '收起' : '展开'}
          </button>
        </div>
        {expanded ? (
          <>
            <div className="task-timer-status">
              {startingId === activeTask.id ? '开始确认中' : startError ? '开始未确认' : activeView?.state === 'overtime' ? '已超时' : '进行中'}
            </div>
            <div className="task-timer-clock">{activeView?.label ?? '开始未确认'}</div>
            <div className="task-timer-actions">
              <button type="button" className="task-timer-done" disabled={mutationBusy || startingId === activeTask.id} onClick={() => void runMutation('done')}>完成</button>
              <button
                type="button"
                className="task-timer-cancel"
                disabled={mutationBusy || startingId === activeTask.id}
                onPointerDown={startCancelHold}
                onPointerUp={clearCancelHold}
                onPointerLeave={clearCancelHold}
                onPointerCancel={clearCancelHold}
              >
                长按取消
              </button>
            </div>
            {error ? <div className="task-timer-error" role="status">{error}</div> : null}
          </>
        ) : null}
      </div>
    </aside>
  );
}
