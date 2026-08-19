import assert from "node:assert/strict";
import { RealityStore } from "../src/lib/reality/realityStore.ts";

function raw(
  sampledAt,
  accel,
  gyro,
  monitoring = true,
) {
  return {
    schemaVersion: 1,
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
    proximity: {
      available: true,
      ready: true,
      value: 5,
      maxRange: 5,
    },
    light: {
      available: true,
      ready: true,
      lux: 154,
    },
    battery: {
      available: true,
      ready: true,
      level: 75,
      charging: true,
    },
  };
}

const faceUpFarBrightCharging = raw(
  0,
  { x: 0, y: 0, z: 9.8 },
  { x: 0, y: 0, z: 0 },
);

const initialStore = new RealityStore();
assert.deepEqual(initialStore.getSnapshot(), {
  schemaVersion: 1,
  physical: {
    raw: null,
    facts: {
      schemaVersion: 1,
      orientation: "unknown",
      proximity: "unknown",
      lightExposure: "unknown",
      charging: null,
      batteryLevel: null,
    },
    motion: "unknown",
    observedAt: null,
  },
});

const factsStore = new RealityStore();
const factsSnapshot = factsStore.ingestPhysical(
  faceUpFarBrightCharging,
  0,
);
assert.deepEqual(factsSnapshot.physical.facts, {
  schemaVersion: 1,
  orientation: "face_up",
  proximity: "far",
  lightExposure: "moderate",
  charging: true,
  batteryLevel: 75,
});
assert.strictEqual(factsSnapshot.physical.raw, faceUpFarBrightCharging);
assert.equal(factsSnapshot.physical.observedAt, 0);

const stillStore = new RealityStore();
for (const at of [0, 500, 1000, 1500]) {
  stillStore.ingestPhysical(
    raw(
      at,
      { x: 0.05, y: 0, z: 9.8 },
      { x: 0.01, y: 0, z: 0 },
    ),
    at,
  );
}
assert.equal(stillStore.getSnapshot().physical.motion, "still");

const movingStore = new RealityStore();
for (const at of [0, 500, 1000, 1500]) {
  movingStore.ingestPhysical(
    raw(
      at,
      { x: 0, y: 0, z: 9.8 },
      { x: 0.25, y: 0, z: 0 },
    ),
    at,
  );
}
assert.equal(movingStore.getSnapshot().physical.motion, "moving");

const duplicateStore = new RealityStore();
for (let index = 0; index < 5; index += 1) {
  duplicateStore.ingestPhysical(faceUpFarBrightCharging, 0);
}
assert.equal(duplicateStore.getSnapshot().physical.motion, "unknown");

const monitoringStore = new RealityStore();
for (const at of [0, 500, 1000, 1500]) {
  monitoringStore.ingestPhysical(
    raw(
      at,
      { x: 0.05, y: 0, z: 9.8 },
      { x: 0.01, y: 0, z: 0 },
    ),
    at,
  );
}
assert.equal(monitoringStore.getSnapshot().physical.motion, "still");
monitoringStore.ingestPhysical(
  raw(
    2000,
    { x: 0.05, y: 0, z: 9.8 },
    { x: 0.01, y: 0, z: 0 },
    false,
  ),
  2000,
);
assert.equal(monitoringStore.getSnapshot().physical.motion, "unknown");

const malformedStore = new RealityStore();
assert.doesNotThrow(() => {
  malformedStore.ingestPhysical(
    {
      schemaVersion: 1,
      monitoring: true,
      accelerometer: { available: true, ready: true, x: Number.NaN },
    },
    100,
  );
});
assert.deepEqual(malformedStore.getSnapshot().physical.facts, {
  schemaVersion: 1,
  orientation: "unknown",
  proximity: "unknown",
  lightExposure: "unknown",
  charging: null,
  batteryLevel: null,
});
assert.equal(malformedStore.getSnapshot().physical.motion, "unknown");
assert.equal(malformedStore.getSnapshot().physical.observedAt, 100);

const invalidTimingStore = new RealityStore();
assert.doesNotThrow(() => {
  invalidTimingStore.ingestPhysical(faceUpFarBrightCharging, Number.NaN);
});
assert.deepEqual(invalidTimingStore.getSnapshot(), initialStore.getSnapshot());

const resetStore = new RealityStore();
resetStore.ingestPhysical(faceUpFarBrightCharging, 0);
const beforeReset = resetStore.getSnapshot();
const afterReset = resetStore.resetPhysical();
assert.notStrictEqual(beforeReset, afterReset);
assert.deepEqual(afterReset, initialStore.getSnapshot());

const resetMotionStore = new RealityStore();
for (const at of [0, 500, 1000, 1500]) {
  resetMotionStore.ingestPhysical(
    raw(
      at,
      { x: 0, y: 0, z: 9.8 },
      { x: 0.25, y: 0, z: 0 },
    ),
    at,
  );
}
assert.equal(resetMotionStore.getSnapshot().physical.motion, "moving");
resetMotionStore.resetPhysical();
resetMotionStore.ingestPhysical(
  raw(
    2000,
    { x: 0, y: 0, z: 9.8 },
    { x: 0, y: 0, z: 0 },
  ),
  2000,
);
assert.equal(resetMotionStore.getSnapshot().physical.motion, "unknown");

const subscriptionStore = new RealityStore();
const events = [];
let badSubscriberCalled = false;
subscriptionStore.subscribe(() => {
  throw new Error("subscriber failure");
});
const unsubscribe = subscriptionStore.subscribe(() => {
  badSubscriberCalled = true;
  const observed = subscriptionStore.getSnapshot();
  assert.strictEqual(observed.physical.raw, faceUpFarBrightCharging);
  assert.deepEqual(observed.physical.facts, {
    schemaVersion: 1,
    orientation: "face_up",
    proximity: "far",
    lightExposure: "moderate",
    charging: true,
    batteryLevel: 75,
  });
  assert.equal(observed.physical.motion, "unknown");
  assert.equal(observed.physical.observedAt, 250);
  events.push(observed);
});
const subscribedSnapshot = subscriptionStore.ingestPhysical(
  faceUpFarBrightCharging,
  250,
);
assert.equal(badSubscriberCalled, true);
assert.equal(events.length, 1);
assert.strictEqual(events[0], subscribedSnapshot);

unsubscribe();
subscriptionStore.ingestPhysical(faceUpFarBrightCharging, 500);
assert.equal(events.length, 1);
unsubscribe();

const immutableStore = new RealityStore();
const immutableRaw = raw(
  0,
  { x: 0, y: 0, z: 9.8 },
  { x: 0, y: 0, z: 0 },
);
const immutableBefore = JSON.stringify(immutableRaw);
immutableStore.ingestPhysical(immutableRaw, 0);
assert.equal(JSON.stringify(immutableRaw), immutableBefore);

console.log("P2C.1d reality store tests: PASS");
