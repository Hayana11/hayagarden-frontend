import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  createDailySoftWindowClient,
  type CarryoverCount,
  type CarryoverMessage,
  type CarryoverRound,
  type DailyContextCurrent,
  type SoftWindowUiState,
} from '../lib/dailySoftWindow';
import {
  DailySoftWindowController,
  type FocusableOpener,
  type SoftWindowControllerSnapshot,
} from '../lib/dailySoftWindowController';

type Options = {
  forceMock?: boolean;
  search?: string;
  /** Formal chat: always probe live BFF (404 = hide). */
  live?: boolean;
};

export type DailySoftWindowControllerApi = {
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
  openDrawer: (opener?: FocusableOpener | null) => void;
  closeDrawer: () => void;
  setDraftCount: (count: CarryoverCount) => void;
  confirmSelection: () => Promise<boolean>;
  /** Preview-only: simulate first send locking 0. Formal chat must NEVER POST 0 before send. */
  lockZeroIfNeeded: () => Promise<boolean>;
  notifySendStarted: () => void;
  notifySendSettled: (success: boolean) => void;
  reload: () => Promise<void>;
};

export function useDailySoftWindow(opts: Options = {}): DailySoftWindowControllerApi {
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

  const controller = useMemo(
    () =>
      new DailySoftWindowController({
        client,
        live: isLive,
        preview: isPreview,
      }),
    [client, isLive, isPreview],
  );

  const [snap, setSnap] = useState<SoftWindowControllerSnapshot>(() => controller.getSnapshot());

  useEffect(() => {
    setSnap(controller.getSnapshot());
    const unsub = controller.subscribe(() => {
      setSnap(controller.getSnapshot());
    });
    if (active) controller.start();
    return () => {
      unsub();
      controller.dispose();
    };
  }, [active, controller]);

  useEffect(() => {
    if (!isLive) return;
    const onFocus = () => controller.onWindowFocus();
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onFocus);
    return () => {
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onFocus);
    };
  }, [isLive, controller]);

  const openDrawer = useCallback(
    (opener?: FocusableOpener | null) => {
      controller.openDrawer(opener);
    },
    [controller],
  );
  const closeDrawer = useCallback(() => {
    controller.closeDrawer();
  }, [controller]);
  const setDraftCount = useCallback(
    (count: CarryoverCount) => {
      controller.setDraftCount(count);
    },
    [controller],
  );
  const confirmSelection = useCallback(() => controller.confirmSelection(), [controller]);
  const lockZeroIfNeeded = useCallback(() => controller.lockZeroIfNeeded(), [controller]);
  const notifySendStarted = useCallback(() => {
    controller.notifySendStarted();
  }, [controller]);
  const notifySendSettled = useCallback(
    (success: boolean) => {
      controller.notifySendSettled(success);
    },
    [controller],
  );
  const reload = useCallback(() => controller.reload(), [controller]);

  return {
    enabled: active,
    live: isLive,
    mode: client.mode,
    uiState: snap.uiState,
    current: snap.current,
    summary: snap.current,
    candidates: snap.candidates,
    rounds: snap.rounds,
    draftCount: snap.draftCount,
    highlightIds: new Set(snap.highlightIds),
    drawerOpen: snap.drawerOpen,
    submitting: snap.submitting,
    statusText: snap.statusText,
    errorDetail: snap.errorDetail,
    locked: snap.locked,
    showPickerCard: snap.showPickerCard,
    showBoundary: snap.showBoundary,
    boundaryMessageId: snap.boundaryMessageId,
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
