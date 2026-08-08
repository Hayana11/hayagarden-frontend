/** Chat cold-start limits — INITIAL vs AUTHORITATIVE vs legacy warm-up. */
export const CHAT_LEGACY_INITIAL_LIMIT = 24;
export const CHAT_AUTHORITATIVE_LIMIT = 80;
export const CHAT_LEGACY_WARMUP_LIMIT = 56;

export type ChatColdStartMark =
  | 'chat_mount'
  | 'initial_history_start'
  | 'initial_history_ready'
  | 'first_history_paint_scheduled'
  | 'background_warm_start'
  | 'background_warm_ready'
  | 'catalog_start'
  | 'catalog_ready';

/** DEV-only performance marks — no console spam in production. */
export function markChatColdStart(phase: ChatColdStartMark): void {
  if (!import.meta.env.DEV) return;
  if (typeof performance === 'undefined' || typeof performance.mark !== 'function') return;
  performance.mark(`chat_cold_start:${phase}`);
}

/** Chrome78-safe: rAF then setTimeout(0). */
export function scheduleAfterFirstPaint(fn: () => void): void {
  const run = () => {
    setTimeout(fn, 0);
  };
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(run);
  } else {
    run();
  }
}

/** Prepend older page with id dedupe; preserves ascending id order. */
export function mergeOlderChatMessages<T extends { id: number }>(
  current: T[],
  older: T[],
): T[] {
  if (!older.length) return current;
  const known = new Set(current.map((m) => m.id));
  const fresh = older.filter((m) => !known.has(m.id));
  if (!fresh.length) return current;
  const curEarliest = current[0]?.id;
  if (curEarliest !== undefined && fresh.some((m) => m.id >= curEarliest)) return current;
  return [...fresh, ...current];
}

// ── Cold-start race coordinator (pure, unit-testable) ──

export type ColdStartRaceState = {
  historyGen: number;
  warmUpGen: number;
  warmUpSatisfied: boolean;
  deferredInitDone: boolean;
};

export function createColdStartRaceState(): ColdStartRaceState {
  return { historyGen: 0, warmUpGen: 0, warmUpSatisfied: false, deferredInitDone: false };
}

export function bumpHistoryGenState(state: ColdStartRaceState): number {
  state.historyGen += 1;
  return state.historyGen;
}

export function isHistoryGenCurrent(state: ColdStartRaceState, requestGen: number): boolean {
  return requestGen === state.historyGen;
}

/** Invalidate only in-flight warm-up — does not clear satisfied coverage. */
export function cancelInFlightWarmUpState(state: ColdStartRaceState): number {
  state.warmUpGen += 1;
  return state.warmUpGen;
}

export function markWarmUpSatisfiedState(state: ColdStartRaceState): void {
  state.warmUpSatisfied = true;
}

export function hasAuthoritativeCoverage(loadedCount: number): boolean {
  return loadedCount >= CHAT_AUTHORITATIVE_LIMIT;
}

export function needsLegacyWarmUp(
  legacyCompat: boolean,
  state: ColdStartRaceState,
  loadedCount: number,
): boolean {
  if (!legacyCompat) return false;
  if (state.warmUpSatisfied) return false;
  if (hasAuthoritativeCoverage(loadedCount)) return false;
  return loadedCount > 0;
}

/** Run deferred init at most once — only after usable history is applied. */
export function tryConsumeDeferredInit(state: ColdStartRaceState): boolean {
  if (state.deferredInitDone) return false;
  state.deferredInitDone = true;
  return true;
}

export function shouldMarkWarmUpSatisfiedAfterPage(
  loadedCountAfterMerge: number,
  hasMoreBefore: boolean,
): boolean {
  if (hasAuthoritativeCoverage(loadedCountAfterMerge)) return true;
  return !hasMoreBefore;
}

export function onAuthoritativeHistorySuccess(
  state: ColdStartRaceState,
  loadedCount: number,
): void {
  cancelInFlightWarmUpState(state);
  if (hasAuthoritativeCoverage(loadedCount)) {
    markWarmUpSatisfiedState(state);
  }
}
