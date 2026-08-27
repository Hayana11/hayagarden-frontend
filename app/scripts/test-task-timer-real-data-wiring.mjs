import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const apiSource = readFileSync(new URL('../src/lib/api.ts', import.meta.url), 'utf8');
const timerSource = readFileSync(new URL('../src/lib/taskTimer.ts', import.meta.url), 'utf8');
const chatSource = readFileSync(new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8');

const {
  pendingTaskToSnapshot,
  computeTaskTimerDisplay,
  TASK_TIMER_POLL_MS,
} = await import('../src/lib/taskTimer.ts');

const pending = {
  id: 17,
  title: '真实行动任务',
  countdown_seconds: 90,
  created_at: 100,
  started_at: 1_000,
};
const snapshot = pendingTaskToSnapshot(pending);
assert.deepEqual(snapshot, {
  taskId: '17',
  title: '真实行动任务',
  mode: 'countdown',
  startedAtMs: 1_000,
  countdownSeconds: 90,
});
assert.equal(pendingTaskToSnapshot({ ...pending, started_at: null }), null);
assert.equal(pendingTaskToSnapshot({ ...pending, countdown_seconds: 0 })?.mode, 'elapsed');
assert.equal(pendingTaskToSnapshot({ ...pending, countdown_seconds: null })?.mode, 'elapsed');

assert.equal(computeTaskTimerDisplay(snapshot, 31_000).primaryLabel, '01:00');
assert.equal(computeTaskTimerDisplay(snapshot, 101_000).primaryLabel, '+00:40');
assert.equal(computeTaskTimerDisplay(snapshot, 101_000).phase, 'overtime');
assert.equal(TASK_TIMER_POLL_MS, 4000);

for (const path of [
  '/api/commands/pending',
  '/api/commands/{id}/started',
  '/api/commands/{id}/done',
  '/api/commands/{id}/cancel',
]) {
  assert.ok(apiSource.includes(path.replace('{id}', '${id}')), 'command API missing: ' + path);
}
assert.ok(timerSource.includes('fetchPendingTaskTimers'));
assert.ok(timerSource.includes('markTaskTimerStarted'));
assert.ok(timerSource.includes('markTaskTimerDone'));
assert.ok(timerSource.includes('startedPostSucceededRef'));
assert.ok(timerSource.includes('pendingRequestRef.current += 1'));
assert.ok(timerSource.includes('setInterval(() =>'));
assert.ok(timerSource.includes('TASK_TIMER_POLL_MS'));
assert.ok(timerSource.includes('document.addEventListener(\'visibilitychange\''));
assert.ok(timerSource.includes('document.removeEventListener(\'visibilitychange\''));
assert.ok(timerSource.includes('Date.now()'));
assert.ok(!timerSource.includes('remaining--'));
assert.ok(!timerSource.includes('elapsed++'));
assert.ok(chatSource.includes('useTaskTimerController(timerFixtureEnabled)'));
assert.ok(!chatSource.includes('useTaskTimerFixtureSnapshot(timerFixtureEnabled)'));

console.log('TASK_TIMER_REAL_DATA_WIRING_PASS');
