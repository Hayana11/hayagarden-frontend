/**
 * Formal Soft Window state machine (no React).
 * Injectable client + scheduler for integration tests.
 *
 * Cross-operation fencing: shared contextGeneration + activeContextKey gate
 * current, candidates, and select so stale in-flight work cannot write after
 * authoritative current moves to a different context.
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

export type ContextKey = {
  context_id: number;
  context_epoch: number;
};

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
  /** Shared context fence generation (bumps when authoritative context identity changes). */
  contextGeneration: number;
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

export function contextKeyFromCurrent(cur: DailyContextCurrent | null | undefined): ContextKey | null {
  if (!cur) return null;
  return { context_id: cur.context_id, context_epoch: cur.context_epoch };
}

export function contextKeysMatch(a: ContextKey | null | undefined, b: ContextKey | null | undefined): boolean {
  if (!a || !b) return false;
  return a.context_id === b.context_id && a.context_epoch === b.context_epoch;
}

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

  /** Bumps when authoritative current context identity changes. */
  contextGeneration = 0;
  private activeContextKey: ContextKey | null = null;

  private currentAbort: AbortController | null = null;
  private candidatesAbort: AbortController | null = null;
  private selectAbort: AbortController | null = null;
  private deferredCancels: Array<{ cancel: () => void }> = [];
  private deferredAttempt = 0;
  private probeGen = 0;
  private candidatesGen = 0;
  private submitGen = 0;
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
      contextGeneration: this.contextGeneration,
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
    this.invalidateInFlightOps({ closeDrawer: true, clearRounds: true });
    this.currentAbort?.abort();
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

  private abortCandidatesOp(): void {
    this.candidatesAbort?.abort();
    this.candidatesGen += 1;
    this.candidatesAbort = null;
  }

  private abortSelectOp(): void {
    this.selectAbort?.abort();
    this.submitGen += 1;
    this.selectAbort = null;
  }

  /**
   * Invalidate candidates/select in-flight work. Optionally tear down drawer UI.
   * Does not bump contextGeneration — caller does that when identity changes.
   */
  private invalidateInFlightOps(opts?: {
    closeDrawer?: boolean;
    clearRounds?: boolean;
    resetSubmitting?: boolean;
  }): void {
    this.abortCandidatesOp();
    this.abortSelectOp();
    if (opts?.resetSubmitting !== false) {
      this.submitting = false;
      this.submitLock = false;
    }
    if (opts?.closeDrawer) {
      this.drawerOpen = false;
      this.restoreFocus();
    }
    if (opts?.clearRounds) {
      this.rounds = [];
      this.candidates = [];
    }
  }

  private applyCurrentFields(cur: DailyContextCurrent, nextRounds?: CarryoverRound[]): void {
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

  /**
   * Authoritative current adoption. When context identity changes, bump shared
   * generation and invalidate cross-operation in-flight work.
   */
  private adoptAuthoritativeCurrent(cur: DailyContextCurrent, nextRounds?: CarryoverRound[]): void {
    const nextKey = contextKeyFromCurrent(cur);
    const contextChanged = !contextKeysMatch(this.activeContextKey, nextKey);

    if (contextChanged) {
      this.contextGeneration += 1;
      this.invalidateInFlightOps({ closeDrawer: true, clearRounds: true });
      this.activeContextKey = nextKey;
      this.applyCurrentFields(cur, nextRounds);
      return;
    }

    this.activeContextKey = nextKey;
    this.applyCurrentFields(cur, nextRounds);

    // Formal live: preserve modal rounds when same context + drawer still open.
    if (this.live && !this.preview && !this.drawerOpen) {
      this.rounds = [];
      this.candidates = [];
    }
  }

  private contextStillOwned(
    capturedKey: ContextKey | null,
    capturedGen: number,
    responseKey?: ContextKey | null,
  ): boolean {
    if (!capturedKey || capturedGen !== this.contextGeneration) return false;
    if (!contextKeysMatch(capturedKey, this.activeContextKey)) return false;
    if (!contextKeysMatch(capturedKey, contextKeyFromCurrent(this.current))) return false;
    if (responseKey && !contextKeysMatch(capturedKey, responseKey)) return false;
    return true;
  }

  private async authoritativeRefresh(): Promise<void> {
    await this.probeCurrent();
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
          const nextKey = contextKeyFromCurrent(cur);
          const contextChanged = !contextKeysMatch(this.activeContextKey, nextKey);
          if (contextChanged) this.contextGeneration += 1;
          this.activeContextKey = nextKey;
          if (cur.selection_finalized) {
            this.applyCurrentFields(cur, cand.rounds);
          } else if (!cand.rounds.length) {
            this.current = cur;
            this.uiState = 'empty';
            this.draftCount = 10;
          } else {
            this.applyCurrentFields(cur, cand.rounds);
          }
        } catch (err) {
          if (this.disposed || gen !== this.probeGen) return;
          const kind = classifySoftWindowError(err);
          if (kind === 'disabled') {
            this.contextGeneration += 1;
            this.activeContextKey = null;
            this.current = null;
            this.uiState = 'disabled';
            this.rounds = [];
            this.candidates = [];
            this.emit();
            return;
          }
          this.adoptAuthoritativeCurrent(cur);
        }
        this.emit();
        return;
      }

      this.adoptAuthoritativeCurrent(cur);
      this.emit();
    } catch (err) {
      if (this.disposed || gen !== this.probeGen) return;
      if (err instanceof DOMException && err.name === 'AbortError') return;
      if (err instanceof Error && err.name === 'AbortError') return;

      const kind = classifySoftWindowError(err);
      if (kind === 'disabled') {
        this.clearDeferredTimers();
        this.contextGeneration += 1;
        this.activeContextKey = null;
        this.invalidateInFlightOps({ closeDrawer: true, clearRounds: true });
        this.current = null;
        this.highlightOverride = null;
        this.uiState = 'disabled';
        this.emit();
        return;
      }
      if (kind === 'deferred') {
        this.contextGeneration += 1;
        this.activeContextKey = null;
        this.invalidateInFlightOps({ closeDrawer: true, clearRounds: true });
        this.current = null;
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

    const opGen = ++this.candidatesGen;
    this.candidatesAbort?.abort();
    const ctrl = new AbortController();
    this.candidatesAbort = ctrl;
    const capturedKey = contextKeyFromCurrent(this.current);
    const capturedGen = this.contextGeneration;

    void (async () => {
      if (!capturedKey) return;
      try {
        const cand = await this.client.getCandidates({ signal: ctrl.signal });
        if (this.disposed || opGen !== this.candidatesGen || !this.drawerOpen) return;

        const responseKey: ContextKey = {
          context_id: cand.context_id,
          context_epoch: cand.context_epoch,
        };
        if (!this.contextStillOwned(capturedKey, capturedGen, responseKey)) {
          this.drawerOpen = false;
          this.rounds = [];
          this.candidates = [];
          this.restoreFocus();
          this.emit();
          await this.authoritativeRefresh();
          return;
        }

        this.rounds = cand.rounds;
        this.candidates = cand.candidates;
        this.uiState = !cand.rounds.length ? 'empty' : 'ready';
        this.emit();
      } catch (err) {
        if (this.disposed || opGen !== this.candidatesGen) return;
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
    this.abortCandidatesOp();
    this.drawerOpen = false;
    this.restoreFocus();
    this.emit();
  }

  async confirmSelection(): Promise<boolean> {
    const snap = this.getSnapshot();
    if (!this.active || snap.locked || this.submitting || this.submitLock) return false;

    const capturedKey = contextKeyFromCurrent(this.current);
    const capturedGen = this.contextGeneration;
    if (!capturedKey) return false;

    const submitOpGen = ++this.submitGen;
    this.selectAbort?.abort();
    const selectCtrl = new AbortController();
    this.selectAbort = selectCtrl;

    this.submitLock = true;
    this.submitting = true;
    this.uiState = 'submitting';
    this.errorDetail = '';
    this.emit();

    const requested = this.draftCount;

    const finishSubmit = async (ok: boolean): Promise<boolean> => {
      this.submitting = false;
      this.submitLock = false;
      if (this.selectAbort === selectCtrl) this.selectAbort = null;
      this.emit();
      return ok;
    };

    const rejectStaleSubmit = async (): Promise<boolean> => {
      this.drawerOpen = false;
      this.restoreFocus();
      await finishSubmit(false);
      await this.authoritativeRefresh();
      return false;
    };

    try {
      const res = await this.client.selectCarryover(requested, { signal: selectCtrl.signal });
      if (this.disposed) return true;

      const responseKey: ContextKey = {
        context_id: res.context_id,
        context_epoch: res.context_epoch,
      };

      if (
        submitOpGen !== this.submitGen ||
        !this.contextStillOwned(capturedKey, capturedGen, responseKey)
      ) {
        return rejectStaleSubmit();
      }

      const base = this.current;
      if (!base || !contextKeysMatch(capturedKey, contextKeyFromCurrent(base))) {
        return rejectStaleSubmit();
      }

      const next: DailyContextCurrent = {
        ...base,
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
      this.activeContextKey = contextKeyFromCurrent(next);
      this.highlightOverride = res.selected_message_ids.slice();
      this.draftCount = draftCountFromCurrent(next);
      this.uiState = 'locked';
      this.drawerOpen = false;
      this.pickerSuppressed = false;
      this.restoreFocus();
      return finishSubmit(true);
    } catch (err) {
      if (this.disposed) return false;
      if (err instanceof DOMException && err.name === 'AbortError') return finishSubmit(false);
      if (err instanceof Error && err.name === 'AbortError') return finishSubmit(false);

      if (submitOpGen !== this.submitGen || capturedGen !== this.contextGeneration) {
        return rejectStaleSubmit();
      }

      const kind = classifySoftWindowError(err);
      if (kind === 'conflict') {
        this.drawerOpen = false;
        this.restoreFocus();
        await finishSubmit(false);
        await this.authoritativeRefresh();
        return false;
      }
      if (kind === 'deferred') {
        this.drawerOpen = false;
        this.uiState = 'deferred';
        this.deferredAttempt = 0;
        this.restoreFocus();
        await finishSubmit(false);
        void this.probeCurrent({ fromDeferred: true });
        return false;
      }
      if (kind === 'disabled') {
        this.drawerOpen = false;
        this.current = null;
        this.activeContextKey = null;
        this.rounds = [];
        this.candidates = [];
        this.uiState = 'disabled';
        this.restoreFocus();
        await finishSubmit(false);
        return false;
      }
      this.drawerOpen = false;
      this.errorDetail = softWindowErrorMessage(kind, err);
      this.restoreFocus();
      if (kind === 'auth_error') {
        console.error('[AUTH_BRIDGE] select-carryover failed', err);
        this.uiState = 'unavailable';
        await finishSubmit(false);
        return false;
      }
      await finishSubmit(false);
      await this.authoritativeRefresh();
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
      this.activeContextKey = contextKeyFromCurrent(next);
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
    this.invalidateInFlightOps({ closeDrawer: true, clearRounds: true });
    this.pickerSuppressed = true;
    this.emit();
  }

  notifySendSettled(success: boolean): void {
    void (async () => {
      await this.probeCurrent();
      if (this.disposed) return;
      if (this.uiState === 'disabled') {
        this.pickerSuppressed = false;
        this.emit();
        return;
      }
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
