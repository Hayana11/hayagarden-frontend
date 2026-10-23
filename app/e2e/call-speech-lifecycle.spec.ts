import { expect, test, type Page } from '@playwright/test';

// The Canary shell destroys its SpeechRecognizer in onPause (screen locked, app
// backgrounded, a system dialog on top). The call page must not keep claiming
// "在听…" afterwards, and must listen again once it is back in front.

const CALL = { id: 'c1', state: 'active', created_at: 1, answered_at: 1, ended_at: null };

type Routes = { holdReply: boolean; replyPending: boolean };

async function installCallRoutes(page: Page): Promise<Routes> {
  const routes: Routes = { holdReply: false, replyPending: false };
  let seq = 0;
  await page.route('**/api/call/current', (route) => route.fulfill({ json: { call: CALL } }));
  await page.route('**/api/call/c1', (route) => route.fulfill({ json: { call: CALL, lines: [] } }));
  await page.route('**/api/call/c1/say', (route) => {
    const text = (route.request().postDataJSON() as { text: string }).text;
    routes.replyPending = true;
    seq += 1;
    return route.fulfill({ json: { line: { seq, speaker: 'her', text } } });
  });
  // An empty stream ends at once and the page reopens it after 2s, so a released
  // reply reaches the page on the next reopen.
  await page.route('**/api/call/c1/stream**', (route) => {
    let body = '';
    if (routes.replyPending && !routes.holdReply) {
      routes.replyPending = false;
      seq += 1;
      body = `event: line\ndata: ${JSON.stringify({ seq, speaker: 'him', text: '嗯，我在。', audio_key: null })}\n\n`;
    }
    return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body });
  });
  // No audio: the line counts as spoken (voiceless) and the mic comes back after it.
  await page.route('**/api/call/c1/audio/**', (route) => route.fulfill({ status: 404, body: '' }));
  await page.route('**/api/call/c1/hangup', (route) => route.fulfill({ json: { call: { ...CALL, state: 'ended' }, record: '' } }));
  return routes;
}

async function installFakeShell(page: Page) {
  await page.addInitScript(() => {
    const w = window as unknown as Record<string, unknown>;
    const log: string[] = [];
    const utterances: string[] = [];
    w.__speechLog = log;
    w.__utterances = utterances;
    const emit = (event: object) => setTimeout(() => {
      const sink = w.__elpisSpeech as ((json: string) => void) | undefined;
      if (sink) sink(JSON.stringify(event));
    }, 0);
    let hidden = false;
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') });
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
    w.__native = emit;
    w.__setHidden = (h: boolean) => {
      hidden = h;
      document.dispatchEvent(new Event('visibilitychange'));
    };
    w.ElpisSpeech = {
      isAvailable: () => true,
      hasPermission: () => true,
      requestPermission: () => emit({ type: 'permission', granted: true }),
      start: (language: string) => {
        log.push(`start:${language}`);
        const text = utterances.shift();
        if (text) setTimeout(() => emit({ type: 'final', text }), 300);
      },
      stop: () => log.push('stop'),
      cancel: () => log.push('cancel'),
    };
  });
}

const starts = (page: Page) =>
  page.evaluate(() => ((window as unknown as { __speechLog: string[] }).__speechLog).filter((l) => l.startsWith('start:')).length);
const lastCall = (page: Page) =>
  page.evaluate(() => {
    const log = (window as unknown as { __speechLog: string[] }).__speechLog;
    return log[log.length - 1];
  });
const setHidden = (page: Page, hidden: boolean) =>
  page.evaluate((h) => (window as unknown as { __setHidden: (h: boolean) => void }).__setHidden(h), hidden);
const native = (page: Page, event: object) =>
  page.evaluate((e) => (window as unknown as { __native: (e: object) => void }).__native(e), event);
const waitForStartsAbove = (page: Page, n: number, timeout = 3000) =>
  page.waitForFunction(
    (count) => ((window as unknown as { __speechLog: string[] }).__speechLog).filter((l) => l.startsWith('start:')).length > count,
    n,
    { timeout },
  );

test.describe('CallScreen speech lifecycle (Canary shell)', () => {
  test('background → foreground clears and restores listening', async ({ page }) => {
    await installCallRoutes(page);
    await installFakeShell(page);
    await page.goto('/dash/call');
    const mic = page.locator('.call-mic');

    await mic.click();
    await waitForStartsAbove(page, 0);
    await expect(mic).toHaveText('在听…');

    // Screen locked / app backgrounded.
    await setHidden(page, true);
    expect(await lastCall(page)).toBe('cancel');
    await expect(mic).toHaveText('麦已开');
    const hiddenStarts = await starts(page);
    await page.waitForTimeout(1200);
    expect(await starts(page)).toBe(hiddenStarts);

    // Back in front: listen again in zh-CN.
    await setHidden(page, false);
    await waitForStartsAbove(page, hiddenStarts);
    expect(await lastCall(page)).toBe('start:zh-CN');
    await expect(mic).toHaveText('在听…');
  });

  test('native pause without the page going hidden', async ({ page }) => {
    await installCallRoutes(page);
    await installFakeShell(page);
    await page.goto('/dash/call');
    const mic = page.locator('.call-mic');
    await mic.click();
    await waitForStartsAbove(page, 0);

    // A system dialog pauses the activity: release() reports the killed listen.
    await native(page, { type: 'error', code: 'paused' });
    await expect(mic).toHaveText('麦已开');
    const pausedStarts = await starts(page);
    await page.waitForTimeout(1200);
    expect(await starts(page)).toBe(pausedStarts);

    await native(page, { type: 'resumed' });
    await waitForStartsAbove(page, pausedStarts);
    await expect(mic).toHaveText('在听…');
  });

  test('coming back while his reply is pending waits for him', async ({ page }) => {
    const routes = await installCallRoutes(page);
    await installFakeShell(page);
    await page.goto('/dash/call');
    const mic = page.locator('.call-mic');

    routes.holdReply = true;
    await page.evaluate(() => (window as unknown as { __utterances: string[] }).__utterances.push('你在吗'));
    await mic.click();
    await expect(page.getByText('你在吗')).toBeVisible();
    const afterSay = await starts(page);

    await setHidden(page, true);
    await setHidden(page, false);
    await page.waitForTimeout(1200);
    expect(await starts(page)).toBe(afterSay);
    await expect(mic).toHaveText('麦已开');

    routes.holdReply = false;
    await expect(page.getByText('嗯，我在。')).toBeVisible({ timeout: 5000 });
    await waitForStartsAbove(page, afterSay, 5000);
    await expect(mic).toHaveText('在听…');
  });
});
