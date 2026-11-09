import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import test from 'node:test';

import { pollRecentMail, queueUnifiedWake } from '../scripts/gmail_wake_poll.mjs';

const SCRATCH = '/var/tmp/hayagarden-agent/scratch';

async function withCursor(run) {
  const dir = await mkdtemp(join(SCRATCH, 'gmail-wake-test-'));
  try { await run(join(dir, 'cursor.json')); }
  finally { await rm(dir, { recursive: true, force: true }); }
}

test('first poll seeds only message ids, then queues each new id once', async () => {
  await withCursor(async (cursorPath) => {
    const queued = [];
    const first = await pollRecentMail({
      cursorPath,
      messages: [{ id: 'old_1', subject: 'private subject', body: 'private body' }],
      queueWake: async (id) => queued.push(id),
    });
    assert.deepEqual(first, { initialized: true, notified: 0 });
    assert.deepEqual(JSON.parse(await readFile(cursorPath, 'utf8')), { message_ids: ['old_1'] });
    const messages = [{ id: 'new_2', subject: 'private subject', body: 'private body' }, { id: 'old_1' }];
    const next = await pollRecentMail({ cursorPath, messages, queueWake: async (id) => queued.push(id) });
    const repeated = await pollRecentMail({ cursorPath, messages, queueWake: async (id) => queued.push(id) });
    assert.deepEqual(next, { initialized: false, notified: 1 });
    assert.deepEqual(repeated, { initialized: false, notified: 0 });
    assert.deepEqual(queued, ['new_2']);
  });
});

test('mail notice uses the existing self-trigger route and carries no message body', async () => {
  let request;
  await queueUnifiedWake('gmail_message_123', async (url, options) => {
    request = { url, options };
    return { ok: true, json: async () => ({ ok: true, id: 42 }) };
  });
  assert.equal(request.url, 'http://127.0.0.1:5050/api/self_triggers');
  assert.equal(request.options.method, 'POST');
  const payload = JSON.parse(request.options.body);
  assert.equal(payload.minutes, 1);
  assert.match(payload.note, /邮箱有新邮件/);
  assert.match(payload.note, /gmail_message_123/);
  assert.doesNotMatch(payload.note, /private body/);
});
