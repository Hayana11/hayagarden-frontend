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
assert.deepEqual(normalize(['A', 'B', 'C']), ['A', 'B', 'C']);
assert.equal(normalize(['x'.repeat(121)]).length, 0);
assert.deepEqual(normalize(['😀'.repeat(120)]), ['😀'.repeat(120)]);
assert.equal(normalize(['😀'.repeat(121)]).length, 0);
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
assert.match(composerSend, /setInput\(''\)/);
assert.match(composerSend, /setInput\(rawText\)/);
const failureCheck = composerSend.indexOf('if (messageId === null)');
const composerPostCall = composerSend.indexOf('sendChatMessage(attempt.text, extra)');
assert.ok(failureCheck >= 0 && composerPostCall >= 0);
assert.ok(composerSend.indexOf('postingRef.current = true') < composerPostCall);
assert.ok(composerSend.indexOf('setPosting(true)') < composerPostCall);
assert.ok(composerSend.indexOf('setPosting(false)') > composerPostCall);
assert.ok(composerSend.indexOf('setPosting(false)') < failureCheck);
assert.ok(composerSend.indexOf('setPendingFiles', failureCheck) > composerSend.indexOf('return;', failureCheck));
assert.ok(composerSend.indexOf('setPendingImages', failureCheck) > composerSend.indexOf('return;', failureCheck));

assert.match(choiceSend, /sendChatMessage\(choice\)/);
const choicePostCall = choiceSend.indexOf('sendChatMessage(choice)');
assert.ok(choiceSend.indexOf('postingRef.current = true') < choicePostCall);
assert.ok(choiceSend.indexOf('setPosting(false)') > choicePostCall);
assert.ok(choiceSend.indexOf('setPosting(false)') < choiceSend.indexOf('await refetchLatest()'));
assert.match(choiceSend, /uploadCoordinatorRef\.current\.beginChoicePost\(\)/);
assert.match(choiceSend, /uploadCoordinatorRef\.current\.endChoicePost\(\)/);
assert.doesNotMatch(choiceSend, /composerMutationRevisionRef\.current \+= 1/);
assert.doesNotMatch(choiceSend, /pendingFiles|pendingImages|setPendingFiles|setPendingImages/);

const composerUi = screen.slice(
  screen.indexOf('<input ref={imgInputRef}'),
  screen.indexOf('{/* ══ thinking drawer'),
);
assert.equal((composerUi.match(/multiple/g) || []).length, 2);
assert.match(composerUi, /\.pdf,\.doc,\.docx/);
assert.match(screen, /MAX_COMPOSER_ATTACHMENTS = 4/);
assert.match(screen, /attachments\.map/);
assert.match(screen, /attachment\.type === 'image'/);
assert.match(chat, /export interface ChatAttachment/);
assert.match(chat, /attachments: ChatAttachment\[\]/);
assert.match(chat, /normalizeChatAttachments/);
assert.match(chat, /row\.attachments/);
assert.match(composerUi, /pendingFiles\.map/);
assert.match(composerUi, /pendingImages\.map/);
assert.match(screen, /const \[uploadingFileCount, setUploadingFileCount\] = useState\(0\)/);
assert.match(screen, /const uploadingFileSlotsRef = useRef\(0\)/);
assert.match(screen, /- uploadingFileSlotsRef\.current/);
assert.match(composerUi, /disabled=\{posting \|\| uploadingFileCount > 0\}/);
const composerValue = screen.indexOf('value={input}');
const composerTextareaStart = screen.lastIndexOf('<textarea', composerValue);
const composerTextareaEnd = screen.indexOf('/>', composerValue);
const composerTextarea = screen.slice(composerTextareaStart, composerTextareaEnd);
assert.match(composerTextarea, /disabled=\{posting\}/);
assert.match(composerTextarea, /if \(postingRef\.current\) return/);
assert.doesNotMatch(composerTextarea, /disabled=\{sending\}/);

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

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function exerciseChoiceUploadRace({ uploadDuringPost }) {
  const files = [
    { fileUrl: '/static/uploads/files/queued-a.txt', fileName: 'queued-a.txt' },
    { fileUrl: '/static/uploads/files/queued-b.pdf', fileName: 'queued-b.pdf' },
  ];
  const upload = deferred();
  const choicePost = deferred();
  const sent = [];
  let currentRevision = 7;
  let pending = [];
  const raceCoordinator = new ComposerUploadCoordinator(
    () => currentRevision,
    (next) => { pending = [...pending, ...next]; },
  );

  const uploadTask = raceCoordinator.settle(upload.promise, currentRevision);
  raceCoordinator.beginChoicePost();
  const choiceTask = (async () => {
    try {
      sent.push({ content: 'Choice A' });
      await choicePost.promise;
    } finally {
      raceCoordinator.endChoicePost();
    }
  })();

  if (uploadDuringPost) {
    upload.resolve(files);
    await uploadTask;
    assert.deepEqual(pending, []);
    choicePost.resolve(101);
    await choiceTask;
  } else {
    choicePost.resolve(101);
    await choiceTask;
    upload.resolve(files);
    await uploadTask;
  }

  assert.deepEqual(sent, [{ content: 'Choice A' }]);
  assert.deepEqual(pending, files);
}

await exerciseChoiceUploadRace({ uploadDuringPost: false });
await exerciseChoiceUploadRace({ uploadDuringPost: true });

console.log('FILE+CHOICES frontend contract: ok');
