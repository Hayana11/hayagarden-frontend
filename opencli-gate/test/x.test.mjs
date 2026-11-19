import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { startGate, buildXRequest, weightedLength, loadConfig } from '../gate.mjs';

const TOKEN = 'x'.repeat(40);
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'gate-x-test-'));
const argvLog = path.join(tmp, 'argv.jsonl');
const modeFile = path.join(tmp, 'mode.json');
const fake = path.join(tmp, 'fake-opencli.mjs');
fs.writeFileSync(fake, `
import fs from 'node:fs';
const mode = JSON.parse(fs.readFileSync(${JSON.stringify(modeFile)}, 'utf8'));
if (mode.expectState) {
  const state = JSON.parse(fs.readFileSync(mode.expectState.path, 'utf8'));
  const matches = state.posts === mode.expectState.posts
    && state.replies === mode.expectState.replies
    && state.hashes.length === mode.expectState.hashes
    && state.last_write_at > 0;
  if (!matches) process.exit(91);
}
fs.appendFileSync(${JSON.stringify(argvLog)}, JSON.stringify(process.argv.slice(2)) + '\\n');
if (mode.stdout !== undefined) process.stdout.write(mode.stdout);
process.exit(mode.exit ?? 0);
`);
const OK = { stdout: '[{"status":"success","url":"https://x.com/fedya/status/123456"}]' };
const setMode = (m) => fs.writeFileSync(modeFile, JSON.stringify(m));
const calls = () => (fs.existsSync(argvLog) ? fs.readFileSync(argvLog, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l)) : []);
const TAIL = ['-f', 'json', '--window', 'background'];

let n = 0;
function paths() {
  n += 1;
  return { xStatePath: path.join(tmp, `state-${n}.json`), xLogPath: path.join(tmp, `log-${n}.jsonl`) };
}
function cfgFor(over = {}) {
  return {
    host: '127.0.0.1', allowLoopback: true, port: 0, token: TOKEN,
    opencliExe: process.execPath, opencliPrefixArgs: [fake],
    minIntervalMs: 0, timeoutMs: 5000,
    xDailyPostLimit: 5, xDailyReplyLimit: 20, xMinWriteIntervalMs: 0,
    ...paths(), ...over,
  };
}
const reset = () => { fs.rmSync(argvLog, { force: true }); setMode({ stdout: '{"items":[1]}' }); };
async function withGate(over, fn) {
  const cfg = cfgFor(over);
  const server = await startGate(cfg);
  const base = `http://127.0.0.1:${server.address().port}`;
  const origLog = console.log; console.log = () => {};
  try { await fn(base, cfg); } finally { console.log = origLog; server.close(); server.closeAllConnections?.(); }
}
const postX = (base, body) => fetch(base + '/x', {
  method: 'POST',
  headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});
const getState = async (base) => (await fetch(base + '/x/state', { headers: { Authorization: `Bearer ${TOKEN}` } })).json();
const STATUS = 'https://x.com/someone/status/1234567890';

test('x read commands build exact argv', async () => {
  const cases = [
    [{ command: 'timeline' }, ['twitter', 'timeline', '--limit', '10']],
    [{ command: 'timeline', type: 'following', limit: 20 }, ['twitter', 'timeline', '--type', 'following', '--limit', '20']],
    [{ command: 'search', query: '咖啡 拉花' }, ['twitter', 'search', '咖啡 拉花', '--limit', '10']],
    [{ command: 'search', query: 'ai', product: 'live', from: '@Some_One', limit: 3 }, ['twitter', 'search', 'ai', '--product', 'live', '--from', 'Some_One', '--limit', '3']],
    [{ command: 'thread', url: '1234567890' }, ['twitter', 'thread', '1234567890', '--limit', '20']],
    [{ command: 'thread', url: 'https://twitter.com/abc/status/9999999/photo/1', limit: 50 }, ['twitter', 'thread', 'https://x.com/abc/status/9999999', '--limit', '50']],
    [{ command: 'tweets' }, ['twitter', 'tweets', '--limit', '10', '--page-delay', '2']],
    [{ command: 'tweets', user: '@abc', limit: 5 }, ['twitter', 'tweets', 'abc', '--limit', '5', '--page-delay', '2']],
    [{ command: 'profile' }, ['twitter', 'profile']],
    [{ command: 'profile', user: 'abc' }, ['twitter', 'profile', 'abc']],
    [{ command: 'trending', limit: 7 }, ['twitter', 'trending', '--limit', '7']],
  ];
  await withGate({}, async (base) => {
    for (const [body, expected] of cases) {
      reset();
      const r = await postX(base, body);
      const j = await r.json();
      assert.equal(r.status, 200, JSON.stringify(j));
      assert.equal(j.ok, true);
      assert.deepEqual(calls()[0], [...expected, ...TAIL]);
    }
  });
});

test('x write commands build exact argv', async () => {
  await withGate({}, async (base) => {
    reset(); setMode(OK);
    let j = await (await postX(base, { command: 'post', text: '  你好 world  ' })).json();
    assert.equal(j.ok, true);
    assert.deepEqual(calls()[0], ['twitter', 'post', '你好 world', ...TAIL]);
    reset(); setMode(OK);
    j = await (await postX(base, { command: 'reply', url: 'https://www.x.com/someone/status/1234567890?s=1', text: 'hi there' })).json();
    assert.equal(j.ok, true);
    assert.deepEqual(calls()[0], ['twitter', 'reply', STATUS, 'hi there', ...TAIL]);
  });
});

test('x rejected commands and bad input -> 400, nothing spawned', async () => {
  const bad = [
    ...['like', 'retweet', 'quote', 'follow', 'unfollow', 'delete', 'block', 'dm', 'reply-dm', 'accept', 'bookmark', 'bookmarks',
      'list-add', 'download', 'notifications', 'likes', 'followers', 'following', 'hide-reply', 'article', 'device-follow', 'feed'].map((c) => ({ command: c })),
    { command: 'post' }, { command: 'post', text: '   ' }, { command: 'post', text: '-rf' }, { command: 'post', text: ' -x' },
    { command: 'post', text: 'a\u0000b' }, { command: 'post', text: 'a\rb' }, { command: 'post', text: 'hi', images: ['a.png'] },
    { command: 'post', text: 'hi', url: STATUS }, { command: 'post', text: 5 },
    { command: 'reply', text: 'hi' }, { command: 'reply', url: STATUS }, { command: 'reply', url: STATUS, text: 'hi', image: 'x' },
    { command: 'reply', url: '1234567890', text: 'hi' },
    { command: 'reply', url: 'http://x.com/a/status/12345', text: 'hi' },
    { command: 'reply', url: 'https://evil.com/a/status/12345', text: 'hi' },
    { command: 'reply', url: 'https://x.com.evil.com/a/status/12345', text: 'hi' },
    { command: 'reply', url: 'https://x.com:8443/a/status/12345', text: 'hi' },
    { command: 'reply', url: 'https://u:p@x.com/a/status/12345', text: 'hi' },
    { command: 'reply', url: 'https://x.com/a/likes', text: 'hi' },
    { command: 'reply', url: 'https://x.com/toolongname_toolong/status/12345', text: 'hi' },
    { command: 'thread', url: '1234' }, { command: 'thread', url: 'abc' }, { command: 'thread' }, { command: 'thread', url: '12345', limit: 51 },
    { command: 'tweets', user: 'a b' }, { command: 'tweets', user: 'a'.repeat(16) }, { command: 'tweets', user: '-abc' }, { command: 'tweets', limit: 21 },
    { command: 'profile', user: 'a-b' }, { command: 'profile', limit: 3 },
    { command: 'search' }, { command: 'search', query: '-x' }, { command: 'search', query: 'a\tb' }, { command: 'search', query: 'a'.repeat(201) },
    { command: 'search', query: 'q', product: 'bogus' }, { command: 'search', query: 'q', from: 'bad name' }, { command: 'search', query: 'q', extra: 1 },
    { command: 'timeline', type: 'x' }, { command: 'timeline', limit: 0 }, { command: 'trending', limit: 21 },
    {}, [],
  ];
  await withGate({}, async (base) => {
    reset();
    for (const body of bad) {
      const r = await postX(base, body);
      assert.equal(r.status, 400, JSON.stringify(body));
    }
    assert.equal(calls().length, 0);
  });
  assert.doesNotThrow(() => buildXRequest({ command: 'search', query: 'line1\nline2' }));
  assert.equal(buildXRequest({ command: 'tweets', user: 'a'.repeat(15) }).args[2], 'a'.repeat(15));
});

test('weighted length: CJK/fullwidth/emoji count 2, boundary 280', () => {
  assert.equal(weightedLength('abc'), 3);
  assert.equal(weightedLength('你好'), 4);
  assert.equal(weightedLength('ＡＢ'), 4);
  assert.equal(weightedLength('😀'), 2);
  assert.equal(weightedLength('a\nb'), 3);
  assert.doesNotThrow(() => buildXRequest({ command: 'post', text: 'a'.repeat(280) }));
  assert.throws(() => buildXRequest({ command: 'post', text: 'a'.repeat(281) }));
  assert.doesNotThrow(() => buildXRequest({ command: 'post', text: '你'.repeat(140) }));
  assert.throws(() => buildXRequest({ command: 'post', text: '你'.repeat(141) }));
  assert.doesNotThrow(() => buildXRequest({ command: 'post', text: '你'.repeat(139) + 'ab' }));
  assert.throws(() => buildXRequest({ command: 'post', text: '你'.repeat(139) + 'abc' }));
  assert.doesNotThrow(() => buildXRequest({ command: 'post', text: '😀'.repeat(140) }));
  assert.throws(() => buildXRequest({ command: 'post', text: '😀'.repeat(141) }));
});

test('daily limits: posts and replies counted separately, only successes count', async () => {
  await withGate({ xDailyPostLimit: 2, xDailyReplyLimit: 1 }, async (base) => {
    reset(); setMode({ exit: 1, stdout: '' });
    let r = await postX(base, { command: 'post', text: 'failed one' });
    assert.equal((await r.json()).ok, false);
    setMode(OK);
    assert.equal((await postX(base, { command: 'post', text: 'p1' })).status, 200);
    assert.equal((await postX(base, { command: 'post', text: 'p2' })).status, 200);
    r = await postX(base, { command: 'post', text: 'p3' });
    assert.equal(r.status, 429);
    assert.equal((await r.json()).error, 'x_daily_limit');
    assert.equal((await postX(base, { command: 'reply', url: STATUS, text: 'r1' })).status, 200);
    r = await postX(base, { command: 'reply', url: STATUS, text: 'r2' });
    assert.equal(r.status, 429);
    assert.equal((await r.json()).error, 'x_daily_limit');
    const st = await getState(base);
    assert.deepEqual(st.posts, { count: 2, limit: 2 });
    assert.deepEqual(st.replies, { count: 1, limit: 1 });
    assert.equal(calls().length, 4); // 1 failed + 2 posts + 1 reply; refused ones never spawned
    // reads are never limited
    assert.equal((await postX(base, { command: 'trending' })).status, 200);
  });
});

test('reported failure inside exit-0 JSON is not counted', async () => {
  await withGate({}, async (base) => {
    reset(); setMode({ stdout: '[{"status":"failed","message":"nope"}]' });
    const j = await (await postX(base, { command: 'post', text: 'x1' })).json();
    assert.equal(j.ok, false);
    assert.equal((await getState(base)).posts.count, 0);
  });
});

test('min write interval -> 429 x_write_too_soon with retry_after_ms; reads unaffected', async () => {
  await withGate({ xMinWriteIntervalMs: 60000 }, async (base) => {
    reset(); setMode(OK);
    assert.equal((await postX(base, { command: 'post', text: 'first' })).status, 200);
    const r = await postX(base, { command: 'post', text: 'second' });
    assert.equal(r.status, 429);
    const j = await r.json();
    assert.equal(j.error, 'x_write_too_soon');
    assert.ok(j.retry_after_ms > 0 && j.retry_after_ms <= 60000);
    assert.equal(calls().length, 1);
    assert.equal((await postX(base, { command: 'profile' })).status, 200);
  });
  await withGate({ xMinWriteIntervalMs: 300 }, async (base) => {
    reset(); setMode(OK);
    assert.equal((await postX(base, { command: 'post', text: 'a-one' })).status, 200);
    await new Promise((r) => setTimeout(r, 350));
    assert.equal((await postX(base, { command: 'post', text: 'a-two' })).status, 200);
  });
});

test('duplicate text (whitespace-normalized) refused across post and reply', async () => {
  await withGate({}, async (base) => {
    reset(); setMode(OK);
    assert.equal((await postX(base, { command: 'post', text: 'Hello   there\nfriend' })).status, 200);
    let r = await postX(base, { command: 'post', text: ' Hello there friend ' });
    assert.equal(r.status, 409);
    assert.equal((await r.json()).error, 'x_duplicate');
    r = await postX(base, { command: 'reply', url: STATUS, text: 'Hello there friend' });
    assert.equal(r.status, 409);
    assert.equal(calls().length, 1);
  });
});

test('state persists across a gate restart; old duplicates expire; new day resets counters', async () => {
  const p = paths();
  await withGate({ ...p, xMinWriteIntervalMs: 60000 }, async (base) => {
    reset(); setMode(OK);
    assert.equal((await postX(base, { command: 'post', text: 'persisted' })).status, 200);
  });
  const saved = JSON.parse(fs.readFileSync(p.xStatePath, 'utf8'));
  assert.equal(saved.posts, 1);
  assert.equal(saved.hashes.length, 1);
  assert.ok(!JSON.stringify(saved).includes('persisted'), 'state stores hashes, not text');
  await withGate({ ...p, xMinWriteIntervalMs: 60000 }, async (base) => {
    assert.equal((await getState(base)).posts.count, 1);
    let r = await postX(base, { command: 'post', text: 'persisted' });
    assert.equal(r.status, 409);
    r = await postX(base, { command: 'post', text: 'other' });
    assert.equal((await r.json()).error, 'x_write_too_soon');
  });
  // simulate yesterday + hash older than 24h
  saved.day = '2000-01-01'; saved.posts = 5; saved.last_write_at = 0;
  saved.hashes = saved.hashes.map((h) => ({ ...h, t: Date.now() - 25 * 3600 * 1000 }));
  fs.writeFileSync(p.xStatePath, JSON.stringify(saved));
  await withGate({ ...p }, async (base) => {
    assert.equal((await getState(base)).posts.count, 0);
    reset(); setMode(OK);
    assert.equal((await postX(base, { command: 'post', text: 'persisted' })).status, 200);
  });
  assert.equal(fs.readdirSync(tmp).filter((f) => f.endsWith('.tmp')).length, 0);
});

test('post log has text/reply_to/result; request log lines never contain text, url or query', async () => {
  const lines = [];
  const cfg = cfgFor();
  const server = await startGate(cfg);
  const base = `http://127.0.0.1:${server.address().port}`;
  const orig = console.log; console.log = (l) => lines.push(String(l));
  try {
    reset(); setMode(OK);
    await postX(base, { command: 'post', text: 'secret tweet body' });
    await postX(base, { command: 'reply', url: STATUS, text: 'secret reply body' });
    await postX(base, { command: 'search', query: 'secretquery' });
    await postX(base, { command: 'post', text: '-bad secret' });
    await getState(base);
    await new Promise((r) => setTimeout(r, 50));
  } finally { console.log = orig; server.close(); server.closeAllConnections?.(); }
  const all = lines.join('\n');
  assert.ok(lines.length >= 5);
  for (const w of ['secret', 'someone', '1234567890', TOKEN]) assert.ok(!all.includes(w), w);
  const log = fs.readFileSync(cfg.xLogPath, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  assert.equal(log.length, 2);
  assert.equal(log[0].command, 'post');
  assert.equal(log[0].text, 'secret tweet body');
  assert.equal(log[0].reply_to, null);
  assert.equal(log[0].ok, true);
  assert.deepEqual(log[0].result, { url: 'https://x.com/fedya/status/123456', id: null });
  assert.equal(log[1].command, 'reply');
  assert.equal(log[1].reply_to, STATUS);
  assert.ok(!Number.isNaN(Date.parse(log[0].time)));
});

test('failed attempts are logged with error', async () => {
  await withGate({}, async (base, cfg) => {
    reset(); setMode({ exit: 77, stdout: '' });
    const j = await (await postX(base, { command: 'post', text: 'will fail' })).json();
    assert.equal(j.error, 'auth_required');
    const log = fs.readFileSync(cfg.xLogPath, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
    assert.equal(log[0].ok, false);
    assert.equal(log[0].error, 'auth_required');
    assert.equal((await getState(base)).posts.count, 0);
  });
});

test('/x/state: counts, limits, last 10 entries, auth required', async () => {
  await withGate({ xDailyPostLimit: 50 }, async (base) => {
    assert.equal((await fetch(base + '/x/state')).status, 401);
    reset(); setMode(OK);
    for (let i = 0; i < 12; i++) await postX(base, { command: 'post', text: `msg ${i}` });
    const st = await getState(base);
    assert.equal(st.ok, true);
    assert.deepEqual(st.posts, { count: 12, limit: 50 });
    assert.deepEqual(st.replies, { count: 0, limit: 20 });
    assert.equal(st.recent.length, 10);
    assert.equal(st.recent[9].text, 'msg 11');
    assert.equal(st.recent[0].text, 'msg 2');
    assert.match(st.day, /^\d{4}-\d{2}-\d{2}$/);
  });
});

test('x endpoint shares auth / content-type / method rules', async () => {
  await withGate({}, async (base) => {
    assert.equal((await fetch(base + '/x', { method: 'POST', body: '{}' })).status, 401);
    const noCt = await fetch(base + '/x', { method: 'POST', headers: { Authorization: `Bearer ${TOKEN}` }, body: '{}' });
    assert.equal(noCt.status, 400);
    assert.equal((await fetch(base + '/x', { headers: { Authorization: `Bearer ${TOKEN}` } })).status, 405);
  });
});

test('malformed or partial existing state fails closed without spawning', async () => {
  for (const text of ['{broken', '{}']) {
    await withGate({}, async (base, cfg) => {
      reset(); setMode(OK);
      fs.writeFileSync(cfg.xStatePath, text);
      const r = await postX(base, { command: 'post', text: 'must not post' });
      assert.equal(r.status, 500);
      assert.equal((await r.json()).error, 'internal_error');
      assert.equal(calls().length, 0);
    });
  }
});

test('state reservation must persist before OpenCLI is spawned', async () => {
  await withGate({}, async (base, cfg) => {
    reset();
    setMode({
      ...OK,
      expectState: { path: cfg.xStatePath, posts: 1, replies: 0, hashes: 1 },
    });
    const j = await (await postX(base, { command: 'post', text: 'reserved first' })).json();
    assert.equal(j.ok, true);
    assert.equal(calls().length, 1);
  });
});

test('unpersistable state refuses a write before OpenCLI is spawned', async () => {
  const missing = path.join(tmp, 'missing-parent', 'state.json');
  fs.rmSync(path.dirname(missing), { recursive: true, force: true });
  await withGate({ xStatePath: missing }, async (base) => {
    reset(); setMode(OK);
    const r = await postX(base, { command: 'post', text: 'cannot reserve' });
    assert.equal(r.status, 500);
    assert.equal((await r.json()).error, 'internal_error');
    assert.equal(calls().length, 0);
  });
});

test('definite failure rolls back quota and hash but keeps write interval', async () => {
  await withGate({ xMinWriteIntervalMs: 60000 }, async (base, cfg) => {
    reset(); setMode({ exit: 77, stdout: '' });
    const first = await (await postX(base, { command: 'post', text: 'known failure' })).json();
    assert.equal(first.error, 'auth_required');
    const saved = JSON.parse(fs.readFileSync(cfg.xStatePath, 'utf8'));
    assert.equal(saved.posts, 0);
    assert.equal(saved.hashes.length, 0);
    assert.ok(saved.last_write_at > 0);

    setMode(OK);
    const r = await postX(base, { command: 'post', text: 'known failure' });
    assert.equal(r.status, 429);
    assert.equal((await r.json()).error, 'x_write_too_soon');
    assert.equal(calls().length, 1);
  });
});

test('ambiguous output remains counted and deduped', async () => {
  await withGate({}, async (base, cfg) => {
    reset(); setMode({ stdout: 'not-json' });
    const first = await (await postX(base, { command: 'post', text: 'maybe posted' })).json();
    assert.equal(first.error, 'bad_output');
    const saved = JSON.parse(fs.readFileSync(cfg.xStatePath, 'utf8'));
    assert.equal(saved.posts, 1);
    assert.equal(saved.hashes.length, 1);

    setMode(OK);
    const r = await postX(base, { command: 'post', text: 'maybe posted' });
    assert.equal(r.status, 409);
    assert.equal((await r.json()).error, 'x_duplicate');
    assert.equal(calls().length, 1);
  });
});

test('config defaults for x guards', () => {
  const c = loadConfig({}, path.join(tmp, 'missing.json'));
  assert.equal(c.xDailyPostLimit, 5);
  assert.equal(c.xDailyReplyLimit, 20);
  assert.equal(c.xMinWriteIntervalMs, 60000);
  assert.match(c.xStatePath, /gate-state\.json$/);
  assert.match(c.xLogPath, /x-post-log\.jsonl$/);
});
