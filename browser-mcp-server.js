'use strict';

const { McpServer } = require('@modelcontextprotocol/sdk/server/mcp.js');
const { StdioServerTransport } = require('@modelcontextprotocol/sdk/server/stdio.js');
const { execFileSync } = require('child_process');
const { z } = require('zod');

const BROWSER_SERVER_NAME = 'browser';
const BROWSER_TOOL_NAMES = Object.freeze(['taobao_read']);

function readTaobao(url) {
  const python = process.env.PYTHON || 'python3';
  const cwd = process.env.UH_A0_REPO_ROOT || process.cwd();
  const output = execFileSync(python, ['-m', 'tools.shopping_taobao_read_adapter'], {
    cwd,
    env: process.env,
    input: JSON.stringify({ operation: 'read_taobao_page', url }),
    encoding: 'utf8',
    timeout: 65000,
  });
  return JSON.parse(output || '{}');
}

function formatResult(result) {
  const parts = [];
  if (result.need_login) {
    parts.push('⚠️ 淘宝浏览器当前需要登录或登录态已失效。');
  }
  parts.push('🛒 ' + String(result.url || '淘宝页面'));
  parts.push('');
  parts.push(String(result.text || '（页面没有可提取的文字）'));
  return parts.join('\n');
}

function buildServer() {
  const server = new McpServer({ name: BROWSER_SERVER_NAME, version: '1.0.0' });
  server.tool(
    'taobao_read',
    '用唯一常驻、已登录的购物浏览器读取淘宝或天猫页面。只读；不会点击、加购、结算或启动第二只浏览器。',
    { url: z.string().url().describe('taobao.com 或 tmall.com 页面地址') },
    async ({ url }) => {
      try {
        return { content: [{ type: 'text', text: formatResult(readTaobao(url)) }] };
      } catch (error) {
        return {
          isError: true,
          content: [{ type: 'text', text: 'BROWSER_READ_FAILED: ' + error.message }],
        };
      }
    },
  );
  return server;
}

async function main() {
  const transport = new StdioServerTransport();
  await buildServer().connect(transport);
}

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write('BROWSER_MCP_SERVER_FAILED: ' + error.message + '\n');
    process.exitCode = 1;
  });
}

module.exports = { BROWSER_SERVER_NAME, BROWSER_TOOL_NAMES, buildServer, formatResult, readTaobao };
