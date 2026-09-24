'use strict';

const express = require('express');
const { McpServer } = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StreamableHTTPServerTransport } = require('@modelcontextprotocol/sdk/server/streamableHttp.js');
const { execFileSync } = require('child_process');
const { randomUUID } = require('crypto');
const { z } = require('zod');
const { createHealthCapabilities } = require('./tools/xiaomi_health/capability');

const INTERNAL_SERVER_NAME = 'internal-mcp';
const INTERNAL_TOOL_NAMES = Object.freeze([
  'get_todos',
  'add_todo',
  'get_ledger',
  'get_ledger_budget',
  'add_ledger',
  'search_memories',
  'write_memory',
  'get.health',
]);
const HEALTH_OPERATIONS = new Set(['get_health']);

function adapterCommand({ python = process.env.PYTHON || 'python3', cwd = process.env.UH_A0_REPO_ROOT || process.cwd() } = {}) {
  return { python, cwd };
}

function adapterModuleFor(operation) {
  if (operation === 'get_todos' || operation === 'add_todo') {
    return 'tools.todo_internal_adapter';
  }
  if (operation === 'get_ledger' || operation === 'get_ledger_budget' || operation === 'add_ledger') {
    return 'tools.ledger_internal_adapter';
  }
  if (operation === 'search_memories') {
    return 'tools.memory_internal_adapter';
  }
  if (operation === 'write_memory') {
    return 'tools.memory_write_adapter';
  }
  if (HEALTH_OPERATIONS.has(operation)) {
    return 'tools.xiaomi_health.internal_adapter';
  }
  throw new Error('unknown Internal MCP operation');
}

function callInternalAdapter(operation, input, { dbPath, python, cwd } = {}) {
  const command = adapterCommand({ python, cwd });
  const payload = { operation, ...input };
  if (dbPath) payload.db_path = dbPath;
  const output = execFileSync(command.python, ['-m', adapterModuleFor(operation)], {
    cwd: command.cwd,
    env: { ...process.env, ...(dbPath ? { TODO_INTERNAL_DB_PATH: dbPath } : {}) },
    input: JSON.stringify(payload),
    encoding: 'utf8',
    timeout: HEALTH_OPERATIONS.has(operation)
      ? (input.metric === 'all' ? 40_000 : 17_000)
      : 5000,
  });
  return JSON.parse(output || '{}');
}

function gateFailure(decision) {
  const name = decision && decision.lease_decision ? decision.lease_decision : 'LEASE_MISMATCH';
  return { content: [{ type: 'text', text: 'UH-A0 ' + name }] };
}

function verifyCurrentInternalAction(toolName, toolInput, {
  python = process.env.PYTHON || 'python3',
  cwd = process.env.UH_A0_REPO_ROOT || process.cwd(),
} = {}) {
  try {
    const raw = execFileSync(python, ['-m', 'tools.execution_fence', 'verify-json'], {
      cwd,
      env: process.env,
      input: JSON.stringify({ tool_name: toolName, tool_input: toolInput }),
      encoding: 'utf8',
      timeout: 3000,
    });
    return JSON.parse(raw || '{}');
  } catch (_error) {
    return { lease_decision: 'LEASE_MISMATCH' };
  }
}

function formatMemorySearch(posts) {
  const items = (Array.isArray(posts) ? posts : []).map((post) => {
    const id = post?.id ?? '';
    const type = post?.type ?? '';
    const date = String(post?.created_at || '').slice(0, 10);
    const pinned = post?.pinned ? ' 📌' : '';
    const content = String(post?.content || '').slice(0, 220);
    return `[#${id} ${type} ${date}${pinned}] ${content}`;
  });
  return items.join('\n---\n') || '没有找到相关记忆';
}

function buildServer({ dbPath, verify = verifyCurrentInternalAction, python, cwd, uhA0Profile = false, healthAdapter = null } = {}) {
  const server = new McpServer({ name: INTERNAL_SERVER_NAME, version: '1.0.0' });
  const health = createHealthCapabilities({
    runAdapter: (operation, input) => healthAdapter
      ? healthAdapter(operation, input)
      : callInternalAdapter(operation, input, { dbPath, python, cwd }),
  });

  server.tool(
    'get_todos',
    {},
    async () => ({
      content: [{
        type: 'text',
        text: JSON.stringify(callInternalAdapter('get_todos', {}, { dbPath, python, cwd })),
      }],
    }),
  );

  server.tool(
    'add_todo',
    {
      content: z.string().describe('待办内容'),
      due_date: z.string().optional().describe('YYYY-MM-DD'),
    },
    async ({ content, due_date }) => {
      const toolInput = { content };
      if (due_date !== undefined) toolInput.due_date = due_date;
      if (!uhA0Profile) {
        return gateFailure({ lease_decision: 'PROFILE_REQUIRED' });
      }
      const decision = await verify('mcp__internal__add_todo', toolInput);
      if (!decision || decision.lease_decision !== 'ALLOW') {
        return gateFailure(decision);
      }
      try {
        const result = callInternalAdapter(
          'add_todo',
          { content, due_date: due_date ?? null },
          { dbPath, python, cwd },
        );
        return { content: [{ type: 'text', text: JSON.stringify(result) }] };
      } catch (_error) {
        return { content: [{ type: 'text', text: 'INTERNAL_TODO_WRITE_FAILED' }] };
      }
    },
  );

  server.tool(
    'get_ledger',
    { month: z.string().optional().describe('YYYY-MM，默认当月') },
    async ({ month }) => {
      const resolvedMonth = month || new Date().toISOString().slice(0, 7);
      const result = callInternalAdapter(
        'get_ledger',
        { month: resolvedMonth },
        { dbPath, python, cwd },
      );
      return { content: [{ type: 'text', text: JSON.stringify(result) }] };
    },
  );

  server.tool(
    'get_ledger_budget',
    { month: z.string().optional().describe('YYYY-MM，默认当月') },
    async ({ month }) => {
      const resolvedMonth = month || new Date().toISOString().slice(0, 7);
      const result = callInternalAdapter(
        'get_ledger_budget',
        { month: resolvedMonth },
        { dbPath, python, cwd },
      );
      return { content: [{ type: 'text', text: JSON.stringify(result) }] };
    },
  );

  server.tool(
    'add_ledger',
    {
      amount: z.number().describe('正数=收入，负数=支出'),
      category: z.string().optional().describe('餐饮/购物/交通/娱乐/居家/其他'),
      note: z.string().optional().describe('备注'),
      date: z.string().optional().describe('YYYY-MM-DD，默认今天'),
    },
    async ({ amount, category, note, date }) => {
      const toolInput = { amount };
      if (category !== undefined) toolInput.category = category;
      if (note !== undefined) toolInput.note = note;
      if (date !== undefined) toolInput.date = date;
      if (!uhA0Profile) {
        return gateFailure({ lease_decision: 'PROFILE_REQUIRED' });
      }
      const decision = await verify('mcp__internal__add_ledger', toolInput);
      if (!decision || decision.lease_decision !== 'ALLOW') {
        return gateFailure(decision);
      }
      try {
        const result = callInternalAdapter(
          'add_ledger',
          {
            amount,
            category: category || '其他',
            note: note || null,
            date: date || new Date().toISOString().slice(0, 10),
            author: 'fyodor_api',
          },
          { dbPath, python, cwd },
        );
        return { content: [{ type: 'text', text: JSON.stringify(result) }] };
      } catch (_error) {
        return { content: [{ type: 'text', text: 'INTERNAL_LEDGER_WRITE_FAILED' }] };
      }
    },
  );

  server.tool(
    'search_memories',
    {
      keyword: z.string().describe('搜索关键词'),
    },
    async ({ keyword }) => {
      try {
        const result = callInternalAdapter(
          'search_memories',
          { keyword, limit: 8 },
          { dbPath, python, cwd },
        );
        return {
          content: [{ type: 'text', text: formatMemorySearch(result.posts) }],
        };
      } catch (error) {
        return {
          content: [{ type: 'text', text: 'Error: ' + error.message }],
        };
      }
    },
  );

  server.tool(
    'write_memory',
    {
      content: z.string().min(1).max(4000).describe('要保存的长期记忆正文'),
    },
    async ({ content }) => {
      if (!uhA0Profile) {
        return gateFailure({ lease_decision: 'PROFILE_REQUIRED' });
      }
      const toolInput = { content };
      const decision = await verify('mcp__internal__write_memory', toolInput);
      if (!decision || decision.lease_decision !== 'ALLOW') {
        return gateFailure(decision);
      }
      try {
        const result = callInternalAdapter(
          'write_memory',
          toolInput,
          { dbPath, python, cwd },
        );
        if (result.status === 'CREATED') {
          return { content: [{ type: 'text', text: 'MEMORY_CREATED' }] };
        }
        return { content: [{ type: 'text', text: String(result.status || 'MEMORY_WRITE_FAILED') }] };
      } catch (_error) {
        return { content: [{ type: 'text', text: 'MEMORY_WRITE_FAILED' }] };
      }
    },
  );

  server.tool(
    'get.health',
    {
      metric: z.enum(['all', 'status', 'steps', 'sleep', 'heart_rate']).default('all').describe('健康指标，默认 all'),
      days: z.number().int().min(1).max(30).default(7).describe('读取最近 1 到 30 天，默认 7 天'),
    },
    async ({ metric, days }) => ({
      content: [{ type: 'text', text: JSON.stringify(await health.get({ metric, days })) }],
    }),
  );

  return server;
}

function createApp(options = {}) {
  const app = express();
  app.use(express.json());
  const sessions = {};
  // Production resolves the existing service DB boundary once; adapters receive
  // the resolved path explicitly. Tests may pass an isolated dbPath directly.
  const dbPath = options.dbPath || process.env.TODO_INTERNAL_DB_PATH;

  app.all('/mcp', async (req, res) => {
    try {
      const sid = req.headers['mcp-session-id'];
      if (sid && sessions[sid]) {
        await sessions[sid].transport.handleRequest(req, res, req.body);
        return;
      }
      const isInit = req.body && (
        req.body.method === 'initialize'
        || (Array.isArray(req.body) && req.body.some((message) => message && message.method === 'initialize'))
      );
      if (!isInit) {
        res.status(404).json({
          jsonrpc: '2.0',
          error: { code: -32001, message: 'Session not found — reinitialize' },
          id: req.body && req.body.id != null ? req.body.id : null,
        });
        return;
      }

      const uhA0Profile = String(req.headers['x-uh-a0-profile'] || '').trim() === 'uh_a0';
      const server = buildServer({ ...options, dbPath, uhA0Profile });
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => randomUUID(),
        onsessioninitialized: (id) => { sessions[id] = { server, transport }; },
      });
      transport.onclose = () => {
        if (transport.sessionId && sessions[transport.sessionId]) {
          delete sessions[transport.sessionId];
          try { server.close(); } catch (_error) {}
        }
      };
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
    } catch (error) {
      if (!res.headersSent) {
        res.status(500).json({
          jsonrpc: '2.0',
          error: { code: -32603, message: 'Internal server error' },
          id: null,
        });
      }
    }
  });
  return app;
}

function startInternalMcpServer({ host = '127.0.0.1', port = 0, ...options } = {}) {
  const app = createApp(options);
  return new Promise((resolve) => {
    const listener = app.listen(port, host, () => resolve({ app, listener, port: listener.address().port }));
  });
}

if (require.main === module) {
  const port = Number(process.env.INTERNAL_MCP_PORT || 0);
  startInternalMcpServer({ port }).then(({ port: actualPort }) => {
    console.log(`Internal MCP server listening on 127.0.0.1:${actualPort}/mcp`);
  });
}

module.exports = {
  INTERNAL_SERVER_NAME,
  INTERNAL_TOOL_NAMES,
  buildServer,
  createApp,
  startInternalMcpServer,
  verifyCurrentInternalAction,
};
