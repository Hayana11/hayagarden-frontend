import assert from "node:assert/strict";
import {
  PHYSICAL_POLL_MS,
  PhysicalRealityRuntime,
} from "../src/lib/reality/realityRuntime.ts";
import { RealityStore } from "../src/lib/reality/realityStore.ts";

function raw(sampledAt, accel, gyro, monitoring = true) {
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

function createEnvironment({
  visible = true,
  bridge,
  now = 0,
} = {}) {
  let currentVisible = visible;
  let currentBridge = bridge;
  let currentNow = now;
  let intervalCallback = null;
  let nextIntervalId = 1;
  const listeners = new Map();
  const metrics = {
    bridgeReads: 0,
    intervalStarts: 0,
    intervalClears: 0,
    listenerAdds: 0,
    listenerRemoves: 0,
  };

  const environment = {
    now: () => currentNow,
    getBridge: () => currentBridge,
    isVisible: () => currentVisible,
    setInterval: (callback, intervalMs) => {
      assert.equal(intervalMs, PHYSICAL_POLL_MS);
      metrics.intervalStarts += 1;
      intervalCallback = callback;
      return nextIntervalId++;
    },
    clearInterval: () => {
      metrics.intervalClears += 1;
      intervalCallback = null;
    },
    addEventListener: (event, listener) => {
      metrics.listenerAdds += 1;
      const eventListeners = listeners.get(event) ?? new Set();
      eventListeners.add(listener);
      listeners.set(event, eventListeners);
    },
    removeEventListener: (event, listener) => {
      metrics.listenerRemoves += 1;
      listeners.get(event)?.delete(listener);
    },
    tick: () => {
      intervalCallback?.();
    },
    fire: (event) => {
      for (const listener of listeners.get(event) ?? []) {
        listener();
      }
    },
    setVisible: (value) => {
      currentVisible = value;
    },
    setBridge: (value) => {
      currentBridge = value;
    },
    setNow: (value) => {
      currentNow = value;
    },
    metrics,
  };

  return environment;
}

function validJson(at, accel, gyro) {
  return JSON.stringify(
    raw(
      at,
      accel ?? { x: 0, y: 0, z: 9.8 },
      gyro ?? { x: 0, y: 0, z: 0 },
    ),
  );
}

function assertInitial(store) {
  assert.deepEqual(store.getSnapshot(), {
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
}

assert.equal(PHYSICAL_POLL_MS, 500);

let immediateReads = 0;
const validBridge = {
  getPhysicalState: () => {
    immediateReads += 1;
    return validJson(0);
  },
};
const visibleStore = new RealityStore();
const visibleEnvironment = createEnvironment({
  bridge: validBridge,
  now: 1000,
});
const visibleRuntime = new PhysicalRealityRuntime(
  visibleStore,
  visibleEnvironment,
);
visibleRuntime.start();
assert.equal(immediateReads, 1);
assert.equal(visibleStore.getSnapshot().physical.observedAt, 1000);
assert.equal(visibleStore.getSnapshot().physical.facts.orientation, "face_up");

const countingBridge = {
  getPhysicalState: () => {
    visibleEnvironment.metrics.bridgeReads += 1;
    return validJson(1500);
  },
};
visibleEnvironment.setBridge(countingBridge);
visibleEnvironment.setNow(1500);
visibleEnvironment.tick();
assert.equal(visibleEnvironment.metrics.bridgeReads, 1);
assert.equal(visibleStore.getSnapshot().physical.observedAt, 1500);

function assertSourceFailure(result) {
  const store = new RealityStore();
  const environment = createEnvironment({ bridge: result });
  const runtime = new PhysicalRealityRuntime(store, environment);
  runtime.start();
  assertInitial(store);
  runtime.stop();
}

assertSourceFailure(undefined);
assertSourceFailure({
  getPhysicalState: () => {
    throw new Error("bridge failure");
  },
});
assertSourceFailure({
  getPhysicalState: () => "{not-json",
});
assertSourceFailure({
  getPhysicalState: () => JSON.stringify({ schemaVersion: 2 }),
});
assertSourceFailure({
  getPhysicalState: () => 123,
});

const hiddenStore = new RealityStore();
const hiddenBridge = {
  getPhysicalState: () => validJson(0),
};
const hiddenEnvironment = createEnvironment({
  bridge: hiddenBridge,
  now: 0,
});
const hiddenRuntime = new PhysicalRealityRuntime(
  hiddenStore,
  hiddenEnvironment,
);
hiddenRuntime.start();
assert.equal(hiddenEnvironment.metrics.intervalStarts, 1);
hiddenEnvironment.setVisible(false);
hiddenEnvironment.fire("visibilitychange");
assert.equal(hiddenEnvironment.metrics.intervalClears, 1);
const hiddenReads = hiddenEnvironment.metrics.bridgeReads;
hiddenEnvironment.tick();
assert.equal(hiddenEnvironment.metrics.bridgeReads, hiddenReads);
assertInitial(hiddenStore);

hiddenEnvironment.setBridge({
  getPhysicalState: () => validJson(2000),
});
hiddenEnvironment.setNow(2000);
hiddenEnvironment.setVisible(true);
hiddenEnvironment.fire("visibilitychange");
assert.equal(hiddenEnvironment.metrics.intervalStarts, 2);
assert.equal(hiddenStore.getSnapshot().physical.observedAt, 2000);

const idempotentStore = new RealityStore();
const idempotentEnvironment = createEnvironment({
  bridge: { getPhysicalState: () => validJson(0) },
});
const idempotentRuntime = new PhysicalRealityRuntime(
  idempotentStore,
  idempotentEnvironment,
);
idempotentRuntime.start();
idempotentRuntime.start();
assert.equal(idempotentEnvironment.metrics.intervalStarts, 1);
assert.equal(idempotentEnvironment.metrics.listenerAdds, 3);
idempotentRuntime.stop();
idempotentRuntime.stop();
assert.equal(idempotentEnvironment.metrics.intervalClears, 1);
assert.equal(idempotentEnvironment.metrics.listenerRemoves, 3);
assertInitial(idempotentStore);

const redundantResetStore = new RealityStore();
let redundantResetNotifications = 0;
redundantResetStore.subscribe(() => {
  redundantResetNotifications += 1;
});
const redundantResetEnvironment = createEnvironment();
const redundantResetRuntime = new PhysicalRealityRuntime(
  redundantResetStore,
  redundantResetEnvironment,
);
redundantResetRuntime.start();
redundantResetEnvironment.tick();
redundantResetEnvironment.tick();
assert.equal(redundantResetNotifications, 0);

const accumulationStore = new RealityStore();
const samples = [
  { at: 0, gyro: { x: 0.25, y: 0, z: 0 } },
  { at: 500, gyro: { x: 0.25, y: 0, z: 0 } },
  { at: 1000, gyro: { x: 0.25, y: 0, z: 0 } },
  { at: 1500, gyro: { x: 0.25, y: 0, z: 0 } },
];
let sampleIndex = 0;
const accumulationEnvironment = createEnvironment({
  bridge: {
    getPhysicalState: () => validJson(
      samples[sampleIndex].at,
      { x: 0, y: 0, z: 9.8 },
      samples[sampleIndex++].gyro,
    ),
  },
  now: 0,
});
const accumulationRuntime = new PhysicalRealityRuntime(
  accumulationStore,
  accumulationEnvironment,
);
accumulationRuntime.start();
for (const at of [500, 1000, 1500]) {
  accumulationEnvironment.setNow(at);
  accumulationEnvironment.tick();
}
assert.equal(accumulationStore.getSnapshot().physical.motion, "moving");

const normalWebStore = new RealityStore();
const normalWebEnvironment = createEnvironment();
const normalWebRuntime = new PhysicalRealityRuntime(
  normalWebStore,
  normalWebEnvironment,
);
assert.doesNotThrow(() => {
  normalWebRuntime.start();
  normalWebRuntime.stop();
});
assertInitial(normalWebStore);

console.log("P2C.1e reality runtime tests: PASS");
