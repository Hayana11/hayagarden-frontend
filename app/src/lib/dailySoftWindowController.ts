/**
 * Formal Soft Window state machine (no React).
 * Injectable client + scheduler for integration tests.
 */
import {
  classifySoftWindowError,
  draftCountFromCurrent,
  flattenRoundMessageIds,
  isCarryoverCount,
  pickLastNRounds,
  softWindowErrorMessage,
  type CarryoverCount,
  type CarryoverMessage,
  type CarryoverRound,
  type DailyContextCurrent,
  type DailySoftWindowClient,
  type SoftWindowUiState,
} from './dailySoftWindow';
import { HttpError } from './http';

export const DEFERRED_RETRY_MS = [750, 2000, 5000] as const;

export type FocusableOpener = {
  focus: () => void;
  isConnected?: boolean;
};

export type SoftWindowControllerSnapshot = {
  uiState: SoftWindowUiState;
  current: DailyContextCurrent | null;
  candidates: CarryoverMessage[];
  rounds: CarryoverRound[];
  draftCount: CarryoverCount;
  drawerOpen: boolean;
  submitting: boolean;
  statusText: string;
  errorDetail: string;
  locked: boolean;
  pickerSuppressed: boolean;
  showPickerCard: boolean;
  showBoundary: boolean;
  boundaryMessageId: number;
  highlightIds: number[];
  /** Test/debug: last opener used for focus return. */
  opener: FocusableOpener | null;
};

export type SoftWindowScheduler = {
  schedule: (fn: () => void, ms: number) => { cancel: () => void };
};

const defaultScheduler: SoftWindowScheduler = {
  schedule: (fn, ms) => {
    const id = setTimeout(fn, ms);
    return { cancel: () => clearTimeout(id) };
  },
};

type Listener = () => void;

export type SoftWindowControllerOptions = {
  client: DailySoftWindowClient;
  /** Formal chat live path. */
  live?: boolean;
  /** Preview playground (eager candidates, lockZero helper). */
  preview?: boolean;
  scheduler?: SoftWindowScheduler;
};

export class DailySoftWindowController {
  readonly client: DailySoftWindowClient;
  readonly live: boolean;
  readonly preview: boolean;
  private readonly scheduler: SoftWindowScheduler;
  private listeners = new Set<Listener>();
  private disposed = false;

  uiState: SoftWindowUiState = 'probing';
  current: DailyContextCurrent | null = null;
  candidates: CarryoverMessage[] = [];
  rounds: CarryoverRound[] = [];
  draftCount: CarryoverCount = 10;
  drawerOpen = false;
  submitting = false;
  errorDetail = '';
  pickerSuppressed = false;
  highlightOverride: number[] | null = null;
  opener: FocusableOpener | null = null;

  private currentAbort: AbortController | null = null;
  private candidatesAbort: AbortController | null = null;
  private deferredCancels: Array<{ cancel: () => void }> = [];
  private deferredAttempt = 0;
  private probeGen = 0;
  private candidatesGen = 0;
  private submitLock = false;

  constructor(opts: SoftWindowControllerOptions) {
    this.client = opts.client;
    this.live = Boolean(opts.live);
    this.preview = Boolean(opts.preview) && !this.live;
    this.scheduler = opts.scheduler ?? defaultScheduler;
    this.uiState = this.active ? 'probing' : 'idle';
  }

  get active(): boolean {
    return this.live || this.preview;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit(): void {
    for (const l of this.listeners) l();
  }

  getSnapshot(): SoftWindowControllerSnapshot {
    const locked = Boolean(this.current?.selection_finalized) || this.uiState === 'locked';
    let highlightIds: number[] = [];
    if (this.highlightOverride && locked) highlightIds = this.highlightOverride.slice();
    else if (locked && this.current?.selected_message_ids?.length) {
      highlightIds = this.current.selected_message_ids.slice();
    } else if (this.drawerOpen && !locked && this.rounds.length) {
      highlightIds = flattenRoundMessageIds(pickLastNRounds(this.rounds, this.draftCount));
    }

    const showPickerCardLive =
      this.active &&
      this.uiState !== 'disabled' &&
      this.uiState !== 'deferred' &&
      this.uiState !== 'unavailable' &&
      this.uiState !== 'probing' &&
      this.uiState !== 'idle' &&
      (locked ||
        ((this.uiState === 'ready' || this.uiState === 'empty' || this.uiState === 'submitting') &&
          !this.pickerSuppressed));

    const showPickerCard = this.preview
      ? this.active && this.uiState !== 'idle' && this.uiState !== 'disabled'
      : showPickerCardLive;

    const boundaryMessageId = this.current?.boundary_message_id ?? 0;
    const showBoundary =
      this.live &&
      this.uiState !== 'disabled' &&
      this.uiState !== 'unavailable' &&
      this.uiState !== 'deferred' &&
      boundaryMessageId > 0;

    return {
      uiState: this.uiState,
      current: this.current,
      candidates: this.candidates,
      rounds: this.rounds,
      draftCount: this.draftCount,
      drawerOpen: this.drawerOpen,
      submitting: this.submitting,
      statusText: this.computeStatusText(locked),
      errorDetail: this.errorDetail,
      locked,
      pickerSuppressed: this.pickerSuppressed,
      showPickerCard,
      showBoundary,
      boundaryMessageId,
      highlightIds,
      opener: this.opener,
    };
  }

  private computeStatusText(locked: boolean): string {
    if (!this.active) return '';
    if (this.uiState === 'probing' || this.uiState === 'loading') return '加载中…';
    if (this.uiState === 'disabled') return this.preview ? '接口未启用 · mock 可见' : '';
    if (this.uiState === 'deferred') return '日界换窗中…';
    if (this.uiState === 'conflict') return '选择已锁定';
    if (this.uiState === 'unavailable' || this.uiState === 'error' || this.uiState === 'auth_error') {
      return this.errorDetail || '出错了';
    }
    if (this.uiState === 'empty') return '昨天暂无可带走的对话轮';
    if (this.uiState === 'submitting') return '换窗中…';
    if (locked) {
      return `已锁定 · ${this.current?.selected_round_count ?? this.current?.carryover_count ?? 0} 轮`;
    }
    if (this.client.mode === 'mock') return '预览 · mock · 按完整对话轮';
    return this.rounds.length ? `候选 ${this.rounds.length} 轮` : '';
  }

  start(): void {
    if (!this.active || this.disposed) return;
    void this.probeCurrent();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.currentAbort?.abort();
    this.candidatesAbort?.abort();
    this.clearDeferredTimers();
    this.listeners.clear();
  }

  private clearDeferredTimers(): void {
    for (const t of this.deferredCancels) t.cancel();
    this.deferredCancels = [];
  }

  private restoreFocus(): void {
    const el = this.opener;
    this.opener = null;
    if (!el) return;
    const connected = el.isConnected !== false;
    if (connected) {
      try {
        el.focus();
      } catch {
        /* ignore */
      }
    }
  }

  private applyCurrent(cur: DailyContextCurrent, nextRounds?: CarryoverRound[]): void {
    this.current = cur;
    this.errorDetail = '';
    if (cur.selection_finalized) {
      this.uiState = 'locked';
      this.draftCount = draftCountFromCurrent(cur);
      this.highlightOverride = cur.selected_message_ids.slice();
      this.pickerSuppressed = false;
      this.drawerOpen = false;
      return;
    }
    this.highlightOverride = null;
    if (nextRounds && nextRounds.length === 0) {
      this.uiState = 'empty';
      this.draftCount = 10;
      return;
    }
    this.uiState = nextRounds && nextRounds.length === 0 ? 'empty' : 'ready';
    this.draftCount = 10;
  }

  async probeCurrent(opts?: { fromDeferred?: boolean }): Promise<void> {
    if (!this.active || this.disposed) return;
    const gen = ++this.probeGen;
    this.currentAbort?.abort();
    const ctrl = new AbortController();
    this.currentAbort = ctrl;

    if (!opts?.fromDeferred) {
      if (this.uiState !== 'locked' && this.uiState !== 'ready' && this.uiState !== 'empty') {
        this.uiState = 'probing';
        this.emit();
      }
    }

    try {
      const cur = await this.client.getCurrent({ signal: ctrl.signal });
      if (this.disposed || gen !== this.probeGen) return;
      this.clearDeferredTimers();
      this.deferredAttempt = 0;

      if (this.preview) {
        try {
          const cand = await this.client.getCandidates();
          if (this.disposed || gen !== this.probeGen) return;
          this.rounds = cand.rounds;
          this.candidates = cand.candidates;
          if (cur.selection_finalized) {
            this.applyCurrent(cur, cand.rounds);
          } else if (!cand.rounds.length) {
            this.current = cur;
            this.uiState = 'empty';
            this.draftCount = 10;
          } else {
            this.applyCurrent(cur, cand.rounds);
          }
        } catch (err) {
          if (this.disposed || gen !== this.probeGen) return;
          const kind = classifySoftWindowError(err);
          if (kind === 'disabled') {
            this.current = null;
            this.uiState = 'disabled';
            this.rounds = [];
            this.candidates = [];
            this.emit();
            return;
          }
          this.applyCurrent(cur);
        }
        this.emit();
        return;
      }

      // Formal: do NOT fetch candidates until modal open.
      this.rounds = [];
      this.candidates = [];
      this.applyCurrent(cur);
      this.emit();
    } catch (err) {
      if (this.disposed || gen !== this.probeGen) return;
      if (err instanceof DOMException && err.name === 'AbortError') return;
      if (err instanceof Error && err.name === 'AbortError') return;

      const kind = classifySoftWindowError(err);
      if (kind === 'disabled') {
        this.clearDeferredTimers();
        this.current = null;
        this.rounds = [];
        this.candidates = [];
        this.highlightOverride = null;
        this.drawerOpen = false;
        this.uiState = 'disabled';
        this.emit();
        return;
      }
      if (kind === 'deferred') {
        this.current = null;
        this.rounds = [];
        this.candidates = [];
        this.drawerOpen = false;
        this.uiState = 'deferred';
        this.emit();
        if (this.deferredAttempt < DEFERRED_RETRY_MS.length) {
          const delay = DEFERRED_RETRY_MS[this.deferredAttempt];
          this.deferredAttempt += 1;
          const handle = this.scheduler.schedule(() => {
            void this.probeCurrent({ fromDeferred: true });
          }, delay);
          this.deferredCancels.push(handle);
        }
        return;
      }
      if (kind === 'auth_error') {
        console.error('[AUTH_BRIDGE] daily soft window probe failed', err);
        this.current = null;
        this.uiState = 'unavailable';
        this.errorDetail = softWindowErrorMessage('auth_error', err);
        this.emit();
        return;
      }
      this.uiState = 'unavailable';
      this.errorDetail = softWindowErrorMessage('unavailable', err);
      this.emit();
    }
  }

  async reload(): Promise<void> {
    this.deferredAttempt = 0;
    this.clearDeferredTimers();
    await this.probeCurrent();
  }

  onWindowFocus(): void {
    if (!this.live || this.disposed) return;
    if (typeof document !== 'undefined' && document.visibilityState && document.visibilityState !== 'visible') {
      return;
    }
    if (this.uiState === 'deferred' && this.deferredAttempt >= DEFERRED_RETRY_MS.length) {
      this.deferredAttempt = 0;
    }
    void this.probeCurrent(this.uiState === 'deferred' ? { fromDeferred: true } : undefined);
  }

  setDraftCount(count: CarryoverCount): void {
    this.draftCount = count;
    this.emit();
  }

  openDrawer(opener?: FocusableOpener | null): void {
    const snap = this.getSnapshot();
    if (snap.locked) return;
    this.opener = opener ?? null;
    this.drawerOpen = true;
    this.emit();

    if (this.preview) return;

    const gen = ++this.candidatesGen;
    this.candidatesAbort?.abort();
    const ctrl = new AbortController();
    this.candidatesAbort = ctrl;
    const capturedId = this.current?.context_id;
    const capturedEpoch = this.current?.context_epoch;

    void (async () => {
      if (capturedId == null || capturedEpoch == null) return;
      try {
        const cand = await this.client.getCandidates({ signal: ctrl.signal });
        if (this.disposed || gen !== this.candidatesGen || !this.drawerOpen) return;
        if (cand.context_id !== capturedId || cand.context_epoch !== capturedEpoch) {
          this.drawerOpen = false;
          this.rounds = [];
          this.candidates = [];
          this.restoreFocus();
          this.emit();
          await this.reload();
          return;
        }
        this.rounds = cand.rounds;
        this.candidates = cand.candidates;
        this.uiState = !cand.rounds.length ? 'empty' : 'ready';
        this.emit();
      } catch (err) {
        if (this.disposed || gen !== this.candidatesGen) return;
        if (err instanceof DOMException && err.name === 'AbortError') return;
        if (err instanceof Error && err.name === 'AbortError') return;
        const kind = classifySoftWindowError(err);
        if (kind === 'disabled') {
          this.drawerOpen = false;
          this.uiState = 'disabled';
          this.current = null;
          this.restoreFocus();
          this.emit();
          return;
        }
        if (kind === 'deferred') {
          this.drawerOpen = false;
          this.uiState = 'deferred';
          this.deferredAttempt = 0;
          this.restoreFocus();
          this.emit();
          void this.probeCurrent({ fromDeferred: true });
          return;
        }
        this.drawerOpen = false;
        this.errorDetail = softWindowErrorMessage(kind, err);
        this.restoreFocus();
        if (kind === 'auth_error') {
          console.error('[AUTH_BRIDGE] carryover-candidates failed', err);
          this.uiState = 'unavailable';
        }
        this.emit();
      }
    })();
  }

  closeDrawer(): void {
    this.candidatesAbort?.abort();
    this.candidatesGen += 1;
    this.drawerOpen = false;
    this.restoreFocus();
    this.emit();
  }

  async confirmSelection(): Promise<boolean> {
    const snap = this.getSnapshot();
    if (!this.active || snap.locked || this.submitting || this.submitLock) return false;
    this.submitLock = true;
    this.submitting = true;
    this.uiState = 'submitting';
    this.errorDetail = '';
    this.emit();

    const capturedId = this.current?.context_id ?? null;
    const capturedEpoch = this.current?.context_epoch ?? null;
    const requested = this.draftCount;

    try {
      const res = await this.client.selectCarryover(requested);
      if (this.disposed) return true;

      // Context ownership: response must match capture; else discard + GET current.
      if (
        capturedId == null ||
        capturedEpoch == null ||
        res.context_id !== capturedId ||
        res.context_epoch !== capturedEpoch
      ) {
        this.submitting = false;
        this.drawerOpen = false;
        this.submitLock = false;
        this.restoreFocus();
        this.emit();
        await this.probeCurrent();
        return false;
      }

      // Never patch with local draftCount — parser already validated tiers.
      const next: DailyContextCurrent = {
        ...(this.current ?? {
          context_id: res.context_id,
          context_epoch: res.context_epoch,
          local_day: '',
          boundary_message_id: 0,
          carryover_unit: 'round' as const,
          handoff_status: 'ABSENT',
          resident_generation: 1,
          requested_round_count: null,
          selected_round_count: 0,
          selected_message_count: 0,
          selected_message_ids: [],
          carryover_count: 0,
          selection_finalized: false,
        }),
        context_id: res.context_id,
        context_epoch: res.context_epoch,
        requested_round_count: res.requested_round_count,
        selected_round_count: res.selected_round_count,
        selected_message_count: res.selected_message_count,
        selected_message_ids: res.selected_message_ids.slice(),
        carryover_count: res.carryover_count,
        selection_finalized: true,
      };
      this.current = next;
      this.highlightOverride = res.selected_message_ids.slice();
      this.draftCount = draftCountFromCurrent(next);
      this.uiState = 'locked';
      this.drawerOpen = false;
      this.pickerSuppressed = false;
      this.submitting = false;
      this.submitLock = false;
      this.restoreFocus();
      this.emit();
      return true;
    } catch (err) {
      if (this.disposed) return false;
      const kind = classifySoftWindowError(err);
      if (kind === 'conflict') {
        this.submitting = false;
        this.drawerOpen = false;
        this.submitLock = false;
        this.restoreFocus();
        this.emit();
        await this.probeCurrent();
        return false;
      }
      if (kind === 'deferred') {
        this.submitting = false;
        this.drawerOpen = false;
        this.uiState = 'deferred';
        this.deferredAttempt = 0;
        this.submitLock = false;
        this.restoreFocus();
        this.emit();
        void this.probeCurrent({ fromDeferred: true });
        return false;
      }
      if (kind === 'disabled') {
        this.submitting = false;
        this.drawerOpen = false;
        this.current = null;
        this.rounds = [];
        this.candidates = [];
        this.uiState = 'disabled';
        this.submitLock = false;
        this.restoreFocus();
        this.emit();
        return false;
      }
      // malformed / 5xx — fail-soft + GET current; never lock from draftCount
      this.submitting = false;
      this.drawerOpen = false;
      this.errorDetail = softWindowErrorMessage(kind, err);
      this.submitLock = false;
      this.restoreFocus();
      if (kind === 'auth_error') {
        console.error('[AUTH_BRIDGE] select-carryover failed', err);
        this.uiState = 'unavailable';
        this.emit();
        return false;
      }
      this.emit();
      await this.probeCurrent();
      return false;
    }
  }

  /** Preview-only helper */
  async lockZeroIfNeeded(): Promise<boolean> {
    if (!this.preview) return true;
    if (this.current?.selection_finalized) return true;
    this.draftCount = 0;
    this.submitting = true;
    this.emit();
    try {
      const res = await this.client.selectCarryover(0);
      if (this.disposed) return true;
      const next: DailyContextCurrent = {
        ...(this.current ?? {
          context_id: res.context_id,
          context_epoch: res.context_epoch,
          local_day: '',
          boundary_message_id: 0,
          carryover_unit: 'round' as const,
          handoff_status: 'ABSENT',
          resident_generation: 1,
          requested_round_count: null,
          selected_round_count: 0,
          selected_message_count: 0,
          selected_message_ids: [],
          carryover_count: 0,
          selection_finalized: false,
        }),
        context_id: res.context_id,
        context_epoch: res.context_epoch,
        requested_round_count: 0,
        selected_round_count: res.selected_round_count,
        selected_message_count: res.selected_message_count,
        selected_message_ids: res.selected_message_ids.slice(),
        carryover_count: res.carryover_count,
        selection_finalized: true,
      };
      this.current = next;
      this.highlightOverride = res.selected_message_ids.slice();
      this.uiState = 'locked';
      this.drawerOpen = false;
      this.emit();
      return true;
    } catch (err) {
      if (this.disposed) return true;
      const kind = classifySoftWindowError(err);
      if (kind === 'conflict') {
        this.uiState = 'locked';
        if (this.current) this.current = { ...this.current, selection_finalized: true };
      } else {
        this.uiState = kind === 'disabled' ? 'disabled' : 'error';
        this.errorDetail = softWindowErrorMessage(kind, err);
      }
      this.emit();
      return true;
    } finally {
      this.submitting = false;
      this.emit();
    }
  }

  notifySendStarted(): void {
    this.candidatesAbort?.abort();
    this.candidatesGen += 1;
    this.drawerOpen = false;
    this.pickerSuppressed = true;
    this.emit();
  }

  notifySendSettled(success: boolean): void {
    // Reuse unified probe — never bypass probe generation with raw getCurrent.
    void (async () => {
      await this.probeCurrent();
      if (this.disposed) return;
      if (this.uiState === 'disabled') {
        this.pickerSuppressed = false;
        this.emit();
        return;
      }
      // After probe, restore picker visibility if still unselected / or locked.
      this.pickerSuppressed = false;
      void success;
      this.emit();
    })();
  }
}

export function isCarryoverCountSafe(n: number): n is CarryoverCount {
  return isCarryoverCount(n);
}

export function httpErrorStatus(err: unknown): number | null {
  return err instanceof HttpError ? err.status : null;
}
