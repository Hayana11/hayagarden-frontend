import { expect, test, type Page } from '@playwright/test';

// Bilingual calls: his line shows its Chinese gloss; tapping shows what he said; the
// choice is remembered on this device, on the call page and in the transcript sheet.
const CALL = { id: 'c1', state: 'active', created_at: 1, answered_at: 1, ended_at: null, language: 'en' };
const LINES = [
  { seq: 1, speaker: 'her', text: '早上好' },
  { seq: 2, speaker: 'him', text: '[softly] Wake up, little cat.', gloss: '醒醒，小猫。', audio_key: null },
  { seq: 3, speaker: 'him', text: 'No subtitle here.', audio_key: null },
];

async function installRoutes(page: Page) {
  await page.route('**/api/call/current', (route) => route.fulfill({ json: { call: CALL } }));
  await page.route('**/api/call/c1', (route) => route.fulfill({ json: { call: CALL, lines: LINES } }));
  await page.route('**/api/call/c1/stream**', (route) => route.fulfill({
    status: 200,
    headers: { 'content-type': 'text/event-stream' },
    body: LINES.map((line) => `event: line\ndata: ${JSON.stringify(line)}\n\n`).join(''),
  }));
  await page.route('**/api/call/c1/audio/**', (route) => route.fulfill({ status: 503, json: { ok: false } }));
  await page.addInitScript(() => {
    HTMLMediaElement.prototype.play = () => Promise.resolve();
  });
}

test.describe('Bilingual call subtitles', () => {
  test('gloss first, original on tap, remembered across visits', async ({ page }) => {
    await installRoutes(page);
    await page.goto('/dash/call');
    const his = page.locator('.call-line--him').first();
    await expect(his.locator('.call-bubble')).toContainText('醒醒，小猫。');
    await expect(his).not.toContainText('Wake up');
    await expect(page.locator('.call-subtitle-toggle')).toHaveCount(1);  // only the line that has a gloss
    await expect(page.getByText('No subtitle here.')).toBeVisible();
    await expect(page.getByText('早上好')).toBeVisible();

    await his.getByRole('button', { name: '显示原文' }).click();
    await expect(his.locator('.call-bubble')).toContainText('Wake up, little cat.');
    await expect(his).not.toContainText('[softly]');
    await expect(his.getByRole('button', { name: '显示中文' })).toBeVisible();

    await page.reload();
    await expect(page.locator('.call-line--him').first().locator('.call-bubble')).toContainText('Wake up, little cat.');
    await page.getByRole('button', { name: '显示中文' }).click();
    await expect(page.locator('.call-line--him').first().locator('.call-bubble')).toContainText('醒醒，小猫。');
  });
});
