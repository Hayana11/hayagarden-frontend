import { useEffect, useMemo, useRef, useState } from 'react';

/**
 * Task timer state — presentation-layer only.
 *
 * NOTE (real-data gap, reported and intentionally not worked around):
 * The current frontend has no `task.timer.start` runtime state surface.
 * `ChatToolCall` (lib/chat.ts) only carries generic tool-call fields
 * (name/id/args/tool_input/result/success/running) — there is no
 * started_at/deadline/task_id/title flowing from the SSE stream into
 * ChatScreen state, and lib/api.ts has no timer/task/capability
 * completion endpoint. Until those real fields exist, this module is
 * driven either by an explicit `TaskTimerSnapshot` passed in by a future
 * caller, or by the dev-only fixture below.
 */

export type TaskTimerMode = 'countdown' | 'elapsed';
export type TaskTimerPhase = 'active' | 'overtime' | 'completed';

export interface TaskTimerSnapshot {
  taskId: string;
  /** Only render when a real contract actually supplies a title. */
  title?: string;
  mode: TaskTimerMode;
  /** Date.now() base the timer counts from. */
  startedAtMs: number;
  /** Only meaningful when mode === 'countdown'. */
  countdownSeconds?: number;
  completed?: boolean;
}

export interface TaskTimerDisplay {
  phase: TaskTimerPhase;
  /** "12:48" / "+00:37" / "00:07:14" */
  primaryLabel: string;
  statusText: string;
  /** 0..1 for the countdown progress hairline; null when not applicable. */
  progressRatio: number | null;
}

function pad2(n: number): string {
  return String(Math.max(0, Math.floor(n))).padStart(2, '0');
}

function formatMMSS(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${pad2(m)}:${pad2(sec)}`;
}

function formatHHMMSS(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h > 0 ? `${pad2(h)}:${pad2(m)}:${pad2(sec)}` : `${pad2(m)}:${pad2(sec)}`;
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
      progressRatio: snapshot.countdownSeconds > 0 ? Math.min(1, Math.max(0, remaining / snapshot.countdownSeconds)) : null,
    };
  }

  return {
    phase: 'overtime',
    primaryLabel: `+${formatMMSS(-remaining)}`,
    statusText: '已超时',
    progressRatio: null,
  };
}

/** Ticks once per second; pauses while the tab is hidden and catches up on visibility return. */
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

export function isTaskTimerFixtureEnabled(): boolean {
  if (!import.meta.env.DEV) return false;
  try {
    if (new URLSearchParams(window.location.search).get('timerFixture') === '1') return true;
  } catch {
    // ignore — window/location unavailable
  }
  return import.meta.env.VITE_TASK_TIMER_FIXTURE === '1';
}

/**
 * Dev-only fixture: cycles a demo task through countdown → near-zero →
 * overtime → completed so the four TaskTimerCard phases can be reviewed
 * without a real task.timer.start feed. No-op (returns null) when disabled.
 */
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
        const completedAt = cycleStartRef.current + Math.floor(elapsed / CYCLE_SECONDS) * CYCLE_SECONDS * 1000 + FIXTURE_COUNTDOWN_SECONDS * 1000;
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
      const segmentStart = cycleStartRef.current + Math.floor(elapsed / CYCLE_SECONDS) * CYCLE_SECONDS * 1000;
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
