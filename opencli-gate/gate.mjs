// opencli-gate: 小红书只读门卫。零依赖，Node >= 20。
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

      if (req.method === 'POST' && urlPath === '/xhs') {
        const ct = String(req.headers['content-type'] || '');
        if (!/^application\/json\b/i.test(ct)) return send(400, { ok: false, error: '需要 Content-Type: application/json' });
        let raw;
        try { raw = await readBody(req); } catch (e) { return send(e.status || 400, { ok: false, error: e.status === 413 ? '请求体过大（上限 8KB）' : '读取请求体失败' }); }
        let body;
        try { body = JSON.parse(raw); } catch { return send(400, { ok: false, error: '请求体不是合法 JSON' }); }
        let args;
        try { args = buildArgs(body); } catch (e) {
          if (e instanceof BadRequest) { if (body && typeof body.command === 'string') command = body.command.slice(0, 20).replace(/[^a-z_]/gi, '?'); return send(400, { ok: false, error: e.message }); }
          throw e;
        }
        command = body.command;
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

      if (urlPath === '/status' || urlPath === '/xhs') return send(405, { ok: false, error: 'method_not_allowed' });
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
