import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import { mkdtemp, readFile, rm, stat } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createServer as createNetServer } from 'node:net';

const require = createRequire(import.meta.url);
const Module = require('node:module');
const localCorePath = fileURLToPath(new URL('../app/node_modules/playwright-core/index.js', import.meta.url));
const browserScriptPath = fileURLToPath(new URL('../tools/browser.js', import.meta.url));
const source = await readFile(browserScriptPath, 'utf8');
const { chromium } = require('../app/node_modules/playwright');

assert.match(source, /connectOverCDP\(cdpEndpoint,\s*\{\s*noDefaults:\s*true\s*\}\)/,
  'shot must use connectOverCDP with noDefaults');
assert.match(source, /browser = isChat\s*\n\s*\? await chromium\.connectOverCDP/,
  'shot must connect before creating its context');
assert.match(source, /ctx = await browser\.newContext\(/, 'shot must create an isolated context');
assert.match(source, /await ctx\.close\(\);\s*\n\s*ctx = null;\s*\n\s*await browser\.close\(\);/,
  'shot must close its own context before disconnecting');
assert.match(source, /: await chromium\.launch\(\{ headless: true, args: LAUNCH_ARGS \}\);/,
  'page launch behavior must remain present');
assert.doesNotMatch(source, /if\s*\(isChat[\s\S]{0,500}chromium\.launch/,
  'shot must not have a launch fallback');

const originalLoad = Module._load;
Module._load = function load(request, parent, isMain) {
  if (request === '/opt/frontend/node_modules/playwright-core') {
    return originalLoad(localCorePath, parent, isMain);
  }
  return originalLoad(request, parent, isMain);
};
const { run } = require(browserScriptPath);
Module._load = originalLoad;

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createNetServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close((error) => error ? reject(error) : resolve(port));
    });
  });
}

function captureRun(options) {
  const lines = [];
  const originalWrite = process.stdout.write;
  process.stdout.write = (chunk, ...rest) => {
    lines.push(String(chunk));
    return true;
  };
  return run(options).finally(() => {
    process.stdout.write = originalWrite;
  }).then(() => JSON.parse(lines.at(-1)));
}

const seenCookies = [];
const server = createServer((request, response) => {
  const requestUrl = new URL(request.url, 'http://127.0.0.1');
  if (requestUrl.pathname === '/seen') {
    seenCookies.push(requestUrl.searchParams.get('cookies') || '');
    response.writeHead(204);
    response.end();
    return;
  }
  if (requestUrl.pathname === '/plain') {
    response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    response.end('<!doctype html><title>CDP Screenshot Probe</title><div id="msgs"><div class="msg-row">plain</div></div>');
    return;
  }
  response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
  response.end(`<!doctype html>
<title>CDP Screenshot Probe</title>
<div id="msgs"><div class="msg-row">probe</div></div>
<script>
  document.cookie = 'isolated_cookie=isolated; path=/';
  fetch('/seen?cookies=' + encodeURIComponent(document.cookie));
</script>`);
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const serverPort = server.address().port;
const probeUrl = `http://127.0.0.1:${serverPort}/chat`;

const tempRoot = await mkdtemp(join(tmpdir(), 'hayagarden-cdp-s1-'));
const shotDir = join(tempRoot, 'attachments');
const cdpPort = await freePort();
const cdpEndpoint = `http://127.0.0.1:${cdpPort}`;
const executablePath = chromium.executablePath();
assert.ok(existsSync(executablePath), `Playwright Chromium is missing: ${executablePath}`);

let defaultContext;
let baseLauncher;
let defaultConnection;
try {
  baseLauncher = await chromium.launch({
    headless: true,
    args: [`--remote-debugging-port=${cdpPort}`, '--no-first-run', '--no-default-browser-check'],
  });
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      defaultConnection = await chromium.connectOverCDP(cdpEndpoint, { noDefaults: true });
      break;
    } catch (error) {
      if (attempt === 39) throw error;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  defaultContext = defaultConnection.contexts()[0];
  assert.ok(defaultContext, 'disposable CDP browser must expose its default context');
  await defaultContext.addCookies([{
    name: 'default_cookie',
    value: 'default',
    domain: '127.0.0.1',
    path: '/',
  }]);
  await defaultConnection.close();
  defaultConnection = null;

  const shotResult = await captureRun({
    runMode: 'shot',
    runUrl: probeUrl,
    cdpEndpoint,
    shotDir,
  });
  assert.deepEqual(Object.keys(shotResult).sort(), ['mode', 'ok', 'shot', 'title', 'url']);
  assert.equal(shotResult.ok, true);
  assert.equal(shotResult.mode, 'shot');
  assert.equal(shotResult.url, probeUrl);
  assert.equal(shotResult.title, 'CDP Screenshot Probe');
  const screenshotStat = await stat(shotResult.shot);
  assert.ok(screenshotStat.isFile());
  const screenshot = await readFile(shotResult.shot);
  assert.equal(screenshot.toString('ascii', 1, 4), 'PNG');
  assert.equal(screenshot.readUInt32BE(16), 880, 'shot viewport width must remain 440 at 2x');
  assert.equal(screenshot.readUInt32BE(20), 1840, 'shot viewport height must remain 920 at 2x');

  await new Promise((resolve) => setTimeout(resolve, 250));
  assert.ok(seenCookies.length > 0, 'probe page must report its isolated cookie state');
  assert.ok(seenCookies.some((cookies) => cookies.includes('isolated_cookie=isolated')));
  assert.ok(seenCookies.every((cookies) => !cookies.includes('default_cookie=default')),
    'default context cookie leaked into screenshot context');

  const reconnect = await chromium.connectOverCDP(cdpEndpoint, { noDefaults: true });
  assert.equal(reconnect.contexts().length, 1, 'consumer context must be closed');

  const survivingContext = reconnect.contexts()[0];
  const survivingPage = await survivingContext.newPage();
  await survivingPage.goto(`http://127.0.0.1:${serverPort}/plain`, { waitUntil: 'domcontentloaded' });
  assert.ok((await survivingPage.evaluate(() => document.cookie)).includes('default_cookie=default'),
    'default context must survive consumer disconnect and retain its cookie');
  assert.ok(!(await survivingPage.evaluate(() => document.cookie)).includes('isolated_cookie=isolated'),
    'isolated cookie must not leak into default context');
  await survivingPage.close();
  await reconnect.close();

  const unavailablePort = await freePort();
  const unavailable = await captureRun({
    runMode: 'shot',
    runUrl: probeUrl,
    cdpEndpoint: `http://127.0.0.1:${unavailablePort}`,
    shotDir,
  });
  assert.equal(unavailable.ok, false);
  assert.match(unavailable.error, /connect|ECONNREFUSED|Target page|WebSocket/i);
  assert.doesNotMatch(unavailable.error, /launch/i, 'unavailable Browser Base must not fall back to launch');

  const pageResult = await captureRun({ runMode: 'page', runUrl: probeUrl, shotDir });
  assert.equal(pageResult.ok, true);
  assert.deepEqual(Object.keys(pageResult).sort(), ['finalUrl', 'mode', 'ok', 'shot', 'status', 'text', 'title', 'url']);
  assert.equal(pageResult.mode, 'page');
  assert.equal(pageResult.status, 200);
  assert.equal(pageResult.title, 'CDP Screenshot Probe');
  assert.match(pageResult.text, /probe/);
  assert.equal(pageResult.finalUrl, probeUrl);
  assert.ok((await stat(pageResult.shot)).isFile());
} finally {
  if (defaultConnection) await defaultConnection.close();
  if (baseLauncher) await baseLauncher.close();
  server.close();
  await rm(tempRoot, { recursive: true, force: true });
}

console.log('BROWSER-CONSUMER-CUTOVER-S1 screenshot/CDP probe: PASS');

