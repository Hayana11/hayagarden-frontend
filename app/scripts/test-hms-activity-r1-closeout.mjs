import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../..", import.meta.url));
const repair = readFileSync(`${root}/static/repair.html`, "utf8");
const store = readFileSync(
  `${root}/app/src/lib/reality/realityStore.ts`,
  "utf8",
);
const compiler = readFileSync(
  `${root}/app/src/lib/reality/realityContextCompiler.ts`,
  "utf8",
);
const runtime = readFileSync(
  `${root}/app/src/lib/reality/realityRuntime.ts`,
  "utf8",
);
const chat = readFileSync(
  `${root}/app/src/screens/ChatScreen.tsx`,
  "utf8",
);

for (const token of [
  "HMS_ACTIVITY_R1_REPAIR_DIAGNOSTIC_BEGIN",
  "hms-activity-user",
  "hms-activity-age",
  "hms-activity-source",
  "hms-activity-possibility",
  "hms-activity-registration",
  "hms-activity-error-code",
  "hms-activity-callback",
  "hms-activity-extras",
  "hms-activity-response",
  "hms-activity-count",
  "hms-activity-raw",
  "refreshHmsActivityDiagnostic",
  "setInterval(refreshHmsActivityDiagnostic, 1000)",
]) {
  assert.ok(repair.includes(token), `repair page missing ${token}`);
}

for (const token of [
  "activitySampledAt",
  "REALITY_ACTIVITY_MAX_AGE_MS",
  "getActivityFreshness",
  "getFreshUserActivity",
  'source !== "hms"',
]) {
  assert.ok(store.includes(token), `activity freshness contract missing ${token}`);
}
assert.ok(compiler.includes('"userActivity"'));
assert.ok(compiler.includes("getFreshUserActivity"));
assert.ok(runtime.includes("getActivityBridge"));
assert.ok(runtime.includes("window.ElpisActivity"));
assert.equal(chat.includes("ElpisActivity"), false);
assert.equal(chat.includes("getActivityState"), false);

console.log("HMS_ACTIVITY_R1 closeout source contract: PASS");
