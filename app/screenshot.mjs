import { chromium } from 'playwright';

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const page = await browser.newPage({ viewport: { width: 430, height: 900 } });

await page.goto('http://localhost:5173/', { waitUntil: 'networkidle' });
await page.waitForTimeout(1000);
await page.screenshot({ 
  path: '/tmp/claude-0/-home-claude/dc3e1578-7ec6-548d-a5b8-31cd22a86592/scratchpad/dashboard.png',
  fullPage: true
});

await browser.close();
console.log('Screenshot saved');
