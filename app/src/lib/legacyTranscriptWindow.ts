/** Bounded DOM render window for legacy native Chat (msgs state stays full). */

export const LEGACY_WINDOW_SIZE = 20;
export const LEGACY_WINDOW_STEP = 12;

export interface TranscriptWindow {
  /** Inclusive start index into loaded msgs. */
  start: number;
  /** Exclusive end index into loaded msgs. */
  end: number;
}

export function windowSize(win: TranscriptWindow): number {
  return Math.max(0, win.end - win.start);
}

export function clampTranscriptWindow(start: number, end: number, total: number): TranscriptWindow {
  if (total <= 0) return { start: 0, end: 0 };
  if (total <= LEGACY_WINDOW_SIZE) return { start: 0, end: total };

  let s = Math.max(0, Math.min(start, total - 1));
  let e = Math.max(s + 1, Math.min(end, total));

  if (e - s > LEGACY_WINDOW_SIZE) {
    e = s + LEGACY_WINDOW_SIZE;
  }
  if (e - s < LEGACY_WINDOW_SIZE) {
    s = Math.max(0, e - LEGACY_WINDOW_SIZE);
    e = Math.min(total, s + LEGACY_WINDOW_SIZE);
  }
  return { start: s, end: e };
}

/** Initial / pin-to-latest window: newest SIZE messages. */
export function latestTranscriptWindow(total: number): TranscriptWindow {
  if (total <= 0) return { start: 0, end: 0 };
  if (total <= LEGACY_WINDOW_SIZE) return { start: 0, end: total };
  return { start: total - LEGACY_WINDOW_SIZE, end: total };
}

export function isTranscriptWindowAtLatest(win: TranscriptWindow, total: number): boolean {
  return total <= 0 || win.end >= total;
}

/** Move older by STEP; keep ~SIZE mounted with overlap. */
export function shiftTranscriptWindowOlder(win: TranscriptWindow, total: number): TranscriptWindow {
  if (total <= 0) return { start: 0, end: 0 };
  if (win.start <= 0) {
    return clampTranscriptWindow(0, Math.min(LEGACY_WINDOW_SIZE, total), total);
  }
  const start = Math.max(0, win.start - LEGACY_WINDOW_STEP);
  return clampTranscriptWindow(start, start + LEGACY_WINDOW_SIZE, total);
}

/** Move newer by STEP; keep ~SIZE mounted with overlap. */
export function shiftTranscriptWindowNewer(win: TranscriptWindow, total: number): TranscriptWindow {
  if (total <= 0) return { start: 0, end: 0 };
  if (win.end >= total) return latestTranscriptWindow(total);
  const end = Math.min(total, win.end + LEGACY_WINDOW_STEP);
  return clampTranscriptWindow(end - LEGACY_WINDOW_SIZE, end, total);
}

/**
 * After server loadEarlier prepends `prepended` messages:
 * include some newly loaded + some previously earliest-visible (continuity).
 */
export function transcriptWindowAfterPrepend(prepended: number, newTotal: number): TranscriptWindow {
  if (newTotal <= 0) return { start: 0, end: 0 };
  if (newTotal <= LEGACY_WINDOW_SIZE) return { start: 0, end: newTotal };
  const overlap = LEGACY_WINDOW_SIZE - LEGACY_WINDOW_STEP; // keep ~8 of previous earliest
  const start = Math.max(0, prepended - overlap);
  return clampTranscriptWindow(start, start + LEGACY_WINDOW_SIZE, newTotal);
}

/** Window that includes target index with surrounding context. */
export function transcriptWindowAroundIndex(index: number, total: number): TranscriptWindow {
  if (total <= 0) return { start: 0, end: 0 };
  if (total <= LEGACY_WINDOW_SIZE) return { start: 0, end: total };
  const idx = Math.max(0, Math.min(index, total - 1));
  const before = Math.floor((LEGACY_WINDOW_SIZE - 1) / 2);
  let start = Math.max(0, idx - before);
  let end = start + LEGACY_WINDOW_SIZE;
  if (end > total) {
    end = total;
    start = Math.max(0, end - LEGACY_WINDOW_SIZE);
  }
  return { start, end };
}

/**
 * Search jump follow intent: only chase latest when the target itself is the
 * newest message. Geometric "window touches tail" must NOT imply follow-latest.
 */
export function followLatestAfterSearchJump(index: number, total: number): boolean {
  return total > 0 && index === total - 1;
}
