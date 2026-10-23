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
