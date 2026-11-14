import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import test from 'node:test';

import { pollRecentMail, recordPendingMailNotice } from '../scripts/gmail_wake_poll.mjs';

const SCRATCH = '/var/tmp/hayagarden-agent/scratch';

async function withCursor(run) {
  const dir = await mkdtemp(join(SCRATCH, 'gmail-mail-notice-test-'));
  try { await run(join(dir, 'cursor.json')); }
  finally { await rm(dir, { recursive: true, force: true }); }
}

test('new Gmail ids register pending notices once without wake or self-trigger requests', async () => {
  await withCursor(async (cursorPath) => {
    const requests = [];
    const fetchImpl = async (url, options) => {
      requests.push({ url, options });
      return { ok: true, json: async () => ({ ok: true, inserted: true }) };
    };
    const recordNotice = (id) => recordPendingMailNotice(id, fetchImpl);
    const messages = [
      { id: 'old_1', subject: 'private subject', body: 'private body' },
      { id: 'new_2', subject: 'private subject', body: 'private body' },
    ];

    const first = await pollRecentMail({
      cursorPath, messages: [{ id: 'old_1' }], recordNotice,
    });
    assert.deepEqual(first, { initialized: true, recorded: 0 });
    const next = await pollRecentMail({ cursorPath, messages, recordNotice });
    const repeated = await pollRecentMail({ cursorPath, messages, recordNotice });

    assert.deepEqual(next, { initialized: false, recorded: 1 });
    assert.deepEqual(repeated, { initialized: false, recorded: 0 });
    assert.deepEqual(JSON.parse(await readFile(cursorPath, 'utf8')), {
      message_ids: ['old_1', 'new_2'],
    });
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, 'http://127.0.0.1:5050/api/mail_notices');
    assert.doesNotMatch(requests[0].url, /self_triggers|wake/);
    assert.equal(requests[0].options.method, 'POST');
    assert.deepEqual(JSON.parse(requests[0].options.body), { message_id: 'new_2' });
    assert.doesNotMatch(requests[0].options.body, /private body|private subject/);
  });
});
