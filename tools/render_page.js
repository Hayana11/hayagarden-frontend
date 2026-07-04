'use strict';
// 无头浏览器渲染单个网页：加载(含JS)、抽正文、截图，输出一行 JSON。
// 由 gateway._read_webpage 通过 subprocess 调用。为省内存：single-process、
// 屏蔽字体/媒体、只截 viewport（不截整页，长页会爆内存+巨图）。
const { chromium } = require('/opt/frontend/node_modules/playwright-core');
const { randomUUID } = require('crypto');
const fs = require('fs');

const url = process.argv[2];
const SHOT_DIR = '/opt/frontend/static/uploads/shots';

function out(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }

(async () => {
  if (!url) { out({ ok: false, error: 'no url' }); return; }
  let browser;
  try {
    fs.mkdirSync(SHOT_DIR, { recursive: true });
    browser = await chromium.launch({
      headless: true,
      args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--single-process',
             '--no-zygote', '--disable-extensions', '--disable-background-networking', '--mute-audio'],
    });
    const ctx = await browser.newContext({
      viewport: { width: 1280, height: 800 },
      userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
      locale: 'zh-CN',
    });
    const page = await ctx.newPage();
    // 省内存：字体/媒体不下载，图片和样式保留以便截图有意义
    await page.route('**/*', (route) => {
      const t = route.request().resourceType();
      if (t === 'media' || t === 'font') return route.abort();
      return route.continue();
    });
    const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 25000 });
    await page.waitForTimeout(900); // 给前端 JS 一点渲染时间
    const title = await page.title();
    let text = await page.evaluate(() => (document.body ? document.body.innerText : ''));
    text = (text || '').replace(/\n{3,}/g, '\n\n').replace(/[ \t]{2,}/g, ' ').trim();
    const fname = randomUUID().slice(0, 8) + '.png';
    await page.screenshot({ path: SHOT_DIR + '/' + fname }); // 仅 viewport
    const status = resp ? resp.status() : 0;
    const finalUrl = page.url();
    await browser.close();
    out({ ok: true, status, title, url, finalUrl,
          text: text.slice(0, 4000), shot: '/static/uploads/shots/' + fname });
  } catch (e) {
    try { if (browser) await browser.close(); } catch (_) {}
    out({ ok: false, error: String((e && e.message) || e) });
  }
})();
