import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ManualContextWindowController,
  type ManualWindowSnapshot,
} from '../lib/manualContextWindowController';
import {
  createManualContextWindowClient,
  type CarryoverCount,
  type ManualWindowUiState,
} from '../lib/manualContextWindow';

export type ManualContextWindowApi = ManualWindowSnapshot & {
  openModal: () => void;
  closeModal: () => void;
  setDraftCount: (count: CarryoverCount) => void;
  confirmSwitch: () => Promise<boolean>;
};

export type UseManualContextWindowOptions = {
  /** Disable switch button when chat is replying or gen lock is active. */
  switchBlocked?: boolean;
};

export function useManualContextWindow(
  opts: UseManualContextWindowOptions = {},
): ManualContextWindowApi {
  const switchBlocked = Boolean(opts.switchBlocked);
  const client = useMemo(() => createManualContextWindowClient(), []);
  const controller = useMemo(() => new ManualContextWindowController({ client }), [client]);
  const [snap, setSnap] = useState<ManualWindowSnapshot>(() => controller.getSnapshot());

  useEffect(() => {
    setSnap(controller.getSnapshot());
    const unsub = controller.subscribe(() => setSnap(controller.getSnapshot()));
    void controller.probeEnabled();
    return () => {
      unsub();
      controller.dispose();
    };
  }, [controller]);

  const openModal = useCallback(() => {
    if (switchBlocked) return;
    void controller.openModal();
  }, [controller, switchBlocked]);

  const closeModal = useCallback(() => {
    controller.closeModal();
  }, [controller]);

  const setDraftCount = useCallback(
    (count: CarryoverCount) => {
      controller.setDraftCount(count);
    },
    [controller],
  );

  const confirmSwitch = useCallback(() => controller.confirmSwitch(), [controller]);

  return {
    ...snap,
    openModal,
    closeModal,
    setDraftCount,
    confirmSwitch,
  };
}

export type { CarryoverCount, ManualWindowUiState };
