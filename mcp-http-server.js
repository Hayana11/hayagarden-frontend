'use strict';

const express                            = require('express');
const { McpServer }                      = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StreamableHTTPServerTransport }  = require('@modelcontextprotocol/sdk/server/streamableHttp.js');
const { execSync }                       = require('child_process');
const { randomUUID }                     = require('crypto');
const { z }                              = require('zod');

function buildServer() {
  const server = new McpServer({ name: 'home-mcp', version: '1.0.0' });

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

  server.tool('light_on',  {}, () => callLight('/light/on',  'POST'));
  server.tool('light_off', {}, () => callLight('/light/off', 'POST'));
  server.tool('get_light_status', {}, () => callLight('/light/status', 'GET'));
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
  async function postFrontend(path, bodyObj) {
    try {
      const r   = await fetch(FRONTEND + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
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
    ({ amount, category, note, date }) => {
      const d = date || new Date().toISOString().slice(0, 10);
      return postFrontend('/api/ledger', { amount, category: category||'其他', note: note||null, date: d, author: 'fyodor_api' });
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
    ({ content, due_date }) =>
      postFrontend('/api/todos', { content, due_date: due_date||null, author: 'fyodor_api' })
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
    ({ turn_key, previous_turns, caption }) =>
      postFrontend('/api/moments/collect-intent', {
        turn_key: turn_key || null,
        conversation_id: 'hayana-chat',
        previous_turns: previous_turns ?? 0,
        caption: caption ?? '',
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
      const server = buildServer();
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

app.listen(3100, '127.0.0.1', () => {
  console.log('MCP streamable-http server listening on 127.0.0.1:3100/mcp');
});

process.on('SIGTERM', () => process.exit(0));
process.on('SIGINT',  () => process.exit(0));
