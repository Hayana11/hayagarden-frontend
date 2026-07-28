import { chromium } from 'playwright';
import { mkdirSync } from 'fs';

const out = '/opt/cursor/artifacts/screenshots';
mkdirSync(out, { recursive: true });
const BASE = process.env.DSW_BASE || 'http://127.0.0.1:5174';

const browser = await chromium.launch({ args: ['--no-sandbox'] });

async function shot(page, name) {
  const path = `${out}/${name}`;
  await page.screenshot({ path, fullPage: false });
  console.log('saved', path);
}

// Mobile card + boundary (scroll to soft day mark)
{
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  await page.goto(`${BASE}/dash/daily-soft-window?mockScenario=ready`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(700);
  await shot(page, 'dsw-fe-r0-card-mobile.png');

  // scroll transcript to show soft boundary
  await page.evaluate(() => {
    const sc = document.querySelector('.hide-scrollbar');
    if (sc) sc.scrollTop = sc.scrollHeight;
  });
  await page.waitForTimeout(300);
  await shot(page, 'dsw-fe-r0-day-boundary-mobile.png');

  await page.locator('.dsw-card').first().click();
  await page.waitForTimeout(450);
  await page.locator('.dsw-opt').nth(2).click(); // 5
  await page.waitForTimeout(350);
  await shot(page, 'dsw-fe-r0-drawer-5-mobile.png');
  await page.locator('.dsw-btn.primary').click();
  await page.waitForTimeout(450);
  await shot(page, 'dsw-fe-r0-locked-mobile.png');
  await context.close();
}

// Desktop
{
  const context = await browser.newContext({ viewport: { width: 1100, height: 800 } });
  const page = await context.newPage();
  await page.goto(`${BASE}/dash/daily-soft-window?mockScenario=ready`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(500);
  const toggle = page.getByRole('button', { name: /手机抽屉|桌面抽屉/ });
  if (await toggle.count()) {
    const label = await toggle.innerText();
    if (label.includes('手机')) await toggle.click();
  }
  await page.locator('.dsw-card').first().click();
  await page.waitForTimeout(450);
  await shot(page, 'dsw-fe-r0-drawer-desktop.png');
  await context.close();
}

// 404 — open drawer
{
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  await page.goto(`${BASE}/dash/daily-soft-window?mockScenario=disabled`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(800);
  if (await page.locator('.dsw-card').count()) {
    await page.locator('.dsw-card').first().click();
    await page.waitForTimeout(400);
  }
  await shot(page, 'dsw-fe-r0-404-mobile.png');
  await context.close();
}

await browser.close();
console.log('done');
