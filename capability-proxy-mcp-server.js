'use strict';

const { McpServer } = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StdioServerTransport } = require('@modelcontextprotocol/sdk/server/stdio.js');
const { execFileSync } = require('child_process');
const { z } = require('zod');

const CAPABILITY_SERVER_NAME = 'capability';
const CAPABILITY_PROXY_TOOL_NAMES = Object.freeze([
  'memory_search',
  'memory_write',
  'todo_read',
  'todo_write',
  'ledger_read',
  'ledger_write',
]);

const ADAPTER_MODULES = Object.freeze({
  memory_search: 'tools.memory_internal_adapter',
  memory_write: 'tools.memory_write_adapter',
  todo_read: 'tools.todo_internal_adapter',
  todo_write: 'tools.todo_internal_adapter',
  ledger_read: 'tools.ledger_internal_adapter',
  ledger_write: 'tools.ledger_internal_adapter',
});

function adapterCommand() {
  return {
    python: process.env.PYTHON || 'python3',
    cwd: process.env.UH_A0_REPO_ROOT || process.cwd(),
  };
}

function verifyCapabilityAction(toolName, toolInput) {
  const command = adapterCommand();
  const physicalToolName = 'mcp__capability__' + toolName;
  try {
    const output = execFileSync(command.python, ['-m', 'tools.execution_fence', 'verify-json'], {
      cwd: command.cwd,
      env: process.env,
      input: JSON.stringify({
        tool_name: physicalToolName,
        tool_input: toolInput,
      }),
      encoding: 'utf8',
      timeout: 3000,
    });
    return JSON.parse(output || '{}');
  } catch (_error) {
    return { lease_decision: 'LEASE_MISMATCH' };
  }
}

function callAdapter(toolName, input) {
  const moduleName = ADAPTER_MODULES[toolName];
  if (!moduleName) {
    throw new Error('unknown capability proxy tool');
  }
  const dbPath = String(process.env.TODO_INTERNAL_DB_PATH || '').trim();
  if (!dbPath) {
    throw new Error('TODO_INTERNAL_DB_PATH is required');
  }
  const command = adapterCommand();
  const payload = {
    operation: toolName === 'memory_search'
      ? 'search_memories'
      : toolName === 'memory_write'
        ? 'write_memory'
        : toolName === 'todo_read'
          ? 'get_todos'
          : toolName === 'todo_write'
            ? 'add_todo'
            : toolName === 'ledger_read'
              ? 'get_ledger'
              : 'add_ledger',
    ...input,
    db_path: dbPath,
  };
  const output = execFileSync(command.python, ['-m', moduleName], {
    cwd: command.cwd,
    env: { ...process.env, TODO_INTERNAL_DB_PATH: dbPath },
    input: JSON.stringify(payload),
    encoding: 'utf8',
    timeout: 5000,
  });
  return JSON.parse(output || '{}');
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
  return JSON.stringify(result);
}

function gateFailure(decision) {
  const name = decision && decision.lease_decision
    ? decision.lease_decision
    : 'LEASE_MISMATCH';
  return { content: [{ type: 'text', text: 'UH-A0 ' + name }] };
}

function runProxy(toolName, input) {
  const decision = verifyCapabilityAction(toolName, input);
  if (!decision || decision.lease_decision !== 'ALLOW') {
    return gateFailure(decision);
  }
  try {
    return {
      content: [{
        type: 'text',
        text: resultText(toolName, callAdapter(toolName, input)),
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

function buildServer() {
  const server = new McpServer({
    name: CAPABILITY_SERVER_NAME,
    version: '1.0.0',
  });

  server.tool(
    'memory_search',
    { keyword: z.string().describe('搜索关键词') },
    async ({ keyword }) => runProxy('memory_search', { keyword }),
  );
  server.tool(
    'memory_write',
    { content: z.string().min(1).max(4000).describe('要保存的长期记忆正文') },
    async ({ content }) => runProxy('memory_write', { content }),
  );
  server.tool(
    'todo_read',
    {},
    async () => runProxy('todo_read', {}),
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
    }),
  );
  server.tool(
    'ledger_read',
    { month: z.string().optional().describe('YYYY-MM，默认当月') },
    async ({ month }) => runProxy('ledger_read', {
      month: month ?? null,
    }),
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
    }),
  );

  return server;
}

async function main() {
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
  buildServer,
  callAdapter,
  verifyCapabilityAction,
};
