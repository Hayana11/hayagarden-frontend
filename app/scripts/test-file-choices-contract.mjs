import assert from 'node:assert/strict';
import fs from 'node:fs';
import { ComposerUploadCoordinator } from '../src/lib/composerUpload.ts';

const screen = fs.readFileSync(new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8');
const api = fs.readFileSync(new URL('../src/lib/api.ts', import.meta.url), 'utf8');
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
assert.equal(normalize(['x'.repeat(121)]).length, 0);
assert.equal(normalize(Array.from({ length: 12 }, (_, i) => String(i))).length, 8);
assert.equal(chatFilePreviewUrl('/static/uploads/files/abc_design.html'), '/api/chat/files/abc_design.html/preview');
assert.equal(chatFilePreviewUrl('/etc/passwd'), '');
assert.equal(chatFilePreviewUrl('/static/uploads/files/../secret.txt'), '');

const sendStart = screen.indexOf('const send = useCallback');
const choiceStart = screen.indexOf('const sendChoice = useCallback');
const chooseStart = screen.indexOf('const chooseOption = useCallback');
assert.ok(sendStart >= 0 && choiceStart > sendStart && chooseStart > choiceStart);

const composerSend = screen.slice(sendStart, choiceStart);
const choiceSend = screen.slice(choiceStart, chooseStart);
assert.match(composerSend, /files: pendingFiles/);
assert.match(composerSend, /images: pendingImages/);
assert.match(composerSend, /const extra = \{ files: attempt\.files, imageFiles: attempt\.images \}/);
assert.match(composerSend, /setPendingFiles/);
assert.match(composerSend, /setPendingImages/);
assert.match(choiceSend, /sendChatMessage\(choice\)/);
assert.doesNotMatch(choiceSend, /pendingFiles|pendingImages|setPendingFiles|setPendingImages/);

const composerUi = screen.slice(
  screen.indexOf('<input ref={imgInputRef}'),
  screen.indexOf('{/* ══ thinking drawer'),
);
assert.equal((composerUi.match(/multiple/g) || []).length, 2);
assert.match(composerUi, /\.pdf,\.doc,\.docx/);
assert.match(screen, /MAX_COMPOSER_ATTACHMENTS = 4/);
assert.match(composerUi, /pendingFiles\.map/);
assert.match(composerUi, /pendingImages\.map/);

assert.match(api, /fd\.append\('attachments', JSON\.stringify\(extra\.files \|\| \[\]\)\)/);
assert.match(api, /for \(const image of extra\.imageFiles \|\| \[\]\) fd\.append\('image', image\)/);

let revision = 7;
const committed = [];
const coordinator = new ComposerUploadCoordinator(
  () => revision,
  (files) => committed.push(files),
);
const one = { fileUrl: '/static/uploads/files/a.txt', fileName: 'a.txt' };
const two = { fileUrl: '/static/uploads/files/b.pdf', fileName: 'b.pdf' };
assert.equal(await coordinator.settle(Promise.resolve([one, two]), revision), true);
assert.deepEqual(committed, [[one, two]]);

coordinator.beginChoicePost();
assert.equal(await coordinator.settle(Promise.resolve([one]), revision), true);
assert.deepEqual(committed, [[one, two]]);
coordinator.endChoicePost();
assert.deepEqual(committed, [[one, two], [one]]);

coordinator.beginChoicePost();
const staleRevision = revision;
revision += 1;
assert.equal(await coordinator.settle(Promise.resolve([two]), staleRevision), true);
coordinator.endChoicePost();
assert.deepEqual(committed, [[one, two], [one]]);

console.log('FILE+CHOICES frontend contract: ok');
