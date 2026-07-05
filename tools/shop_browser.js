'use strict';
// 购物浏览器（带登录态）—— VPS 侧 headless 运行时。
//   node shop_browser.js state <site>              → 报告该站是否已有 storageState、cookie 数
//   node shop_browser.js browse <site> <url>       → 带登录态打开页面，回正文 + 截图 + 发现的 cashier/alipay 链接
//   node shop_browser.js act <site> <url> <actionsFile>
//     → 先 goto url，再按序执行 actionsFile（JSON 数组）里的动作，最后同样回正文+截图+cashier链接。
//     动作类型：{click_text}（按可见文字点击）、{click_selector}（CSS选择器点击）、
//     {fill_selector,value}（填写输入框）、{wait_ms}（等待）、{wait_selector}（等到某个元素出现）。
//     每一步失败不中断整个流程（记入 step_errors），因为页面结构变化很常见，一步失败不应该杀掉整个请求。
// 登录态由 Cursor 桌面的 taobao_login_capture 捕获后拷进来，存：
//   /opt/frontend/private/shop_state/<site>.json
// 与 read_webpage 的 browser.js 分开，互不影响（那个无登录态、专用读公开网页）。
const { chromium } = require('/opt/frontend/node_modules/playwright-core');
const fs = require('fs');
const { randomUUID } = require('crypto');

const STATE_DIR = '/opt/frontend/private/shop_state';
const SHOT_DIR  = '/opt/frontend/attachments';
const UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36';
const LAUNCH_ARGS = ['--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--single-process',
                    '--no-zygote','--disable-extensions','--disable-background-networking','--mute-audio',
                    '--disable-blink-features=AutomationControlled'];

function out(obj){ process.stdout.write(JSON.stringify(obj) + '\n'); }
function statePath(site){ return STATE_DIR + '/' + site.replace(/[^a-z0-9_-]/gi,'_') + '.json'; }

const mode = process.argv[2];
const site = process.argv[3];
const url  = process.argv[4];
const actionsFile = process.argv[5];

// browse 和 act 共用的收尾提取：页面文字 + 截图 + 登录态健康 + cashier 链接
async function _extractResult(page, ctx, cashierHits){
  const domLinks = await page.evaluate(() => {
    const set = new Set();
    document.querySelectorAll('a[href]').forEach(a => {
      const h = a.href || '';
      if (h.indexOf('cashier') >= 0 && h.indexOf('alipay') >= 0) set.add(h);
    });
    return Array.from(set);
  }).catch(() => []);
  domLinks.forEach(h => cashierHits.add(h));
  const finalUrl = page.url();
  const needLogin = /login\.(taobao|tmall|alipay)\.com/i.test(finalUrl) ||
    /请登录|亲，请登录/.test(await page.title().catch(()=>''));
  const fname = randomUUID().slice(0,8) + '.png';
  const shotPath = SHOT_DIR + '/' + fname;
  await page.screenshot({ path: shotPath });
  let text = await page.evaluate(() => document.body ? document.body.innerText : '').catch(() => '');
  text = (text||'').replace(/\n{3,}/g,'\n\n').replace(/[ \t]{2,}/g,' ').trim();
  return { finalUrl, needLogin, shotPath, text };
}

async function main(){
  if (!mode || !site){ out({ ok:false, error:'usage: shop_browser.js <state|browse> <site> [url]' }); return; }
  const sp = statePath(site);
  const hasState = fs.existsSync(sp);

  if (mode === 'state'){
    let cookieCount = 0, savedAt = null;
    if (hasState){
      try { const st = JSON.parse(fs.readFileSync(sp,'utf8')); cookieCount = (st.cookies||[]).length; } catch(_){}
      try { savedAt = fs.statSync(sp).mtime.toISOString(); } catch(_){}
    }
    out({ ok:true, site, has_state:hasState, cookies:cookieCount, saved_at:savedAt });
    return;
  }

  if (mode === 'browse'){
    if (!url){ out({ ok:false, error:'browse needs <url>' }); return; }
    fs.mkdirSync(SHOT_DIR, { recursive:true });
    const browser = await chromium.launch({ headless:true, args:LAUNCH_ARGS });
    try {
      const ctxOpts = { viewport:{width:960,height:720}, userAgent:UA, locale:'zh-CN' };
      if (hasState) ctxOpts.storageState = sp;
      const ctx = await browser.newContext(ctxOpts);
      const page = await ctx.newPage();
      // 阻断字体/媒体请求——不只是省带宽：这台 VPS 的 headless-shell chromium 会在
      // page.screenshot() 的 "waiting for fonts to load" 阶段卡住甚至永久不返回（没装系统
      // 字体，淘宝页面又恕愈把堆字体拉到飞起）——不阻断就会被外层 timeout 直接杀。
      await page.route('**/*', (route) => {
        const t = route.request().resourceType();
        if (t === 'media' || t === 'font') return route.abort();
        return route.continue();
      });
      // 监听所有导航/弹窗里出现的 cashier.alipay.com（下单后常以跳转形式出现）
      const cashierHits = new Set();
      page.on('framenavigated', f => { const u=f.url()||''; if (u.indexOf('cashier')>=0 && u.indexOf('alipay')>=0) cashierHits.add(u); });
      ctx.on('page', pg => { const u=pg.url()||''; if (u.indexOf('cashier')>=0 && u.indexOf('alipay')>=0) cashierHits.add(u); });

      const resp = await page.goto(url, { waitUntil:'domcontentloaded', timeout:45000 });
      await page.waitForTimeout(6000);  // 搜索/列表页靠 XHR 异步填充内容，这台慢机器 3s 不够

      const r = await _extractResult(page, ctx, cashierHits);
      await browser.close();
      out({ ok:true, mode, site, url, finalUrl:r.finalUrl, status: resp?resp.status():0,
            need_login: r.needLogin, cashier_links: Array.from(cashierHits),
            text: r.text.slice(0,4000), shot: r.shotPath });
    } catch(e){
      try { await browser.close(); } catch(_){}
      out({ ok:false, error: String((e&&e.message)||e) });
    }
    return;
  }

  if (mode === 'act'){
    if (!url){ out({ ok:false, error:'act needs <url>' }); return; }
    let actions = [];
    if (actionsFile){
      try { actions = JSON.parse(fs.readFileSync(actionsFile, 'utf8')); } catch(e){
        out({ ok:false, error:'读不懂 actionsFile: ' + e.message }); return;
      }
    }
    if (!Array.isArray(actions)) actions = [];
    fs.mkdirSync(SHOT_DIR, { recursive:true });
    const browser = await chromium.launch({ headless:true, args:LAUNCH_ARGS });
    const stepErrors = [];
    try {
      const ctxOpts = { viewport:{width:960,height:720}, userAgent:UA, locale:'zh-CN' };
      if (hasState) ctxOpts.storageState = sp;
      const ctx = await browser.newContext(ctxOpts);
      const page = await ctx.newPage();
      await page.route('**/*', (route) => {
        const t = route.request().resourceType();
        if (t === 'media' || t === 'font') return route.abort();
        return route.continue();
      });
      const cashierHits = new Set();
      page.on('framenavigated', f => { const u=f.url()||''; if (u.indexOf('cashier')>=0 && u.indexOf('alipay')>=0) cashierHits.add(u); });
      ctx.on('page', pg => { const u=pg.url()||''; if (u.indexOf('cashier')>=0 && u.indexOf('alipay')>=0) cashierHits.add(u); });

      await page.goto(url, { waitUntil:'domcontentloaded', timeout:45000 });
      await page.waitForTimeout(5000);

      for (const a of actions){
        try {
          if (a.click_text){
            await page.getByText(a.click_text, { exact:false }).first().click({ timeout:8000 });
          } else if (a.click_selector){
            await page.click(a.click_selector, { timeout:8000 });
          } else if (a.fill_selector){
            await page.fill(a.fill_selector, String(a.value||''), { timeout:8000 });
          } else if (a.wait_selector){
            await page.waitForSelector(a.wait_selector, { timeout:a.timeout_ms||8000 });
          } else if (a.wait_ms){
            await page.waitForTimeout(a.wait_ms);
          }
        } catch(stepErr){
          stepErrors.push({ action:a, error:String((stepErr&&stepErr.message)||stepErr).slice(0,200) });
        }
      }
      await page.waitForTimeout(1500);

      const r = await _extractResult(page, ctx, cashierHits);
      await browser.close();
      out({ ok:true, mode, site, url, finalUrl:r.finalUrl, need_login:r.needLogin,
            cashier_links: Array.from(cashierHits), text: r.text.slice(0,4000), shot: r.shotPath,
            step_errors: stepErrors });
    } catch(e){
      try { await browser.close(); } catch(_){}
      out({ ok:false, error: String((e&&e.message)||e), step_errors: stepErrors });
    }
    return;
  }

  out({ ok:false, error:'unknown mode: ' + mode });
}
main();
