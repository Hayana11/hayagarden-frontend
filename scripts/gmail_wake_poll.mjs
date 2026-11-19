import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const MAIL_MCP_URL = 'http://127.0.0.1:3457/mcp';
const MAIL_NOTICE_URL = 'http://127.0.0.1:5050/api/mail_notices';
const CURSOR_PATH = '/var/lib/ai-penpal-mail/wake-cursor.json';
const MAX_RECENT = 50;

export async function recordPendingMailNotice(messageId, fetchImpl = globalThis.fetch) {
  const response = await fetchImpl(MAIL_NOTICE_URL, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ message_id: messageId }),
    signal: AbortSignal.timeout(5_000),
    redirect: 'error',
  });
  if (!response.ok) throw new Error('Mail notice registration rejected');
  const result = await response.json();
  if (result.ok !== true) throw new Error('Mail notice registration returned an invalid result');
}

export async function pollRecentMail({ messages, cursorPath = CURSOR_PATH, recordNotice = recordPendingMailNotice }) {
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
    return { initialized: true, recorded: 0 };
  }

  const previous = new Set(priorIds);
  const newIds = recentIds.filter((id) => !previous.has(id));
  for (const id of newIds) await recordNotice(id);
  const messageIds = [...new Set([...recentIds, ...priorIds])].slice(0, MAX_RECENT);
  await writeFile(cursorPath, JSON.stringify({ message_ids: messageIds }), { mode: 0o600 });
  return { initialized: false, recorded: newIds.length };
}

async function main() {
  const client = new Client({ name: 'hayagarden-gmail-mail-notice-poll', version: '1.0.0' });
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
    console.log(`[gmail-mail-notice-poll] initialized=${status.initialized} recorded=${status.recorded}`);
  } catch (error) {
    console.error(`[gmail-mail-notice-poll] failed: ${error?.name || 'Error'}`);
    process.exitCode = 1;
  } finally {
    await client.close();
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  await main();
}
