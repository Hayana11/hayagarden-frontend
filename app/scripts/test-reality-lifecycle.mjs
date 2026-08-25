import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  PHYSICAL_BACKGROUND_POLL_MS,
  PHYSICAL_POLL_MS,
  PhysicalRealityRuntime,
} from "../src/lib/reality/realityRuntime.ts";
import {
  REALITY_ASSISTANT_MAX_AGE_MS,
  RealityStore,
  getAssistantRealitySnapshot,
  getRealityFreshness,
} from "../src/lib/reality/realityStore.ts";

function raw(level) {
  return JSON.stringify({
    schemaVersion: 1,
    monitoring: true,
    accelerometer: {
      available: true,
      ready: true,
      x: 0,
      y: 0,
      z: 9.8,
      sampledAt: level,
    },
    gyroscope: {
      available: true,
      ready: true,
      x: 0,
      y: 0,
      z: 0,
      sampledAt: level,
    },
    proximity: { available: false, ready: false, value: null, maxRange: null },
    light: { available: false, ready: false, lux: null },
    battery: { available: true, ready: true, level, charging: false },
  });
}

function createEnvironment({ visible = true, state = 53, now = 0 } = {}) {
  let currentVisible = visible;
  let currentState = state;
  let currentNow = now;
  let intervalCallback = null;
  let intervalMs = null;
  const listeners = new Map();
  const metrics = {
    bridgeReads: 0,
    intervalStarts: [],
    intervalClears: 0,
  };

  return {
    now: () => currentNow,
    getBridge: () => ({
      getPhysicalState: () => {
        metrics.bridgeReads += 1;
        return raw(currentState);
      },
    }),
    isVisible: () => currentVisible,
    setInterval: (callback, requestedMs) => {
      intervalCallback = callback;
      intervalMs = requestedMs;
      metrics.intervalStarts.push(requestedMs);
      return metrics.intervalStarts.length;
    },
    clearInterval: () => {
      intervalCallback = null;
      intervalMs = null;
      metrics.intervalClears += 1;
    },
    addEventListener: (event, listener) => {
      const eventListeners = listeners.get(event) ?? new Set();
      eventListeners.add(listener);
      listeners.set(event, eventListeners);
    },
    removeEventListener: (event, listener) => {
      listeners.get(event)?.delete(listener);
    },
    tick: () => intervalCallback?.(),
    fire: (event) => {
      for (const listener of listeners.get(event) ?? []) listener();
    },
    setVisible: (value) => {
      currentVisible = value;
    },
    setState: (value) => {
      currentState = value;
    },
    setNow: (value) => {
      currentNow = value;
    },
    get intervalMs() {
      return intervalMs;
    },
    metrics,
  };
}

assert.equal(PHYSICAL_POLL_MS, 500);
assert.equal(PHYSICAL_BACKGROUND_POLL_MS, 5 * 60 * 1000);
assert.equal(REALITY_ASSISTANT_MAX_AGE_MS, 10 * 60 * 1000);

const store = new RealityStore();
const environment = createEnvironment({ state: 53, now: 1000 });
const runtime = new PhysicalRealityRuntime(store, environment);

runtime.start();
assert.deepEqual(environment.metrics.intervalStarts, [PHYSICAL_POLL_MS]);
assert.equal(environment.intervalMs, PHYSICAL_POLL_MS);
assert.equal(store.getSnapshot().physical.facts.batteryLevel, 53);
assert.equal(store.getSnapshot().physical.observedAt, 1000);

environment.setState(52);
environment.setNow(1500);
environment.tick();
assert.equal(store.getSnapshot().physical.facts.batteryLevel, 52);
assert.equal(store.getSnapshot().physical.observedAt, 1500);

environment.setVisible(false);
environment.fire("visibilitychange");
assert.deepEqual(environment.metrics.intervalStarts, [
  PHYSICAL_POLL_MS,
  PHYSICAL_BACKGROUND_POLL_MS,
]);
assert.equal(environment.intervalMs, PHYSICAL_BACKGROUND_POLL_MS);
assert.equal(store.getSnapshot().physical.facts.batteryLevel, 52);
assert.equal(store.getSnapshot().physical.observedAt, 1500);

environment.setState(51);
environment.setNow(300000);
environment.tick();
assert.equal(environment.metrics.bridgeReads, 4);
assert.equal(store.getSnapshot().physical.facts.batteryLevel, 51);
assert.equal(store.getSnapshot().physical.observedAt, 300000);
assert.equal(getRealityFreshness(store.getSnapshot(), 300000).status, "fresh");

environment.setState(50);
environment.setNow(600000);
environment.fire("pagehide");
assert.equal(environment.intervalMs, PHYSICAL_BACKGROUND_POLL_MS);
assert.equal(store.getSnapshot().physical.facts.batteryLevel, 50);
assert.equal(store.getSnapshot().physical.observedAt, 600000);

environment.setNow(600000 + REALITY_ASSISTANT_MAX_AGE_MS + 1);
const stale = getRealityFreshness(store.getSnapshot(), 600000 + REALITY_ASSISTANT_MAX_AGE_MS + 1);
assert.equal(stale.status, "stale");
assert.equal(stale.observedAt, 600000);
assert.equal(getAssistantRealitySnapshot(
  store.getSnapshot(),
  600000 + REALITY_ASSISTANT_MAX_AGE_MS + 1,
), null);

environment.setVisible(true);
environment.setState(49);
environment.setNow(1200000);
environment.fire("pageshow");
assert.equal(environment.intervalMs, PHYSICAL_POLL_MS);
assert.equal(store.getSnapshot().physical.facts.batteryLevel, 49);
assert.equal(store.getSnapshot().physical.observedAt, 1200000);

runtime.stop();
assert.equal(environment.metrics.intervalClears, 3);
assert.equal(store.getSnapshot().physical.observedAt, null);

const runtimeSource = readFileSync(
  fileURLToPath(new URL("../src/lib/reality/realityRuntime.ts", import.meta.url)),
  "utf8",
);
const storeSource = readFileSync(
  fileURLToPath(new URL("../src/lib/reality/realityStore.ts", import.meta.url)),
  "utf8",
);
for (const forbidden of [
  "new Worker",
  "service worker",
  "systemFingerprint",
  "system_changed",
  "resident identity",
  "prompt cache",
]) {
  assert.equal(runtimeSource.includes(forbidden), false, forbidden);
  assert.equal(storeSource.includes(forbidden), false, forbidden);
}

const chatSource = readFileSync(
  fileURLToPath(new URL("../src/screens/ChatScreen.tsx", import.meta.url)),
  "utf8",
);
assert.equal(chatSource.split("realityPromptProjection.getSnapshot().text").length - 1, 3);
assert.ok(chatSource.includes("await runStream(messageId, { realityContext });"));

console.log("REALITY-LIFECYCLE-FIX-R1: PASS");
