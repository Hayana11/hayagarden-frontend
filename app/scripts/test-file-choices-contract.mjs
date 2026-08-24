import assert from 'node:assert/strict';
import fs from 'node:fs';
import { ComposerUploadCoordinator } from '../src/lib/composerUpload.ts';

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
assert.deepEqual(normalize(['😀'.repeat(120)]), ['😀'.repeat(120)]);
assert.equal(normalize(['😀'.repeat(121)]).length, 0);
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
assert.match(composerSend, /setInput\(''\)/);
assert.match(composerSend, /setInput\(rawText\)/);
const failureCheck = composerSend.indexOf('if (messageId === null)');
assert.ok(failureCheck >= 0);
const composerPostCall = composerSend.indexOf('sendChatMessage(attempt.text, extra)');
assert.ok(composerSend.indexOf('postingRef.current = true') < composerPostCall);
assert.ok(composerSend.indexOf('setPosting(true)') < composerPostCall);
assert.ok(composerSend.indexOf('postingRef.current = false') > composerPostCall);
assert.ok(composerSend.indexOf('setPosting(false)') > composerPostCall);
assert.ok(composerSend.indexOf('setPosting(false)') < failureCheck);
assert.ok(composerSend.indexOf('setPendingFile', failureCheck) > composerSend.indexOf('return;', failureCheck));
assert.ok(composerSend.indexOf('setPendingImage', failureCheck) > composerSend.indexOf('return;', failureCheck));

assert.match(choiceSend, /sendChatMessage\(choice\)/);
const choicePostCall = choiceSend.indexOf('sendChatMessage(choice)');
assert.ok(choiceSend.indexOf('postingRef.current = true') < choicePostCall);
assert.ok(choiceSend.indexOf('setPosting(false)') > choicePostCall);
assert.ok(choiceSend.indexOf('setPosting(false)') < choiceSend.indexOf('await refetchLatest()'));
assert.doesNotMatch(choiceSend, /composerMutationRevisionRef\.current \+= 1/);
assert.match(choiceSend, /uploadCoordinatorRef\.current\.beginChoicePost\(\)/);
assert.match(choiceSend, /uploadCoordinatorRef\.current\.endChoicePost\(\)/);
assert.doesNotMatch(choiceSend, /pendingFile|pendingImage|setInput|setPending/);

const attachStart = screen.indexOf('const onAttachFile = useCallback');
const renderStart = screen.indexOf('// ── message block renderers');
const composerValue = screen.indexOf('value={input}');
const composerTextareaStart = screen.lastIndexOf('<textarea', composerValue);
const composerTextareaEnd = screen.indexOf('/>', composerValue);
const composerUi = screen.slice(screen.indexOf('<input ref={imgInputRef}'), screen.indexOf('{/* ══ thinking drawer'));
const attachHandler = screen.slice(attachStart, renderStart);
const composerTextarea = screen.slice(composerTextareaStart, composerTextareaEnd);
assert.match(attachHandler, /if \(!f \|\| postingRef\.current\) return/);
assert.match(attachHandler, /uploadCoordinatorRef\.current\.settle/);
assert.ok((composerUi.match(/disabled=\{posting\}/g) || []).length >= 3);
assert.match(composerUi, /if \(f && !postingRef\.current\)/);
assert.match(composerUi, /if \(!postingRef\.current\) \{ setPendingFile\(null\); setPendingImage\(null\); \}/);
assert.match(composerTextarea, /disabled=\{posting\}/);
assert.match(composerTextarea, /if \(postingRef\.current\) return/);
assert.doesNotMatch(composerTextarea, /disabled=\{sending\}/);

// Pending POST mutations are rejected, so a failed attempt restores all three
// composer fields without a newer text/file/image replacing the snapshot.
const oldFile = { fileUrl: '/static/uploads/files/old.txt', fileName: 'old.txt' };
const oldImage = { name: 'old.png' };
const failedAttempt = { text: 'failed text', file: oldFile, image: oldImage };
let composerState = { text: '', file: oldFile, image: oldImage };
let posting = true;
const mutateWhileAllowed = (next) => {
  if (!posting) composerState = next;
};
mutateWhileAllowed({ text: 'new draft', file: { fileName: 'new.txt' }, image: { name: 'new.png' } });
posting = false;
composerState = { ...composerState, text: failedAttempt.text };
assert.deepEqual(composerState, failedAttempt);

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function exerciseChoiceUploadRace({ uploadDuringPost }) {
  const file = { fileUrl: '/static/uploads/files/queued.txt', fileName: 'queued.txt' };
  const upload = deferred();
  const choicePost = deferred();
  const sent = [];
  const draft = 'keep this draft';
  let revision = 7;
  let pendingFile = null;
  const coordinator = new ComposerUploadCoordinator(
    () => revision,
    (next) => { pendingFile = next; },
  );

  const uploadTask = coordinator.settle(upload.promise, revision);
  coordinator.beginChoicePost();
  const choiceTask = (async () => {
    try {
      sent.push({ content: 'Choice A' });
      await choicePost.promise;
    } finally {
      coordinator.endChoicePost();
    }
  })();

  if (uploadDuringPost) {
    upload.resolve(file);
    await uploadTask;
    assert.equal(pendingFile, null);
    choicePost.resolve(101);
    await choiceTask;
  } else {
    choicePost.resolve(101);
    await choiceTask;
    upload.resolve(file);
    await uploadTask;
  }

  assert.deepEqual(sent, [{ content: 'Choice A' }]);
  assert.deepEqual(pendingFile, file);
  assert.equal(draft, 'keep this draft');
}

await exerciseChoiceUploadRace({ uploadDuringPost: false });
await exerciseChoiceUploadRace({ uploadDuringPost: true });

console.log('FILE+CHOICES frontend contract: ok');
