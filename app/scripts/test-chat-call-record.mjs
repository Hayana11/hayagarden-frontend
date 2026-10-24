/**
 * Chat call record card: call turns are folded server-side, the record row opens
 * the call's transcript (no vitest). Run: npm run test:chat-call-record
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const { rowToMsg, callRecordLabel } = await import('../src/lib/chat.ts');

const base = { author: 'fyodor', content: 'ᯅ 通话时长 00:30', created_at: '2026-10-24 21:30:00' };
const mine = rowToMsg({ id: 7, ...base, call_id: 'c1', call_part: 'record', call_caller: 'her' });
assert.equal(mine.callId, 'c1');
assert.equal(mine.callCaller, 'her');
const his = rowToMsg({ id: 8, ...base, call_id: 'c2', call_part: 'record', call_caller: 'fyodor' });
assert.equal(his.callCaller, 'fyodor');
const unknown = rowToMsg({ id: 9, ...base, call_id: 'c3', call_part: 'record', call_caller: 'someone' });
assert.equal(unknown.callCaller, 'her', 'an unknown caller falls back to her side');
const plain = rowToMsg({ id: 10, author: 'fyodor', content: '晚安', created_at: '2026-10-24 21:31:00' });
assert.equal(plain.callId, undefined, 'ordinary messages are not call records');
assert.equal('callCaller' in plain, false);
const notRecord = rowToMsg({ id: 11, ...base, call_id: 'c4', call_part: 'turn' });
assert.equal(notRecord.callId, undefined, 'only record rows become cards');

assert.equal(callRecordLabel('ᯅ 通话时长 00:30'), '通话时长 00:30');
assert.equal(callRecordLabel('  ᯅ未接听'), '未接听');
assert.equal(callRecordLabel('ᯅ '), '通话');

const screen = readFileSync(new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8');
const card = screen.indexOf('if (message.callId) {');
const bubble = screen.indexOf("{message.role === 'user'", card);
assert.ok(card > 0 && bubble > card, 'a call record renders as its card, before the normal bubbles');
assert.match(screen, /renderCallRecord\(message\)/);
assert.match(screen, /setOpenCall\(\{ id: m\.callId as string, title: label \}\)/, 'tapping the card opens that call');
assert.match(screen, /<CallTranscriptSheet callId=\{openCall\.id\}/);
assert.match(screen, /alignItems: mine \? 'flex-end' : 'flex-start'/, 'the card sits on the caller\'s side');

const sheet = readFileSync(new URL('../src/components/CallTranscriptSheet.tsx', import.meta.url), 'utf8');
assert.match(sheet, /voiceCall\.load\(callId\)/, 'the sheet reads the call\'s own lines');
assert.match(sheet, /line\.speaker === 'her' \? 'flex-end' : 'flex-start'/);
assert.doesNotMatch(sheet, /\bgap\s*:/, 'Chrome 78: no flex gap');
assert.doesNotMatch(sheet, /\binset\s*:/, 'Chrome 78: no inset shorthand');
assert.match(sheet, /return \(\) => \{ alive = false; \};/, 'a closed sheet ignores a late response');

console.log('chat call record: OK');
