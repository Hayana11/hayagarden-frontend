import { RealityStore } from "./realityStore";
import type { RealitySnapshot } from "./realityStore";
import { REALITY_BACKGROUND_POLL_MS } from "./realityStore";

export const PHYSICAL_POLL_MS = 500;
export const PHYSICAL_BACKGROUND_POLL_MS = REALITY_BACKGROUND_POLL_MS;

export interface ElpisPhysicalBridge {
  getPhysicalState?: () => unknown;
}

export interface ElpisActivityBridge {
  getActivityState?: () => unknown;
}

export type RealityLifecycleEvent =
  | "visibilitychange"
  | "pagehide"
  | "pageshow";

export interface RealityRuntimeEnvironment {
  now: () => number;
  getBridge: () => ElpisPhysicalBridge | undefined;
  getActivityBridge?: () => ElpisActivityBridge | undefined;
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
  private activePollIntervalMs: number | null = null;
  private listenersAttached = false;
  private readonly store: RealityStore;
  private readonly environment: RealityRuntimeEnvironment;

  private readonly onVisibilityChange = (): void => {
    if (!this.started) {
      return;
    }

    if (this.environment.isVisible()) {
      this.refresh();
      this.startPolling(PHYSICAL_POLL_MS);
      return;
    }

    this.refresh();
    this.startPolling(REALITY_BACKGROUND_POLL_MS);
  };

  private readonly onPageShow = (): void => {
    if (this.started && this.environment.isVisible()) {
      this.refresh();
      this.startPolling(PHYSICAL_POLL_MS);
    }
  };

  private readonly onPageHide = (): void => {
    if (!this.started) {
      return;
    }

    this.refresh();
    this.startPolling(REALITY_BACKGROUND_POLL_MS);
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
      this.startPolling(PHYSICAL_POLL_MS);
      return;
    }

    this.refresh();
    this.startPolling(REALITY_BACKGROUND_POLL_MS);
  }

  stop(): void {
    this.started = false;
    this.stopPolling();
    this.detachListeners();
    this.resetIfNeeded();
    this.store.resetActivity();
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

  private startPolling(intervalMs: number): void {
    if (
      this.intervalHandle !== null &&
      this.activePollIntervalMs === intervalMs
    ) {
      return;
    }

    this.stopPolling();
    this.intervalHandle = this.environment.setInterval(
      () => this.refresh(),
      intervalMs,
    );
    this.activePollIntervalMs = intervalMs;
  }

  private stopPolling(): void {
    if (this.intervalHandle === null) {
      return;
    }

    this.environment.clearInterval(this.intervalHandle);
    this.intervalHandle = null;
    this.activePollIntervalMs = null;
  }

  private refresh(): void {
    this.refreshPhysical();
    this.refreshActivity();
  }

  private refreshPhysical(): void {
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

  private refreshActivity(): void {
    if (typeof this.environment.getActivityBridge !== "function") {
      this.store.resetActivity();
      return;
    }

    let bridge: ElpisActivityBridge | undefined;
    try {
      bridge = this.environment.getActivityBridge();
    } catch {
      this.store.resetActivity();
      return;
    }

    if (!bridge || typeof bridge.getActivityState !== "function") {
      this.store.resetActivity();
      return;
    }

    let result: unknown;
    try {
      result = bridge.getActivityState();
    } catch {
      this.store.resetActivity();
      return;
    }

    if (typeof result !== "string") {
      this.store.resetActivity();
      return;
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(result);
    } catch {
      this.store.resetActivity();
      return;
    }

    if (!isSchemaV1Object(parsed)) {
      this.store.resetActivity();
      return;
    }

    this.store.ingestActivity(parsed);
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
    getActivityBridge: () =>
      typeof window === "undefined" ? undefined : window.ElpisActivity,
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
    ElpisActivity?: ElpisActivityBridge;
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
