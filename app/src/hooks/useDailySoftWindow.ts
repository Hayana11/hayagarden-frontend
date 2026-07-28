import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  classifySoftWindowError,
  createDailySoftWindowClient,
  isCarryoverCount,
  isDailySoftWindowFeEnabled,
  pickLastNCandidates,
  selectedMessageIds,
  softWindowErrorMessage,
  type CarryoverCandidate,
  type CarryoverCount,
  type DailyContextSummary,
  type SoftWindowUiState,
} from '../lib/dailySoftWindow';

type Options = {
  /** Force enable (preview page). Default reads URL/localStorage gate. */
  enabled?: boolean;
  forceMock?: boolean;
  search?: string;
};

export type DailySoftWindowController = {
  enabled: boolean;
  mode: 'mock' | 'live';
  uiState: SoftWindowUiState;
  summary: DailyContextSummary | null;
  candidates: CarryoverCandidate[];
  draftCount: CarryoverCount;
  highlightIds: Set<number>;
  drawerOpen: boolean;
  submitting: boolean;
  statusText: string;
  errorDetail: string;
  locked: boolean;
  showPickerCard: boolean;
  openDrawer: () => void;
  closeDrawer: () => void;
  setDraftCount: (count: CarryoverCount) => void;
  confirmSelection: () => Promise<boolean>;
  /** Call before/when first user message of the day is sent without an explicit pick. */
  lockZeroIfNeeded: () => Promise<boolean>;
  reload: () => Promise<void>;
};

export function useDailySoftWindow(opts: Options = {}): DailySoftWindowController {
  const search = opts.search ?? (typeof location !== 'undefined' ? location.search : '');
  const enabled = opts.enabled ?? isDailySoftWindowFeEnabled(search);
  const client = useMemo(
    () => createDailySoftWindowClient({ search, forceMock: opts.forceMock }),
    [search, opts.forceMock],
  );

  const [uiState, setUiState] = useState<SoftWindowUiState>('idle');
  const [summary, setSummary] = useState<DailyContextSummary | null>(null);
  const [candidates, setCandidates] = useState<CarryoverCandidate[]>([]);
  const [draftCount, setDraftCount] = useState<CarryoverCount>(3);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [errorDetail, setErrorDetail] = useState('');
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const reload = useCallback(async () => {
    if (!enabled) return;
    setUiState('loading');
    setErrorDetail('');
    try {
      const [cur, cand] = await Promise.all([client.getCurrent(), client.getCandidates()]);
      if (!mounted.current) return;
      setSummary(cur);
      setCandidates(cand.candidates || []);
      if (cur.selection_finalized) {
        setUiState('locked');
        if (isCarryoverCount(cur.carryover_count)) setDraftCount(cur.carryover_count);
      } else if (!(cand.candidates || []).length) {
        setUiState('empty');
      } else {
        setUiState('ready');
      }
    } catch (err) {
      if (!mounted.current) return;
      const state = classifySoftWindowError(err);
      setUiState(state);
      setErrorDetail(softWindowErrorMessage(state, err));
    }
  }, [client, enabled]);

  useEffect(() => {
    if (!enabled) return;
    void reload();
  }, [enabled, reload]);

  const locked = Boolean(summary?.selection_finalized) || uiState === 'locked';

  const highlightIds = useMemo(() => {
    if (locked && summary) {
      // After lock, highlight the finalized selection from candidates when possible.
      const ids = pickLastNCandidates(
        candidates,
        isCarryoverCount(summary.carryover_count) ? summary.carryover_count : 0,
      ).map((c) => c.message_id);
      return new Set(ids);
    }
    return new Set(selectedMessageIds(candidates, draftCount));
  }, [locked, summary, candidates, draftCount]);

  const confirmSelection = useCallback(async () => {
    if (!enabled || locked) return false;
    setSubmitting(true);
    setErrorDetail('');
    try {
      const res = await client.selectCarryover(draftCount);
      if (!mounted.current) return true;
      setSummary((prev) =>
        prev
          ? {
              ...prev,
              selection_finalized: true,
              carryover_count: res.carryover_count,
              context_epoch: res.context_epoch,
            }
          : prev,
      );
      setUiState('locked');
      setDrawerOpen(false);
      return true;
    } catch (err) {
      if (!mounted.current) return false;
      const state = classifySoftWindowError(err);
      setUiState(state);
      setErrorDetail(softWindowErrorMessage(state, err));
      return false;
    } finally {
      if (mounted.current) setSubmitting(false);
    }
  }, [client, draftCount, enabled, locked]);

  const lockZeroIfNeeded = useCallback(async () => {
    if (!enabled) return true;
    if (summary?.selection_finalized) return true;
    setDraftCount(0);
    setSubmitting(true);
    try {
      const res = await client.selectCarryover(0);
      if (!mounted.current) return true;
      setSummary((prev) =>
        prev
          ? {
              ...prev,
              selection_finalized: true,
              carryover_count: res.carryover_count,
              context_epoch: res.context_epoch,
            }
          : {
              local_day: '',
              context_epoch: res.context_epoch,
              status: 'PROVISIONAL',
              boundary_message_id: 0,
              carryover_count: 0,
              selection_finalized: true,
              context_id: 0,
            },
      );
      setUiState('locked');
      setDrawerOpen(false);
      return true;
    } catch (err) {
      // Sending must not be blocked by Soft Window FE — fail open after surfacing state.
      if (!mounted.current) return true;
      const state = classifySoftWindowError(err);
      if (state === 'conflict') {
        setUiState('locked');
        setSummary((prev) => (prev ? { ...prev, selection_finalized: true } : prev));
      } else {
        setUiState(state);
        setErrorDetail(softWindowErrorMessage(state, err));
      }
      return true;
    } finally {
      if (mounted.current) setSubmitting(false);
    }
  }, [client, enabled, summary?.selection_finalized]);

  const statusText = useMemo(() => {
    if (!enabled) return '';
    if (uiState === 'loading') return '加载中…';
    if (uiState === 'disabled') return '接口未启用 · mock/flag 可见';
    if (uiState === 'conflict') return '选择已锁定';
    if (uiState === 'error') return errorDetail || '出错了';
    if (uiState === 'empty') return '昨天暂无可带走的句子';
    if (locked) return `已锁定 · ${summary?.carryover_count ?? 0} 条`;
    if (client.mode === 'mock') return '开发预览 · mock API';
    return `候选 ${candidates.length} 条`;
  }, [enabled, uiState, errorDetail, locked, summary?.carryover_count, client.mode, candidates.length]);

  const showPickerCard =
    enabled &&
    uiState !== 'idle' &&
    uiState !== 'disabled' &&
    // Still show locked readonly card; hide only when fully unavailable.
    (uiState === 'ready' ||
      uiState === 'empty' ||
      uiState === 'loading' ||
      uiState === 'locked' ||
      uiState === 'conflict' ||
      uiState === 'error');

  return {
    enabled,
    mode: client.mode,
    uiState,
    summary,
    candidates,
    draftCount,
    highlightIds,
    drawerOpen,
    submitting,
    statusText,
    errorDetail,
    locked,
    showPickerCard,
    openDrawer: () => setDrawerOpen(true),
    closeDrawer: () => setDrawerOpen(false),
    setDraftCount,
    confirmSelection,
    lockZeroIfNeeded,
    reload,
  };
}
