import { RealityStore } from "./realityStore";
import type { RealitySnapshot } from "./realityStore";

export const PHYSICAL_POLL_MS = 500;

export interface ElpisPhysicalBridge {
  getPhysicalState?: () => unknown;
}

export type RealityLifecycleEvent =
  | "visibilitychange"
  | "pagehide"
  | "pageshow";

export interface RealityRuntimeEnvironment {
  now: () => number;
  getBridge: () => ElpisPhysicalBridge | undefined;
  isVisible: () => boolean;
  setInterval: (callback: () => void, intervalMs: number) => unknown;
  clearInterval: (handle: unknown) => void;
  addEventListener: (
    event: RealityLifecycleEvent,
    listener: () => void,
  ) => void;
  removeEventListener: (
    event: RealityLifecycleEvent,
    listener: () => void,
  ) => void;
}

function isSchemaV1Object(value: unknown): value is Record<string, unknown> {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    (value as { schemaVersion?: unknown }).schemaVersion === 1
  );
}

export class PhysicalRealityRuntime {
  private started = false;
  private intervalHandle: unknown = null;
  private listenersAttached = false;
  private readonly store: RealityStore;
  private readonly environment: RealityRuntimeEnvironment;

  private readonly onVisibilityChange = (): void => {
    if (!this.started) {
      return;
    }

    if (this.environment.isVisible()) {
      this.refresh();
      this.startPolling();
      return;
    }

    this.stopPolling();
    this.resetIfNeeded();
  };

  private readonly onPageShow = (): void => {
    if (this.started && this.environment.isVisible()) {
      this.refresh();
      this.startPolling();
    }
  };

  private readonly onPageHide = (): void => {
    if (!this.started) {
      return;
    }

    this.stopPolling();
    this.resetIfNeeded();
  };

  constructor(
    store: RealityStore,
    environment: RealityRuntimeEnvironment,
  ) {
    this.store = store;
    this.environment = environment;
  }

  start(): void {
    if (this.started) {
      return;
    }

    this.started = true;
    this.attachListeners();

    if (this.environment.isVisible()) {
      this.refresh();
      this.startPolling();
      return;
    }

    this.resetIfNeeded();
  }

  stop(): void {
    this.started = false;
    this.stopPolling();
    this.detachListeners();
    this.resetIfNeeded();
  }

  private attachListeners(): void {
    if (this.listenersAttached) {
      return;
    }

    this.environment.addEventListener(
      "visibilitychange",
      this.onVisibilityChange,
    );
    this.environment.addEventListener("pagehide", this.onPageHide);
    this.environment.addEventListener("pageshow", this.onPageShow);
    this.listenersAttached = true;
  }

  private detachListeners(): void {
    if (!this.listenersAttached) {
      return;
    }

    this.environment.removeEventListener(
      "visibilitychange",
      this.onVisibilityChange,
    );
    this.environment.removeEventListener("pagehide", this.onPageHide);
    this.environment.removeEventListener("pageshow", this.onPageShow);
    this.listenersAttached = false;
  }

  private startPolling(): void {
    if (this.intervalHandle !== null) {
      return;
    }

    this.intervalHandle = this.environment.setInterval(
      () => this.refresh(),
      PHYSICAL_POLL_MS,
    );
  }

  private stopPolling(): void {
    if (this.intervalHandle === null) {
      return;
    }

    this.environment.clearInterval(this.intervalHandle);
    this.intervalHandle = null;
  }

  private refresh(): void {
    let bridge: ElpisPhysicalBridge | undefined;
    try {
      bridge = this.environment.getBridge();
    } catch {
      this.resetIfNeeded();
      return;
    }

    if (!bridge || typeof bridge.getPhysicalState !== "function") {
      this.resetIfNeeded();
      return;
    }

    let result: unknown;
    try {
      result = bridge.getPhysicalState();
    } catch {
      this.resetIfNeeded();
      return;
    }

    if (typeof result !== "string") {
      this.resetIfNeeded();
      return;
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(result);
    } catch {
      this.resetIfNeeded();
      return;
    }

    if (!isSchemaV1Object(parsed)) {
      this.resetIfNeeded();
      return;
    }

    this.store.ingestPhysical(parsed, this.environment.now());
  }

  private resetIfNeeded(): void {
    const snapshot: RealitySnapshot = this.store.getSnapshot();
    const physical = snapshot.physical;
    const facts = physical.facts;

    if (
      physical.raw !== null ||
      physical.motion !== "unknown" ||
      physical.observedAt !== null ||
      facts.orientation !== "unknown" ||
      facts.proximity !== "unknown" ||
      facts.lightExposure !== "unknown" ||
      facts.charging !== null ||
      facts.batteryLevel !== null
    ) {
      this.store.resetPhysical();
    }
  }
}

function createProductionEnvironment(): RealityRuntimeEnvironment {
  return {
    now: () => Date.now(),
    getBridge: () =>
      typeof window === "undefined" ? undefined : window.ElpisPhysical,
    isVisible: () =>
      typeof document !== "undefined" &&
      document.visibilityState === "visible",
    setInterval: (callback, intervalMs) =>
      typeof window === "undefined"
        ? null
        : window.setInterval(callback, intervalMs),
    clearInterval: (handle) => {
      if (typeof window !== "undefined") {
        window.clearInterval(handle as number);
      }
    },
    addEventListener: (event, listener) => {
      if (typeof window !== "undefined") {
        window.addEventListener(event, listener as EventListener);
      }
    },
    removeEventListener: (event, listener) => {
      if (typeof window !== "undefined") {
        window.removeEventListener(event, listener as EventListener);
      }
    },
  };
}

declare global {
  interface Window {
    ElpisPhysical?: ElpisPhysicalBridge;
  }
}

export const realityStore = new RealityStore();

const productionRuntime = new PhysicalRealityRuntime(
  realityStore,
  createProductionEnvironment(),
);

export function startRealityRuntime(): void {
  productionRuntime.start();
}

export function stopRealityRuntime(): void {
  productionRuntime.stop();
}
