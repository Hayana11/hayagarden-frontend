'use strict';
// 撞墙诊断版：扫码登录后，不管落在哪个页面，都截图+抽文字+找验证控件特征，
// 而不是像 taobao_login.js 那样直接判定"过期/失败"。
// 目的：搞清楚 normal_validate.htm 到底要什么（短信/滑块/刷脸），而不是盲猜。
const { chromium } = require('/opt/frontend/node_modules/playwright-core');
const fs = require('fs');

const PRIVATE   = '/opt/frontend/private';
const USERDATA  = PRIVATE + '/tb_userdata';
const STATUS    = PRIVATE + '/tb_status.json';
const QR_PUB    = '/opt/frontend/static/uploads/taobao_qr.png';
const DIAG_PNG  = PRIVATE + '/tb_diag.png';
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
    await page.waitForTimeout(2000);
    if (await loggedIn()) { setStatus({ state: 'ok', reused: true }); await ctx.close(); return; }

    try { await page.waitForSelector('canvas, [class*=qrcode] img, img[src*="qr"]', { timeout: 8000 }); } catch(e){}
    await page.waitForTimeout(600);
    await page.screenshot({ path: QR_PUB });
    setStatus({ state: 'waiting', qr: '/static/uploads/taobao_qr.png' });

    // 轮询：登录成功 或 离开登录域（哪怕是跳到验证页也算"有动静"）
    let moved = false;
    for (let i = 0; i < 96; i++) {
      await page.waitForTimeout(2500);
      if (await loggedIn() || !page.url().includes('login.taobao.com')) { moved = true; break; }
    }
    try { fs.unlinkSync(QR_PUB); } catch(e){}

    if (!moved) { setStatus({ state: 'expired' }); await ctx.close(); return; }

    if (await loggedIn()) {
      setStatus({ state: 'ok', url: page.url() });
      await ctx.close(); return;
    }

    // 落在了验证页（或别的页）——不撤，留下来看个究竟
    await page.waitForTimeout(2000);
    const url = page.url();
    const title = await page.title();
    const bodyText = ((await page.evaluate(()=>document.body?document.body.innerText:'')) || '')
      .replace(/\s+/g,' ').slice(0, 500);
    const hints = await page.evaluate(() => {
      const has = (sel) => !!document.querySelector(sel);
      return {
        smsInput: has('input[type=tel], input[placeholder*="验证码"], input[name*=sms], input[id*=sms]'),
        sliderCanvas: has('canvas, [class*=nc_], [class*=slide], [id*=nc_]'),
        faceIframe: Array.from(document.querySelectorAll('iframe')).some(f => /face|zface|biometric/i.test(f.src||f.id||'')),
        iframes: Array.from(document.querySelectorAll('iframe')).map(f => f.src || f.id || f.className).slice(0,6),
        inputs: Array.from(document.querySelectorAll('input')).map(i => ({type:i.type, name:i.name, placeholder:i.placeholder})).slice(0,10),
        buttons: Array.from(document.querySelectorAll('button, a.btn, [class*=btn]')).map(b => (b.innerText||'').trim()).filter(Boolean).slice(0,10),
      };
    });
    await page.screenshot({ path: DIAG_PNG, fullPage: true });
    setStatus({ state: 'blocked', url, title, bodyText, hints, diag: DIAG_PNG });
    await ctx.close();
  } catch (e) {
    setStatus({ state: 'error', error: String(e.message || e) });
    try { await ctx.close(); } catch(_){}
  }
})();
