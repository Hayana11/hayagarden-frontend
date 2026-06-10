'use strict';

const { McpServer }                      = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StreamableHTTPServerTransport }  = require('@modelcontextprotocol/sdk/server/streamableHttp.js');
const { createMcpExpressApp }            = require('@modelcontextprotocol/sdk/server/express.js');
const { execSync }                       = require('child_process');
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

  return server;
}

// host: '0.0.0.0' — disable localhost-only DNS-rebinding middleware
// (nginx handles host validation upstream)
const app = createMcpExpressApp({ host: '0.0.0.0' });

app.post('/mcp', async (req, res) => {
  const server = buildServer();
  try {
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: undefined,   // stateless — no sessions, no auth
    });
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
    res.on('close', () => {
      transport.close();
      server.close();
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

app.get('/mcp', (_req, res) => {
  res.status(405).json({
    jsonrpc: '2.0',
    error: { code: -32000, message: 'Method not allowed.' },
    id: null,
  });
});

app.delete('/mcp', (_req, res) => {
  res.status(405).json({
    jsonrpc: '2.0',
    error: { code: -32000, message: 'Method not allowed.' },
    id: null,
  });
});

const PORT = 3100;
app.listen(PORT, '0.0.0.0', (err) => {
  if (err) { console.error('Failed to start:', err); process.exit(1); }
  console.log(`MCP Streamable HTTP server on port ${PORT}, endpoint POST /mcp`);
});

process.on('SIGTERM', () => process.exit(0));
process.on('SIGINT',  () => process.exit(0));
