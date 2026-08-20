import { interpretPhysicalState } from "./physicalInterpretation";
import type { PhysicalDeviceFacts } from "./physicalInterpretation";
import { PhysicalMotionWindow } from "./physicalMotion";
import type { PhysicalMotion } from "./physicalMotion";

export interface RealitySnapshot {
  schemaVersion: 1;
  physical: {
    raw: unknown | null;
    facts: PhysicalDeviceFacts;
    motion: PhysicalMotion;
    observedAt: number | null;
  };
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

function initialSnapshot(): RealitySnapshot {
  return {
    schemaVersion: 1,
    physical: {
      raw: null,
      facts: unknownFacts(),
      motion: "unknown",
      observedAt: null,
    },
  };
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
    };
    this.notify();
    return this.snapshot;
  }

  resetPhysical(): RealitySnapshot {
    this.motionWindow.reset();
    this.snapshot = initialSnapshot();
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
