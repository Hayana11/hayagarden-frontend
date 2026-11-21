'use strict';

const express                            = require('express');
const { McpServer }                      = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StreamableHTTPServerTransport }  = require('@modelcontextprotocol/sdk/server/streamableHttp.js');
const { execSync, execFileSync }       = require('child_process');
const { randomUUID }                     = require('crypto');
const { readFileSync }                    = require('fs');
const os                                  = require('os');
const path                                = require('path');
const { z }                              = require('zod');
const { readLightStatus }                = require('./light-status-adapter');

function momentsOwnerToken() {
  const direct = (process.env.MOMENTS_OWNER_TOKEN || '').trim();
  if (direct) return direct;

  try {
    const lines = readFileSync('/opt/frontend/.env', 'utf8').split(/\r?\n/);
    for (const rawLine of lines) {
      const match = rawLine.trim().match(/^MOMENTS_OWNER_TOKEN\s*=\s*(.*)$/);
      if (!match) continue;
      let value = match[1].trim();
      const quoted = (value.startsWith('"') && value.endsWith('"'))
        || (value.startsWith("'") && value.endsWith("'"));
      if (quoted) value = value.slice(1, -1);
      return value;
    }
  } catch (_error) {
    // The tool returns a configuration error below when the secret is unavailable.
  }
  return '';
}

const XHS_MAX_CHARS = 30000;
const XHS_OFFLINE = 'computer_not_connected：哈娅电脑上的小红书门卫不在线（电脑关机、睡眠或 Tailscale 未连接）。';
const XHS_MISCONFIGURED = 'opencli_gate_misconfigured：未配置 OPENCLI_GATE_URL / OPENCLI_GATE_TOKEN（/opt/frontend/.env 或环境变量）';
const XHS_ERROR_TEXT = {
  auth_required: '小红书账号需要重新登录（请哈娅在电脑的 OpenCLI Chrome 里重新登录）。',
  bridge_down: '浏览器扩展没连上（哈娅电脑上的 OpenCLI Chrome 扩展未运行）。',
  busy: '门卫正在执行上一条命令，稍后重试。',
  empty: '没有找到内容（结果为空）。',
  timeout: '小红书命令超时。',
  bad_output: '门卫返回了无法解析的输出。',
  opencli_failed: 'OpenCLI 执行失败。',
};

function envValue(key) {
  const direct = (process.env[key] || '').trim();
  if (direct) return direct;
  try {
    const lines = readFileSync('/opt/frontend/.env', 'utf8').split(/\r?\n/);
    for (const rawLine of lines) {
      const line = rawLine.trim();
      if (!line.startsWith(`${key}=`)) continue;
      let value = line.slice(key.length + 1).trim();
      const quoted = (value.startsWith('"') && value.endsWith('"'))
        || (value.startsWith("'") && value.endsWith("'"));
      if (quoted) value = value.slice(1, -1);
      return value;
    }
  } catch (_error) {
    // Falls through to the misconfigured message.
  }
  return '';
}

function xhsText(text) {
  return { content: [{ type: 'text', text }] };
}

async function callXhsGate(method, path, body, timeoutMs) {
  const base = envValue('OPENCLI_GATE_URL').replace(/\/+$/, '');
  const token = envValue('OPENCLI_GATE_TOKEN');
  if (!base || !token) return { error: XHS_MISCONFIGURED };
  let resp;
  try {
    resp = await fetch(base + path, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (_error) {
    return { error: XHS_OFFLINE };
  }
  if (resp.status === 401) {
    return { error: 'opencli_gate_auth_failed：门卫拒绝了 token（OPENCLI_GATE_TOKEN 与电脑上的配置不一致）' };
  }
  let payload;
  try {
    payload = await resp.json();
  } catch (_error) {
    return { error: `opencli_gate_http_${resp.status}：门卫返回了非 JSON 内容` };
  }
  if (!payload || typeof payload !== 'object') {
    return { error: 'opencli_gate_error：门卫返回格式异常' };
  }
  return { payload };
}

function xhsTruncate(text, limit = XHS_MAX_CHARS) {
  return text.length <= limit ? text : `${text.slice(0, limit)}\n…（已截断，共 ${text.length} 字符）`;
}

async function xhsStatus() {
  const { payload, error } = await callXhsGate('GET', '/status', null, 25000);
  if (error) return xhsText(error);
  const doctor = xhsTruncate(String(payload.doctor || ''), 2000);
  if (!payload.ok) return xhsText(`opencli_gate_unhealthy：门卫在线，但 OpenCLI 自检未通过。\n${doctor}`);
  return xhsText(`小红书门卫在线。\n${doctor}`);
}

async function xhsRead(params) {
  const body = {};
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) body[k] = v;
  }
  const { payload, error } = await callXhsGate('POST', '/xhs', body, 120000);
  if (error) return xhsText(error);
  if (payload.ok) return xhsText(xhsTruncate(JSON.stringify(payload.data)));
  const code = String(payload.error || 'opencli_failed');
  const text = XHS_ERROR_TEXT[code] || `失败：${code.slice(0, 200)}`;
  return xhsText(`${code}：${text}`);
}

const X_ERROR_TEXT = {
  ...XHS_ERROR_TEXT,
  auth_required: '推特账号需要重新登录（请哈娅在电脑的 OpenCLI Chrome 里重新登录）。',
  timeout: '推特命令超时。',
  x_daily_limit: '今天的发推/回复额度用完了。',
  x_write_too_soon: '两次发言间隔太短，稍后再发。',
  x_duplicate: '这句话 24 小时内发过了。',
};

function xFormatFailure(payload) {
  const code = String(payload.error || 'opencli_failed');
  let text = X_ERROR_TEXT[code] || `失败：${code.slice(0, 200)}`;
  if (code === 'x_write_too_soon' && typeof payload.retry_after_ms === 'number') {
    text += `（约 ${Math.floor(payload.retry_after_ms / 1000) + 1} 秒后可再发）`;
  }
  return `${code}：${text}`;
}

async function xCommand(params) {
  const body = {};
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) body[k] = v;
  }
  const { payload, error } = await callXhsGate('POST', '/x', body, 120000);
  if (error) return xhsText(error);
  if (payload.ok) return xhsText(xhsTruncate(JSON.stringify(payload.data)));
  return xhsText(xFormatFailure(payload));
}

async function xState() {
  const { payload, error } = await callXhsGate('GET', '/x/state', null, 25000);
  if (error) return xhsText(error);
  if (!payload.ok) return xhsText(xFormatFailure(payload));
  const { ok: _ok, ...rest } = payload;
  return xhsText(xhsTruncate(JSON.stringify(rest)));
}

async function runGatedHomeWrite({
  uhA0Profile,
  requireUhA0Profile = false,
  toolName,
  toolInput,
  verify,
  post,
}) {
  if (requireUhA0Profile && !uhA0Profile) {
    return { content: [{ type: 'text', text: 'UH-A0 PROFILE_REQUIRED' }] };
  }
  // Other legacy /mcp Wake calls deliberately retain their historical write path.
  if (!uhA0Profile) return post();
  const gate = verify(toolName, toolInput);
  if (!gate || !gate.ok) {
    return gate && gate.result
      ? gate.result
      : { content: [{ type: 'text', text: 'UH-A0 LEASE_MISMATCH' }] };
  }
  return post();
}

function residentTurnLeasePath(rawPath) {
  const raw = String(rawPath || '').trim();
  if (!raw) return '';
  const resolved = path.resolve(raw);
  const leaseDir = path.join(os.tmpdir(), 'hayagarden-uh-a0-turn-leases');
  if (path.dirname(resolved) !== leaseDir) return '';
  if (!/^[0-9a-f]{32}\.json$/.test(path.basename(resolved))) return '';
  return resolved;
}

function buildServer({ uhA0Profile = false, turnLeasePath = '' } = {}) {
  const verifiedTurnLeasePath = residentTurnLeasePath(turnLeasePath);
  const server = new McpServer({ name: 'home-mcp', version: '1.0.0' });

  server.tool(
    'get_vps_status',
    {},
    async () => {
      try {
        const cpuModel = (os.cpus()[0] && os.cpus()[0].model) || 'unknown';
        const cpuCount = os.cpus().length;
        const totalMemGiB = (os.totalmem() / 1024 ** 3).toFixed(1);
        const freeMemGiB = (os.freemem() / 1024 ** 3).toFixed(1);
        const disk = execFileSync('df', ['-h', '--output=size,used,avail,pcent', '/'], { encoding: 'utf-8' })
          .trim().split(/\n/).slice(-1)[0].trim().replace(/\s+/g, ' ');
        const uptimeHours = (os.uptime() / 3600).toFixed(1);
        const text = [
          `主机：${os.hostname()}`,
          `CPU：${cpuCount} vCPU · ${cpuModel}`,
          `内存：${totalMemGiB} GiB（当前空闲 ${freeMemGiB} GiB）`,
          `系统盘：${disk}`,
          `已运行：${uptimeHours} 小时`,
        ].join('\n');
        return { content: [{ type: 'text', text }] };
      } catch (e) {
        return { content: [{ type: 'text', text: 'Error: ' + e.message }] };
      }
    }
  );

  server.tool(
    'exec_vps',
    { command: z.string().describe('Shell command to execute on the VPS') },
    async ({ command }) => {
      try {
        const out = execSync(command, {
          timeout: 30000,
          encoding: 'utf-8',
          maxBuffer: 1024 * 1024,
        });
        return { content: [{ type: 'text', text: out || '(no output)' }] };
      } catch (e) {
        return { content: [{ type: 'text', text: 'Error: ' + e.message }] };
      }
    }
  );

  const LIGHT_DAEMON = 'http://127.0.0.1:5052';
  async function callLight(path, method, bodyObj) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (bodyObj) opts.body = JSON.stringify(bodyObj);
    try {
      const r   = await fetch(LIGHT_DAEMON + path, opts);
      const txt = await r.text();
      return { content: [{ type: 'text', text: txt || '(no output)' }] };
    } catch (e) {
      return { content: [{ type: 'text', text: 'Error: ' + e.message }] };
    }
  }

  // M3: 官端(CC)对 posts 记忆库的视野——网页端有 system 注入,官端靠这个工具主动翻
  server.tool(
    'search_memories',
    { keyword: z.string().describe('搜索关键词'), },
    async ({ keyword }) => {
      try {
        const r = await fetch('http://127.0.0.1:5050/api/posts?search='
          + encodeURIComponent(keyword) + '&limit=8');
        const d = await r.json();
        const items = (d.posts || []).map(p =>
          `[#${p.id} ${p.type} ${(p.created_at || '').slice(0, 10)}${p.pinned ? ' 📌' : ''}] ${(p.content || '').slice(0, 220)}`);
        return { content: [{ type: 'text', text: items.join('\n---\n') || '没有找到相关记忆' }] };
      } catch (e) {
        return { content: [{ type: 'text', text: 'Error: ' + e.message }] };
      }
    });

  server.tool(
    'write_diary',
    {
      content: z.string().describe('要保存的日记正文'),
    },
    async ({ content }) => runGatedHomeWrite({
      uhA0Profile,
      requireUhA0Profile: true,
      toolName: 'mcp__home__write_diary',
      toolInput: { content },
      verify: verifyCurrentHomeAction,
      post: () => writeDiaryDirect(content),
    })
  );

  server.tool('light_on',  {}, () => callLight('/light/on',  'POST'));
  server.tool('light_off', {}, () => callLight('/light/off', 'POST'));
  server.tool('get_light_status', {}, async () => ({
    content: [{ type: 'text', text: await readLightStatus() }],
  }));
  server.tool('light_bedside_warm',    {}, () => callLight('/light/bedside/warm',    'POST'));
  server.tool('light_bedside_neutral', {}, () => callLight('/light/bedside/neutral', 'POST'));
  server.tool(
    'set_brightness',
    { value: z.number().min(1).max(100).describe('Brightness 1-100') },
    ({ value }) => callLight('/light/brightness', 'POST', { value })
  );
  server.tool(
    'set_color_temp',
    { value: z.number().describe('Color temperature in Kelvin, e.g. 4000') },
    ({ value }) => callLight('/light/color_temp', 'POST', { value })
  );

  const FRONTEND = 'http://127.0.0.1:5050';
  async function callFrontend(path) {
    try {
      const r   = await fetch(FRONTEND + path);
      const txt = await r.text();
      return { content: [{ type: 'text', text: txt }] };
    } catch (e) {
      return { content: [{ type: 'text', text: 'Error: ' + e.message }] };
    }
  }
  function verifyCurrentHomeAction(toolName, toolInput) {
    if (!verifiedTurnLeasePath) {
      return { ok: false, decision: { lease_decision: 'LEASE_MISMATCH' }, result: { content: [{ type: 'text', text: 'UH-A0 LEASE_MISMATCH' }] } };
    }
    try {
      const raw = execFileSync(process.env.PYTHON || 'python3', ['-m', 'tools.execution_fence', 'verify-json'], {
        cwd: process.env.UH_A0_REPO_ROOT || '/opt/frontend',
        env: { ...process.env, UH_A0_TURN_LEASE_PATH: verifiedTurnLeasePath },
        input: JSON.stringify({ tool_name: toolName, tool_input: toolInput }),
        encoding: 'utf8', timeout: 3000,
      });
      const decision = JSON.parse(raw || '{}');
      if (decision.lease_decision === 'ALLOW') return { ok: true, decision };
      return { ok: false, decision, result: { content: [{ type: 'text', text: 'UH-A0 ' + (decision.lease_decision || 'LEASE_MISMATCH') }] } };
    } catch (_error) {
      return { ok: false, decision: { lease_decision: 'LEASE_MISMATCH' }, result: { content: [{ type: 'text', text: 'UH-A0 LEASE_MISMATCH' }] } };
    }
  }

  function writeDiaryDirect(content) {
    try {
      const raw = execFileSync(process.env.PYTHON || 'python3', ['-m', 'tools.diary_writer'], {
        cwd: process.env.UH_A0_REPO_ROOT || '/opt/frontend',
        env: process.env,
        input: JSON.stringify({ content }),
        encoding: 'utf8',
        timeout: 5000,
      });
      const result = JSON.parse(raw || '{}');
      if (result.status === 'CREATED') {
        return { content: [{ type: 'text', text: 'DIARY_CREATED' }] };
      }
      return { content: [{ type: 'text', text: String(result.status || 'DIARY_WRITE_FAILED') }] };
    } catch (_error) {
      return { content: [{ type: 'text', text: 'DIARY_WRITE_FAILED' }] };
    }
  }

  async function postFrontend(path, bodyObj, extraHeaders = {}) {
    try {
      const r   = await fetch(FRONTEND + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...extraHeaders },
        body: JSON.stringify(bodyObj),
      });
      const txt = await r.text();
      return { content: [{ type: 'text', text: txt }] };
    } catch (e) {
      return { content: [{ type: 'text', text: 'Error: ' + e.message }] };
    }
  }

  // ── Calendar: Ledger 记账本 ──────────────────────────────
  server.tool(
    'get_ledger',
    { month: z.string().optional().describe('YYYY-MM，默认当月') },
    ({ month }) => {
      const m = month || new Date().toISOString().slice(0, 7);
      return callFrontend('/api/ledger?month=' + m);
    }
  );
  server.tool(
    'add_ledger',
    {
      amount:   z.number().describe('正数=收入，负数=支出'),
      category: z.string().optional().describe('餐饮/购物/交通/娱乐/居家/其他'),
      note:     z.string().optional().describe('备注'),
      date:     z.string().optional().describe('YYYY-MM-DD，默认今天'),
    },
    async ({ amount, category, note, date }) => {
      const input = { amount };
      if (category !== undefined) input.category = category;
      if (note !== undefined) input.note = note;
      if (date !== undefined) input.date = date;
      const d = date || new Date().toISOString().slice(0, 10);
      return runGatedHomeWrite({
        uhA0Profile,
        toolName: 'mcp__home__add_ledger',
        toolInput: input,
        verify: verifyCurrentHomeAction,
        post: () => postFrontend('/api/ledger', {
          amount, category:category||'其他', note:note||null, date:d, author:'fyodor_api',
        }),
      });
    }
  );
  server.tool(
    'get_ledger_budget',
    { month: z.string().optional().describe('YYYY-MM，默认当月') },
    ({ month }) => {
      const m = month || new Date().toISOString().slice(0, 7);
      return callFrontend('/api/ledger/budget?month=' + m);
    }
  );

  // ── Calendar: To-Do List ─────────────────────────────────
  server.tool(
    'get_todos',
    {},
    () => callFrontend('/api/todos')
  );
  server.tool(
    'add_todo',
    {
      content:  z.string().describe('待办内容'),
      due_date: z.string().optional().describe('YYYY-MM-DD'),
    },
    async ({ content, due_date }) => {
      const input = { content };
      if (due_date !== undefined) input.due_date = due_date;
      return runGatedHomeWrite({
        uhA0Profile,
        toolName: 'mcp__home__add_todo',
        toolInput: input,
        verify: verifyCurrentHomeAction,
        post: () => postFrontend('/api/todos', {
          content, due_date:due_date||null, author:'fyodor_api',
        }),
      });
    }
  );

  // ── Calendar: Countdowns 倒计时 ──────────────────────────
  server.tool(
    'get_countdowns',
    {},
    () => callFrontend('/api/countdowns')
  );

  // ── Moments: 聊天收藏到朋友圈 ─────────────────────────────
  server.tool(
    'collect_chat_moment',
    {
      turn_key: z.string().optional().describe('本轮 turn_key；省略时自动绑定当前活跃轮次'),
      previous_turns: z.number().int().min(0).max(2).optional().describe('除当前轮外再向前包含几轮完整问答'),
      caption: z.string().max(500).optional().describe('转发卡片附言，可留空'),
    },
    ({ turn_key, previous_turns, caption }) => {
      const ownerToken = momentsOwnerToken();
      if (!ownerToken) {
        return {
          content: [{
            type: 'text',
            text: 'Error: MOMENTS_OWNER_TOKEN is not configured for home-mcp',
          }],
        };
      }
      return postFrontend('/api/moments/collect-intent', {
        turn_key: turn_key || null,
        conversation_id: 'hayana-chat',
        previous_turns: previous_turns ?? 0,
        caption: caption ?? '',
      }, { Authorization: `Bearer ${ownerToken}` });
    }
  );

  // ── 小红书（经哈娅电脑上的 OpenCLI 门卫，只读）────────────
  server.tool(
    'xhs_status',
    {},
    () => xhsStatus()
  );
  server.tool(
    'xhs_read',
    {
      command: z.enum(['feed', 'search', 'note', 'comments', 'user']).describe('只读命令：feed 首页推荐 / search 搜索 / note 笔记正文 / comments 评论 / user 用户主页'),
      query: z.string().optional().describe('search 的关键词（1-100 字，不能以 - 开头）'),
      url: z.string().optional().describe('note / comments 用的完整笔记链接，必须带 xsec_token（用 feed/search 返回的原始链接）'),
      user: z.string().optional().describe('user 用的 24 位用户 id 或主页链接'),
      limit: z.number().int().optional().describe('返回条数：feed/search 1-20，comments 1-50，user 1-30'),
      with_replies: z.boolean().optional().describe('comments 时是否带上楼中楼回复'),
    },
    (params) => xhsRead(params)
  );


  // ── 推特/X（费佳的自动化账号，经哈娅电脑上的 OpenCLI 门卫；读 + 受限发帖）────────────
  server.tool(
    'x_state',
    {},
    () => xState()
  );
  server.tool(
    'x_read',
    {
      command: z.enum(['timeline', 'search', 'thread', 'tweets', 'profile', 'trending']).describe('读取命令：timeline 时间线 / search 搜索 / thread 推文对话 / tweets 某用户推文 / profile 用户资料 / trending 热搜'),
      type: z.enum(['for-you', 'following']).optional().describe('timeline 的类型'),
      query: z.string().optional().describe('search 的关键词（1-200 字，不能以 - 开头）'),
      product: z.enum(['top', 'live', 'photos', 'videos']).optional().describe('search 的结果类型'),
      from: z.string().optional().describe('search 限定发推用户名'),
      url: z.string().optional().describe('thread 用的推文 id 或推文链接（x.com / twitter.com）'),
      user: z.string().optional().describe('tweets / profile 用的用户名，可带 @；不传表示自己的账号'),
      limit: z.number().int().optional().describe('返回条数：timeline/search/tweets/trending 1-20，thread 1-50'),
    },
    (params) => xCommand(params)
  );
  server.tool(
    'x_post',
    {
      command: z.enum(['post', 'reply']).describe('post 发推 / reply 回复'),
      text: z.string().describe('推文内容（加权长度 ≤280，中文/表情算 2；不能以 - 开头）'),
      url: z.string().optional().describe('reply 用的目标推文链接（https://x.com/<用户名>/status/<id>）'),
    },
    ({ command, text, url }) => runGatedHomeWrite({
      uhA0Profile,
      toolName: 'mcp__home__x_post',
      toolInput: { command, text, url },
      verify: verifyCurrentHomeAction,
      post: () => xCommand({ command, text, url }),
    })
  );

  return server;
}

const app = express();
app.use(express.json());

// Session-mode streamable-http（SDK 标准写法）。
// 旧的 stateless 模式对"不带 initialize 的请求"不是拒绝而是无限挂起——
// CC 等 MCP 客户端等不到响应就断 socket（"socket closed unexpectedly"），
// claude.ai 侧的频繁断线重连也是同一个病。
const sessions = {}; // sessionId -> { server, transport }

app.all('/mcp', async (req, res) => {
  try {
    const sid = req.headers['mcp-session-id'];
    if (sid && sessions[sid]) {
      await sessions[sid].transport.handleRequest(req, res, req.body);
      return;
    }
    const isInit = req.body && (
      req.body.method === 'initialize' ||
      (Array.isArray(req.body) && req.body.some(m => m && m.method === 'initialize'))
    );
    if (isInit) {
      const uhA0Profile = String(req.headers['x-uh-a0-profile'] || '').trim() === 'uh_a0';
      const turnLeasePath = req.headers['x-uh-a0-turn-lease-path'];
      const server = buildServer({ uhA0Profile, turnLeasePath });
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => randomUUID(),
        onsessioninitialized: (id) => { sessions[id] = { server, transport }; },
      });
      transport.onclose = () => {
        if (transport.sessionId && sessions[transport.sessionId]) {
          delete sessions[transport.sessionId];
          try { server.close(); } catch (e) {}
        }
      };
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
      return;
    }
    // 无 session 又不是 initialize：按规范回 404——客户端收到 404 才知道要重新 initialize
    // （400 会让客户端以为连接还健康，攥着死 session 无限重试）
    res.status(404).json({
      jsonrpc: '2.0',
      error: { code: -32001, message: 'Session not found — reinitialize' },
      id: (req.body && req.body.id) != null ? req.body.id : null,
    });
  } catch (err) {
    console.error('MCP request error:', err);
    if (!res.headersSent) {
      res.status(500).json({
        jsonrpc: '2.0',
        error: { code: -32603, message: 'Internal server error' },
        id: null,
      });
    }
  }
});

if (require.main === module) {
  app.listen(3100, '127.0.0.1', () => {
    console.log('MCP streamable-http server listening on 127.0.0.1:3100/mcp');
  });
  process.on('SIGTERM', () => process.exit(0));
  process.on('SIGINT',  () => process.exit(0));
}

module.exports = { buildServer, runGatedHomeWrite };
