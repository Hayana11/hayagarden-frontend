import { expect, test, type Page } from '@playwright/test';

const CALL = { id: 'c1', state: 'active', created_at: 1, answered_at: 1, ended_at: null };
const SILENT_WAV = 'UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=';

async function installRoutes(page: Page) {
  let streamed = false;
  let seq = 1;
  await page.route('**/api/call/current', (route) => route.fulfill({ json: { call: CALL } }));
  await page.route('**/api/call/c1', (route) => route.fulfill({ json: { call: CALL, lines: [] } }));
  await page.route('**/api/call/c1/interrupt**', (route) => route.fulfill({ json: { ok: true } }));
  await page.route('**/api/call/c1/hear**', async (route) => {
    const body = route.request().postDataBuffer();
    expect(route.request().url()).toContain('seq=7');
    expect(route.request().headers()['content-type']).toBe('audio/wav');
    expect(body && body.length).toBeGreaterThanOrEqual(44);
    seq += 1;
    return route.fulfill({ json: { ok: true, utterance: 7, text: '我回来了', line: { seq, speaker: 'her', text: '我回来了' } } });
  });
  await page.route('**/api/call/c1/stream**', (route) => {
    if (streamed) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' });
    streamed = true;
    const line = { seq: 1, speaker: 'him', text: '先听我说', audio_key: null };
    return route.fulfill({
      status: 200,
      headers: { 'content-type': 'text/event-stream' },
      body: `event: line\ndata: ${JSON.stringify(line)}\n\n`,
    });
  });
  await page.route('**/api/call/c1/audio/**', (route) => route.fulfill({ status: 200, body: 'mp3' }));
  await page.route('**/api/call/c1/hangup', (route) => route.fulfill({ json: { call: { ...CALL, state: 'ended' }, record: '' } }));
}

async function installFakeShell(page: Page) {
  await page.addInitScript((silent) => {
    const w = window as unknown as Record<string, unknown>;
    const voiceLog: string[] = [];
    const speechLog: string[] = [];
    w.__voiceLog = voiceLog;
    w.__speechLog = speechLog;
    w.__nativeVoice = (event: object) => {
      const sink = w.__elpisVoice as ((json: string) => void) | undefined;
      if (sink) sink(JSON.stringify(event));
    };
    w.__emitVoice = (event: object) => (w.__nativeVoice as (event: object) => void)(event);
    w.ElpisSpeech = {
      isAvailable: () => true,
      hasPermission: () => true,
      start: () => speechLog.push('speech-start'),
      cancel: () => speechLog.push('speech-cancel'),
    };
    w.ElpisVoice = {
      isAvailable: () => true,
      hasPermission: () => true,
      start: () => voiceLog.push('start'),
      stop: () => voiceLog.push('stop'),
      cancel: () => voiceLog.push('cancel'),
      takeUtterance: (seq: number) => {
        voiceLog.push(`take:${seq}`);
        return seq === 7 ? silent : '';
      },
    };
    const originalPlay = HTMLMediaElement.prototype.play;
    const originalPause = HTMLMediaElement.prototype.pause;
    HTMLMediaElement.prototype.play = () => Promise.resolve();
    HTMLMediaElement.prototype.pause = function pause() {
      voiceLog.push('audio-pause');
      return originalPause.call(this);
    };
    w.__restoreAudio = () => {
      HTMLMediaElement.prototype.play = originalPlay;
      HTMLMediaElement.prototype.pause = originalPause;
    };
  }, SILENT_WAV);
}

test.describe('P2F native voice bridge', () => {
  test('prefers ElpisVoice, barges in, uploads WAV with sequence, and keeps capture running', async ({ page }) => {
    await installRoutes(page);
    await installFakeShell(page);
    await page.goto('/dash/call');
    await page.waitForFunction(() => !!(window as unknown as { __elpisVoice?: unknown }).__elpisVoice);

    const mic = page.locator('.call-mic');
    await expect(mic).toHaveText('开麦');
    await mic.click();
    await expect(mic).toHaveText('麦已开');
    expect(await page.evaluate(() => (window as unknown as { __speechLog: string[] }).__speechLog)).toEqual([]);

    await page.evaluate(() => (window as unknown as { __emitVoice: (event: object) => void }).__emitVoice({ type: 'speech_start', seq: 7 }));
    await expect.poll(() => page.evaluate(() => (window as unknown as { __voiceLog: string[] }).__voiceLog)).toContain('audio-pause');
    await expect.poll(() => page.evaluate(() => (window as unknown as { __voiceLog: string[] }).__voiceLog)).toContain('start');

    await page.evaluate(() => (window as unknown as { __emitVoice: (event: object) => void }).__emitVoice({
      type: 'utterance_ready', seq: 7, duration_ms: 900, bytes: 52,
    }));
    await expect(page.getByText('我回来了')).toBeVisible();
    const log = await page.evaluate(() => (window as unknown as { __voiceLog: string[] }).__voiceLog);
    expect(log).toContain('take:7');
    expect(log).not.toContain('stop');
    await page.evaluate(() => (window as unknown as { __restoreAudio: () => void }).__restoreAudio());
  });
});
