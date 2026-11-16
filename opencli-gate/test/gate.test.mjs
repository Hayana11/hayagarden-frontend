import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { loadConfig, validateConfig, startGate } from '../gate.mjs';

const TOKEN = 'x'.repeat(40);
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'gate-test-'));
const argvLog = path.join(tmp, 'argv.jsonl');
const modeFile = path.join(tmp, 'mode.json');
const fake = path.join(tmp, 'fake-opencli.mjs');
fs.writeFileSync(fake, `
import fs from 'node:fs';
const mode = JSON.parse(fs.readFileSync(${JSON.stringify(modeFile)}, 'utf8'));
fs.appendFileSync(${JSON.stringify(argvLog)}, JSON.stringify(process.argv.slice(2)) + '\\n');
if (mode.sleep) await new Promise(r => setTimeout(r, mode.sleep));
if (mode.stderr) process.stderr.write(mode.stderr);
if (mode.stdout !== undefined) process.stdout.write(mode.stdout);
process.exit(mode.exit ?? 0);
`);
const setMode = (m) => fs.writeFileSync(modeFile, JSON.stringify(m));
const calls = () => (fs.existsSync(argvLog) ? fs.readFileSync(argvLog, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l)) : []);
const reset = () => { fs.rmSync(argvLog, { force: true }); setMode({ stdout: '{"items":[1]}' }); };

const NOTE = 'https://www.xiaohongshu.com/explore/' + 'a'.repeat(24) + '?xsec_token=abc&xsec_source=pc_feed';
const USERID = '0123456789abcdef01234567';

function cfgFor(over = {}) {
  return {
    host: '127.0.0.1', allowLoopback: true, port: 0, token: TOKEN,
    opencliExe: process.execPath, opencliPrefixArgs: [fake],
    minIntervalMs: 0, timeoutMs: 5000, ...over,
  };
}
async function withGate(over, fn) {
  const server = await startGate(cfgFor(over));
  const base = `http://127.0.0.1:${server.address().port}`;
  const origLog = console.log; console.log = () => {};
  try { await fn(base); } finally { console.log = origLog; server.close(); server.closeAllConnections?.(); }
}
const post = (base, body, headers = {}) => fetch(base + '/xhs', {
  method: 'POST',
  headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json', ...headers },
  body: typeof body === 'string' ? body : JSON.stringify(body),
});
const TAIL = ['-f', 'json', '--window', 'background'];

test('auth: missing / wrong / wrong length -> 401', async () => {
  reset();
  await withGate({}, async (base) => {
    assert.equal((await fetch(base + '/status')).status, 401);
    assert.equal((await fetch(base + '/status', { headers: { Authorization: 'Bearer nope' } })).status, 401);
    assert.equal((await fetch(base + '/xhs', { method: 'POST', headers: { Authorization: 'Bearer ' + 'y'.repeat(40) }, body: '{}' })).status, 401);
    assert.equal(calls().length, 0);
  });
});

test('unknown path 404', async () => {
  await withGate({}, async (base) => {
    const r = await fetch(base + '/nope', { headers: { Authorization: `Bearer ${TOKEN}` } });
    assert.equal(r.status, 404);
  });
});

test('status runs doctor', async () => {
  reset(); setMode({ stdout: 'all good\n', exit: 0 });
  await withGate({}, async (base) => {
    const r = await fetch(base + '/status', { headers: { Authorization: `Bearer ${TOKEN}` } });
    const j = await r.json();
    assert.equal(j.ok, true);
    assert.match(j.doctor, /all good/);
    assert.deepEqual(calls()[0], ['doctor']);
  });
  reset(); setMode({ stdout: 'broken', exit: 1 });
  await withGate({}, async (base) => {
    const j = await (await fetch(base + '/status', { headers: { Authorization: `Bearer ${TOKEN}` } })).json();
    assert.equal(j.ok, false);
  });
});

test('whitelisted commands build exact argv', async () => {
  const cases = [
    [{ command: 'feed' }, ['xiaohongshu', 'feed', '--limit', '10']],
    [{ command: 'feed', limit: 20 }, ['xiaohongshu', 'feed', '--limit', '20']],
    [{ command: 'search', query: '咖啡 拉花', limit: 5 }, ['xiaohongshu', 'search', '咖啡 拉花', '--limit', '5']],
    [{ command: 'note', url: NOTE }, ['xiaohongshu', 'note', NOTE]],
    [{ command: 'comments', url: NOTE }, ['xiaohongshu', 'comments', NOTE, '--limit', '20']],
    [{ command: 'comments', url: NOTE, limit: 50, with_replies: true }, ['xiaohongshu', 'comments', NOTE, '--limit', '50', '--with-replies']],
    [{ command: 'user', user: USERID }, ['xiaohongshu', 'user', USERID, '--limit', '15']],
    [{ command: 'user', user: `https://www.xiaohongshu.com/user/profile/${USERID}`, limit: 30 }, ['xiaohongshu', 'user', `https://www.xiaohongshu.com/user/profile/${USERID}`, '--limit', '30']],
    [{ command: 'note', url: 'https://www.xiaohongshu.com/discovery/item/' + 'b'.repeat(24) + '?xsec_token=t' }, ['xiaohongshu', 'note', 'https://www.xiaohongshu.com/discovery/item/' + 'b'.repeat(24) + '?xsec_token=t']],
  ];
  await withGate({}, async (base) => {
    for (const [body, expected] of cases) {
      reset();
      const r = await post(base, body);
      const j = await r.json();
      assert.equal(r.status, 200, JSON.stringify(j));
      assert.equal(j.ok, true);
      assert.equal(j.command, body.command);
      assert.deepEqual(j.data, { items: [1] });
      assert.deepEqual(calls()[0], [...expected, ...TAIL]);
    }
  });
});

test('prefix args are passed before the command', async () => {
  // fake script path is the prefix; real prefix is checked via direct config
  const cfg = loadConfig({ LOCALAPPDATA: 'C:\\Users\\x\\AppData\\Local' }, path.join(tmp, 'missing.json'));
  assert.deepEqual(cfg.opencliPrefixArgs, ['__opencli_shim']);
  assert.equal(cfg.opencliExe, path.join('C:\\Users\\x\\AppData\\Local', 'OpenCLIApp', 'opencli-app.exe'));
  assert.equal(cfg.port, 19830);
  assert.equal(cfg.minIntervalMs, 4000);
  assert.equal(cfg.timeoutMs, 90000);
});

test('limits clamped/rejected and bad inputs -> 400, no process spawned', async () => {
  const badBodies = [
    { command: 'feed', limit: 0 }, { command: 'feed', limit: 21 }, { command: 'feed', limit: 1.5 }, { command: 'feed', limit: '5' },
    { command: 'search', query: 'x', limit: 21 },
    { command: 'comments', url: NOTE, limit: 51 },
    { command: 'user', user: USERID, limit: 31 },
    { command: 'search', query: '-rf' },
    { command: 'search', query: '' },
    { command: 'search', query: 'a'.repeat(101) },
    { command: 'search', query: 'a\nb' },
    { command: 'search' },
    { command: 'note', url: 'http://www.xiaohongshu.com/explore/' + 'a'.repeat(24) + '?xsec_token=t' },
    { command: 'note', url: 'https://xiaohongshu.com/explore/' + 'a'.repeat(24) + '?xsec_token=t' },
    { command: 'note', url: 'https://www.xiaohongshu.com.evil.com/explore/' + 'a'.repeat(24) + '?xsec_token=t' },
    { command: 'note', url: 'https://evil.com/explore/' + 'a'.repeat(24) + '?xsec_token=t' },
    { command: 'note', url: 'https://www.xiaohongshu.com/explore/short?xsec_token=t' },
    { command: 'note', url: 'https://www.xiaohongshu.com/explore/' + 'a'.repeat(24) },
    { command: 'note', url: 'https://www.xiaohongshu.com/explore/' + 'a'.repeat(24) + '?xsec_token=' + 'z'.repeat(1000) },
    { command: 'note', url: 'https://www.xiaohongshu.com/other/' + 'a'.repeat(24) + '?xsec_token=t' },
    { command: 'note' },
    { command: 'user', user: 'zzz' },
    { command: 'user', user: 'https://evil.com/user/profile/' + USERID },
    { command: 'user', user: 'https://www.xiaohongshu.com/user/other/' + USERID },
    { command: 'note', url: NOTE, extra: 1 },
    { command: 'feed', url: NOTE },
    { command: 'comments', url: NOTE, with_replies: 'yes' },
    { command: 'publish' }, { command: 'follow', user: USERID }, { command: 'unfollow' }, { command: 'notifications' },
    { command: 5 }, {}, [], 'str',
  ];
  reset();
  await withGate({}, async (base) => {
    for (const b of badBodies) {
      const r = await post(base, b);
      assert.equal(r.status, 400, JSON.stringify(b).slice(0, 80));
      const j = await r.json();
      assert.equal(j.ok, false);
      assert.ok(j.error);
    }
    assert.equal((await post(base, 'not json')).status, 400);
    assert.equal((await post(base, '{}', { 'Content-Type': 'text/plain' })).status, 400);
    assert.equal((await post(base, JSON.stringify({ command: 'search', query: 'a'.repeat(9000) }))).status, 413);
    assert.equal(calls().length, 0);
  });
});

test('busy -> 429, and minInterval waits instead of rejecting', async () => {
  reset(); setMode({ stdout: '{}', sleep: 600 });
  await withGate({}, async (base) => {
    const first = post(base, { command: 'feed' });
    await new Promise((r) => setTimeout(r, 200));
    const second = await post(base, { command: 'feed' });
    assert.equal(second.status, 429);
    assert.equal((await second.json()).error, 'busy');
    assert.equal((await first).status, 200);
  });
  reset();
  await withGate({ minIntervalMs: 700 }, async (base) => {
    const t0 = Date.now();
    assert.equal((await post(base, { command: 'feed' })).status, 200);
    assert.equal((await post(base, { command: 'feed' })).status, 200);
    assert.ok(Date.now() - t0 >= 650, 'second call should have waited');
  });
});

test('exit code mapping', async () => {
  const map = { 69: 'bridge_down', 77: 'auth_required', 66: 'empty', 75: 'timeout', 1: 'opencli_failed', 2: 'opencli_failed' };
  await withGate({}, async (base) => {
    for (const [code, name] of Object.entries(map)) {
      reset(); setMode({ stdout: '', stderr: 'Update available\nboom', exit: Number(code) });
      const r = await post(base, { command: 'feed' });
      assert.equal(r.status, 200);
      const j = await r.json();
      assert.equal(j.ok, false);
      assert.equal(j.exit_code, Number(code));
      assert.equal(j.error, name);
    }
  });
});

test('stderr noise ignored on success; bad JSON -> bad_output', async () => {
  await withGate({}, async (base) => {
    reset(); setMode({ stdout: '{"a":1}', stderr: 'Update available', exit: 0 });
    assert.deepEqual((await (await post(base, { command: 'feed' })).json()).data, { a: 1 });
    reset(); setMode({ stdout: 'x'.repeat(3000), exit: 0 });
    const j = await (await post(base, { command: 'feed' })).json();
    assert.equal(j.ok, false);
    assert.equal(j.error, 'bad_output');
    assert.equal(j.stdout.length, 2000);
  });
});

test('timeout -> 504', async () => {
  reset(); setMode({ stdout: '{}', sleep: 3000 });
  await withGate({ timeoutMs: 300 }, async (base) => {
    const r = await post(base, { command: 'feed' });
    assert.equal(r.status, 504);
    assert.equal((await r.json()).error, 'timeout');
  });
});

test('logs never contain token, query or url', async () => {
  reset();
  const lines = [];
  const server = await startGate(cfgFor());
  const base = `http://127.0.0.1:${server.address().port}`;
  const orig = console.log; console.log = (l) => lines.push(String(l));
  try {
    await post(base, { command: 'search', query: 'secretquery' });
    await post(base, { command: 'note', url: NOTE });
    await new Promise((r) => setTimeout(r, 50));
  } finally { console.log = orig; server.close(); server.closeAllConnections?.(); }
  const all = lines.join('\n');
  assert.ok(lines.length >= 2);
  assert.ok(!all.includes(TOKEN) && !all.includes('secretquery') && !all.includes('xsec_token'));
});

test('startup refuses bad host / short token', async () => {
  const base = cfgFor();
  const refuse = (over) => assert.throws(() => validateConfig({ ...base, ...over }));
  refuse({ host: '' });
  refuse({ host: '0.0.0.0' });
  refuse({ host: '::' });
  refuse({ host: '192.168.1.5' });
  refuse({ host: '100.63.0.1' });
  refuse({ host: '100.128.0.1' });
  refuse({ host: '127.0.0.1', allowLoopback: false });
  refuse({ host: '127.0.0.2', allowLoopback: true });
  refuse({ token: 'short' });
  refuse({ token: 'x'.repeat(31) });
  assert.doesNotThrow(() => validateConfig({ ...base, host: '100.64.0.1', allowLoopback: false }));
  assert.doesNotThrow(() => validateConfig({ ...base, host: '100.127.255.254', allowLoopback: false }));
  assert.doesNotThrow(() => validateConfig(base));
  await assert.rejects(startGate({ ...base, host: '0.0.0.0' }));
});

test('env overrides config file', () => {
  const f = path.join(tmp, 'c.json');
  fs.writeFileSync(f, JSON.stringify({ host: '100.64.0.9', token: 'a'.repeat(32), port: 1111 }));
  const c = loadConfig({ GATE_PORT: '2222', GATE_TOKEN: 'b'.repeat(40) }, f);
  assert.equal(c.host, '100.64.0.9');
  assert.equal(c.port, 2222);
  assert.equal(c.token, 'b'.repeat(40));
});
