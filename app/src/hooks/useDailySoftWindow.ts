import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  classifySoftWindowError,
  createDailySoftWindowClient,
  groupIntoRounds,
  isCarryoverCount,
  isDailySoftWindowFeEnabled,
  selectedRoundMessageIds,
  softWindowErrorMessage,
  type CarryoverCandidate,
  type CarryoverCount,
  type CarryoverRound,
  type DailyContextSummary,
  type SoftWindowUiState,
} from '../lib/dailySoftWindow';

type Options = {
  /** Force enable (preview page only). Chat must not pass true until R1.1. */
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
  rounds: CarryoverRound[];
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
  /** Preview-only: simulate first send locking 0. Not used by formal chat. */
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
  const [rounds, setRounds] = useState<CarryoverRound[]>([]);
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
      const nextCandidates = cand.candidates || [];
      const nextRounds = cand.rounds?.length ? cand.rounds : groupIntoRounds(nextCandidates);
      setCandidates(nextCandidates);
      setRounds(nextRounds);
      if (cur.selection_finalized) {
        setUiState('locked');
        if (isCarryoverCount(cur.carryover_count)) setDraftCount(cur.carryover_count);
      } else if (!nextRounds.length) {
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
      const n = isCarryoverCount(summary.carryover_count) ? summary.carryover_count : 0;
      return new Set(selectedRoundMessageIds(rounds, n));
    }
    return new Set(selectedRoundMessageIds(rounds, draftCount));
  }, [locked, summary, rounds, draftCount]);

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
      // Preview send must not hang Soft Window state — fail open after surfacing.
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
    if (uiState === 'disabled') return '接口未启用 · mock 可见';
    if (uiState === 'conflict') return '选择已锁定';
    if (uiState === 'error') return errorDetail || '出错了';
    if (uiState === 'empty') return '昨天暂无可带走的对话轮';
    if (locked) return `已锁定 · ${summary?.carryover_count ?? 0} 轮`;
    if (client.mode === 'mock') return '预览 · mock · 按完整对话轮';
    return `候选 ${rounds.length} 轮`;
  }, [enabled, uiState, errorDetail, locked, summary?.carryover_count, client.mode, rounds.length]);

  // Preview-only card. Formal chat must not mount this hook with enabled=true yet.
  const showPickerCard = enabled && uiState !== 'idle';

  return {
    enabled,
    mode: client.mode,
    uiState,
    summary,
    candidates,
    rounds,
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
