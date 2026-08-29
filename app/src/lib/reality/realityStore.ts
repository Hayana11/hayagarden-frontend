import { interpretPhysicalState } from "./physicalInterpretation";
import type { PhysicalDeviceFacts } from "./physicalInterpretation";
import { PhysicalMotionWindow } from "./physicalMotion";
import type { PhysicalMotion } from "./physicalMotion";

export const REALITY_BACKGROUND_POLL_MS = 5 * 60 * 1000;
export const REALITY_ASSISTANT_MAX_AGE_MS = 2 * REALITY_BACKGROUND_POLL_MS;
export const REALITY_ACTIVITY_MAX_AGE_MS = 3 * 60 * 1000;

export type RealityFreshness = "fresh" | "stale" | "unknown";
export type UserActivity =
  | "still"
  | "walking"
  | "running"
  | "cycling"
  | "in_vehicle"
  | "unknown";
export type RealityActivitySource = "hms" | "none";

export interface RealityFreshnessResult {
  status: RealityFreshness;
  ageMs: number | null;
  observedAt: number | null;
}

export interface RealityActivityState {
  raw: unknown | null;
  userActivity: UserActivity;
  possibility: number | null;
  activitySampledAt: number | null;
  source: RealityActivitySource;
  registration: string;
  lastErrorCode: string | null;
  callbackReceived: boolean;
  intentHasExtras: boolean;
  responsePresent: boolean;
  activityDataCount: number;
  rawCandidate: number | null;
  rawPossibility: number | null;
}

export interface RealitySnapshot {
  schemaVersion: 1;
  physical: {
    raw: unknown | null;
    facts: PhysicalDeviceFacts;
    motion: PhysicalMotion;
    observedAt: number | null;
  };
  activity: RealityActivityState;
}

export function getRealityFreshness(
  snapshot: RealitySnapshot,
  nowMs: number,
  maxAgeMs: number = REALITY_ASSISTANT_MAX_AGE_MS,
): RealityFreshnessResult {
  const observedAt = snapshot.physical.observedAt;
  if (
    observedAt === null ||
    !Number.isFinite(observedAt) ||
    !Number.isFinite(nowMs) ||
    !Number.isFinite(maxAgeMs) ||
    maxAgeMs < 0
  ) {
    return { status: "unknown", ageMs: null, observedAt };
  }

  const ageMs = nowMs - observedAt;
  if (ageMs < 0) {
    return { status: "unknown", ageMs: null, observedAt };
  }

  return {
    status: ageMs <= maxAgeMs ? "fresh" : "stale",
    ageMs,
    observedAt,
  };
}

export function getActivityFreshness(
  snapshot: RealitySnapshot,
  nowMs: number,
  maxAgeMs: number = REALITY_ACTIVITY_MAX_AGE_MS,
): RealityFreshnessResult {
  const observedAt = snapshot.activity?.activitySampledAt ?? null;
  if (
    observedAt === null ||
    !Number.isFinite(observedAt) ||
    !Number.isFinite(nowMs) ||
    !Number.isFinite(maxAgeMs) ||
    maxAgeMs < 0
  ) {
    return { status: "unknown", ageMs: null, observedAt };
  }

  const ageMs = nowMs - observedAt;
  if (ageMs < 0) {
    return { status: "unknown", ageMs: null, observedAt };
  }

  return {
    status: ageMs <= maxAgeMs ? "fresh" : "stale",
    ageMs,
    observedAt,
  };
}

export function getFreshUserActivity(
  snapshot: RealitySnapshot,
  nowMs: number,
  maxAgeMs: number = REALITY_ACTIVITY_MAX_AGE_MS,
): Exclude<UserActivity, "unknown"> | null {
  const activity = snapshot.activity;
  if (
    !activity ||
    activity.source !== "hms" ||
    activity.userActivity === "unknown" ||
    getActivityFreshness(snapshot, nowMs, maxAgeMs).status !== "fresh"
  ) {
    return null;
  }

  return activity.userActivity;
}

/**
 * Assistant/Wake callers must use this gate instead of treating a stored value
 * as current solely because it exists. A stale snapshot is deliberately absent.
 */
export function getAssistantRealitySnapshot(
  snapshot: RealitySnapshot,
  nowMs: number,
  maxAgeMs: number = REALITY_ASSISTANT_MAX_AGE_MS,
): RealitySnapshot | null {
  return getRealityFreshness(snapshot, nowMs, maxAgeMs).status === "fresh"
    ? snapshot
    : null;
}

function unknownFacts(): PhysicalDeviceFacts {
  return {
    schemaVersion: 1,
    orientation: "unknown",
    proximity: "unknown",
    lightExposure: "unknown",
    charging: null,
    batteryLevel: null,
  };
}

function unknownActivity(): RealityActivityState {
  return {
    raw: null,
    userActivity: "unknown",
    possibility: null,
    activitySampledAt: null,
    source: "none",
    registration: "unknown",
    lastErrorCode: null,
    callbackReceived: false,
    intentHasExtras: false,
    responsePresent: false,
    activityDataCount: 0,
    rawCandidate: null,
    rawPossibility: null,
  };
}

function initialSnapshot(): RealitySnapshot {
  return {
    schemaVersion: 1,
    physical: {
      raw: null,
      facts: unknownFacts(),
      motion: "unknown",
      observedAt: null,
    },
    activity: unknownActivity(),
  };
}

function isUserActivity(value: unknown): value is UserActivity {
  return (
    value === "still" ||
    value === "walking" ||
    value === "running" ||
    value === "cycling" ||
    value === "in_vehicle" ||
    value === "unknown"
  );
}

function finiteIntegerOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function normalizeActivity(raw: unknown): RealityActivityState {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return unknownActivity();
  }

  const value = raw as Record<string, unknown>;
  const sampledAt =
    typeof value.activitySampledAt === "number" &&
    Number.isFinite(value.activitySampledAt) &&
    value.activitySampledAt >= 0
      ? value.activitySampledAt
      : null;
  const possibility = finiteIntegerOrNull(value.activityPossibility);
  const rawPossibility = finiteIntegerOrNull(value.rawPossibility);
  const rawCandidate = finiteIntegerOrNull(value.rawCandidate);
  const activityDataCount = finiteIntegerOrNull(value.activityDataCount);

  return {
    raw,
    userActivity: isUserActivity(value.userActivity)
      ? value.userActivity
      : "unknown",
    possibility,
    activitySampledAt: sampledAt,
    source: value.source === "hms" ? "hms" : "none",
    registration:
      typeof value.registration === "string" ? value.registration : "unknown",
    lastErrorCode:
      typeof value.lastErrorCode === "string" ? value.lastErrorCode : null,
    callbackReceived: value.callbackReceived === true,
    intentHasExtras: value.intentHasExtras === true,
    responsePresent: value.responsePresent === true,
    activityDataCount:
      activityDataCount !== null && activityDataCount >= 0
        ? activityDataCount
        : 0,
    rawCandidate,
    rawPossibility,
  };
}

function sameActivity(
  left: RealityActivityState,
  right: RealityActivityState,
): boolean {
  return (
    left.userActivity === right.userActivity &&
    left.possibility === right.possibility &&
    left.activitySampledAt === right.activitySampledAt &&
    left.source === right.source &&
    left.registration === right.registration &&
    left.lastErrorCode === right.lastErrorCode &&
    left.callbackReceived === right.callbackReceived &&
    left.intentHasExtras === right.intentHasExtras &&
    left.responsePresent === right.responsePresent &&
    left.activityDataCount === right.activityDataCount &&
    left.rawCandidate === right.rawCandidate &&
    left.rawPossibility === right.rawPossibility
  );
}

export class RealityStore {
  private readonly motionWindow = new PhysicalMotionWindow();
  private readonly listeners = new Set<() => void>();
  private snapshot = initialSnapshot();

  ingestPhysical(raw: unknown, nowMs: number): RealitySnapshot {
    if (!Number.isFinite(nowMs)) {
      return this.resetPhysical();
    }

    const facts = interpretPhysicalState(raw, nowMs);
    const motion = this.motionWindow.push(raw, nowMs);

    this.snapshot = {
      schemaVersion: 1,
      physical: {
        raw,
        facts,
        motion,
        observedAt: nowMs,
      },
      activity: this.snapshot.activity,
    };
    this.notify();
    return this.snapshot;
  }

  ingestActivity(raw: unknown): RealitySnapshot {
    const activity = normalizeActivity(raw);
    this.snapshot = {
      ...this.snapshot,
      activity,
    };
    // Runtime polling must also refresh the prompt when activitySampledAt
    // crosses the TTL while the native values themselves remain unchanged.
    this.notify();
    return this.snapshot;
  }

  resetPhysical(): RealitySnapshot {
    this.motionWindow.reset();
    const activity = this.snapshot.activity;
    this.snapshot = {
      schemaVersion: 1,
      physical: {
        raw: null,
        facts: unknownFacts(),
        motion: "unknown",
        observedAt: null,
      },
      activity,
    };
    this.notify();
    return this.snapshot;
  }

  resetActivity(): RealitySnapshot {
    if (sameActivity(this.snapshot.activity, unknownActivity())) {
      return this.snapshot;
    }

    this.snapshot = {
      ...this.snapshot,
      activity: unknownActivity(),
    };
    this.notify();
    return this.snapshot;
  }

  getSnapshot(): RealitySnapshot {
    return this.snapshot;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    let subscribed = true;

    return () => {
      if (subscribed) {
        subscribed = false;
        this.listeners.delete(listener);
      }
    };
  }

  private notify(): void {
    for (const listener of [...this.listeners]) {
      try {
        listener();
      } catch {
        // A subscriber failure must not affect the authoritative snapshot.
      }
    }
  }
}
