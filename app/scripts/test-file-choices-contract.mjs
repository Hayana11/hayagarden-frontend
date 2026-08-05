import assert from 'node:assert/strict';
import fs from 'node:fs';

const screen = fs.readFileSync(new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8');
const chat = fs.readFileSync(new URL('../src/lib/chat.ts', import.meta.url), 'utf8');

const maxChoices = Number(chat.match(/MAX_CHAT_CHOICES = (\d+)/)?.[1]);
const maxChoiceLength = Number(chat.match(/MAX_CHAT_CHOICE_LENGTH = (\d+)/)?.[1]);
const normalizeBody = chat.match(/export function normalizeChatChoices\([^)]*\): string\[\] \{([\s\S]*?)\n\}/)?.[1]
  ?.replace(/\(item\): item is string/g, '(item)');
const previewBody = chat.match(/export function chatFilePreviewUrl\([^)]*\): string \{([\s\S]*?)\n\}/)?.[1];
assert.ok(maxChoices && maxChoiceLength && normalizeBody && previewBody);
const normalizeChatChoices = new Function(
  'value', 'MAX_CHAT_CHOICE_LENGTH', 'MAX_CHAT_CHOICES',
  normalizeBody,
);
const normalize = (value) => normalizeChatChoices(value, maxChoiceLength, maxChoices);
const chatFilePreviewUrl = new Function('fileUrl', previewBody);

assert.deepEqual(normalize('abc'), []);
assert.deepEqual(normalize([1, null, ' A ', '']), ['A']);
assert.deepEqual(normalize(['A', 'B', 'C']), ['A', 'B', 'C']);
assert.equal(normalize(['x'.repeat(121)]).length, 0);
assert.equal(normalize(Array.from({ length: 12 }, (_, i) => String(i))).length, 8);

assert.equal(
  chatFilePreviewUrl('/static/uploads/files/abc_design.html'),
  '/api/chat/files/abc_design.html/preview',
);
assert.equal(chatFilePreviewUrl('/etc/passwd'), '');
assert.equal(chatFilePreviewUrl('/static/uploads/files/../secret.txt'), '');

const sendStart = screen.indexOf('const send = useCallback');
const choiceStart = screen.indexOf('const sendChoice = useCallback');
const chooseStart = screen.indexOf('const chooseOption = useCallback');
assert.ok(sendStart >= 0 && choiceStart > sendStart && chooseStart > choiceStart);

const composerSend = screen.slice(sendStart, choiceStart);
const choiceSend = screen.slice(choiceStart, chooseStart);
assert.match(composerSend, /file: pendingFile/);
assert.match(composerSend, /image: pendingImage/);
assert.match(composerSend, /setInput\(\(current\) => current \|\| attempt\.text\)/);
const failureCheck = composerSend.indexOf('if (messageId === null)');
assert.ok(failureCheck >= 0);
assert.ok(composerSend.indexOf('setPendingFile', failureCheck) > composerSend.indexOf('return;', failureCheck));
assert.ok(composerSend.indexOf('setPendingImage', failureCheck) > composerSend.indexOf('return;', failureCheck));

assert.match(choiceSend, /sendChatMessage\(choice\)/);
assert.doesNotMatch(choiceSend, /pendingFile|pendingImage|setInput|setPending/);

console.log('FILE+CHOICES frontend contract: ok');
