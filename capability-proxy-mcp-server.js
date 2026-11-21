'use strict';

const os = require('os');
const path = require('path');
const { McpServer } = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StdioServerTransport } = require('@modelcontextprotocol/sdk/server/stdio.js');
const { execFileSync, spawn } = require('child_process');
const { timingSafeEqual } = require('crypto');
const { z } = require('zod');
const { readLightStatus } = require('./light-status-adapter');

const CAPABILITY_SERVER_NAME = 'capability';
const CAPABILITY_PROXY_TOOL_NAMES = Object.freeze([
  'memory_search',
  'memory_write',
  'diary_write',
  'task_timer_start',
  'home_light_status',
  'todo_read',
  'todo_write',
  'ledger_read',
  'ledger_budget_read',
  'ledger_write',
  'gallery_save',
  'gallery_recall',
  'gallery_screenshot',
]);

const ADAPTER_MODULES = Object.freeze({
  memory_search: 'tools.memory_internal_adapter',
  memory_write: 'tools.memory_write_adapter',
  diary_write: 'tools.diary_capability_adapter',
  task_timer_start: 'tools.task_timer_capability_adapter',
  todo_read: 'tools.todo_internal_adapter',
  todo_write: 'tools.todo_internal_adapter',
  ledger_read: 'tools.ledger_internal_adapter',
  ledger_budget_read: 'tools.ledger_internal_adapter',
  ledger_write: 'tools.ledger_internal_adapter',
  gallery_save: 'tools.gallery_capability_adapter',
  gallery_recall: 'tools.gallery_capability_adapter',
  gallery_screenshot: 'tools.gallery_capability_adapter',
});

const DEFAULT_ADAPTER_TIMEOUT_MS = 5000;
const GALLERY_ADAPTER_TIMEOUTS_MS = Object.freeze({
  gallery_save: 50000,
  gallery_recall: 25000,
  gallery_screenshot: 50000,
});

function adapterTimeoutMs(toolName) {
  const timeout = GALLERY_ADAPTER_TIMEOUTS_MS[toolName] ?? DEFAULT_ADAPTER_TIMEOUT_MS;
  if (!Number.isFinite(timeout) || timeout <= 0) {
    throw new Error('invalid capability adapter timeout');
  }
  return timeout;
}

// Resident HTTP mode serves every Claude process; each request names its
// resident's turn lease file by header. Only files under the shared lease
// directory are accepted (cc_resident._new_uh_a0_turn_lease_path).
const TURN_LEASE_HEADER = 'x-uh-a0-turn-lease-path';
// Loopback is reachable by every local user (including the workspace exec
// sandbox), and lease paths are not secret, so the service also requires a
// bearer token that only root-readable configuration holds.
const SERVICE_TOKEN_ENV = 'UH_A0_CAPABILITY_MCP_TOKEN';
const MIN_SERVICE_TOKEN_LENGTH = 32;
const TURN_LEASE_FILENAME = /^[0-9a-f]{32}\.json$/;

function turnLeaseDir(env = process.env) {
  const configured = String(env.UH_A0_TURN_LEASE_DIR || '').trim();
  return path.resolve(configured || path.join(os.tmpdir(), 'hayagarden-uh-a0-turn-leases'));
}

function acceptedTurnLeasePath(raw, env = process.env) {
  const value = typeof raw === 'string' ? raw.trim() : '';
  if (!value || !path.isAbsolute(value) || value !== path.normalize(value)) return null;
  if (path.dirname(value) !== turnLeaseDir(env)) return null;
  if (!TURN_LEASE_FILENAME.test(path.basename(value))) return null;
  return value;
}

function serviceToken(env = process.env) {
  const token = String(env[SERVICE_TOKEN_ENV] || '').trim();
  return token.length >= MIN_SERVICE_TOKEN_LENGTH ? token : '';
}

function bearerMatches(header, token) {
  const match = /^Bearer (.+)$/.exec(typeof header === 'string' ? header.trim() : '');
  if (!match || !token) return false;
  const given = Buffer.from(match[1]);
  const expected = Buffer.from(token);
  return given.length === expected.length && timingSafeEqual(given, expected);
}

function adapterCommand(env = process.env) {
  return {
    python: env.PYTHON || 'python3',
    cwd: env.UH_A0_REPO_ROOT || process.cwd(),
  };
}

function verifyInvocation(toolName, toolInput, env) {
  const command = adapterCommand(env);
  return {
    file: command.python,
    args: ['-m', 'tools.execution_fence', 'verify-json'],
    options: {
      cwd: command.cwd,
      env,
      input: JSON.stringify({
        tool_name: 'mcp__capability__' + toolName,
        tool_input: toolInput,
      }),
      encoding: 'utf8',
      timeout: 3000,
    },
  };
}

function verifyCapabilityAction(toolName, toolInput, env = process.env) {
  const call = verifyInvocation(toolName, toolInput, env);
  try {
    return JSON.parse(execFileSync(call.file, call.args, call.options) || '{}');
  } catch (_error) {
    return { lease_decision: 'LEASE_MISMATCH' };
  }
}

// Async twin of execFileSync for the tool path: a shared resident server must
// not block every other resident while one adapter runs (gallery: up to 50s).
function execFileAsync(file, args, { cwd, env, input, timeout }) {
  return new Promise((resolve, reject) => {
    const child = spawn(file, args, { cwd, env, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '';
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error); else resolve(value);
    };
    const timer = setTimeout(() => {
      child.kill('SIGTERM');
      finish(new Error('adapter timed out (ETIMEDOUT)'));
    }, timeout);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.pipe(process.stderr, { end: false });
    child.on('error', (error) => finish(error));
    child.on('close', (code) => {
      if (code === 0) finish(null, stdout);
      else finish(new Error('adapter exited with status ' + code));
    });
    child.stdin.on('error', () => {});
    child.stdin.end(input);
  });
}

async function verifyCapabilityActionAsync(toolName, toolInput, env = process.env) {
  const call = verifyInvocation(toolName, toolInput, env);
  try {
    return JSON.parse((await execFileAsync(call.file, call.args, call.options)) || '{}');
  } catch (_error) {
    return { lease_decision: 'LEASE_MISMATCH' };
  }
}

function adapterInvocation(toolName, input, verifiedTurnId, env) {
  const moduleName = ADAPTER_MODULES[toolName];
  if (!moduleName) {
    throw new Error('unknown capability proxy tool');
  }
  const isGallery = toolName.startsWith('gallery_');
  const dbEnvName = isGallery
    ? 'HAYA_DB_PATH'
    : toolName === 'task_timer_start'
    ? 'TASK_TIMER_COMMANDS_DB_PATH'
    : 'TODO_INTERNAL_DB_PATH';
  const dbPath = String(env[dbEnvName] || '').trim();
  if (!dbPath) {
    throw new Error(dbEnvName + ' is required');
  }
  const command = adapterCommand(env);
  const payload = isGallery ? {
    operation: toolName,
    tool_input: input,
    verified_turn_id: verifiedTurnId,
    db_path: dbPath,
  } : {
    operation: toolName === 'memory_search'
      ? 'search_memories'
      : toolName === 'memory_write'
        ? 'write_memory'
        : toolName === 'diary_write'
          ? 'write_diary'
          : toolName === 'task_timer_start'
            ? 'start_task_timer'
            : toolName === 'todo_read'
            ? 'get_todos'
            : toolName === 'todo_write'
              ? 'add_todo'
              : toolName === 'ledger_read'
                ? 'get_ledger'
                : toolName === 'ledger_budget_read'
                  ? 'get_ledger_budget'
                  : 'add_ledger',
    ...input,
    db_path: dbPath,
  };
  if (toolName === 'memory_search' || toolName === 'memory_write') {
    payload.shadow_surface = 'claude_code';
    if (verifiedTurnId) payload.shadow_turn_id = verifiedTurnId;
  }
  return {
    file: command.python,
    args: ['-m', moduleName],
    options: {
      cwd: command.cwd,
      env: {
        ...env,
        TODO_INTERNAL_DB_PATH: dbPath,
        HAYA_DB_PATH: isGallery ? dbPath : env.HAYA_DB_PATH,
      },
      input: JSON.stringify(payload),
      encoding: 'utf8',
      timeout: adapterTimeoutMs(toolName),
    },
  };
}

function callAdapter(toolName, input, verifiedTurnId = null, env = process.env) {
  const call = adapterInvocation(toolName, input, verifiedTurnId, env);
  return JSON.parse(execFileSync(call.file, call.args, call.options) || '{}');
}

async function callAdapterAsync(toolName, input, verifiedTurnId = null, env = process.env) {
  const call = adapterInvocation(toolName, input, verifiedTurnId, env);
  return JSON.parse((await execFileAsync(call.file, call.args, call.options)) || '{}');
}

function formatMemorySearch(result) {
  const items = (Array.isArray(result.posts) ? result.posts : []).map((post) => {
    const id = post?.id ?? '';
    const type = post?.type ?? '';
    const date = String(post?.created_at || '').slice(0, 10);
    const pinned = post?.pinned ? ' 📌' : '';
    const content = String(post?.content || '').slice(0, 220);
    return `[#${id} ${type} ${date}${pinned}] ${content}`;
  });
  return items.join('\n---\n') || '没有找到相关记忆';
}

function resultText(toolName, result) {
  if (toolName === 'memory_search') {
    return formatMemorySearch(result);
  }
  if (toolName === 'memory_write') {
    return String(result.status || 'MEMORY_WRITE_FAILED');
  }
  if (toolName === 'task_timer_start') {
    return result.status === 'CREATED'
      ? 'TASK_TIMER_CREATED'
      : String(result.status || 'TASK_TIMER_FAILED');
  }
  if (toolName === 'diary_write') {
    return result.status === 'CREATED'
      ? 'DIARY_CREATED'
      : String(result.status || 'DIARY_WRITE_FAILED');
  }
  return JSON.stringify(result);
}

function gateFailure(decision) {
  const name = decision && decision.lease_decision
    ? decision.lease_decision
    : 'LEASE_MISMATCH';
  return { content: [{ type: 'text', text: 'UH-A0 ' + name }] };
}

async function runProxy(toolName, input, env = process.env) {
  const decision = await verifyCapabilityActionAsync(toolName, input, env);
  if (!decision || decision.lease_decision !== 'ALLOW') {
    return gateFailure(decision);
  }
  try {
    return {
      content: [{
        type: 'text',
        text: resultText(toolName, await callAdapterAsync(
          toolName,
          input,
          decision.verified_turn_id ?? null,
          env,
        )),
      }],
    };
  } catch (error) {
    return {
      content: [{
        type: 'text',
        text: 'CAPABILITY_PROXY_FAILED: ' + error.message,
      }],
    };
  }
}

async function runLightStatusProxy(input = {}, env = process.env) {
  const decision = await verifyCapabilityActionAsync('home_light_status', input, env);
  if (!decision || decision.lease_decision !== 'ALLOW') {
    return gateFailure(decision);
  }
  return {
    content: [{
      type: 'text',
      text: await readLightStatus(),
    }],
  };
}

function buildServer({ env = process.env } = {}) {
  const server = new McpServer({
    name: CAPABILITY_SERVER_NAME,
    version: '1.0.0',
  });

  server.tool(
    'memory_search',
    { keyword: z.string().describe('搜索关键词') },
    async ({ keyword }) => runProxy('memory_search', { keyword }, env),
  );
  server.tool(
    'memory_write',
    { content: z.string().min(1).max(4000).describe('要保存的长期记忆正文') },
    async ({ content }) => runProxy('memory_write', { content }, env),
  );
  server.tool(
    'diary_write',
    { content: z.string().describe('要保存的日记正文') },
    async ({ content }) => runProxy('diary_write', { content }, env),
  );
  server.tool(
    'task_timer_start',
    {
      title: z.string().describe('行动任务标题'),
      countdown_seconds: z.number().int().optional().describe('倒计时秒数；省略或 0 表示正计时'),
    },
    async ({ title, countdown_seconds }) => runProxy('task_timer_start', {
      title,
      countdown_seconds: countdown_seconds ?? null,
    }, env),
  );
  server.tool(
    'home_light_status',
    {},
    async () => runLightStatusProxy({}, env),
  );
  server.tool(
    'todo_read',
    {},
    async () => runProxy('todo_read', {}, env),
  );
  server.tool(
    'todo_write',
    {
      content: z.string().describe('待办内容'),
      due_date: z.string().optional().describe('YYYY-MM-DD'),
    },
    async ({ content, due_date }) => runProxy('todo_write', {
      content,
      due_date: due_date ?? null,
    }, env),
  );
  server.tool(
    'ledger_read',
    { month: z.string().optional().describe('YYYY-MM，默认当月') },
    async ({ month }) => runProxy('ledger_read', {
      month: month ?? null,
    }, env),
  );
  server.tool(
    'ledger_budget_read',
    { month: z.string().optional().describe('YYYY-MM，默认当月') },
    async ({ month }) => runProxy('ledger_budget_read', {
      month: month ?? null,
    }, env),
  );

  server.tool(
    'ledger_write',
    {
      amount: z.number().describe('正数=收入，负数=支出'),
      category: z.string().optional().describe('餐饮/购物/交通/娱乐/居家/其他'),
      note: z.string().optional().describe('备注'),
      date: z.string().optional().describe('YYYY-MM-DD，默认今天'),
    },
    async ({ amount, category, note, date }) => runProxy('ledger_write', {
      amount,
      category: category ?? null,
      note: note ?? null,
      date: date ?? null,
    }, env),
  );

  server.tool(
    'gallery_save',
    {
      attachment: z.string().optional(),
      image_index: z.number().int().min(0).max(3).optional(),
      note: z.string().optional(),
      album: z.string().optional(),
      first_impression: z.string().max(800).optional(),
    },
    async ({ attachment, image_index, note, album, first_impression }) => runProxy(
      'gallery_save',
      {
        attachment: attachment ?? null,
        image_index: image_index ?? null,
        note: note ?? null,
        album: album ?? null,
        first_impression: first_impression ?? null,
      },
      env,
    ),
  );
  server.tool(
    'gallery_recall',
    {
      keyword: z.string().optional(),
      emotion: z.string().optional(),
      pid: z.string().optional(),
      inspect_question: z.string().max(500).optional(),
    },
    async ({ keyword, emotion, pid, inspect_question }) => runProxy(
      'gallery_recall',
      {
        keyword: keyword ?? null,
        emotion: emotion ?? null,
        pid: pid ?? null,
        inspect_question: inspect_question ?? null,
      },
      env,
    ),
  );
  server.tool(
    'gallery_screenshot',
    { viewpoint: z.enum(['fyodor', 'hayana']).optional() },
    async ({ viewpoint }) => runProxy('gallery_screenshot', {
      viewpoint: viewpoint ?? 'fyodor',
    }, env),
  );

  return server;
}

function createApp({ env = process.env } = {}) {
  const token = serviceToken(env);
  if (!token) {
    throw new Error(SERVICE_TOKEN_ENV + ' must be set (at least ' + MIN_SERVICE_TOKEN_LENGTH + ' characters)');
  }
  // Loaded lazily so the stdio path does not pay for express at every spawn.
  const express = require('express');
  const { StreamableHTTPServerTransport } = require('@modelcontextprotocol/sdk/server/streamableHttp.js');
  const app = express();
  app.use(express.json({ limit: '1mb' }));

  function rpcError(res, status, code, message, body) {
    res.status(status).json({
      jsonrpc: '2.0',
      error: { code, message },
      id: body && body.id != null ? body.id : null,
    });
  }

  // Stateless: every request carries its resident's lease header and gets a
  // fresh server bound to it, so a service restart cannot strand a session
  // and no request can borrow another resident's lease.
  app.post('/mcp', async (req, res) => {
    if (!bearerMatches(req.headers.authorization, token)) {
      rpcError(res, 401, -32001, 'Unauthorized', req.body);
      return;
    }
    const leasePath = acceptedTurnLeasePath(req.headers[TURN_LEASE_HEADER], env);
    if (!leasePath) {
      rpcError(res, 403, -32003, 'UH-A0 turn lease path rejected', req.body);
      return;
    }
    const server = buildServer({ env: { ...env, UH_A0_TURN_LEASE_PATH: leasePath } });
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    res.on('close', () => {
      transport.close().catch(() => {});
      server.close().catch(() => {});
    });
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
    } catch (_error) {
      if (!res.headersSent) rpcError(res, 500, -32603, 'Internal server error', null);
    }
  });
  app.all('/mcp', (req, res) => {
    if (!bearerMatches(req.headers.authorization, token)) {
      rpcError(res, 401, -32001, 'Unauthorized', null);
      return;
    }
    rpcError(res, 405, -32000, 'Method not allowed', null);
  });
  return app;
}

function startCapabilityMcpServer({ host = '127.0.0.1', port = 0, ...options } = {}) {
  const app = createApp(options);
  return new Promise((resolve) => {
    const listener = app.listen(port, host, () => resolve({ app, listener, port: listener.address().port }));
  });
}

async function main() {
  const port = Number(process.env.CAPABILITY_MCP_PORT || 0);
  if (port > 0) {
    await startCapabilityMcpServer({ port });
    console.log(`Capability MCP server listening on 127.0.0.1:${port}/mcp`);
    return;
  }
  const transport = new StdioServerTransport();
  await buildServer().connect(transport);
}

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write('CAPABILITY_PROXY_SERVER_FAILED: ' + error.message + '\n');
    process.exitCode = 1;
  });
}

module.exports = {
  ADAPTER_MODULES,
  CAPABILITY_PROXY_TOOL_NAMES,
  SERVICE_TOKEN_ENV,
  TURN_LEASE_HEADER,
  acceptedTurnLeasePath,
  adapterTimeoutMs,
  buildServer,
  callAdapter,
  callAdapterAsync,
  createApp,
  runLightStatusProxy,
  startCapabilityMcpServer,
  turnLeaseDir,
  verifyCapabilityAction,
  verifyCapabilityActionAsync,
};
