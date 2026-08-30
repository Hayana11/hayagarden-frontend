/**
 * Pure P2C.1b translation of one raw ElpisPhysical snapshot.
 *
 * The values describe phone sensor facts only. They do not describe the room,
 * the user's posture, intent, activity, or state of mind.
 */

export type PhysicalOrientation =
  | "face_up"
  | "face_down"
  | "vertical"
  | "horizontal"
  | "tilted"
  | "unknown";

export type PhysicalProximity = "near" | "far" | "unknown";

export type PhysicalLightExposure =
  | "dark"
  | "dim"
  | "moderate"
  | "bright"
  | "unknown";

export interface PhysicalDeviceFacts {
  schemaVersion: 1;
  orientation: PhysicalOrientation;
  proximity: PhysicalProximity;
  lightExposure: PhysicalLightExposure;
  charging: boolean | null;
  batteryLevel: number | null;
}

export interface PhysicalRawState {
  schemaVersion?: unknown;
  accelerometer?: unknown;
  proximity?: unknown;
  light?: unknown;
  battery?: unknown;
}

type RawObject = { [key: string]: unknown };

const GRAVITY_MIN_MPS2 = 7.0;
const GRAVITY_MAX_MPS2 = 12.5;
const DOMINANT_AXIS_RATIO = 0.75;
const LIGHT_DIM_LUX = 10;
const LIGHT_MODERATE_LUX = 100;
const LIGHT_BRIGHT_LUX = 1000;

function asObject(value: unknown): RawObject | null {
  return value !== null && typeof value === "object"
    ? (value as RawObject)
    : null;
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
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

function orientationFrom(rawSensor: unknown): PhysicalOrientation {
  const sensor = asObject(rawSensor);
  if (!sensor || sensor.available !== true || sensor.ready !== true) {
    return "unknown";
  }

  const x = sensor.x;
  const y = sensor.y;
  const z = sensor.z;
  if (!finiteNumber(x) || !finiteNumber(y) || !finiteNumber(z)) {
    return "unknown";
  }

  const magnitude = Math.sqrt(x * x + y * y + z * z);
  if (
    !Number.isFinite(magnitude) ||
    magnitude < GRAVITY_MIN_MPS2 ||
    magnitude > GRAVITY_MAX_MPS2
  ) {
    return "unknown";
  }

  if (Math.abs(z) / magnitude >= DOMINANT_AXIS_RATIO) {
    return z > 0 ? "face_up" : z < 0 ? "face_down" : "unknown";
  }

  if (Math.abs(y) / magnitude >= DOMINANT_AXIS_RATIO) {
    return "vertical";
  }

  if (Math.abs(x) / magnitude >= DOMINANT_AXIS_RATIO) {
    return "horizontal";
  }

  return "tilted";
}

function proximityFrom(rawSensor: unknown): PhysicalProximity {
  const sensor = asObject(rawSensor);
  if (!sensor || sensor.available !== true || sensor.ready !== true) {
    return "unknown";
  }

  const value = sensor.value;
  const maxRange = sensor.maxRange;
  if (
    !finiteNumber(value) ||
    !finiteNumber(maxRange) ||
    value < 0 ||
    maxRange <= 0
  ) {
    return "unknown";
  }

  return value < maxRange ? "near" : "far";
}

function lightExposureFrom(rawSensor: unknown): PhysicalLightExposure {
  const sensor = asObject(rawSensor);
  if (!sensor || sensor.available !== true || sensor.ready !== true) {
    return "unknown";
  }

  const lux = sensor.lux;
  if (!finiteNumber(lux) || lux < 0) {
    return "unknown";
  }

  if (lux < LIGHT_DIM_LUX) return "dark";
  if (lux < LIGHT_MODERATE_LUX) return "dim";
  if (lux < LIGHT_BRIGHT_LUX) return "moderate";
  return "bright";
}

function batteryFacts(rawBattery: unknown): {
  charging: boolean | null;
  batteryLevel: number | null;
} {
  const battery = asObject(rawBattery);
  if (!battery || battery.available !== true) {
    return { charging: null, batteryLevel: null };
  }

  const charging =
    typeof battery.charging === "boolean" ? battery.charging : null;
  const level =
    Number.isInteger(battery.level) &&
    (battery.level as number) >= 0 &&
    (battery.level as number) <= 100
      ? (battery.level as number)
      : null;

  return { charging, batteryLevel: level };
}

export function interpretPhysicalState(
  raw: unknown,
  nowMs?: number,
): PhysicalDeviceFacts {
  void nowMs;

  const state = asObject(raw);
  if (!state || state.schemaVersion !== 1) {
    return unknownFacts();
  }

  const battery = batteryFacts(state.battery);
  return {
    schemaVersion: 1,
    orientation: orientationFrom(state.accelerometer),
    proximity: proximityFrom(state.proximity),
    lightExposure: lightExposureFrom(state.light),
    charging: battery.charging,
    batteryLevel: battery.batteryLevel,
  };
}
