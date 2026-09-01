'use strict';
// 统一的无头浏览器服务（BrowserService）。由 gateway 通过 subprocess 调用。
//   node browser.js page <url>   → 读网页：加载(含JS)+抽正文+viewport截图
//   node browser.js shot <url>   → 纯截图：给某个 URL 拍照（聊天截图走这条，url 里带 ?as=me&shot=1）
// page 模式使用同一套启动参数；shot 模式连接既有 Browser Base；两种模式共用资源限制（省内存：禁字体/媒体）。
// 调用方(gateway)持有单飞锁，保证同一时刻只有一个 chromium。
const { chromium } = require('/opt/frontend/node_modules/playwright-core');
const { randomUUID } = require('crypto');
const fs = require('fs');

const mode = process.argv[2];
const url = process.argv[3];
// 截图写进 attachments 目录（不在 static 里）。gateway 收到绝对路径后经
// attachment_store 登记、改名为 <id>，返回 attachment://<id> 给前端。
const SHOT_DIR = '/opt/frontend/attachments';
const CDP_ENDPOINT = 'http://127.0.0.1:9333';
const LAUNCH_ARGS = ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--single-process',
                     '--no-zygote', '--disable-extensions', '--disable-background-networking', '--mute-audio'];

function out(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }
const UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36';

async function run({
  runMode = mode,
  runUrl = url,
  cdpEndpoint = CDP_ENDPOINT,
  shotDir = SHOT_DIR,
} = {}) {
  if (!runMode || !runUrl) { out({ ok: false, error: 'usage: browser.js <page|shot> <url>' }); return; }
  fs.mkdirSync(shotDir, { recursive: true });
  const isChat = runMode === 'shot';
  let browser = null;
  let ctx = null;
  try {
    browser = isChat
      ? await chromium.connectOverCDP(cdpEndpoint, { noDefaults: true })
      : await chromium.launch({ headless: true, args: LAUNCH_ARGS });
    ctx = await browser.newContext({
      viewport: isChat ? { width: 440, height: 920 } : { width: 1280, height: 800 },
      deviceScaleFactor: isChat ? 2 : 1,   // 聊天截图 2x 更清晰
      userAgent: UA,
      locale: 'zh-CN',
    });
    const page = await ctx.newPage();
    await page.route('**/*', (route) => {
      const t = route.request().resourceType();
      if (t === 'media' || t === 'font') return route.abort();
      return route.continue();
    });
    const resp = await page.goto(runUrl, { waitUntil: 'domcontentloaded', timeout: 25000 });
    const fname = randomUUID().slice(0, 8) + '.png';
    const shotPath = shotDir + '/' + fname;

    if (isChat) {
      // 聊天页靠 JS 拉消息再渲染；等消息出现，别用 networkidle（页面在轮询，永远不 idle）
      try { await page.waitForSelector('#msgs .msg-row', { timeout: 8000 }); } catch (_) {}
      await page.waitForTimeout(1200);                      // 让 thinking/工具卡等渲染完
      await page.evaluate(() => {                            // 滚到底：截最近的对话
        const m = document.getElementById('msgs');
        if (m) m.scrollTop = m.scrollHeight;
      });
      await page.waitForTimeout(400);
      const title = await page.title();
      await page.screenshot({ path: shotPath });            // viewport 截图（手机比例）
      await ctx.close();
      ctx = null;
      await browser.close();
      browser = null;
      out({ ok: true, mode: runMode, title, url: runUrl, shot: shotPath });  // 绝对路径，交给 gateway 登记
      return;
    }

    // page 模式：读网页
    await page.waitForTimeout(900);
    const title = await page.title();
    let text = await page.evaluate(() => (document.body ? document.body.innerText : ''));
    text = (text || '').replace(/\n{3,}/g, '\n\n').replace(/[ \t]{2,}/g, ' ').trim();
    await page.screenshot({ path: shotPath });
    const finalUrl = page.url();
    await browser.close();
    browser = null;
    out({ ok: true, mode: runMode, status: resp ? resp.status() : 0, title, url: runUrl, finalUrl,
          text: text.slice(0, 4000), shot: shotPath });   // 绝对路径，交给 gateway 登记
  } catch (e) {
    try { if (ctx) await ctx.close(); } catch (_) {}
    try { if (browser) await browser.close(); } catch (_) {}
    out({ ok: false, error: String((e && e.message) || e) });
  }
}

if (require.main === module) run();

module.exports = { run, CDP_ENDPOINT, LAUNCH_ARGS, UA };

