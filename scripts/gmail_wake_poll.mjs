import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const MAIL_MCP_URL = 'http://127.0.0.1:3457/mcp';
const SELF_TRIGGER_URL = 'http://127.0.0.1:5050/api/self_triggers';
const CURSOR_PATH = '/var/lib/ai-penpal-mail/wake-cursor.json';
const MAX_RECENT = 50;

export async function queueUnifiedWake(messageId, fetchImpl = globalThis.fetch) {
  const response = await fetchImpl(SELF_TRIGGER_URL, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      minutes: 1,
      note: `邮箱有新邮件，message id: ${messageId}。`,
    }),
    signal: AbortSignal.timeout(5_000),
    redirect: 'error',
  });
  if (!response.ok) throw new Error('Unified Wake queue rejected the mail notice');
  const queued = await response.json();
  if (queued.ok !== true || !Number.isInteger(queued.id)) {
    throw new Error('Unified Wake queue returned an invalid trigger');
  }
}

export async function pollRecentMail({ messages, cursorPath = CURSOR_PATH, queueWake = queueUnifiedWake }) {
  const recentIds = messages
    .map((message) => message?.id)
    .filter((id) => typeof id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(id));
  let priorIds = null;
  try {
    const cursor = JSON.parse(await readFile(cursorPath, 'utf8'));
    if (!Array.isArray(cursor.message_ids) || cursor.message_ids.some((id) => typeof id !== 'string')) {
      throw new Error('mail wake cursor is invalid');
    }
    priorIds = cursor.message_ids;
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }

  if (priorIds === null) {
    await writeFile(cursorPath, JSON.stringify({ message_ids: recentIds.slice(0, MAX_RECENT) }), { mode: 0o600 });
    return { initialized: true, notified: 0 };
  }

  const previous = new Set(priorIds);
  const newIds = recentIds.filter((id) => !previous.has(id));
  for (const id of newIds) await queueWake(id);
  const messageIds = [...new Set([...recentIds, ...priorIds])].slice(0, MAX_RECENT);
  await writeFile(cursorPath, JSON.stringify({ message_ids: messageIds }), { mode: 0o600 });
  return { initialized: false, notified: newIds.length };
}

async function main() {
  const client = new Client({ name: 'hayagarden-gmail-wake-poll', version: '1.0.0' });
  try {
    await client.connect(new StreamableHTTPClientTransport(new URL(MAIL_MCP_URL)));
    const result = await client.callTool({
      name: 'list_recent',
      arguments: { maxResults: MAX_RECENT, query: 'in:inbox newer_than:2d' },
    });
    if (result.isError) throw new Error('upstream list_recent failed');
    const text = result.content?.find((item) => item.type === 'text')?.text;
    const recent = JSON.parse(text || '{}');
    if (!Array.isArray(recent.messages)) throw new Error('upstream list_recent returned no message list');
    const status = await pollRecentMail({ messages: recent.messages });
    console.log(`[gmail-wake-poll] initialized=${status.initialized} queued=${status.notified}`);
  } catch (error) {
    console.error(`[gmail-wake-poll] failed: ${error?.name || 'Error'}`);
    process.exitCode = 1;
  } finally {
    await client.close();
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  await main();
}
