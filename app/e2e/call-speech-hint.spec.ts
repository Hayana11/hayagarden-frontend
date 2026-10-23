import { expect, test, type Page } from '@playwright/test';

// Inside the Elpis shell, a missing mic button explains itself: an older Canary
// without ElpisSpeech, or a phone with no speech recognition service. A plain
// browser shows nothing (typing only).

const ACTIVE = { id: 'c3', state: 'active', created_at: 1, answered_at: 2, ended_at: null };

async function activeCall(page: Page) {
  await page.route('**/api/call/current', (route) => route.fulfill({ json: { call: ACTIVE } }));
  await page.route('**/api/call/c3', (route) => route.fulfill({ json: { call: ACTIVE, lines: [] } }));
  await page.route('**/api/call/c3/stream**', (route) =>
    route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' }));
}

test('older Canary without the speech bridge says so', async ({ page }) => {
  await activeCall(page);
  await page.addInitScript(() => {
    (window as unknown as Record<string, unknown>).ElpisNative = {};
  });
  await page.goto('/dash/call');
  await expect(page.getByRole('button', { name: '挂断' })).toBeVisible();
  await expect(page.getByTestId('call-speech-hint')).toContainText('新版 Canary');
  await expect(page.locator('.call-mic')).toHaveCount(0);
});

test('no recognition service on the phone says so', async ({ page }) => {
  await activeCall(page);
  await page.addInitScript(() => {
    (window as unknown as Record<string, unknown>).ElpisSpeech = {
      isAvailable: () => false,
      hasPermission: () => true,
      start: () => undefined,
    };
  });
  await page.goto('/dash/call');
  await expect(page.getByRole('button', { name: '挂断' })).toBeVisible();
  await expect(page.getByTestId('call-speech-hint')).toContainText('语音识别服务');
  await expect(page.locator('.call-mic')).toHaveCount(0);
});

test('a plain browser shows no hint', async ({ page }) => {
  await activeCall(page);
  await page.goto('/dash/call');
  await expect(page.getByRole('button', { name: '挂断' })).toBeVisible();
  await expect(page.getByTestId('call-speech-hint')).toHaveCount(0);
});
