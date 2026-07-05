'use strict';
// 淘宝扫码登录助手（一次性/偶尔用）。
// 流程：开登录页 → 截二维码到 static/uploads/taobao_qr.png → 轮询是否登录成功
//       → 成功则保存会话(持久化 profile + storageState) 供以后购物复用。
// 全程不碰密码。登录态文件在 /opt/frontend/private（chmod 锁死、不进 git/static）。
const { chromium } = require('/opt/frontend/node_modules/playwright-core');
const fs = require('fs');

const PRIVATE   = '/opt/frontend/private';
const USERDATA  = PRIVATE + '/tb_userdata';
const STATEFILE = PRIVATE + '/taobao_state.json';
const STATUS    = PRIVATE + '/tb_status.json';
const QR_PUB    = '/opt/frontend/static/uploads/taobao_qr.png';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36';

function setStatus(o){ try{ fs.writeFileSync(STATUS, JSON.stringify({...o, ts: Date.now()})); }catch(e){} }

(async () => {
  fs.mkdirSync(PRIVATE, { recursive: true });
  fs.mkdirSync('/opt/frontend/static/uploads', { recursive: true });
  setStatus({ state: 'starting' });
  const ctx = await chromium.launchPersistentContext(USERDATA, {
    headless: true,
    args: ['--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--no-zygote',
           '--disable-blink-features=AutomationControlled'],
    viewport: { width: 1280, height: 900 },
    userAgent: UA, locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
  });
  await ctx.addInitScript(() => { Object.defineProperty(navigator,'webdriver',{get:()=>undefined}); });
  const page = ctx.pages()[0] || await ctx.newPage();

  async function loggedIn(){
    const cs = await ctx.cookies();
    return cs.some(c => c.name === 'unb' || c.name === '_nk_' || c.name === 'lgc');
  }

  try {
    await page.goto('https://login.taobao.com/', { waitUntil: 'domcontentloaded', timeout: 30000 });
    // 已经登录（profile 里有旧会话）？直接成功
    await page.waitForTimeout(2500);
    if (await loggedIn() || !page.url().includes('login.taobao.com')) {
      await ctx.storageState({ path: STATEFILE });
      setStatus({ state: 'ok', reused: true });
      await ctx.close();
      console.log(JSON.stringify({ ok: true, reused: true })); return;
    }
    // 截二维码
    try { await page.waitForSelector('canvas, [class*=qrcode] img, img[src*="qr"]', { timeout: 8000 }); } catch(e){}
    await page.waitForTimeout(600);
    await page.screenshot({ path: QR_PUB });
    setStatus({ state: 'waiting', qr: '/static/uploads/taobao_qr.png' });

    // 轮询登录（最多 ~240s，给足扫码+手机确认的时间）
    let ok = false;
    for (let i = 0; i < 96; i++) {
      await page.waitForTimeout(2500);
      if (await loggedIn() || !page.url().includes('login.taobao.com')) { ok = true; break; }
    }
    // 诊断：留一张最终页面截图（私有，仅供排查是否撞风控），并记下 cookie 名和 url
    let cookieNames = [];
    try { cookieNames = (await ctx.cookies()).map(c => c.name); } catch(e){}
    try { await page.screenshot({ path: PRIVATE + '/tb_final.png' }); } catch(e){}
    const finalUrl = page.url();
    if (ok) {
      await page.waitForTimeout(1500);
      await ctx.storageState({ path: STATEFILE });
      try { fs.chmodSync(STATEFILE, 0o600); } catch(e){}
      try { fs.unlinkSync(QR_PUB); } catch(e){}       // 登录后删掉公开的二维码
      setStatus({ state: 'ok', url: finalUrl, cookies: cookieNames.length });
      try { await ctx.close(); } catch(e){}
      console.log(JSON.stringify({ ok: true, url: finalUrl }));
      process.exit(0);
    } else {
      try { fs.unlinkSync(QR_PUB); } catch(e){}
      setStatus({ state: 'expired', url: finalUrl, cookieNames: cookieNames });
      try { await ctx.close(); } catch(e){}
      console.log(JSON.stringify({ ok: false, reason: 'timeout/expired', url: finalUrl }));
      process.exit(0);
    }
  } catch (e) {
    setStatus({ state: 'error', error: String(e.message || e) });
    try { await ctx.close(); } catch(_){}
    console.log(JSON.stringify({ ok: false, error: String(e.message || e) }));
  }
})();
