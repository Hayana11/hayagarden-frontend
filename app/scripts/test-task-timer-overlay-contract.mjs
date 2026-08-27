import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const componentPath = fileURLToPath(new URL('../src/components/TaskTimerOverlay.tsx', import.meta.url));
const apiPath = fileURLToPath(new URL('../src/lib/api.ts', import.meta.url));
const chatPath = fileURLToPath(new URL('../src/screens/ChatScreen.tsx', import.meta.url));
const source = readFileSync(componentPath, 'utf8');
const apiSource = readFileSync(apiPath, 'utf8');
const chatSource = readFileSync(chatPath, 'utf8');

const {
  TASK_TIMER_CANCEL_HOLD_MS,
  TASK_TIMER_POLL_MS,
  selectActiveTask,
  formatTaskTimerClock,
  getTaskTimerView,
} = await import('../src/components/TaskTimerOverlay.tsx');

const tasks = [
  { id: 7, title: '先做最早的事', countdown_seconds: 60, created_at: 1, started_at: 10 },
  { id: 11, title: '排队的事', countdown_seconds: null, created_at: 2, started_at: null },
];
assert.equal(TASK_TIMER_POLL_MS, 4000, 'pending poll cadence is frozen at four seconds');
assert.equal(TASK_TIMER_CANCEL_HOLD_MS, 1100, 'cancel remains a long press');
assert.equal(selectActiveTask(tasks)?.id, 7, 'FIFO keeps backend order');
assert.equal(formatTaskTimerClock(61_000), '01:01');

const countdown = getTaskTimerView(tasks[0], 25_000);
assert.equal(countdown.state, 'countdown');
assert.equal(countdown.label, '00:35');
const overtime = getTaskTimerView(tasks[0], 75_000);
assert.equal(overtime.state, 'overtime');
assert.equal(overtime.label, '+00:15');
const countup = getTaskTimerView(tasks[1], 20_010);
assert.equal(countup.state, 'unstarted');
const startedCountup = getTaskTimerView({ ...tasks[1], started_at: 10_000 }, 20_010);
assert.equal(startedCountup.state, 'countup');
assert.equal(startedCountup.label, '00:10');
assert.equal(getTaskTimerView({ ...tasks[1], started_at: null }, 20_010).label, '开始未确认');

for (const path of ['/api/commands/pending', '/api/commands/id/started', '/api/commands/id/done', '/api/commands/id/cancel']) {
  assert.ok(apiSource.includes(path.replace('id', '${id}')), 'real command API wrapper missing: ' + path);
}
assert.ok(!apiSource.slice(apiSource.indexOf('export type TaskTimerPendingCommand')).includes('withFallback'), 'command APIs must not use mock fallback');
assert.ok(chatSource.includes("import { TaskTimerOverlay } from '../components/TaskTimerOverlay';"));
assert.equal((chatSource.match(/<TaskTimerOverlay \/>/g) || []).length, 1, 'ChatScreen only mounts the timer once');

assert.ok(source.includes("document.addEventListener('visibilitychange'"));
assert.ok(source.includes("if (!visible || !activeTask || activeTask.started_at != null) return;"));
assert.ok(source.includes("markTaskTimerStarted(id)"));
assert.ok(source.includes("failedStartIdRef.current = id"));
assert.ok(source.includes("Never invent a local start time"));
assert.ok(source.includes('nowMs - task.started_at'), 'clock must derive from persisted started_at');
assert.ok(!source.includes('remaining--') && !source.includes('elapsed++'));
assert.ok(source.includes("return { state: 'overtime'"));
assert.ok(source.includes('label: `+${formatTaskTimerClock(-remainingMs)}`'));
assert.ok(source.includes("Number(task.countdown_seconds || 0) * 1000"));
assert.ok(source.includes('mutationBusyRef.current'), 'mutation gate prevents duplicate clicks');
assert.equal((source.match(/markTaskTimerDone\(id\)/g) || []).length, 1, 'done has one POST site');
assert.ok(source.includes('The mutation result is uncertain: reconcile once, never auto-POST again.'));
assert.ok(source.includes('const stillPending = await reconcileAfterMutation(epoch, id);'));
assert.ok(source.includes('const epoch = ++mutationEpochRef.current;'));
assert.ok(source.includes('pendingRequestRef.current += 1;'));
assert.ok(source.includes('requestId !== pendingRequestRef.current || epoch !== mutationEpochRef.current'));
assert.ok(source.includes('window.setTimeout'));
assert.ok(source.includes('TASK_TIMER_CANCEL_HOLD_MS'));
assert.ok(source.includes('window.clearTimeout'));
assert.ok(source.includes('window.clearInterval'));
assert.ok(source.includes("document.removeEventListener('visibilitychange'"));
assert.ok(source.includes('if (!activeTask) return null;'));
assert.ok(source.includes('tasks.length > 1 ? <span className="task-timer-queue">+{tasks.length - 1}</span> : null'));

console.log('TASK_TIMER_OVERLAY_CONTRACT_PASS');
