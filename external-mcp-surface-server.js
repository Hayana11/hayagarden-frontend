const { execFileSync } = require('child_process');
const {
  Server,
} = require('@modelcontextprotocol/sdk/server/index.js');
const {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} = require('@modelcontextprotocol/sdk/types.js');
const { StdioServerTransport } = require('@modelcontextprotocol/sdk/server/stdio.js');

const SERVER_NAME = 'external';
const PYTHON_MAX_BUFFER = 2 * 1024 * 1024;
const OUTPUT_MAX_BYTES = 256 * 1024;

function repoRoot() {
  return process.env.UH_A0_REPO_ROOT || process.cwd();
}

function runPython(request) {
  const command = process.env.UH_A0_PYTHON_COMMAND || 'python3';
  const output = execFileSync(
    command,
    ['-m', 'tools.external_mcp_surface'],
    {
      cwd: repoRoot(),
      input: JSON.stringify(request),
      encoding: 'utf8',
      maxBuffer: PYTHON_MAX_BUFFER,
      windowsHide: true,
    },
  );
  if (Buffer.byteLength(output, 'utf8') > OUTPUT_MAX_BYTES) {
    throw new Error('surface adapter output exceeded the byte limit');
  }
  const parsed = JSON.parse(output);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('surface adapter returned an invalid response');
  }
  return parsed;
}

function safeFailure(code = 'SURFACE_ADAPTER_FAILED') {
  return {
    status: 'FAILED_PRE_CALL',
    error: {
      code: String(code).replace(/[^A-Z0-9_]/g, '_').slice(0, 64) || 'SURFACE_ADAPTER_FAILED',
      summary: 'External MCP surface adapter failed.',
    },
  };
}

function callSurface(name, args, runner = runPython) {
  try {
    const result = runner({ operation: 'call', name, arguments: args || {} });
    if (result.status === 'SUCCESS') {
      return { status: 'SUCCESS', result: result.result };
    }
    if (result.status === 'TOOL_ERROR') {
      return {
        status: 'TOOL_ERROR',
        result: result.result,
        error: result.error,
      };
    }
    return safeFailure(result.error && result.error.code);
  } catch {
    return safeFailure();
  }
}

function listSurface(runner = runPython) {
  try {
    const result = runner({ operation: 'list' });
    const tools = result && Array.isArray(result.tools) ? result.tools : [];
    return {
      tools: tools
        .filter((tool) => tool && typeof tool.name === 'string' && tool.name)
        .map((tool) => ({
          name: tool.name,
          description: typeof tool.description === 'string' ? tool.description : tool.name,
          inputSchema: tool.inputSchema && typeof tool.inputSchema === 'object'
            ? tool.inputSchema
            : { type: 'object', properties: {} },
        })),
    };
  } catch {
    return { tools: [] };
  }
}

function isCallToolResult(value) {
  return Boolean(
    value
    && typeof value === 'object'
    && !Array.isArray(value)
    && Array.isArray(value.content)
    && (!Object.hasOwn(value, 'isError') || typeof value.isError === 'boolean'),
  );
}

function localMcpError(summary) {
  return {
    content: [{
      type: 'text',
      text: typeof summary === 'string' && summary
        ? summary
        : 'External MCP call was not completed.',
    }],
    isError: true,
  };
}

function mcpCallResult(result) {
  if ((result.status === 'SUCCESS' || result.status === 'TOOL_ERROR') && isCallToolResult(result.result)) {
    return result.result;
  }
  return localMcpError(
    result.error?.summary || (
      result.status === 'TOOL_ERROR'
        ? 'External MCP tool returned an error.'
        : 'External MCP call was not completed.'
    ),
  );
}

function buildServer({ runner = runPython } = {}) {
  const server = new Server(
    { name: SERVER_NAME, version: '1.0.0' },
    { capabilities: { tools: {} } },
  );
  server.setRequestHandler(ListToolsRequestSchema, async () => listSurface(runner));
  server.setRequestHandler(CallToolRequestSchema, async (request) => (
    mcpCallResult(callSurface(
      request.params?.name,
      request.params?.arguments || {},
      runner,
    ))
  ));
  return server;
}

async function main() {
  const transport = new StdioServerTransport();
  await buildServer().connect(transport);
}

if (require.main === module) {
  main().catch(() => {
    process.exitCode = 1;
  });
}

module.exports = {
  SERVER_NAME,
  buildServer,
  callSurface,
  listSurface,
  mcpCallResult,
};
