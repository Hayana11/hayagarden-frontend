import assert from "node:assert/strict";
import { interpretPhysicalState } from "../src/lib/reality/physicalInterpretation.ts";

const available = {
  available: true,
  ready: true,
};

function raw(overrides = {}) {
  return {
    schemaVersion: 1,
    accelerometer: { ...available, x: 0, y: 0, z: 9.8 },
    proximity: { ...available, value: 5, maxRange: 5 },
    light: { ...available, lux: 154 },
    battery: { available: true, level: 80, charging: false },
    ...overrides,
  };
}

assert.equal(
  interpretPhysicalState(raw()).orientation,
  "face_up",
);
assert.equal(
  interpretPhysicalState(
    raw({ accelerometer: { ...available, x: 0, y: 0, z: -9.8 } }),
  ).orientation,
  "face_down",
);
assert.equal(
  interpretPhysicalState(
    raw({ accelerometer: { ...available, x: 9.8, y: 0, z: 0 } }),
  ).orientation,
  "vertical",
);
assert.equal(
  interpretPhysicalState(
    raw({ accelerometer: { ...available, x: 7, y: 7, z: 0 } }),
  ).orientation,
  "tilted",
);
assert.equal(
  interpretPhysicalState(
    raw({ accelerometer: { ...available, x: 0, y: 0, z: 20 } }),
  ).orientation,
  "unknown",
);
assert.equal(
  interpretPhysicalState(
    raw({ accelerometer: { available: false, ready: false } }),
  ).orientation,
  "unknown",
);

assert.equal(
  interpretPhysicalState(
    raw({ proximity: { ...available, value: 0, maxRange: 5 } }),
  ).proximity,
  "near",
);
assert.equal(
  interpretPhysicalState(
    raw({ proximity: { ...available, value: 5, maxRange: 5 } }),
  ).proximity,
  "far",
);
assert.equal(
  interpretPhysicalState(
    raw({ proximity: { available: false, ready: false } }),
  ).proximity,
  "unknown",
);

assert.equal(
  interpretPhysicalState(raw({ light: { ...available, lux: 0 } })).lightExposure,
  "dark",
);
assert.equal(
  interpretPhysicalState(raw({ light: { ...available, lux: 50 } })).lightExposure,
  "dim",
);
assert.equal(
  interpretPhysicalState(raw({ light: { ...available, lux: 154 } })).lightExposure,
  "moderate",
);
assert.equal(
  interpretPhysicalState(raw({ light: { ...available, lux: 1001 } })).lightExposure,
  "bright",
);
assert.equal(
  interpretPhysicalState(
    raw({ light: { available: false, ready: false } }),
  ).lightExposure,
  "unknown",
);

assert.deepEqual(
  interpretPhysicalState(
    raw({ battery: { available: true, level: 80, charging: true } }),
  ),
  {
    schemaVersion: 1,
    orientation: "face_up",
    proximity: "far",
    lightExposure: "moderate",
    charging: true,
    batteryLevel: 80,
  },
);
assert.equal(
  interpretPhysicalState(
    raw({ battery: { available: true, level: 80, charging: false } }),
  ).charging,
  false,
);
assert.deepEqual(
  interpretPhysicalState(raw({ battery: { available: false } })).charging,
  null,
);
assert.deepEqual(
  interpretPhysicalState(raw({ battery: { available: false } })).batteryLevel,
  null,
);

assert.doesNotThrow(() => {
  const facts = interpretPhysicalState({
    schemaVersion: 1,
    accelerometer: { available: true, ready: true, x: "bad" },
    proximity: null,
    light: { available: true, ready: true, lux: Number.NaN },
    battery: { available: true, level: 101, charging: "yes" },
  });
  assert.equal(facts.orientation, "unknown");
  assert.equal(facts.proximity, "unknown");
  assert.equal(facts.lightExposure, "unknown");
  assert.equal(facts.charging, null);
  assert.equal(facts.batteryLevel, null);
});
assert.deepEqual(
  interpretPhysicalState(null),
  {
    schemaVersion: 1,
    orientation: "unknown",
    proximity: "unknown",
    lightExposure: "unknown",
    charging: null,
    batteryLevel: null,
  },
);

const facts = interpretPhysicalState(raw());
assert.equal("moving" in facts, false);
assert.equal("still" in facts, false);
assert.equal("active" in facts, false);

console.log("P2C.1b physical interpretation tests: PASS");
