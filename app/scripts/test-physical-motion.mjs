import assert from "node:assert/strict";
import {
  MIN_SAMPLE_COUNT,
  MIN_SPAN_MS,
  MOVING_ACCEL_DELTA_MIN,
  MOVING_GYRO_MIN,
  PhysicalMotionWindow,
  SAMPLE_CAP,
  SAMPLE_MAX_AGE_MS,
  STILL_ACCEL_DELTA_MAX,
  STILL_GYRO_MAX,
  WINDOW_MS,
} from "../src/lib/reality/physicalMotion.ts";

function raw(
  sampledAt: number,
  accel,
  gyro,
  monitoring = true,
) {
  return {
    monitoring,
    accelerometer: {
      available: true,
      ready: true,
      x: accel.x,
      y: accel.y,
      z: accel.z,
      sampledAt,
    },
    gyroscope: {
      available: true,
      ready: true,
      x: gyro.x,
      y: gyro.y,
      z: gyro.z,
      sampledAt,
    },
    proximity: { available: false, ready: false, value: null, maxRange: null },
    light: { available: false, ready: false, lux: null },
    battery: { available: false, ready: false, level: null, charging: null },
  };
}

function pushSamples(
  window: PhysicalMotionWindow,
  samples,
) {
  for (const sample of samples) {
    window.push(raw(sample.at, sample.accel, sample.gyro), sample.at);
  }
}

const stationary = new PhysicalMotionWindow();
pushSamples(stationary, [
  { at: 0, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0.01, y: 0, z: 0 } },
  { at: 500, accel: { x: 0.1, y: 0, z: 9.8 }, gyro: { x: 0.01, y: 0, z: 0 } },
  { at: 1000, accel: { x: 0, y: 0.1, z: 9.8 }, gyro: { x: 0, y: 0.01, z: 0 } },
  { at: 1500, accel: { x: 0.1, y: 0, z: 9.8 }, gyro: { x: 0.01, y: 0, z: 0 } },
]);
assert.equal(stationary.getMotion(1500), "still");

const rotation = new PhysicalMotionWindow();
pushSamples(rotation, [
  { at: 0, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0.25, y: 0, z: 0 } },
  { at: 500, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0.25, y: 0, z: 0 } },
  { at: 1000, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0.25, y: 0, z: 0 } },
  { at: 1500, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0.25, y: 0, z: 0 } },
]);
assert.equal(rotation.getMotion(1500), "moving");

const shake = new PhysicalMotionWindow();
pushSamples(shake, [
  { at: 0, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0, y: 0, z: 0 } },
  { at: 500, accel: { x: 1.2, y: 0, z: 9.8 }, gyro: { x: 0, y: 0, z: 0 } },
  { at: 1000, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0, y: 0, z: 0 } },
  { at: 1500, accel: { x: 1.2, y: 0, z: 9.8 }, gyro: { x: 0, y: 0, z: 0 } },
]);
assert.equal(shake.getMotion(1500), "moving");

const borderline = new PhysicalMotionWindow();
pushSamples(borderline, [
  { at: 0, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0.12, y: 0, z: 0 } },
  { at: 500, accel: { x: 0.6, y: 0, z: 9.8 }, gyro: { x: 0.12, y: 0, z: 0 } },
  { at: 1000, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0.12, y: 0, z: 0 } },
  { at: 1500, accel: { x: 0.6, y: 0, z: 9.8 }, gyro: { x: 0.12, y: 0, z: 0 } },
]);
assert.equal(borderline.getMotion(1500), "unknown");

const insufficientSamples = new PhysicalMotionWindow();
pushSamples(insufficientSamples, [
  { at: 0, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0, y: 0, z: 0 } },
  { at: 500, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0, y: 0, z: 0 } },
  { at: 1000, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0, y: 0, z: 0 } },
]);
assert.equal(insufficientSamples.getMotion(1000), "unknown");

const insufficientSpan = new PhysicalMotionWindow();
pushSamples(insufficientSpan, [
  { at: 0, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0, y: 0, z: 0 } },
  { at: 300, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0, y: 0, z: 0 } },
  { at: 600, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0, y: 0, z: 0 } },
  { at: 900, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0, y: 0, z: 0 } },
]);
assert.equal(insufficientSpan.getMotion(900), "unknown");

assert.equal(stationary.getMotion(1500 + SAMPLE_MAX_AGE_MS + 1), "unknown");

const monitoringOff = new PhysicalMotionWindow();
assert.equal(
  monitoringOff.push(
    raw(0, { x: 0, y: 0, z: 9.8 }, { x: 0, y: 0, z: 0 }, false),
    0,
  ),
  "unknown",
);

const duplicates = new PhysicalMotionWindow();
const duplicateSample = raw(
  0,
  { x: 0, y: 0, z: 9.8 },
  { x: 0, y: 0, z: 0 },
);
for (let index = 0; index < MIN_SAMPLE_COUNT + 1; index += 1) {
  duplicates.push(duplicateSample, 0);
}
assert.equal(duplicates.getMotion(0), "unknown");
assert.equal(duplicates.samples.length, 1);

const resetWindow = new PhysicalMotionWindow();
pushSamples(resetWindow, [
  { at: 0, accel: { x: 0, y: 0, z: 9.8 }, gyro: { x: 0.01, y: 0, z: 0 } },
  { at: 500, accel: { x: 0.1, y: 0, z: 9.8 }, gyro: { x: 0.01, y: 0, z: 0 } },
  { at: 1000, accel: { x: 0, y: 0.1, z: 9.8 }, gyro: { x: 0, y: 0.01, z: 0 } },
  { at: 1500, accel: { x: 0.1, y: 0, z: 9.8 }, gyro: { x: 0.01, y: 0, z: 0 } },
]);
assert.equal(resetWindow.getMotion(1500), "still");
resetWindow.reset();
assert.equal(resetWindow.getMotion(1500), "unknown");

const malformed = new PhysicalMotionWindow();
assert.doesNotThrow(() => {
  assert.equal(
    malformed.push(
      {
        monitoring: true,
        accelerometer: { available: true, ready: true, x: Number.NaN },
        gyroscope: null,
      },
      0,
    ),
    "unknown",
  );
});

const bounded = new PhysicalMotionWindow();
for (let timestamp = 0; timestamp <= 39 * 100; timestamp += 100) {
  bounded.push(
    raw(
      timestamp,
      { x: 0, y: 0, z: 9.8 },
      { x: 0, y: 0, z: 0 },
    ),
    timestamp,
  );
}
assert.ok(bounded.samples.length <= SAMPLE_CAP);
assert.ok(bounded.samples.length <= Math.ceil(WINDOW_MS / 100) + 1);

assert.equal(MIN_SAMPLE_COUNT, 4);
assert.equal(MIN_SPAN_MS, 1200);
assert.equal(STILL_ACCEL_DELTA_MAX, 0.3);
assert.equal(STILL_GYRO_MAX, 0.08);
assert.equal(MOVING_ACCEL_DELTA_MIN, 1);
assert.equal(MOVING_GYRO_MIN, 0.2);

console.log("P2C.1c physical motion tests: PASS");
