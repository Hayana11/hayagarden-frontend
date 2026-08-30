import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../..', import.meta.url));
const read = (path) => readFileSync(`${root}/${path}`, 'utf8');
const repair = read('static/repair.html');
const toolroom = read('app/src/screens/ToolroomScreen.tsx');
const runtime = read('app/src/lib/reality/realityRuntime.ts');
const store = read('app/src/lib/reality/realityStore.ts');
const compiler = read('app/src/lib/reality/realityContextCompiler.ts');
const chat = read('app/src/screens/ChatScreen.tsx');

for (const token of [
  'HMS_ACTIVITY_R1_REPAIR_DIAGNOSTIC_BEGIN',
  'hms-activity-user',
  'refreshHmsActivityDiagnostic',
  'setInterval(refreshHmsActivityDiagnostic, 1000)',
]) {
  assert.equal(repair.includes(token), false, `repair page still owns HMS diagnostic: ${token}`);
}

for (const token of [
  'HMS Activity',
  'userActivity',
  'activity age',
  'activity source',
  'possibility',
  'registration',
  'lastErrorCode',
  'callbackReceived',
  'intentHasExtras',
  'responsePresent',
  'activityDataCount',
  'raw activity code',
  'reality.activity',
  'getActivityFreshness',
  'window.setInterval(refreshActivityUi, 1000)',
  'visibilitychange',
  '复用既有 ElpisActivity bridge',
]) {
  assert.ok(toolroom.includes(token), `tool room missing diagnostic token: ${token}`);
}

for (const token of ['getActivityBridge', 'window.ElpisActivity', 'refreshActivity']) {
  assert.ok(runtime.includes(token), `runtime bridge reuse missing: ${token}`);
}
assert.equal(toolroom.includes('window.ElpisActivity'), false, 'tool room must not register/read a second HMS bridge');

for (const token of ['activitySampledAt', 'REALITY_ACTIVITY_MAX_AGE_MS', 'getActivityFreshness', 'getFreshUserActivity']) {
  assert.ok(store.includes(token), `activity freshness contract missing: ${token}`);
}
assert.ok(compiler.includes('getActivityFreshness'));
assert.ok(compiler.includes('getActivitySemanticConfidence'));
assert.ok(compiler.includes('环境【较暗】'));
assert.ok(compiler.includes('手机【正面朝上平放】'));
assert.ok(compiler.includes('"userActivity"'));
assert.equal(chat.includes('ElpisActivity'), false);
assert.equal(chat.includes('getActivityState'), false);

for (const token of ['HMS_ACTIVITY_R1_REPAIR_DIAGNOSTIC_BEGIN', 'hms-activity-user', 'refreshHmsActivityDiagnostic']) {
  assert.equal(repair.includes(token), false);
}

console.log('HMS_ACTIVITY_R1_MOVE_DIAGNOSTIC_TO_TOOL_ROOM: PASS');
