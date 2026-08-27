import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

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
assert.equal(TASK_TIMER_POLL_MS, 4000);
assert.equal(TASK_TIMER_CANCEL_HOLD_MS, 1100);
assert.equal(selectActiveTask(tasks)?.id, 7);
assert.equal(formatTaskTimerClock(61_000), '01:01');
assert.equal(getTaskTimerView(tasks[0], 25_000).label, '00:35');
assert.equal(getTaskTimerView(tasks[0], 75_000).label, '+00:15');
assert.equal(getTaskTimerView(tasks[0], 75_000).state, 'overtime');
assert.equal(getTaskTimerView(tasks[1], 20_010).state, 'unstarted');
assert.equal(getTaskTimerView({ ...tasks[1], started_at: 10_000 }, 20_010).label, '00:10');

for (const path of ['/api/commands/pending', '/api/commands/id/started', '/api/commands/id/done', '/api/commands/id/cancel']) {
  assert.ok(apiSource.includes(path.replace('id', '${id}')), 'real command API wrapper missing: ' + path);
}
assert.ok(!apiSource.slice(apiSource.indexOf('export type TaskTimerPendingCommand')).includes('withFallback'));
assert.ok(chatSource.includes("import { TaskTimerOverlay } from '../components/TaskTimerOverlay';"));
assert.equal((chatSource.match(/<TaskTimerOverlay \/>/g) || []).length, 1);

assert.ok(source.includes('mountedRef.current = true'));
assert.ok(source.includes("document.addEventListener('visibilitychange'"));
assert.ok(source.includes("if (!visible || !activeTask || activeTask.started_at != null) return;"));
assert.ok(source.includes('failedStartIdRef.current = id'));
assert.ok(source.includes('Never invent a local start time'));
assert.ok(source.includes('nowMs - task.started_at'));
assert.ok(source.includes('mutationPhaseRef.current !== \'idle\''));
assert.ok(source.includes('The controls stay locked until one successful pending GET confirms the result.'));
assert.ok(source.includes('The mutation result is uncertain') || source.includes('mutationError'));
assert.ok(!source.includes('remaining--') && !source.includes('elapsed++'));
assert.ok(source.includes('window.clearTimeout'));
assert.ok(source.includes('window.clearInterval'));
assert.ok(source.includes("document.removeEventListener('visibilitychange'"));
assert.ok(source.includes('if (!activeTask) return null;'));

const runtimeDir = join(process.cwd(), '.tmp-task-timer-contract-runtime');
const runtimeHtml = '<!doctype html><html><body><div id="root"></div><script type="module" src="/.tmp-task-timer-contract-runtime/entry.tsx"></script></body></html>';
const runtimeEntry = `import React, { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { TaskTimerOverlay } from '../src/components/TaskTimerOverlay';

const root = createRoot(document.getElementById('root'));
root.render(<StrictMode><TaskTimerOverlay /></StrictMode>);
window.__taskTimerUnmount = () => root.unmount();
`;
mkdirSync(runtimeDir, { recursive: true });
writeFileSync(join(runtimeDir, 'index.html'), runtimeHtml);
writeFileSync(join(runtimeDir, 'entry.tsx'), runtimeEntry);

const viteCli = join(process.cwd(), 'node_modules', 'vite', 'bin', 'vite.js');
const vite = spawn(process.execPath, [viteCli, '--host', '127.0.0.1', '--port', '5174'], {
  cwd: process.cwd(),
  stdio: 'ignore',
});
let browser = null;
let page = null;
let reconciliationRelease = null;
let unmountPendingRelease = null;
let pendingCalls = 0;
let startedCalls = 0;
let startedConcurrent = 0;
let maxStartedConcurrent = 0;
let doneCalls = 0;
let cancelCalls = 0;
let reconciliationStarted = false;
let unmountPendingStarted = false;
let holdNextPending = false;
const startedAt = Date.now() - 120_000;
const initialTask = { id: 7, title: '严格模式任务', countdown_seconds: 60, created_at: startedAt, started_at: null };
const persistedTask = { ...initialTask, started_at: startedAt };
const queueTask = { id: 11, title: '排队任务', countdown_seconds: null, created_at: startedAt, started_at: null };
const taskResponse = () => ({ commands: startedCalls >= 2 ? [persistedTask, queueTask] : [initialTask, queueTask] });

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForServer(url) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // Vite is still starting.
    }
    await wait(100);
  }
  throw new Error('TEST_ENV_BLOCKED: Vite dev server did not start');
}

try {
  await waitForServer('http://127.0.0.1:5174/.tmp-task-timer-contract-runtime/index.html');
  try {
    browser = await chromium.launch({ headless: true });
  } catch (error) {
    throw new Error('TEST_ENV_BLOCKED: Playwright Chromium unavailable: ' + String(error));
  }
  page = await browser.newPage();
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));

  await page.route('**/api/commands/pending', async (route) => {
    pendingCalls += 1;
    if (holdNextPending) {
      unmountPendingStarted = true;
      await new Promise((resolve) => { unmountPendingRelease = resolve; });
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(taskResponse()) });
      return;
    }
    if (doneCalls === 1 && !reconciliationStarted) {
      reconciliationStarted = true;
      await new Promise((resolve) => { reconciliationRelease = resolve; });
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(taskResponse()) });
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(taskResponse()) });
  });
  await page.route('**/api/commands/7/started', async (route) => {
    startedCalls += 1;
    startedConcurrent += 1;
    maxStartedConcurrent = Math.max(maxStartedConcurrent, startedConcurrent);
    await wait(100);
    startedConcurrent -= 1;
    if (startedCalls === 1) {
      await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'simulated start failure' }) });
    } else {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
    }
  });
  await page.route('**/api/commands/7/done', async (route) => {
    doneCalls += 1;
    await route.abort('failed');
  });
  await page.route('**/api/commands/7/cancel', async (route) => {
    cancelCalls += 1;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
  });

  await page.goto('http://127.0.0.1:5174/.tmp-task-timer-contract-runtime/index.html');
  await page.locator('[data-task-timer-state]').waitFor();
  await page.locator('.task-timer-title').waitFor();
  assert.equal(await page.locator('.task-timer-queue').textContent(), '+1');
  await page.waitForFunction(() => document.querySelector('.task-timer-status')?.textContent === '开始未确认');
  assert.equal(startedCalls, 1, 'StrictMode must not duplicate the first started POST');
  await wait(1500);
  assert.equal(startedCalls, 1, 'failed started must not immediately retry');
  const retryDeadline = Date.now() + 10_000;
  while (startedCalls < 2 && Date.now() < retryDeadline) await wait(100);
  assert.equal(startedCalls, 2, 'the next normal visible poll must retry started');
  assert.equal(maxStartedConcurrent, 1, 'started requests never overlap');
  await page.waitForFunction(() => ['countdown', 'overtime'].includes(document.querySelector('[data-task-timer-state]')?.getAttribute('data-task-timer-state')));
  assert.equal(await page.locator('[data-task-timer-state]').getAttribute('data-task-timer-state'), 'overtime', 'persisted started_at drives the clock');

  const doneButton = page.locator('.task-timer-done');
  await doneButton.click();
  const reconcileDeadline = Date.now() + 3000;
  while (!reconciliationStarted && Date.now() < reconcileDeadline) await wait(50);
  assert.equal(doneCalls, 1, 'one done gesture sends one POST');
  assert.equal(reconciliationStarted, true, 'uncertain done starts reconciliation');
  const pendingAtReconcile = pendingCalls;
  assert.equal(await doneButton.isDisabled(), true, 'controls stay locked during reconciliation');
  await wait(TASK_TIMER_POLL_MS + 500);
  assert.equal(pendingCalls, pendingAtReconcile, 'ordinary poll cannot supersede reconciliation');
  reconciliationRelease();
  await page.waitForFunction(async () => !(await document.querySelector('.task-timer-done')?.hasAttribute('disabled')));
  assert.equal(doneCalls, 1, 'uncertain done is never auto-retried');

  const cancelButton = page.locator('.task-timer-cancel');
  await cancelButton.click();
  await wait(150);
  assert.equal(cancelCalls, 0, 'short cancel gesture does not cancel');
  await cancelButton.dispatchEvent('pointerdown');
  await wait(TASK_TIMER_CANCEL_HOLD_MS + 150);
  await cancelButton.dispatchEvent('pointerup');
  const cancelDeadline = Date.now() + 2000;
  while (cancelCalls < 1 && Date.now() < cancelDeadline) await wait(50);
  assert.equal(cancelCalls, 1, 'long cancel gesture cancels once');

  holdNextPending = true;
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  const unmountDeadline = Date.now() + 3000;
  while (!unmountPendingStarted && Date.now() < unmountDeadline) await wait(50);
  assert.equal(unmountPendingStarted, true, 'unmount test has an in-flight pending GET');
  await page.evaluate(() => window.__taskTimerUnmount());
  unmountPendingRelease();
  await wait(250);
  assert.equal(await page.locator('.task-timer-overlay').count(), 0, 'unmount removes the component safely');
  assert.deepEqual(pageErrors, []);

  console.log('TASK_TIMER_OVERLAY_RUNTIME_CONTRACT_PASS');
} finally {
  if (browser) await browser.close();
  vite.kill();
  rmSync(runtimeDir, { recursive: true, force: true });
}
