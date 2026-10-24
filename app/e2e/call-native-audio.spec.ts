import { expect, test, type Page } from '@playwright/test';

// P2H-1: in the Canary shell his lines play through ElpisCallAudio (earpiece by
// default); the page only names lines by call id and seq.
const CALL = { id: 'c1', state: 'active', created_at: 1, answered_at: 1, ended_at: null };
const HIS_LINES = [
  { seq: 1, speaker: 'him', text: '先听我说', audio_key: null },
  { seq: 2, speaker: 'him', text: '然后再说一句', audio_key: null },
];

type Log = string[];
type W = { __audioLog: Log; __emitAudio: (event: object) => void; __emitVoice: (event: object) => void };

async function installRoutes(page: Page): Promise<string[]> {
  let streamed = false;
  const audioRequests: string[] = [];
  await page.route('**/api/call/current', (route) => route.fulfill({ json: { call: CALL } }));
  await page.route('**/api/call/c1', (route) => route.fulfill({ json: { call: CALL, lines: [] } }));
  await page.route('**/api/call/c1/interrupt**', (route) => route.fulfill({ json: { ok: true } }));
  await page.route('**/api/call/c1/stream**', (route) => {
    if (streamed) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' });
    streamed = true;
    return route.fulfill({
      status: 200,
      headers: { 'content-type': 'text/event-stream' },
      body: HIS_LINES.map((line) => `event: line\ndata: ${JSON.stringify(line)}\n\n`).join(''),
    });
  });
  await page.route('**/api/call/c1/audio/**', (route) => {
    audioRequests.push(route.request().url());
    return route.fulfill({ status: 200, body: 'mp3' });
  });
  await page.route('**/api/call/c1/hangup', (route) => route.fulfill({ json: { call: { ...CALL, state: 'ended' }, record: 'ᯅ 通话时长 00:05' } }));
  return audioRequests;
}

async function installFakeShell(page: Page, withCallAudio: boolean) {
  await page.addInitScript((callAudio) => {
    const w = window as unknown as Record<string, unknown>;
    const log: string[] = [];
    w.__audioLog = log;
    const sink = (name: string) => (event: object) => {
      const fn = w[name] as ((json: string) => void) | undefined;
      if (fn) fn(JSON.stringify(event));
    };
    w.__emitAudio = sink('__elpisCallAudio');
    w.__emitVoice = sink('__elpisVoice');
    w.ElpisVoice = {
      isAvailable: () => true,
      hasPermission: () => true,
      start: () => undefined,
      stop: () => undefined,
      cancel: () => undefined,
      takeUtterance: () => '',
    };
    if (callAudio) {
      w.ElpisCallAudio = {
        isAvailable: () => true,
        begin: () => {
          log.push('begin');
          (w.__emitAudio as (event: object) => void)({ type: 'route', route: 'earpiece' });
        },
        play: (callId: string, seq: number) => log.push(`play:${callId}:${seq}`),
        stop: () => log.push('stop'),
        setSpeaker: (on: boolean) => log.push(`speaker:${on}`),
        end: () => log.push('end'),
      };
    }
    HTMLMediaElement.prototype.play = function play() {
      log.push('html-audio-play');
      return Promise.resolve();
    };
  }, withCallAudio);
}

// From the call's begin on: dev StrictMode runs one unmount cleanup (a no-op stop) at mount.
const audioLog = (page: Page) => page.evaluate(() => {
  const log = (window as unknown as W).__audioLog;
  const start = log.indexOf('begin');
  return start < 0 ? [] : log.slice(start);
});
const emitAudio = (page: Page, event: object) => page.evaluate((e) => (window as unknown as W).__emitAudio(e), event);

test.describe('P2H-1 native call audio', () => {
  test('plays his lines natively in seq order, earpiece first, never through <audio>', async ({ page }) => {
    const audioRequests = await installRoutes(page);
    await installFakeShell(page, true);
    await page.goto('/dash/call');

    await expect.poll(() => audioLog(page)).toEqual(['begin', 'play:c1:1']);
    const speaker = page.getByRole('button', { name: '免提' });
    await expect(speaker).toHaveAttribute('aria-pressed', 'false');

    await emitAudio(page, { type: 'started', seq: 1 });
    await emitAudio(page, { type: 'ended', seq: 2 });  // not the playing line: ignored
    await page.waitForTimeout(100);
    expect(await audioLog(page)).toEqual(['begin', 'play:c1:1']);

    await emitAudio(page, { type: 'ended', seq: 1 });
    await expect.poll(() => audioLog(page)).toEqual(['begin', 'play:c1:1', 'play:c1:2']);
    await emitAudio(page, { type: 'error', seq: 2, code: 'ERROR_CODE_IO_BAD_HTTP_STATUS' });
    await expect(page.locator('.call-voiceless')).toHaveCount(1);
    expect(audioRequests).toEqual([]);
    expect(await audioLog(page)).not.toContain('html-audio-play');
  });

  test('the speaker button asks the shell and follows the route it reports', async ({ page }) => {
    await installRoutes(page);
    await installFakeShell(page, true);
    await page.goto('/dash/call');
    await expect.poll(() => audioLog(page)).toContain('play:c1:1');

    const speaker = page.getByRole('button', { name: '免提' });
    await speaker.click();
    await expect.poll(() => audioLog(page)).toContain('speaker:true');
    await expect(speaker).toHaveAttribute('aria-pressed', 'false');  // not until the shell says so
    await emitAudio(page, { type: 'route', route: 'speaker' });
    await expect(speaker).toHaveAttribute('aria-pressed', 'true');
    expect(await audioLog(page)).not.toContain('stop');  // switching never interrupts the line

    await speaker.click();
    await expect.poll(() => audioLog(page)).toContain('speaker:false');
    await emitAudio(page, { type: 'route', route: 'earpiece' });
    await expect(speaker).toHaveAttribute('aria-pressed', 'false');
  });

  test('barge-in stops the native player and hanging up ends call audio', async ({ page }) => {
    await installRoutes(page);
    await installFakeShell(page, true);
    await page.goto('/dash/call');
    await expect.poll(() => audioLog(page)).toContain('play:c1:1');

    await page.locator('.call-mic').click();
    await page.evaluate(() => (window as unknown as W).__emitVoice({ type: 'speech_start', seq: 7 }));
    await expect.poll(() => audioLog(page)).toContain('stop');
    await emitAudio(page, { type: 'ended', seq: 1 });  // late event after the stop: no next line
    await page.waitForTimeout(100);
    expect((await audioLog(page)).filter((entry) => entry.startsWith('play:'))).toEqual(['play:c1:1']);

    await page.getByRole('button', { name: '挂断' }).click();
    await expect.poll(() => audioLog(page)).toContain('end');
  });

  test('without the bridge the page keeps <audio> and shows no speaker button', async ({ page }) => {
    const audioRequests = await installRoutes(page);
    await installFakeShell(page, false);
    await page.goto('/dash/call');
    await expect(page.getByText('先听我说')).toBeVisible();
    await expect.poll(() => audioRequests.length).toBeGreaterThan(0);
    expect(audioRequests[0]).toContain('/api/call/c1/audio/1');
    await expect(page.getByRole('button', { name: '免提' })).toHaveCount(0);
  });
});
