import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  CHAT_IMAGE_FALLBACK_LONG_EDGE,
  CHAT_IMAGE_MAX_LONG_EDGE,
  CHAT_IMAGE_QUALITY_MAX,
  CHAT_IMAGE_QUALITY_MIN,
  CHAT_IMAGE_SOFT_MAX_BYTES,
  CHAT_IMAGE_TARGET_BYTES,
  buildChatImageCompressionPlan,
  compressChatImage,
  createPendingChatImage,
  mergePendingChatImages,
  revokePendingChatImagePreview,
} from '../src/lib/chatImageCompression.ts';
import { ComposerUploadCoordinator } from '../src/lib/composerUpload.ts';

const root = path.dirname(fileURLToPath(import.meta.url));
const screen = fs.readFileSync(path.join(root, '../src/screens/ChatScreen.tsx'), 'utf8');
const api = fs.readFileSync(path.join(root, '../src/lib/api.ts'), 'utf8');

const plan = (bytes, width, height) => buildChatImageCompressionPlan({
  originalBytes: bytes, width, height,
});

{
  const result = plan(80 * 1024, 1000, 800);
  assert.equal(result.passthrough, true);
  assert.equal(result.reason, 'small-image');
  assert.equal(result.passes.length, 0);
}
{
  const result = plan(5 * 1024 * 1024, 4032, 3024);
  assert.equal(result.passthrough, false);
  assert.deepEqual([result.outputWidth, result.outputHeight], [1568, 1176]);
  assert.deepEqual(result.passes.map((pass) => [pass.width, pass.height]), [[1568, 1176], [1280, 960]]);
}
{
  const result = plan(2 * 1024 * 1024, 3024, 4032);
  assert.deepEqual([result.outputWidth, result.outputHeight], [1176, 1568]);
  assert.equal(Math.max(result.outputWidth, result.outputHeight), CHAT_IMAGE_MAX_LONG_EDGE);
  assert.equal(result.outputWidth / result.outputHeight, 1176 / 1568);
}
{
  const result = plan(120 * 1024, 1200, 900);
  assert.deepEqual([result.outputWidth, result.outputHeight], [1200, 900]);
}
{
  const result = plan(400 * 1024, 800, 600);
  assert.equal(result.passes[0].qualities[0], CHAT_IMAGE_QUALITY_MAX);
  assert.equal(result.passes[0].qualities.at(-1), CHAT_IMAGE_QUALITY_MIN);
  assert.ok(result.passes[0].qualities.every((q) => q >= CHAT_IMAGE_QUALITY_MIN && q <= CHAT_IMAGE_QUALITY_MAX));
}
{
  const result = plan(400 * 1024, 1600, 1200);
  assert.equal(result.passes.length, 2);
  assert.ok(result.passes[1].longEdge <= CHAT_IMAGE_FALLBACK_LONG_EDGE);
}
{
  const result = plan(400 * 1024, 900, 700);
  assert.ok(result.passes.length <= 1);
  assert.ok(result.passes.every((pass) => pass.width > 0 && pass.height > 0 && Number.isInteger(pass.width) && Number.isInteger(pass.height)));
}
{
  const result = plan(400 * 1024, 0, 900);
  assert.equal(result.passthrough, true);
  assert.equal(result.reason, 'invalid-dimensions');
}
{
  assert.match(screen, /const pending = selected\.map\(createPendingChatImage\)/);
  assert.match(screen, /Promise\.all\(pending\.map\(async \(image\) =>/);
  assert.match(screen, /status: 'ready'/);
  assert.match(screen, /compressingImageSlotsRef\.current/);
  assert.match(screen, /compressingImageCount === 0/);
  assert.match(screen, /settleImages/);
  assert.match(screen, /pendingFilesRef\.current\.length[\s\S]*pendingImagesRef\.current\.length[\s\S]*uploadingFileSlotsRef\.current[\s\S]*compressingImageSlotsRef\.current/);
  assert.match(screen, /<img src=\{image\.previewUrl\}/);
  assert.match(screen, /objectFit: 'cover'/);
  assert.match(screen, /image\.status === 'compressing'/);
  assert.match(screen, /const pending = selected\.map\(createPendingChatImage\)/);
  assert.match(screen, /removePendingImage\(image\.id\)/);
  assert.match(screen, /attempt\.images\.map\(\(image\) => image\.file\)/);
  assert.match(screen, /pendingImagesRef\.current\.forEach\(revokePendingChatImagePreview\)/);
  assert.match(screen, /attempt\.images\.forEach\(revokePendingChatImagePreview\)/);
  assert.match(screen, /revokePendingChatImagePreview\(removed\)/);
  assert.match(screen, /pendingFiles\.map\(\(file, index\) => \([\s\S]*borderRadius: 999[\s\S]*file\.fileName/);
  assert.ok((screen.match(/status: 'ready'/g) || []).length >= 2);
  assert.doesNotMatch(screen, /URL\.createObjectURL\(file\)/);
  assert.doesNotMatch(screen, /image-\$\{image\.name\}-\$\{index\}/);
}
{
  assert.match(api, /extra: \{ files\?: PendingChatFile\[\]; imageFiles\?: File\[\] \}/);
  assert.match(api, /fd\.append\('image', image\)/);
  assert.doesNotMatch(api, /compressChatImage/);
}
{
  const commits = [];
  const coordinator = new ComposerUploadCoordinator(() => 0, () => {}, (files) => commits.push(files));
  const first = new File(['a'], 'a.jpg', { type: 'image/jpeg' });
  const second = new File(['b'], 'b.jpg', { type: 'image/jpeg' });
  coordinator.beginChoicePost();
  assert.equal(await coordinator.settleImages(Promise.resolve([first, second]), 0), true);
  assert.deepEqual(commits, []);
  coordinator.endChoicePost();
  assert.deepEqual(commits, [[first, second]]);
}
{
  const names = ['A', 'B', 'C', 'D'];
  const results = await Promise.all(names.map(async (name, index) => {
    await new Promise((resolve) => setTimeout(resolve, 3 - index));
    return name + "'";
  }));
  assert.deepEqual(results, ["A'", "B'", "C'", "D'"]);
}
{
  const gif = new File(['GIF89a'], 'animated.gif', { type: 'image/gif' });
  const result = await compressChatImage(gif);
  assert.equal(result.file, gif);
  assert.equal(result.compressed, false);
  assert.equal(result.reason, 'preserve-gif');
}
{
  const svg = new File(['<svg/>'], 'diagram.svg', { type: 'image/svg+xml' });
  const result = await compressChatImage(svg);
  assert.equal(result.file, svg);
  assert.equal(result.compressed, false);
  assert.equal(result.reason, 'preserve-svg');
}
{
  const undecodable = new File(['not-an-image'], 'broken.jpg', { type: 'image/jpeg' });
  const result = await compressChatImage(undecodable);
  assert.equal(result.file, undecodable);
  assert.equal(result.compressed, false);
  assert.equal(result.reason, 'decode-failed-original');
}

{
  const original = new File(['original'], 'photo.png', { type: 'image/png' });
  const pending = createPendingChatImage(original);
  assert.ok(pending.id);
  assert.equal(pending.file, original);
  assert.equal(typeof pending.previewUrl, 'string');
  assert.equal(pending.status, 'compressing');
  const ready = { ...pending, file: new File(['compressed'], 'photo.webp', { type: 'image/webp' }), status: 'ready', outputBytes: 10 };
  const merged = mergePendingChatImages([pending], [ready]);
  assert.deepEqual(merged, [ready]);
  revokePendingChatImagePreview(pending);
}
{
  const first = { id: 'a', file: new File(['a'], 'a.jpg', { type: 'image/jpeg' }), previewUrl: 'blob:a', status: 'compressing', originalBytes: 1 };
  const second = { id: 'b', file: new File(['b'], 'b.jpg', { type: 'image/jpeg' }), previewUrl: 'blob:b', status: 'compressing', originalBytes: 1 };
  const readyFirst = { ...first, status: 'ready' };
  const readySecond = { ...second, status: 'ready' };
  assert.deepEqual(mergePendingChatImages([readySecond], [readyFirst, readySecond]), [readySecond]);
  assert.deepEqual([first, second].map((image) => image.file), [first.file, second.file]);
}
{
  let current = [];
  const original = { id: 'pending', file: new File(['a'], 'pending.jpg', { type: 'image/jpeg' }), previewUrl: 'blob:pending', status: 'compressing', originalBytes: 1 };
  const ready = { ...original, status: 'ready' };
  current = [original];
  const coordinator = new ComposerUploadCoordinator(() => 0, () => {}, (images) => {
    current = mergePendingChatImages(current, images);
  });
  const settling = coordinator.settleImages(new Promise((resolve) => setTimeout(() => resolve([ready]), 5)), 0);
  current = [];
  await settling;
  assert.deepEqual(current, []);
}
assert.ok(CHAT_IMAGE_TARGET_BYTES < CHAT_IMAGE_SOFT_MAX_BYTES);
console.log('chat image compression focused tests passed: 17 cases');
