'use strict';

const express                            = require('express');
const { McpServer }                      = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StreamableHTTPServerTransport }  = require('@modelcontextprotocol/sdk/server/streamableHttp.js');
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

  server.tool('light_on',  {}, () => callLight('/light/on',  'POST'));
  server.tool('light_off', {}, () => callLight('/light/off', 'POST'));
  server.tool('get_light_status', {}, () => callLight('/light/status', 'GET'));
  server.tool('light_bedside_warm', {}, () => callLight('/light/bedside/warm', 'POST'));
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

  return server;
}

const app = express();
app.use(express.json());

// Stateless streamable-http: new transport + server per request, no sessions, no auth
app.all('/mcp', async (req, res) => {
  const server    = buildServer();
  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
  await server.connect(transport);
  try {
    await transport.handleRequest(req, res, req.body);
    res.on('close', () => { transport.close(); server.close(); });
  } catch (err) {
    console.error('MCP request error:', err);
    if (!res.headersSent) {
      res.status(500).json({
        jsonrpc: '2.0',
        error: { code: -32603, message: 'Internal server error' },
        id: null,
      });
    }
    transport.close();
    server.close();
  }
});

app.listen(3100, '127.0.0.1', () => {
  console.log('MCP streamable-http server listening on 127.0.0.1:3100/mcp');
});

process.on('SIGTERM', () => process.exit(0));
process.on('SIGINT',  () => process.exit(0));
