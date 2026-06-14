'use strict';

const express                           = require('express');
const { McpServer }                     = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StreamableHTTPServerTransport } = require('@modelcontextprotocol/sdk/server/streamableHttp.js');
const { REST, Routes }                  = require('discord.js');
const { z }                             = require('zod');
const fs                                = require('fs');

// ── Config ────────────────────────────────────────────────────────────────────

function loadEnv() {
  const env = {};
  try {
    for (const line of fs.readFileSync('/opt/frontend/.env', 'utf8').split('\n')) {
      const m = line.match(/^(\w+)=(.*)$/);
      if (m) env[m[1]] = m[2].trim();
    }
  } catch {}
  return env;
}

const cfg           = loadEnv();
const BEARER_TOKEN  = cfg.DISCORD_MCP_BEARER  || process.env.DISCORD_MCP_BEARER;
const GUANKE_TOKEN  = cfg.DISCORD_GUANKE_TOKEN || process.env.DISCORD_GUANKE_TOKEN;

if (!BEARER_TOKEN) { console.error('DISCORD_MCP_BEARER not set'); process.exit(1); }
if (!GUANKE_TOKEN) { console.error('DISCORD_GUANKE_TOKEN not set'); process.exit(1); }

const rest = new REST({ version: '10' }).setToken(GUANKE_TOKEN);

// ── MCP Tools ─────────────────────────────────────────────────────────────────

function buildServer() {
  const server = new McpServer({ name: 'discord-connector', version: '1.0.0' });

  server.tool(
    'send_discord_message',
    {
      channel_id: z.string().describe('Discord channel ID'),
      content:    z.string().describe('Message text to send'),
    },
    async ({ channel_id, content }) => {
      try {
        const msg = await rest.post(Routes.channelMessages(channel_id), {
          body: { content },
        });
        return { content: [{ type: 'text', text: JSON.stringify({ success: true, message_id: msg.id }) }] };
      } catch (e) {
        return { content: [{ type: 'text', text: JSON.stringify({ success: false, error: String(e.message) }) }] };
      }
    }
  );

  server.tool(
    'read_discord_messages',
    {
      channel_id: z.string().describe('Discord channel ID'),
      limit:      z.number().min(1).max(50).default(10).describe('Number of messages to fetch (newest first)'),
    },
    async ({ channel_id, limit }) => {
      try {
        const msgs = await rest.get(Routes.channelMessages(channel_id), {
          query: new URLSearchParams({ limit: String(limit) }),
        });
        const result = msgs.reverse().map(m => ({
          id:        m.id,
          author:    m.author.username,
          author_id: m.author.id,
          content:   m.content,
          timestamp: m.timestamp,
          is_bot:    m.author.bot || false,
        }));
        return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
      } catch (e) {
        return { content: [{ type: 'text', text: JSON.stringify({ error: String(e.message) }) }] };
      }
    }
  );

  return server;
}

// ── HTTP ──────────────────────────────────────────────────────────────────────

const app = express();
app.use(express.json());

// Bearer auth
app.use((req, res, next) => {
  const auth = req.headers['authorization'] || '';
  if (auth !== `Bearer ${BEARER_TOKEN}`) {
    res.status(401).json({ error: 'unauthorized' });
    return;
  }
  next();
});

app.all('/mcp', async (req, res) => {
  const server    = buildServer();
  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
  await server.connect(transport);
  try {
    await transport.handleRequest(req, res, req.body);
    res.on('close', () => { transport.close(); server.close(); });
  } catch (err) {
    console.error('discord-mcp error:', err);
    if (!res.headersSent) res.status(500).json({ jsonrpc: '2.0', error: { code: -32603, message: 'Internal error' }, id: null });
    transport.close(); server.close();
  }
});

app.listen(3101, '0.0.0.0', () => {
  console.log('discord-mcp listening on :3101/mcp');
});

process.on('SIGTERM', () => process.exit(0));
process.on('SIGINT',  () => process.exit(0));
