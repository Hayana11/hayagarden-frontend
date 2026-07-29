/**
 * Manual context window modal controller (no React).
 */
import {
  captureSourceFromCurrent,
  classifyManualWindowError,
  createManualContextWindowClient,
  manualWindowErrorMessage,
  type CapturedSource,
  type CarryoverCount,
  type CarryoverRound,
  type ManualContextWindowClient,
  type ManualWindowUiState,
} from './manualContextWindow';

export type ManualWindowSnapshot = {
  enabled: boolean;
  uiState: ManualWindowUiState;
  modalOpen: boolean;
  draftCount: CarryoverCount;
  rounds: CarryoverRound[];
  capturedSource: CapturedSource | null;
  submitting: boolean;
  errorDetail: string;
};

type Listener = () => void;

export type ManualWindowControllerOptions = {
  client?: ManualContextWindowClient;
};

export class ManualContextWindowController {
  readonly client: ManualContextWindowClient;

  enabled = false;
  uiState: ManualWindowUiState = 'probing';
  modalOpen = false;
  draftCount: CarryoverCount = 10;
  rounds: CarryoverRound[] = [];
  capturedSource: CapturedSource | null = null;
  submitting = false;
  errorDetail = '';
  private disposed = false;
  private listeners = new Set<Listener>();
  private probeGen = 0;
  private modalGen = 0;
  private switchGen = 0;
  private currentAbort: AbortController | null = null;
  private candidatesAbort: AbortController | null = null;
  private switchAbort: AbortController | null = null;

  constructor(opts: ManualWindowControllerOptions = {}) {
    this.client = opts.client ?? createManualContextWindowClient();
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit(): void {
    for (const l of this.listeners) l();
  }

  getSnapshot(): ManualWindowSnapshot {
    return {
      enabled: this.enabled,
      uiState: this.uiState,
      modalOpen: this.modalOpen,
      draftCount: this.draftCount,
      rounds: this.rounds,
      capturedSource: this.capturedSource,
      submitting: this.submitting,
      errorDetail: this.errorDetail,
    };
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.currentAbort?.abort();
    this.candidatesAbort?.abort();
    this.switchAbort?.abort();
    this.listeners.clear();
  }

  async probeEnabled(): Promise<void> {
    if (this.disposed) return;
    const gen = ++this.probeGen;
    this.currentAbort?.abort();
    const ctrl = new AbortController();
    this.currentAbort = ctrl;
    this.uiState = 'probing';
    this.emit();
    try {
      await this.client.getCurrent({ signal: ctrl.signal });
      if (this.disposed || gen !== this.probeGen) return;
      this.enabled = true;
      this.uiState = 'idle';
      this.errorDetail = '';
      this.emit();
    } catch (err) {
      if (this.disposed || gen !== this.probeGen) return;
      if (err instanceof DOMException && err.name === 'AbortError') return;
      if (err instanceof Error && err.name === 'AbortError') return;
      const kind = classifyManualWindowError(err);
      if (kind === 'disabled') {
        this.enabled = false;
        this.uiState = 'disabled';
        this.errorDetail = '';
        this.emit();
        return;
      }
      this.enabled = false;
      this.uiState = kind;
      this.errorDetail = manualWindowErrorMessage(kind, err);
      this.emit();
    }
  }

  async openModal(): Promise<void> {
    if (this.disposed || !this.enabled || this.submitting) return;
    const gen = ++this.modalGen;
    this.modalOpen = true;
    this.uiState = 'loading';
    this.errorDetail = '';
    this.rounds = [];
    this.capturedSource = null;
    this.emit();

    this.currentAbort?.abort();
    const currentCtrl = new AbortController();
    this.currentAbort = currentCtrl;

    try {
      const cur = await this.client.getCurrent({ signal: currentCtrl.signal });
      if (this.disposed || gen !== this.modalGen || !this.modalOpen) return;
      const source = captureSourceFromCurrent(cur);
      this.capturedSource = source;

      this.candidatesAbort?.abort();
      const candCtrl = new AbortController();
      this.candidatesAbort = candCtrl;
      const cand = await this.client.getCandidates(source, { signal: candCtrl.signal });
      if (this.disposed || gen !== this.modalGen || !this.modalOpen) return;
      if (
        cand.source_context_id !== source.source_context_id ||
        cand.source_context_epoch !== source.source_context_epoch
      ) {
        this.handleStale();
        return;
      }
      this.rounds = cand.rounds;
      this.uiState = cand.rounds.length === 0 ? 'empty' : 'ready';
      this.draftCount = 10;
      this.emit();
    } catch (err) {
      if (this.disposed || gen !== this.modalGen) return;
      if (err instanceof DOMException && err.name === 'AbortError') return;
      if (err instanceof Error && err.name === 'AbortError') return;
      await this.handleModalError(err, gen);
    }
  }

  private async handleModalError(err: unknown, _gen: number): Promise<void> {
    const kind = classifyManualWindowError(err);
    if (kind === 'stale' || kind === 'no_open_context') {
      this.handleStale(kind);
      return;
    }
    if (kind === 'busy') {
      this.errorDetail = manualWindowErrorMessage('busy', err);
      this.uiState = 'busy';
      this.emit();
      return;
    }
    if (kind === 'auth_error') {
      this.errorDetail = manualWindowErrorMessage('auth_error', err);
      this.uiState = 'auth_error';
      this.emit();
      return;
    }
    this.errorDetail = manualWindowErrorMessage('error', err);
    this.uiState = 'error';
    this.emit();
  }

  private handleStale(kind: ManualWindowUiState = 'stale'): void {
    this.capturedSource = null;
    this.rounds = [];
    this.errorDetail = manualWindowErrorMessage(kind);
    this.uiState = kind;
    this.emit();
    void this.refreshAfterStale();
  }

  private async refreshAfterStale(): Promise<void> {
    const gen = this.modalGen;
    try {
      const cur = await this.client.getCurrent();
      if (this.disposed || gen !== this.modalGen || !this.modalOpen) return;
      const source = captureSourceFromCurrent(cur);
      const cand = await this.client.getCandidates(source);
      if (this.disposed || gen !== this.modalGen || !this.modalOpen) return;
      this.capturedSource = source;
      this.rounds = cand.rounds;
      this.uiState = cand.rounds.length === 0 ? 'empty' : 'ready';
      this.errorDetail = manualWindowErrorMessage('stale');
      this.emit();
    } catch {
      /* keep stale message */
    }
  }

  closeModal(): void {
    if (!this.modalOpen) return;
    this.modalGen += 1;
    this.candidatesAbort?.abort();
    this.switchAbort?.abort();
    this.modalOpen = false;
    this.submitting = false;
    if (this.enabled) this.uiState = 'idle';
    this.emit();
  }

  setDraftCount(count: CarryoverCount): void {
    this.draftCount = count;
    this.emit();
  }

  async confirmSwitch(): Promise<boolean> {
    if (this.disposed || !this.modalOpen || this.submitting || !this.capturedSource) {
      return false;
    }
    const source = this.capturedSource;
    const count = this.draftCount;
    const gen = ++this.switchGen;
    this.switchAbort?.abort();
    const ctrl = new AbortController();
    this.switchAbort = ctrl;
    this.submitting = true;
    this.uiState = 'submitting';
    this.errorDetail = '';
    this.emit();

    const requestId = crypto.randomUUID();

    try {
      const res = await this.client.switchWindow(source, count, requestId, { signal: ctrl.signal });
      if (this.disposed || gen !== this.switchGen) return false;
      if (
        res.source_context_id !== source.source_context_id ||
        res.source_context_epoch !== source.source_context_epoch
      ) {
        this.submitting = false;
        this.handleStale();
        return false;
      }
      this.submitting = false;
      this.modalOpen = false;
      this.capturedSource = null;
      this.rounds = [];
      this.uiState = 'idle';
      this.emit();
      return true;
    } catch (err) {
      if (this.disposed || gen !== this.switchGen) return false;
      if (err instanceof DOMException && err.name === 'AbortError') return false;
      if (err instanceof Error && err.name === 'AbortError') return false;
      this.submitting = false;
      const kind = classifyManualWindowError(err);
      if (kind === 'busy') {
        this.errorDetail = manualWindowErrorMessage('busy', err);
        this.uiState = 'busy';
        this.emit();
        return false;
      }
      if (kind === 'stale' || kind === 'no_open_context') {
        this.handleStale(kind);
        return false;
      }
      if (kind === 'auth_error') {
        this.errorDetail = manualWindowErrorMessage('auth_error', err);
        this.uiState = 'auth_error';
        this.emit();
        return false;
      }
      this.errorDetail = manualWindowErrorMessage('error', err);
      this.uiState = 'error';
      this.emit();
      return false;
    }
  }
}
