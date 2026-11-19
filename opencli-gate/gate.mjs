// opencli-gate: 小红书只读 + 推特读/发门卫。零依赖，Node >= 20。
import http from 'node:http';
import path from 'node:path';
import fs from 'node:fs';
import net from 'node:net';
import crypto from 'node:crypto';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HEX24 = /^[0-9a-f]{24}$/i;
const XHS_HOST = 'www.xiaohongshu.com';
const MAX_BODY = 8 * 1024;

// ---------- config ----------

export function loadConfig(env = process.env, configPath) {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const file = configPath ?? path.join(here, 'gate.config.json');
  let fileCfg = {};
  if (fs.existsSync(file)) {
    try {
      fileCfg = JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch (e) {
      throw new Error(`gate.config.json 不是合法 JSON：${e.message}`);
    }
  }
  const pick = (envKey, key) => (env[envKey] !== undefined && env[envKey] !== '' ? env[envKey] : fileCfg[key]);
  const num = (v, d) => (v === undefined || v === null || v === '' ? d : Number(v));
  const defaultExe = path.join(env.LOCALAPPDATA || '', 'OpenCLIApp', 'opencli-app.exe');
  let prefix = fileCfg.opencliPrefixArgs;
  if (env.GATE_OPENCLI_PREFIX_ARGS) prefix = JSON.parse(env.GATE_OPENCLI_PREFIX_ARGS);
  const allowLoopbackRaw = pick('GATE_ALLOW_LOOPBACK', 'allowLoopback');
  return {
    host: String(pick('GATE_HOST', 'host') ?? '').trim(),
    port: num(pick('GATE_PORT', 'port'), 19830),
    token: String(pick('GATE_TOKEN', 'token') ?? ''),
    allowLoopback: allowLoopbackRaw === true || allowLoopbackRaw === 'true' || allowLoopbackRaw === '1',
    opencliExe: pick('GATE_OPENCLI_EXE', 'opencliExe') || defaultExe,
    opencliPrefixArgs: Array.isArray(prefix) ? prefix : ['__opencli_shim'],
    minIntervalMs: num(pick('GATE_MIN_INTERVAL_MS', 'minIntervalMs'), 4000),
    timeoutMs: num(pick('GATE_TIMEOUT_MS', 'timeoutMs'), 90000),
    xDailyPostLimit: num(pick('GATE_X_DAILY_POST_LIMIT', 'xDailyPostLimit'), 5),
    xDailyReplyLimit: num(pick('GATE_X_DAILY_REPLY_LIMIT', 'xDailyReplyLimit'), 20),
    xMinWriteIntervalMs: num(pick('GATE_X_MIN_WRITE_INTERVAL_MS', 'xMinWriteIntervalMs'), 60000),
    xStatePath: String(pick('GATE_X_STATE_PATH', 'xStatePath') || path.join(here, 'gate-state.json')),
    xLogPath: String(pick('GATE_X_LOG_PATH', 'xLogPath') || path.join(here, 'x-post-log.jsonl')),
  };
}

function inTailscaleRange(host) {
  if (net.isIPv4(host) === false) return false;
  const [a, b] = host.split('.').map(Number);
  return a === 100 && b >= 64 && b <= 127; // 100.64.0.0/10
}

export function validateConfig(cfg) {
  const host = cfg.host;
  if (!host) throw new Error('host 未配置：请填写这台电脑的 Tailscale IPv4（100.x.y.z）');
  if (host === '0.0.0.0' || host === '::') throw new Error('host 不能是 0.0.0.0 或 ::（不允许监听所有网卡）');
  if (cfg.allowLoopback && host === '127.0.0.1') {
    // 仅本机测试
  } else if (!inTailscaleRange(host)) {
    throw new Error('host 必须是 Tailscale 地址（100.64.0.0/10）');
  }
  if (typeof cfg.token !== 'string' || cfg.token.length < 32) throw new Error('token 至少 32 个字符');
  if (!Number.isInteger(cfg.port) || cfg.port < 0 || cfg.port > 65535) throw new Error('port 不合法');
  if (!(cfg.minIntervalMs >= 0) || !(cfg.timeoutMs > 0)) throw new Error('minIntervalMs / timeoutMs 不合法');
  for (const k of ['xDailyPostLimit', 'xDailyReplyLimit', 'xMinWriteIntervalMs']) {
    if (cfg[k] !== undefined && !(Number.isInteger(cfg[k]) && cfg[k] >= 0)) throw new Error(`${k} 必须是非负整数`);
  }
  return cfg;
}

// ---------- request validation ----------

class BadRequest extends Error {}
const bad = (msg) => new BadRequest(msg);

const ALLOWED_FIELDS = new Set(['command', 'query', 'url', 'user', 'limit', 'with_replies']);
const COMMANDS = ['feed', 'search', 'note', 'comments', 'user'];

function intLimit(v, def, max, name = 'limit') {
  if (v === undefined) return def;
  if (typeof v !== 'number' || !Number.isInteger(v) || v < 1 || v > max) throw bad(`${name} 必须是 1-${max} 的整数`);
  return v;
}

function checkNoteUrl(u) {
  if (typeof u !== 'string' || u.length === 0 || u.length > 1000) throw bad('url 必须是 1000 字符以内的字符串');
  let parsed;
  try { parsed = new URL(u); } catch { throw bad('url 不是合法网址'); }
  if (parsed.protocol !== 'https:') throw bad('url 必须是 https');
  if (parsed.hostname !== XHS_HOST || parsed.port !== '' || parsed.username || parsed.password) throw bad(`url 主机必须是 ${XHS_HOST}`);
  const m = /^\/(?:explore|discovery\/item)\/([0-9a-fA-F]{24})$/.exec(parsed.pathname);
  if (!m) throw bad('url 路径必须是 /explore/<24位id> 或 /discovery/item/<24位id>');
  if (!parsed.searchParams.get('xsec_token')) throw bad('url 必须带 xsec_token 参数');
  return u;
}

function checkUser(u) {
  if (typeof u !== 'string' || u.length === 0 || u.length > 1000) throw bad('user 必须是字符串');
  if (HEX24.test(u)) return u;
  let parsed;
  try { parsed = new URL(u); } catch { throw bad('user 必须是 24 位 id 或用户主页链接'); }
  if (parsed.protocol !== 'https:' || parsed.hostname !== XHS_HOST || parsed.port !== '' || parsed.username || parsed.password) {
    throw bad(`user 链接必须是 https://${XHS_HOST}/user/profile/<24位id>`);
  }
  if (!/^\/user\/profile\/[0-9a-fA-F]{24}$/.test(parsed.pathname)) throw bad('user 链接路径必须是 /user/profile/<24位id>');
  return u;
}

// 返回 argv（不含前缀）；非法则抛 BadRequest。
export function buildArgs(body) {
  if (body === null || typeof body !== 'object' || Array.isArray(body)) throw bad('请求体必须是 JSON 对象');
  for (const k of Object.keys(body)) if (!ALLOWED_FIELDS.has(k)) throw bad(`不允许的字段：${k}`);
  const { command } = body;
  if (typeof command !== 'string' || !COMMANDS.includes(command)) throw bad(`command 必须是 ${COMMANDS.join(' / ')} 之一`);
  const allowedFor = {
    feed: ['limit'],
    search: ['query', 'limit'],
    note: ['url'],
    comments: ['url', 'limit', 'with_replies'],
    user: ['user', 'limit'],
  }[command];
  for (const k of Object.keys(body)) {
    if (k !== 'command' && !allowedFor.includes(k)) throw bad(`${command} 不接受字段 ${k}`);
  }
  let args;
  switch (command) {
    case 'feed':
      args = ['xiaohongshu', 'feed', '--limit', String(intLimit(body.limit, 10, 20))];
      break;
    case 'search': {
      const q = body.query;
      if (typeof q !== 'string' || q.length < 1 || q.length > 100) throw bad('query 必须是 1-100 字符的字符串');
      if (/[\u0000-\u001f\u007f]/.test(q)) throw bad('query 不能含控制字符');
      if (q.startsWith('-')) throw bad('query 不能以 - 开头');
      args = ['xiaohongshu', 'search', q, '--limit', String(intLimit(body.limit, 10, 20))];
      break;
    }
    case 'note':
      args = ['xiaohongshu', 'note', checkNoteUrl(body.url)];
      break;
    case 'comments': {
      const url = checkNoteUrl(body.url);
      const wr = body.with_replies;
      if (wr !== undefined && typeof wr !== 'boolean') throw bad('with_replies 必须是布尔值');
      args = ['xiaohongshu', 'comments', url, '--limit', String(intLimit(body.limit, 20, 50))];
      if (wr) args.push('--with-replies');
      break;
    }
    case 'user':
      args = ['xiaohongshu', 'user', checkUser(body.user), '--limit', String(intLimit(body.limit, 15, 30))];
      break;
  }
  return [...args, '-f', 'json', '--window', 'background'];
}


// ---------- X (Twitter) request validation ----------

const X_READ_COMMANDS = ['timeline', 'search', 'thread', 'tweets', 'profile', 'trending'];
const X_WRITE_COMMANDS = ['post', 'reply'];
const X_ALLOWED_FIELDS = new Set(['command', 'type', 'query', 'product', 'from', 'url', 'user', 'limit', 'text']);
const X_HOSTS = new Set(['x.com', 'twitter.com', 'www.x.com', 'mobile.twitter.com']);
const X_SCREEN_NAME = /^@?([A-Za-z0-9_]{1,15})$/;
const X_TWEET_ID = /^[0-9]{5,25}$/;
const X_STATUS_PATH = /^\/([A-Za-z0-9_]{1,15})\/status\/([0-9]{5,25})(?:\/.*)?$/;
const X_TEXT_LIMIT = 280;
const CONTROL_EXCEPT_NL = /[\u0000-\u0009\u000b-\u001f\u007f-\u009f]/;
const WIDE = /[\p{Extended_Pictographic}ᄀ-ᇿ⺀-꓏가-힣豈-﫿︰-﹯＀-｠￠-￦\u{1f000}-\u{1faff}\u{20000}-\u{3ffff}]/u;

export function weightedLength(text) {
  let n = 0;
  for (const ch of text) n += WIDE.test(ch) ? 2 : 1;
  return n;
}

function xScreenName(v, name) {
  if (typeof v !== 'string') throw bad(`${name} 必须是字符串`);
  const m = X_SCREEN_NAME.exec(v);
  if (!m) throw bad(`${name} 必须是 1-15 位字母数字下划线的用户名（可带 @）`);
  return m[1];
}

function xStatusUrl(v) {
  if (typeof v !== 'string' || v.length === 0 || v.length > 1000) throw bad('url 必须是 1000 字符以内的字符串');
  let parsed;
  try { parsed = new URL(v); } catch { throw bad('url 不是合法网址'); }
  if (parsed.protocol !== 'https:' || !X_HOSTS.has(parsed.hostname) || parsed.port !== '' || parsed.username || parsed.password) {
    throw bad('url 必须是 https 的 x.com / twitter.com 推文链接');
  }
  const m = X_STATUS_PATH.exec(parsed.pathname);
  if (!m) throw bad('url 路径必须是 /<用户名>/status/<推文id>');
  return `https://x.com/${m[1]}/status/${m[2]}`;
}

function xQuery(v, name, max) {
  if (typeof v !== 'string' || v.length < 1 || v.length > max) throw bad(`${name} 必须是 1-${max} 字符的字符串`);
  if (CONTROL_EXCEPT_NL.test(v)) throw bad(`${name} 不能含控制字符`);
  if (v.trim().length === 0) throw bad(`${name} 不能为空`);
  if (v.startsWith('-') || v.trim().startsWith('-')) throw bad(`${name} 不能以 - 开头`);
  return v;
}

function xText(v) {
  if (typeof v !== 'string') throw bad('text 必须是字符串');
  if (CONTROL_EXCEPT_NL.test(v)) throw bad('text 不能含控制字符');
  const t = v.trim();
  if (t.length === 0) throw bad('text 不能为空');
  if (t.startsWith('-')) throw bad('text 不能以 - 开头');
  if (weightedLength(t) > X_TEXT_LIMIT) throw bad(`text 超过 ${X_TEXT_LIMIT} 字（中日韩/全角/表情算 2）`);
  return t;
}

export function normalizeText(t) {
  return t.replace(/\s+/g, ' ').trim();
}

// 返回 { kind: 'read'|'post'|'reply', args, text, replyTo }；非法则抛 BadRequest。
export function buildXRequest(body) {
  if (body === null || typeof body !== 'object' || Array.isArray(body)) throw bad('请求体必须是 JSON 对象');
  for (const k of Object.keys(body)) if (!X_ALLOWED_FIELDS.has(k)) throw bad(`不允许的字段：${k}`);
  const { command } = body;
  if (typeof command !== 'string' || ![...X_READ_COMMANDS, ...X_WRITE_COMMANDS].includes(command)) {
    throw bad(`command 必须是 ${[...X_READ_COMMANDS, ...X_WRITE_COMMANDS].join(' / ')} 之一`);
  }
  const allowedFor = {
    timeline: ['type', 'limit'],
    search: ['query', 'product', 'from', 'limit'],
    thread: ['url', 'limit'],
    tweets: ['user', 'limit'],
    profile: ['user'],
    trending: ['limit'],
    post: ['text'],
    reply: ['url', 'text'],
  }[command];
  for (const k of Object.keys(body)) {
    if (k !== 'command' && !allowedFor.includes(k)) throw bad(`${command} 不接受字段 ${k}`);
  }
  let args;
  let kind = 'read';
  let text = null;
  let replyTo = null;
  switch (command) {
    case 'timeline': {
      args = ['twitter', 'timeline'];
      if (body.type !== undefined) {
        if (!['for-you', 'following'].includes(body.type)) throw bad('type 必须是 for-you / following');
        args.push('--type', body.type);
      }
      args.push('--limit', String(intLimit(body.limit, 10, 20)));
      break;
    }
    case 'search': {
      args = ['twitter', 'search', xQuery(body.query, 'query', 200)];
      if (body.product !== undefined) {
        if (!['top', 'live', 'photos', 'videos'].includes(body.product)) throw bad('product 必须是 top / live / photos / videos');
        args.push('--product', body.product);
      }
      if (body.from !== undefined) args.push('--from', xScreenName(body.from, 'from'));
      args.push('--limit', String(intLimit(body.limit, 10, 20)));
      break;
    }
    case 'thread': {
      const v = body.url;
      const target = typeof v === 'string' && X_TWEET_ID.test(v) ? v : xStatusUrl(v);
      args = ['twitter', 'thread', target, '--limit', String(intLimit(body.limit, 20, 50))];
      break;
    }
    case 'tweets':
      args = ['twitter', 'tweets'];
      if (body.user !== undefined) args.push(xScreenName(body.user, 'user'));
      args.push('--limit', String(intLimit(body.limit, 10, 20)), '--page-delay', '2');
      break;
    case 'profile':
      args = ['twitter', 'profile'];
      if (body.user !== undefined) args.push(xScreenName(body.user, 'user'));
      break;
    case 'trending':
      args = ['twitter', 'trending', '--limit', String(intLimit(body.limit, 10, 20))];
      break;
    case 'post':
      kind = 'post';
      text = xText(body.text);
      args = ['twitter', 'post', text];
      break;
    case 'reply':
      kind = 'reply';
      replyTo = xStatusUrl(body.url);
      text = xText(body.text);
      args = ['twitter', 'reply', replyTo, text];
      break;
  }
  return { kind, args: [...args, '-f', 'json', '--window', 'background'], text, replyTo };
}

// ---------- X write guards (state + log on disk) ----------

const DAY_MS = 24 * 3600 * 1000;
const localDay = (d = new Date()) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const textHash = (t) => crypto.createHash('sha256').update(normalizeText(t)).digest('hex');

function freshXState() {
  return { day: localDay(), posts: 0, replies: 0, last_write_at: 0, hashes: [] };
}

function loadXState(file) {
  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch (e) {
    if (e.code === 'ENOENT') return freshXState();
    throw e;
  }
  let raw;
  try { raw = JSON.parse(text); } catch { throw new Error('x_state_invalid'); }
  const now = Date.now();
  const valid = raw && typeof raw === 'object' && !Array.isArray(raw)
    && /^\d{4}-\d{2}-\d{2}$/.test(raw.day)
    && Number.isInteger(raw.posts) && raw.posts >= 0
    && Number.isInteger(raw.replies) && raw.replies >= 0
    && Number.isFinite(raw.last_write_at) && raw.last_write_at >= 0
    && Array.isArray(raw.hashes)
    && raw.hashes.every((h) => h && typeof h === 'object' && !Array.isArray(h)
      && /^[0-9a-f]{64}$/.test(h.h) && Number.isFinite(h.t) && h.t >= 0);
  if (!valid) throw new Error('x_state_invalid');
  const st = {
    day: raw.day,
    posts: raw.posts,
    replies: raw.replies,
    last_write_at: raw.last_write_at,
    hashes: raw.hashes.filter((h) => now - h.t < DAY_MS),
  };
  return rollDay(st);
}

function rollDay(st) {
  const today = localDay();
  if (st.day !== today) { st.day = today; st.posts = 0; st.replies = 0; }
  return st;
}

function saveXState(file, st) {
  const tmpFile = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmpFile, JSON.stringify(st), 'utf8');
  fs.renameSync(tmpFile, file);
}

function appendXLog(file, entry) {
  fs.appendFileSync(file, JSON.stringify(entry) + '\n', 'utf8');
}

function readXLogTail(file, n) {
  try {
    const raw = fs.readFileSync(file, 'utf8');
    const lines = raw.split('\n').filter(Boolean).slice(-n);
    return lines.map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
  } catch { return []; }
}

function pickResult(data) {
  const row = Array.isArray(data) ? data[0] : data;
  if (!row || typeof row !== 'object') return null;
  const url = typeof row.url === 'string' ? row.url : null;
  const id = row.id ?? row.tweet_id ?? row.tweetId ?? null;
  if (url === null && id === null) return null;
  return { url, id: id === null ? null : String(id) };
}

// opencli 退出 0 但 JSON 里自报失败时，按失败处理（不计数）。
function reportedFailure(data) {
  const row = Array.isArray(data) ? data[0] : data;
  if (!row || typeof row !== 'object') return null;
  if (row.ok === false || row.success === false) return String(row.message || row.error || 'reported_failure').slice(0, 300);
  if (typeof row.status === 'string' && /^(fail|error)/i.test(row.status)) return String(row.message || row.status).slice(0, 300);
  return null;
}

// ---------- runner ----------

const EXIT_NAMES = { 69: 'bridge_down', 77: 'auth_required', 66: 'empty', 75: 'timeout' };

function runOpencli(cfg, args, timeoutMs) {
  return new Promise((resolve) => {
    execFile(cfg.opencliExe, [...cfg.opencliPrefixArgs, ...args], {
      windowsHide: true,
      timeout: timeoutMs,
      killSignal: 'SIGKILL',
      maxBuffer: 16 * 1024 * 1024,
      encoding: 'utf8',
    }, (err, stdout, stderr) => {
      if (!err) return resolve({ code: 0, stdout, stderr });
      if (err.killed || err.signal) return resolve({ timedOut: true, stdout, stderr });
      resolve({ code: typeof err.code === 'number' ? err.code : -1, stdout, stderr, spawnError: typeof err.code === 'string' ? err.code : undefined });
    });
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- server ----------

export function createGate(cfg) {
  let busy = false;
  let lastStart = 0;
  const xLimits = () => ({
    posts: cfg.xDailyPostLimit ?? 5,
    replies: cfg.xDailyReplyLimit ?? 20,
    minIntervalMs: cfg.xMinWriteIntervalMs ?? 60000,
  });
  const xStatePath = cfg.xStatePath || path.join(path.dirname(fileURLToPath(import.meta.url)), 'gate-state.json');
  const xLogPath = cfg.xLogPath || path.join(path.dirname(fileURLToPath(import.meta.url)), 'x-post-log.jsonl');
  const tokenBuf = Buffer.from(cfg.token);

  function authOk(req) {
    const h = req.headers.authorization || '';
    const m = /^Bearer (.+)$/.exec(h);
    if (!m) return false;
    const given = Buffer.from(m[1]);
    // 长度无关的比较：对 SHA-256 摘要做 timingSafeEqual
    const a = crypto.createHash('sha256').update(given).digest();
    const b = crypto.createHash('sha256').update(tokenBuf).digest();
    return crypto.timingSafeEqual(a, b) && given.length === tokenBuf.length;
  }

  async function exclusive(fn) {
    if (busy) return null;
    busy = true;
    try {
      const wait = lastStart + cfg.minIntervalMs - Date.now();
      if (wait > 0) await sleep(wait);
      lastStart = Date.now();
      return await fn();
    } finally {
      busy = false;
    }
  }

  function readBody(req) {
    return new Promise((resolve, reject) => {
      let size = 0;
      let over = false;
      const chunks = [];
      req.on('data', (c) => {
        if (over) return;
        size += c.length;
        if (size > MAX_BODY) { over = true; chunks.length = 0; reject(Object.assign(new Error('too_large'), { status: 413 })); return; }
        chunks.push(c);
      });
      req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
      req.on('error', reject);
    });
  }


  // 在 exclusive() 里执行：检查护栏 -> 调 opencli -> 记账/记日志。返回 { status, body }。
  async function runXWrite(xreq) {
    const lim = xLimits();
    const st = loadXState(xStatePath);
    const hash = textHash(xreq.text);
    const isPost = xreq.kind === 'post';
    if (st.hashes.some((h) => h.h === hash)) return { status: 409, body: { ok: false, error: 'x_duplicate' } };
    if ((isPost ? st.posts >= lim.posts : st.replies >= lim.replies)) {
      return { status: 429, body: { ok: false, error: 'x_daily_limit', kind: xreq.kind, limit: isPost ? lim.posts : lim.replies } };
    }
    const sinceLast = Date.now() - st.last_write_at;
    if (st.last_write_at && sinceLast < lim.minIntervalMs) {
      return { status: 429, body: { ok: false, error: 'x_write_too_soon', retry_after_ms: lim.minIntervalMs - sinceLast } };
    }

    const reservedAt = Date.now();
    st.last_write_at = reservedAt;
    st.hashes.push({ h: hash, t: reservedAt });
    if (isPost) st.posts += 1; else st.replies += 1;
    saveXState(xStatePath, st);

    const r = await runOpencli(cfg, xreq.args, cfg.timeoutMs);
    const entry = { time: new Date().toISOString(), command: xreq.kind, text: xreq.text, reply_to: xreq.replyTo, ok: false, result: null, error: null };
    let out;
    let definiteFailure = false;
    if (r.timedOut) {
      entry.error = 'timeout';
      out = { status: 504, body: { ok: false, error: 'timeout' } };
    } else if (r.code !== 0) {
      definiteFailure = true;
      const name = r.spawnError ? 'opencli_failed' : (EXIT_NAMES[r.code] || 'opencli_failed');
      entry.error = name;
      out = { status: 200, body: { ok: false, exit_code: r.code, error: name, detail: (r.stderr || r.spawnError || '').slice(0, 500) } };
    } else {
      let data;
      try { data = JSON.parse(r.stdout); } catch { data = undefined; }
      if (data === undefined) {
        entry.error = 'bad_output';
        out = { status: 200, body: { ok: false, error: 'bad_output', stdout: (r.stdout || '').slice(0, 2000) } };
      } else {
        const failure = reportedFailure(data);
        if (failure) {
          definiteFailure = true;
          entry.error = failure;
          out = { status: 200, body: { ok: false, error: 'opencli_failed', detail: failure } };
        } else {
          entry.ok = true;
          entry.result = pickResult(data);
          out = { status: 200, body: { ok: true, command: xreq.kind, data } };
        }
      }
    }
    try { appendXLog(xLogPath, entry); } catch (e) { console.error(`x-post-log 写入失败：${e.code || 'error'}`); }
    if (definiteFailure) {
      st.hashes = st.hashes.filter((h) => !(h.h === hash && h.t === reservedAt));
      if (isPost) st.posts -= 1; else st.replies -= 1;
      saveXState(xStatePath, st);
    }
    return out;
  }

  const server = http.createServer(async (req, res) => {
    const started = Date.now();
    const urlPath = (req.url || '').split('?')[0];
    let command = '-';
    let status = 500;
    const send = (code, obj) => {
      status = code;
      const data = JSON.stringify(obj);
      res.writeHead(code, { Connection: 'close', 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': Buffer.byteLength(data) });
      res.end(data);
    };
    res.on('close', () => {
      console.log(`${new Date().toISOString()} ${req.method} ${urlPath} cmd=${command} status=${status} ${Date.now() - started}ms`);
    });
    try {
      if (!authOk(req)) return send(401, { ok: false, error: 'unauthorized' });

      if (req.method === 'GET' && urlPath === '/status') {
        command = 'doctor';
        const r = await exclusive(() => runOpencli(cfg, ['doctor'], 20000));
        if (r === null) return send(429, { ok: false, error: 'busy' });
        if (r.timedOut) return send(504, { ok: false, error: 'timeout' });
        const text = ((r.stdout || '') + (r.stderr && !r.stdout ? r.stderr : '')).slice(0, 2000);
        return send(200, { ok: r.code === 0, doctor: text });
      }

      if (req.method === 'GET' && urlPath === '/x/state') {
        command = 'x_state';
        const st = loadXState(xStatePath);
        const lim = xLimits();
        return send(200, {
          ok: true,
          day: st.day,
          posts: { count: st.posts, limit: lim.posts },
          replies: { count: st.replies, limit: lim.replies },
          min_write_interval_ms: lim.minIntervalMs,
          last_write_at: st.last_write_at ? new Date(st.last_write_at).toISOString() : null,
          recent: readXLogTail(xLogPath, 10),
        });
      }

      if (req.method === 'POST' && (urlPath === '/xhs' || urlPath === '/x')) {
        const isX = urlPath === '/x';
        const ct = String(req.headers['content-type'] || '');
        if (!/^application\/json\b/i.test(ct)) return send(400, { ok: false, error: '需要 Content-Type: application/json' });
        let raw;
        try { raw = await readBody(req); } catch (e) { return send(e.status || 400, { ok: false, error: e.status === 413 ? '请求体过大（上限 8KB）' : '读取请求体失败' }); }
        let body;
        try { body = JSON.parse(raw); } catch { return send(400, { ok: false, error: '请求体不是合法 JSON' }); }
        let args;
        let xreq = null;
        try {
          if (isX) { xreq = buildXRequest(body); args = xreq.args; } else { args = buildArgs(body); }
        } catch (e) {
          if (e instanceof BadRequest) { if (body && typeof body.command === 'string') command = body.command.slice(0, 20).replace(/[^a-z_]/gi, '?'); return send(400, { ok: false, error: e.message }); }
          throw e;
        }
        command = body.command;

        if (isX && xreq.kind !== 'read') {
          const r = await exclusive(() => runXWrite(xreq));
          if (r === null) return send(429, { ok: false, error: 'busy' });
          return send(r.status, r.body);
        }

        const r = await exclusive(() => runOpencli(cfg, args, cfg.timeoutMs));
        if (r === null) return send(429, { ok: false, error: 'busy' });
        if (r.timedOut) return send(504, { ok: false, error: 'timeout' });
        if (r.code !== 0) {
          const name = r.spawnError ? 'opencli_failed' : (EXIT_NAMES[r.code] || 'opencli_failed');
          return send(200, { ok: false, exit_code: r.code, error: name, detail: (r.stderr || r.spawnError || '').slice(0, 500) });
        }
        let data;
        try { data = JSON.parse(r.stdout); } catch {
          return send(200, { ok: false, error: 'bad_output', stdout: (r.stdout || '').slice(0, 2000) });
        }
        return send(200, { ok: true, command, data });
      }

      if (urlPath === '/status' || urlPath === '/xhs' || urlPath === '/x' || urlPath === '/x/state') return send(405, { ok: false, error: 'method_not_allowed' });
      return send(404, { ok: false, error: 'not_found' });
    } catch (e) {
      if (!res.headersSent) send(500, { ok: false, error: 'internal_error' });
    }
  });
  return server;
}

export async function startGate(cfg) {
  validateConfig(cfg);
  const server = createGate(cfg);
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(cfg.port, cfg.host, () => resolve(server));
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const cfg = loadConfig();
    const server = await startGate(cfg);
    console.log(`opencli-gate 已启动：http://${cfg.host}:${server.address().port}`);
  } catch (e) {
    console.error(`启动失败：${e.message}`);
    process.exit(1);
  }
}
