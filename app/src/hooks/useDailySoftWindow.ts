import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  classifySoftWindowError,
  createDailySoftWindowClient,
  draftCountFromCurrent,
  flattenRoundMessageIds,
  isCarryoverCount,
  pickLastNRounds,
  softWindowErrorMessage,
  type CarryoverCount,
  type CarryoverMessage,
  type CarryoverRound,
  type DailyContextCurrent,
  type SoftWindowUiState,
} from '../lib/dailySoftWindow';
import { HttpError } from '../lib/http';

type Options = {
  forceMock?: boolean;
  search?: string;
  /** Formal chat: always probe live BFF (404 = hide). */
  live?: boolean;
};

const DEFERRED_RETRY_MS = [750, 2000, 5000] as const;

export type DailySoftWindowController = {
  enabled: boolean;
  live: boolean;
  mode: 'mock' | 'live';
  uiState: SoftWindowUiState;
  current: DailyContextCurrent | null;
  /** @deprecated alias of current for preview screens */
  summary: DailyContextCurrent | null;
  candidates: CarryoverMessage[];
  rounds: CarryoverRound[];
  draftCount: CarryoverCount;
  highlightIds: Set<number>;
  drawerOpen: boolean;
  submitting: boolean;
  statusText: string;
  errorDetail: string;
  locked: boolean;
  showPickerCard: boolean;
  showBoundary: boolean;
  boundaryMessageId: number;
  openDrawer: () => void;
  closeDrawer: () => void;
  setDraftCount: (count: CarryoverCount) => void;
  confirmSelection: () => Promise<boolean>;
  /** Preview-only: simulate first send locking 0. Formal chat must NEVER POST 0 before send. */
  lockZeroIfNeeded: () => Promise<boolean>;
  notifySendStarted: () => void;
  notifySendSettled: (success: boolean) => void;
  reload: () => Promise<void>;
};

export function useDailySoftWindow(opts: Options = {}): DailySoftWindowController {
  const search = opts.search ?? (typeof location !== 'undefined' ? location.search : '');
  const isLive = Boolean(opts.live);
  const isPreview = Boolean(opts.forceMock) && !isLive;
  const active = isLive || isPreview;

  const client = useMemo(
    () =>
      createDailySoftWindowClient({
        search,
        forceMock: isLive ? false : opts.forceMock ?? true,
      }),
    [search, opts.forceMock, isLive],
  );

  const [uiState, setUiState] = useState<SoftWindowUiState>(active ? 'probing' : 'idle');
  const [current, setCurrent] = useState<DailyContextCurrent | null>(null);
  const [candidates, setCandidates] = useState<CarryoverMessage[]>([]);
  const [rounds, setRounds] = useState<CarryoverRound[]>([]);
  const [draftCount, setDraftCount] = useState<CarryoverCount>(10);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [errorDetail, setErrorDetail] = useState('');
  const [pickerSuppressed, setPickerSuppressed] = useState(false);
  const [highlightOverride, setHighlightOverride] = useState<number[] | null>(null);

  const mounted = useRef(true);
  const currentAbort = useRef<AbortController | null>(null);
  const deferredTimers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const deferredAttempt = useRef(0);
  const probeGen = useRef(0);
  const submitLock = useRef(false);

  const clearDeferredTimers = useCallback(() => {
    for (const t of deferredTimers.current) clearTimeout(t);
    deferredTimers.current = [];
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      currentAbort.current?.abort();
      clearDeferredTimers();
    };
  }, [clearDeferredTimers]);

  const applyCurrent = useCallback((cur: DailyContextCurrent, nextRounds?: CarryoverRound[]) => {
    setCurrent(cur);
    setErrorDetail('');
    if (cur.selection_finalized) {
      setUiState('locked');
      setDraftCount(draftCountFromCurrent(cur));
      setHighlightOverride(cur.selected_message_ids.slice());
      setPickerSuppressed(false);
      setDrawerOpen(false);
      return;
    }
    setHighlightOverride(null);
    if (nextRounds && nextRounds.length === 0) {
      setUiState('empty');
      setDraftCount(10);
      return;
    }
    // Unselected: ready until candidates prove empty (formal fetches on open).
    setUiState(nextRounds && nextRounds.length === 0 ? 'empty' : 'ready');
    setDraftCount(10);
  }, []);

  const probeCurrent = useCallback(
    async (opts?: { fromDeferred?: boolean }) => {
      if (!active) return;
      const gen = ++probeGen.current;
      currentAbort.current?.abort();
      const ctrl = new AbortController();
      currentAbort.current = ctrl;

      if (!opts?.fromDeferred) {
        setUiState((s) => (s === 'locked' || s === 'ready' || s === 'empty' ? s : 'probing'));
      }

      try {
        const cur = await client.getCurrent({ signal: ctrl.signal });
        if (!mounted.current || gen !== probeGen.current) return;
        clearDeferredTimers();
        deferredAttempt.current = 0;

        if (isPreview) {
          // Preview still eagerly loads candidates for playground convenience.
          try {
            const cand = await client.getCandidates();
            if (!mounted.current || gen !== probeGen.current) return;
            setRounds(cand.rounds);
            setCandidates(cand.candidates);
            if (cur.selection_finalized) {
              applyCurrent(cur, cand.rounds);
            } else if (!cand.rounds.length) {
              setCurrent(cur);
              setUiState('empty');
              setDraftCount(10);
            } else {
              applyCurrent(cur, cand.rounds);
            }
          } catch (err) {
            if (!mounted.current || gen !== probeGen.current) return;
            const kind = classifySoftWindowError(err);
            if (kind === 'disabled') {
              setCurrent(null);
              setUiState('disabled');
              setRounds([]);
              setCandidates([]);
              return;
            }
            applyCurrent(cur);
          }
          return;
        }

        // Formal: do NOT fetch candidates until modal open.
        setRounds([]);
        setCandidates([]);
        applyCurrent(cur);
      } catch (err) {
        if (!mounted.current || gen !== probeGen.current) return;
        if (err instanceof DOMException && err.name === 'AbortError') return;
        if (err instanceof Error && err.name === 'AbortError') return;

        const kind = classifySoftWindowError(err);
        if (kind === 'disabled') {
          clearDeferredTimers();
          setCurrent(null);
          setRounds([]);
          setCandidates([]);
          setHighlightOverride(null);
          setDrawerOpen(false);
          setUiState('disabled');
          return;
        }
        if (kind === 'deferred') {
          setCurrent(null);
          setRounds([]);
          setCandidates([]);
          setDrawerOpen(false);
          setUiState('deferred');
          if (deferredAttempt.current < DEFERRED_RETRY_MS.length) {
            const delay = DEFERRED_RETRY_MS[deferredAttempt.current];
            deferredAttempt.current += 1;
            const t = setTimeout(() => {
              void probeCurrent({ fromDeferred: true });
            }, delay);
            deferredTimers.current.push(t);
          }
          return;
        }
        if (kind === 'auth_error') {
          console.error('[AUTH_BRIDGE] daily soft window probe failed', err);
          setCurrent(null);
          setUiState('unavailable');
          setErrorDetail(softWindowErrorMessage('auth_error', err));
          return;
        }
        // 5xx / network / malformed — fail-soft unavailable
        setUiState('unavailable');
        setErrorDetail(softWindowErrorMessage('unavailable', err));
        if (!isPreview) {
          // keep prior current if any; don't wipe picker mid-session on transient errors after ready
        }
      }
    },
    [active, applyCurrent, clearDeferredTimers, client, isPreview],
  );

  const reload = useCallback(async () => {
    deferredAttempt.current = 0;
    clearDeferredTimers();
    await probeCurrent();
  }, [clearDeferredTimers, probeCurrent]);

  useEffect(() => {
    if (!active) return;
    void probeCurrent();
  }, [active, probeCurrent]);

  // Focus revalidation (live formal only)
  useEffect(() => {
    if (!isLive) return;
    const onFocus = () => {
      if (document.visibilityState && document.visibilityState !== 'visible') return;
      // Restart deferred retries on focus after exhaustion
      if (uiState === 'deferred' && deferredAttempt.current >= DEFERRED_RETRY_MS.length) {
        deferredAttempt.current = 0;
      }
      void probeCurrent(
        uiState === 'deferred' ? { fromDeferred: true } : undefined,
      );
    };
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onFocus);
    return () => {
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onFocus);
    };
  }, [isLive, probeCurrent, uiState]);

  const locked = Boolean(current?.selection_finalized) || uiState === 'locked';

  const highlightIds = useMemo(() => {
    if (highlightOverride && locked) return new Set(highlightOverride);
    if (locked && current?.selected_message_ids?.length) {
      return new Set(current.selected_message_ids);
    }
    // Draft preview ids only while modal open before confirm
    if (drawerOpen && !locked && rounds.length) {
      return new Set(flattenRoundMessageIds(pickLastNRounds(rounds, draftCount)));
    }
    return new Set<number>();
  }, [highlightOverride, locked, current, drawerOpen, rounds, draftCount]);

  const openDrawer = useCallback(() => {
    if (locked) return;
    setDrawerOpen(true);
    if (isPreview) return; // preview already has rounds

    void (async () => {
      if (!current) return;
      try {
        const cand = await client.getCandidates();
        if (!mounted.current) return;
        if (
          cand.context_id !== current.context_id ||
          cand.context_epoch !== current.context_epoch
        ) {
          setDrawerOpen(false);
          setRounds([]);
          setCandidates([]);
          await reload();
          return;
        }
        setRounds(cand.rounds);
        setCandidates(cand.candidates);
        if (!cand.rounds.length) setUiState('empty');
        else setUiState('ready');
      } catch (err) {
        if (!mounted.current) return;
        const kind = classifySoftWindowError(err);
        if (kind === 'disabled') {
          setDrawerOpen(false);
          setUiState('disabled');
          setCurrent(null);
          return;
        }
        if (kind === 'deferred') {
          setDrawerOpen(false);
          setUiState('deferred');
          deferredAttempt.current = 0;
          void probeCurrent({ fromDeferred: true });
          return;
        }
        setDrawerOpen(false);
        setErrorDetail(softWindowErrorMessage(kind, err));
        if (kind === 'auth_error') {
          console.error('[AUTH_BRIDGE] carryover-candidates failed', err);
          setUiState('unavailable');
        }
      }
    })();
  }, [client, current, isPreview, locked, probeCurrent, reload]);

  const closeDrawer = useCallback(() => {
    setDrawerOpen(false);
  }, []);

  const confirmSelection = useCallback(async () => {
    if (!active || locked || submitting || submitLock.current) return false;
    submitLock.current = true;
    setSubmitting(true);
    setUiState('submitting');
    setErrorDetail('');
    try {
      const res = await client.selectCarryover(draftCount);
      if (!mounted.current) return true;
      const next: DailyContextCurrent = {
        ...(current ?? {
          context_id: res.context_id,
          context_epoch: res.context_epoch,
          local_day: '',
          boundary_message_id: 0,
          carryover_unit: 'round',
          handoff_status: 'ABSENT',
          resident_generation: 1,
        }),
        context_id: res.context_id,
        context_epoch: res.context_epoch,
        requested_round_count: isCarryoverCount(res.requested_round_count)
          ? res.requested_round_count
          : draftCount,
        selected_round_count: res.selected_round_count,
        selected_message_count: res.selected_message_count,
        selected_message_ids: res.selected_message_ids.slice(),
        carryover_count: res.carryover_count,
        selection_finalized: true,
      };
      setCurrent(next);
      setHighlightOverride(res.selected_message_ids.slice());
      setDraftCount(draftCountFromCurrent(next));
      setUiState('locked');
      setDrawerOpen(false);
      setPickerSuppressed(false);
      return true;
    } catch (err) {
      if (!mounted.current) return false;
      const kind = classifySoftWindowError(err);
      if (kind === 'conflict') {
        // Recover via GET current; no POST retry
        setSubmitting(false);
        setDrawerOpen(false);
        await reload();
        return false;
      }
      if (kind === 'deferred') {
        // Don't lock; end submitting; hide/wait
        setSubmitting(false);
        setDrawerOpen(false);
        setUiState('deferred');
        deferredAttempt.current = 0;
        void probeCurrent({ fromDeferred: true });
        return false;
      }
      if (kind === 'disabled') {
        setSubmitting(false);
        setDrawerOpen(false);
        setCurrent(null);
        setRounds([]);
        setCandidates([]);
        setUiState('disabled');
        return false;
      }
      // 5xx — don't lock locally
      setSubmitting(false);
      setUiState(current?.selection_finalized ? 'locked' : rounds.length ? 'ready' : 'empty');
      setErrorDetail(softWindowErrorMessage(kind, err));
      if (kind === 'auth_error') {
        console.error('[AUTH_BRIDGE] select-carryover failed', err);
        setUiState('unavailable');
      }
      return false;
    } finally {
      submitLock.current = false;
      if (mounted.current) setSubmitting(false);
    }
  }, [active, client, current, draftCount, locked, probeCurrent, reload, rounds.length, submitting]);

  const lockZeroIfNeeded = useCallback(async () => {
    // Preview-only helper
    if (!isPreview) return true;
    if (current?.selection_finalized) return true;
    setDraftCount(0);
    setSubmitting(true);
    try {
      const res = await client.selectCarryover(0);
      if (!mounted.current) return true;
      const next: DailyContextCurrent = {
        ...(current ?? {
          context_id: res.context_id,
          context_epoch: res.context_epoch,
          local_day: '',
          boundary_message_id: 0,
          carryover_unit: 'round',
          handoff_status: 'ABSENT',
          resident_generation: 1,
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
      setCurrent(next);
      setHighlightOverride(res.selected_message_ids.slice());
      setUiState('locked');
      setDrawerOpen(false);
      return true;
    } catch (err) {
      if (!mounted.current) return true;
      const kind = classifySoftWindowError(err);
      if (kind === 'conflict') {
        setUiState('locked');
        setCurrent((prev) => (prev ? { ...prev, selection_finalized: true } : prev));
      } else {
        setUiState(kind === 'disabled' ? 'disabled' : 'error');
        setErrorDetail(softWindowErrorMessage(kind, err));
      }
      return true;
    } finally {
      if (mounted.current) setSubmitting(false);
    }
  }, [client, current, isPreview]);

  const notifySendStarted = useCallback(() => {
    // Close modal; suppress picker temporarily; DON'T local lock; NEVER POST 0
    setDrawerOpen(false);
    setPickerSuppressed(true);
  }, []);

  const notifySendSettled = useCallback(
    (success: boolean) => {
      void (async () => {
        try {
          const cur = await client.getCurrent();
          if (!mounted.current) return;
          if (cur.selection_finalized) {
            applyCurrent(cur);
            setPickerSuppressed(false);
            return;
          }
          // Still unselected
          if (success) {
            // Server should auto-zero on send; if not locked yet, keep suppressed briefly then restore
            applyCurrent(cur);
            setPickerSuppressed(false);
          } else {
            // Restore picker if still unselected
            setPickerSuppressed(false);
            applyCurrent(cur);
          }
        } catch (err) {
          if (!mounted.current) return;
          const kind = classifySoftWindowError(err);
          if (kind === 'disabled') {
            setUiState('disabled');
            setCurrent(null);
            setPickerSuppressed(false);
            return;
          }
          if (!success) setPickerSuppressed(false);
          if (err instanceof HttpError && (err.status === 401 || err.status === 403)) {
            console.error('[AUTH_BRIDGE] notifySendSettled probe failed', err);
          }
        }
      })();
    },
    [applyCurrent, client],
  );

  const statusText = useMemo(() => {
    if (!active) return '';
    if (uiState === 'probing' || uiState === 'loading') return '加载中…';
    if (uiState === 'disabled') return isPreview ? '接口未启用 · mock 可见' : '';
    if (uiState === 'deferred') return '日界换窗中…';
    if (uiState === 'conflict') return '选择已锁定';
    if (uiState === 'unavailable' || uiState === 'error' || uiState === 'auth_error') {
      return errorDetail || '出错了';
    }
    if (uiState === 'empty') return '昨天暂无可带走的对话轮';
    if (uiState === 'submitting') return '换窗中…';
    if (locked) return `已锁定 · ${current?.selected_round_count ?? current?.carryover_count ?? 0} 轮`;
    if (client.mode === 'mock') return '预览 · mock · 按完整对话轮';
    return rounds.length ? `候选 ${rounds.length} 轮` : '';
  }, [
    active,
    uiState,
    isPreview,
    errorDetail,
    locked,
    current?.selected_round_count,
    current?.carryover_count,
    client.mode,
    rounds.length,
  ]);

  const showPickerCard =
    active &&
    uiState !== 'disabled' &&
    uiState !== 'deferred' &&
    uiState !== 'unavailable' &&
    uiState !== 'probing' &&
    uiState !== 'idle' &&
    (locked || ((uiState === 'ready' || uiState === 'empty' || uiState === 'submitting') && !pickerSuppressed));

  // Preview still shows card during loading/error for playground
  const showPickerCardFinal = isPreview
    ? active && uiState !== 'idle' && uiState !== 'disabled'
    : showPickerCard;

  const boundaryMessageId = current?.boundary_message_id ?? 0;
  const showBoundary =
    isLive &&
    uiState !== 'disabled' &&
    uiState !== 'unavailable' &&
    uiState !== 'deferred' &&
    boundaryMessageId > 0;

  return {
    enabled: active,
    live: isLive,
    mode: client.mode,
    uiState,
    current,
    summary: current,
    candidates,
    rounds,
    draftCount,
    highlightIds,
    drawerOpen,
    submitting,
    statusText,
    errorDetail,
    locked,
    showPickerCard: showPickerCardFinal,
    showBoundary,
    boundaryMessageId,
    openDrawer,
    closeDrawer,
    setDraftCount,
    confirmSelection,
    lockZeroIfNeeded,
    notifySendStarted,
    notifySendSettled,
    reload,
  };
}
