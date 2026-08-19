/**
 * Pure bounded temporal interpretation of recent ElpisPhysical samples.
 * This describes phone device motion only; it does not infer user activity.
 */

export type PhysicalMotion = "moving" | "still" | "unknown";

export const SAMPLE_MAX_AGE_MS = 2_000;
export const WINDOW_MS = 3_000;
export const MIN_SAMPLE_COUNT = 4;
export const MIN_SPAN_MS = 1_200;
export const SAMPLE_CAP = 16;

export const STILL_ACCEL_DELTA_MAX = 0.3;
export const STILL_GYRO_MAX = 0.08;
export const MOVING_ACCEL_DELTA_MIN = 1.0;
export const MOVING_GYRO_MIN = 0.2;

type RawObject = Record<string, unknown>;
type Vector = { x: number; y: number; z: number };
type MotionSample = {
  timestamp: number;
  accel: Vector;
  gyro: Vector;
  dedupeKey: string;
};

function asObject(value: unknown): RawObject | null {
  return value !== null && typeof value === "object"
    ? (value as RawObject)
    : null;
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function vectorFrom(value: unknown): Vector | null {
  const object = asObject(value);
  if (
    object === null ||
    object.available !== true ||
    object.ready !== true ||
    !finiteNumber(object.x) ||
    !finiteNumber(object.y) ||
    !finiteNumber(object.z)
  ) {
    return null;
  }

  return {
    x: object.x,
    y: object.y,
    z: object.z,
  };
}

function timestampFrom(value: unknown): number | null {
  const object = asObject(value);
  return object !== null && finiteNumber(object.sampledAt)
    ? object.sampledAt
    : null;
}

function isFresh(timestamp: number, nowMs: number): boolean {
  const age = nowMs - timestamp;
  return age >= 0 && age <= SAMPLE_MAX_AGE_MS;
}

function motionFromSamples(
  samples: MotionSample[],
  nowMs: number,
): PhysicalMotion {
  if (!Number.isFinite(nowMs)) {
    return "unknown";
  }

  const current = samples.filter((sample) =>
    isFresh(sample.timestamp, nowMs),
  );

  if (current.length < MIN_SAMPLE_COUNT) {
    return "unknown";
  }

  const span =
    current[current.length - 1].timestamp - current[0].timestamp;
  if (span < MIN_SPAN_MS) {
    return "unknown";
  }

  const intervals = current.slice(1).map((sample, index) => {
    const previous = current[index];
    const dx = sample.accel.x - previous.accel.x;
    const dy = sample.accel.y - previous.accel.y;
    const dz = sample.accel.z - previous.accel.z;

    return {
      accelDelta: Math.sqrt(dx * dx + dy * dy + dz * dz),
      gyroMagnitude: Math.sqrt(
        sample.gyro.x * sample.gyro.x +
          sample.gyro.y * sample.gyro.y +
          sample.gyro.z * sample.gyro.z,
      ),
    };
  });

  if (intervals.length === 0) {
    return "unknown";
  }

  const movingIntervals = intervals.filter(
    (interval) =>
      interval.accelDelta >= MOVING_ACCEL_DELTA_MIN ||
      interval.gyroMagnitude >= MOVING_GYRO_MIN,
  ).length;
  if (movingIntervals >= 2) {
    return "moving";
  }

  const quietIntervals = intervals.filter(
    (interval) =>
      interval.accelDelta <= STILL_ACCEL_DELTA_MAX &&
      interval.gyroMagnitude <= STILL_GYRO_MAX,
  ).length;
  if (quietIntervals >= Math.ceil(intervals.length * 0.8)) {
    return "still";
  }

  return "unknown";
}

export class PhysicalMotionWindow {
  private samples: MotionSample[] = [];

  push(raw: unknown, nowMs: number): PhysicalMotion {
    if (!Number.isFinite(nowMs)) {
      this.reset();
      return "unknown";
    }

    const state = asObject(raw);
    if (state === null || state.monitoring !== true) {
      this.reset();
      return "unknown";
    }

    this.prune(nowMs);

    const accel = vectorFrom(state.accelerometer);
    const gyro = vectorFrom(state.gyroscope);
    const accelTimestamp = timestampFrom(state.accelerometer);
    const gyroTimestamp = timestampFrom(state.gyroscope);

    if (
      accel === null ||
      gyro === null ||
      accelTimestamp === null ||
      gyroTimestamp === null ||
      !isFresh(accelTimestamp, nowMs) ||
      !isFresh(gyroTimestamp, nowMs)
    ) {
      return "unknown";
    }

    const dedupeKey = `${accelTimestamp}|${gyroTimestamp}`;
    if (!this.samples.some((sample) => sample.dedupeKey === dedupeKey)) {
      this.samples.push({
        timestamp: Math.max(accelTimestamp, gyroTimestamp),
        accel: { ...accel },
        gyro: { ...gyro },
        dedupeKey,
      });
      this.samples.sort((left, right) => left.timestamp - right.timestamp);
      this.trimWindow();
    }

    return motionFromSamples(this.samples, nowMs);
  }

  getMotion(nowMs: number): PhysicalMotion {
    if (!Number.isFinite(nowMs)) {
      return "unknown";
    }

    this.prune(nowMs);
    return motionFromSamples(this.samples, nowMs);
  }

  reset(): void {
    this.samples = [];
  }

  private prune(nowMs: number): void {
    this.samples = this.samples.filter((sample) => {
      const age = nowMs - sample.timestamp;
      return age >= 0 && age <= WINDOW_MS;
    });
    this.trimWindow();
  }

  private trimWindow(): void {
    if (this.samples.length === 0) {
      return;
    }

    const latestTimestamp = this.samples[this.samples.length - 1].timestamp;
    this.samples = this.samples.filter(
      (sample) => latestTimestamp - sample.timestamp <= WINDOW_MS,
    );

    if (this.samples.length > SAMPLE_CAP) {
      this.samples = this.samples.slice(-SAMPLE_CAP);
    }
  }
}
