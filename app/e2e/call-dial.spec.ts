import { expect, test } from '@playwright/test';

// "拨给费佳": she calls him. One tap rings and picks up, so the page opens the
// call already connected instead of showing an incoming call she must answer.

const RINGING = { id: 'c2', state: 'ringing', created_at: 1, answered_at: null, ended_at: null };
const ACTIVE = { ...RINGING, state: 'active', answered_at: 2 };

test('dialing him opens an active call in one tap', async ({ page }) => {
  const calls: string[] = [];
  await page.route('**/api/call/current', (route) => route.fulfill({ json: { call: null } }));
  await page.route('**/api/call/ring', (route) => {
    calls.push('ring');
    return route.fulfill({ json: { call: RINGING } });
  });
  await page.route('**/api/call/c2/answer', (route) => {
    calls.push('answer');
    return route.fulfill({ json: { call: ACTIVE } });
  });
  await page.route('**/api/call/c2', (route) => route.fulfill({ json: { call: ACTIVE, lines: [] } }));
  await page.route('**/api/call/c2/stream**', (route) =>
    route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' }));
  await page.route('**/api/call/c2/hangup', (route) =>
    route.fulfill({ json: { call: { ...ACTIVE, state: 'ended' }, record: '' } }));

  await page.goto('/dash/call');
  await page.getByRole('button', { name: '拨给费佳' }).click();

  await expect(page.getByRole('button', { name: '挂断' })).toBeVisible();
  await expect(page.getByRole('button', { name: '接听' })).toHaveCount(0);
  expect(calls).toEqual(['ring', 'answer']);
});

test('dials in the language she picks', async ({ page }) => {
  const rings: unknown[] = [];
  await page.route('**/api/call/current', (route) => route.fulfill({ json: { call: null } }));
  await page.route('**/api/call/languages', (route) => route.fulfill({ json: { languages: [
    { code: 'ja', label: '日本語', speech: 'ja-JP' },
    { code: 'en', label: 'English', speech: 'en-US' },
  ] } }));
  await page.route('**/api/call/ring', (route) => {
    rings.push(route.request().postDataJSON());
    return route.fulfill({ json: { call: { ...RINGING, language: 'en' } } });
  });
  await page.route('**/api/call/c2/answer', (route) => route.fulfill({ json: { call: { ...ACTIVE, language: 'en' } } }));
  await page.route('**/api/call/c2', (route) => route.fulfill({ json: { call: { ...ACTIVE, language: 'en' }, lines: [] } }));
  await page.route('**/api/call/c2/stream**', (route) =>
    route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' }));

  await page.goto('/dash/call');
  const ja = page.getByRole('radio', { name: '日本語' });
  const en = page.getByRole('radio', { name: 'English' });
  await expect(ja).toHaveAttribute('aria-checked', 'true');  // the first configured language by default
  await en.click();
  await expect(en).toHaveAttribute('aria-checked', 'true');
  await expect(ja).toHaveAttribute('aria-checked', 'false');
  await page.getByRole('button', { name: '拨给费佳' }).click();

  await expect(page.getByRole('button', { name: '挂断' })).toBeVisible();
  expect(rings).toEqual([{ language: 'en' }]);
  await expect(page.getByRole('radio')).toHaveCount(0);  // the picker belongs to the idle screen only
});
