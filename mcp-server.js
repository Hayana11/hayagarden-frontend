const { McpServer } = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StdioServerTransport } = require('@modelcontextprotocol/sdk/server/stdio.js');
const { execSync } = require('child_process');
const { z } = require('zod');

const server = new McpServer({ name: "home-mcp", version: "1.0.0" });

server.tool("exec_vps", { command: z.string() }, async ({ command }) => {
  try {
    const r = execSync(command, { timeout: 30000, encoding: 'utf-8', maxBuffer: 1024*1024 });
    return { content: [{ type: "text", text: r || "(no output)" }] };
  } catch(e) {
    return { content: [{ type: "text", text: "Error: " + e.message }] };
  }
});

async function main() {
  const t = new StdioServerTransport();
  await server.connect(t);
}
main().catch(console.error);
