import assert from "node:assert/strict";
import { RealityStore } from "../src/lib/reality/realityStore.ts";
import {
  compileRealityContext,
} from "../src/lib/reality/realityContextCompiler.ts";
import {
  RealityPromptProjection,
} from "../src/lib/reality/realityPromptProjection.ts";

function snapshot({
  motion = "unknown",
  charging = null,
  batteryLevel = null,
  orientation = "unknown",
  proximity = "unknown",
  lightExposure = "unknown",
  raw = null,
  observedAt = null,
} = {}) {
  return {
    schemaVersion: 1,
    physical: {
      raw,
      facts: {
        schemaVersion: 1,
        orientation,
        proximity,
        lightExposure,
        charging,
        batteryLevel,
      },
      motion,
      observedAt,
    },
  };
}

function raw({
  at = 0,
  accel = { x: 0, y: 0, z: 9.8 },
  gyro = { x: 0, y: 0, z: 0 },
  monitoring = true,
  charging = null,
  batteryLevel = null,
  lux = 154,
  proximity = 5,
} = {}) {
  return {
    schemaVersion: 1,
    monitoring,
    accelerometer: {
      available: true,
      ready: true,
      x: accel.x,
      y: accel.y,
      z: accel.z,
      sampledAt: at,
    },
    gyroscope: {
      available: true,
      ready: true,
      x: gyro.x,
      y: gyro.y,
      z: gyro.z,
      sampledAt: at,
    },
    proximity: {
      available: true,
      ready: true,
      value: proximity,
      maxRange: 5,
    },
    light: {
      available: true,
      ready: true,
      lux,
    },
    battery: {
      available: charging !== null || batteryLevel !== null,
      ready: true,
      charging,
      level: batteryLevel,
    },
  };
}

function compile(options) {
  return compileRealityContext(snapshot(options));
}

const stillCharging80 = compile({
  motion: "still",
  charging: true,
  batteryLevel: 80,
});
assert.equal(stillCharging80.text, "设备当前静止，正在充电，电量80%。");

const movingNotCharging79 = compile({
  motion: "moving",
  charging: false,
  batteryLevel: 79,
});
assert.equal(movingNotCharging79.text, "设备当前移动中，未充电，电量79%。");

assert.equal(
  compile({ charging: true, batteryLevel: 80 }).text,
  "设备当前正在充电，电量80%。",
);
assert.equal(
  compile({ motion: "still", batteryLevel: 80 }).text,
  "设备当前静止，电量80%。",
);
assert.equal(
  compile({ motion: "still", charging: true }).text,
  "设备当前静止，正在充电。",
);
assert.equal(compile({}).text, "");

const segments = stillCharging80.segments;
assert.equal(segments.map((segment) => segment.text).join(""), stillCharging80.text);
assert.deepEqual(
  segments
    .filter((segment) => segment.kind === "dynamic")
    .map((segment) => segment.key),
  ["motion", "charging", "batteryLevel"],
);
assert.equal(stillCharging80.text.includes("**"), false);

const orientationOnly = compile({
  motion: "still",
  charging: true,
  batteryLevel: 80,
  orientation: "face_up",
  proximity: "far",
  lightExposure: "bright",
});
const orientationChanged = compile({
  motion: "still",
  charging: true,
  batteryLevel: 80,
  orientation: "face_down",
  proximity: "near",
  lightExposure: "dark",
});
assert.equal(orientationOnly.text, orientationChanged.text);

const rawOne = { sensor: { x: 1, y: 2, z: 3 }, lux: 10 };
const rawTwo = { sensor: { x: 9, y: 8, z: 7 }, lux: 999 };
assert.equal(
  compile({
    motion: "still",
    charging: true,
    batteryLevel: 80,
    raw: rawOne,
  }).text,
  compile({
    motion: "still",
    charging: true,
    batteryLevel: 80,
    raw: rawTwo,
  }).text,
);

const projectionInitialStore = new RealityStore();
projectionInitialStore.ingestPhysical(
  raw({ charging: true, batteryLevel: 80 }),
  0,
);
const projectionInitial = new RealityPromptProjection(projectionInitialStore);
assert.equal(
  projectionInitial.getSnapshot().text,
  "设备当前正在充电，电量80%。",
);
projectionInitial.dispose();

const semanticStore = new RealityStore();
const semanticProjection = new RealityPromptProjection(semanticStore);
let semanticNotifications = 0;
semanticProjection.subscribe(() => {
  semanticNotifications += 1;
});
semanticStore.ingestPhysical(
  raw({ charging: true, batteryLevel: 80 }),
  0,
);
assert.equal(semanticProjection.getSnapshot().text, "设备当前正在充电，电量80%。");
assert.equal(semanticNotifications, 1);

const rawOnlyStore = new RealityStore();
const rawOnlyProjection = new RealityPromptProjection(rawOnlyStore);
let rawOnlyNotifications = 0;
rawOnlyProjection.subscribe(() => {
  rawOnlyNotifications += 1;
});
rawOnlyStore.ingestPhysical(
  raw({ at: 0, charging: true, batteryLevel: 80, lux: 10 }),
  0,
);
rawOnlyNotifications = 0;
rawOnlyStore.ingestPhysical(
  raw({ at: 500, charging: true, batteryLevel: 80, lux: 900 }),
  500,
);
assert.equal(rawOnlyProjection.getSnapshot().text, "设备当前正在充电，电量80%。");
assert.equal(rawOnlyNotifications, 0);

const motionStore = new RealityStore();
const motionProjection = new RealityPromptProjection(motionStore);
let motionNotifications = 0;
motionProjection.subscribe(() => {
  motionNotifications += 1;
});
for (const at of [0, 500, 1000, 1500]) {
  motionStore.ingestPhysical({
    ...raw({
      at,
      gyro: { x: 0.25, y: 0, z: 0 },
      charging: null,
      batteryLevel: null,
    }),
  }, at);
}
assert.equal(motionProjection.getSnapshot().text, "设备当前移动中。");
assert.equal(motionNotifications, 1);

const chargingStore = new RealityStore();
const chargingProjection = new RealityPromptProjection(chargingStore);
let chargingNotifications = 0;
chargingProjection.subscribe(() => {
  chargingNotifications += 1;
});
chargingStore.ingestPhysical(
  raw({ charging: true, batteryLevel: null }),
  0,
);
chargingNotifications = 0;
chargingStore.ingestPhysical(
  raw({ at: 500, charging: false, batteryLevel: null }),
  500,
);
assert.equal(chargingProjection.getSnapshot().text, "设备当前未充电。");
assert.equal(chargingNotifications, 1);

const batteryStore = new RealityStore();
const batteryProjection = new RealityPromptProjection(batteryStore);
let batteryNotifications = 0;
batteryProjection.subscribe(() => {
  batteryNotifications += 1;
});
batteryStore.ingestPhysical(
  raw({ charging: null, batteryLevel: 80 }),
  0,
);
batteryNotifications = 0;
batteryStore.ingestPhysical(
  raw({ at: 500, charging: null, batteryLevel: 79 }),
  500,
);
assert.equal(batteryProjection.getSnapshot().text, "设备当前电量79%。");
assert.equal(batteryNotifications, 1);

const resetStore = new RealityStore();
const resetProjection = new RealityPromptProjection(resetStore);
let resetNotifications = 0;
resetProjection.subscribe(() => {
  resetNotifications += 1;
});
resetStore.ingestPhysical(
  raw({ charging: true, batteryLevel: 80 }),
  0,
);
resetNotifications = 0;
resetStore.resetPhysical();
assert.equal(resetProjection.getSnapshot().text, "");
assert.equal(resetNotifications, 1);

const unsubscribeStore = new RealityStore();
const unsubscribeProjection = new RealityPromptProjection(unsubscribeStore);
let unsubscribeNotifications = 0;
const unsubscribe = unsubscribeProjection.subscribe(() => {
  unsubscribeNotifications += 1;
});
unsubscribe();
unsubscribe();
unsubscribeStore.ingestPhysical(
  raw({ charging: true, batteryLevel: 80 }),
  0,
);
assert.equal(unsubscribeNotifications, 0);

const disposeStore = new RealityStore();
const disposeProjection = new RealityPromptProjection(disposeStore);
let disposeNotifications = 0;
disposeProjection.subscribe(() => {
  disposeNotifications += 1;
});
const beforeDispose = disposeProjection.getSnapshot();
disposeProjection.dispose();
disposeProjection.dispose();
disposeStore.ingestPhysical(
  raw({ charging: true, batteryLevel: 80 }),
  0,
);
assert.strictEqual(disposeProjection.getSnapshot(), beforeDispose);
assert.equal(disposeNotifications, 0);

const badSubscriberStore = new RealityStore();
const badSubscriberProjection = new RealityPromptProjection(badSubscriberStore);
let goodSubscriberNotifications = 0;
badSubscriberProjection.subscribe(() => {
  throw new Error("bad projection subscriber");
});
badSubscriberProjection.subscribe(() => {
  goodSubscriberNotifications += 1;
});
assert.doesNotThrow(() => {
  badSubscriberStore.ingestPhysical(
    raw({ charging: true, batteryLevel: 80 }),
    0,
  );
});
assert.equal(goodSubscriberNotifications, 1);
assert.equal(
  badSubscriberProjection.getSnapshot().text,
  "设备当前正在充电，电量80%。",
);

console.log("P2C.1f reality context compiler tests: PASS");
