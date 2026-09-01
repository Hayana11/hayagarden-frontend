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
  assert.match(screen, /Promise\.all\(selected\.map\(\(file\) => compressChatImage\(file\)\)\)/);
  assert.match(screen, /compressingImageSlotsRef\.current/);
  assert.match(screen, /compressingImageCount === 0/);
  assert.match(screen, /settleImages/);
  assert.match(screen, /pendingFilesRef\.current\.length[\s\S]*pendingImagesRef\.current\.length[\s\S]*uploadingFileSlotsRef\.current[\s\S]*compressingImageSlotsRef\.current/);
  assert.match(screen, /正在处理图片/);
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
assert.ok(CHAT_IMAGE_TARGET_BYTES < CHAT_IMAGE_SOFT_MAX_BYTES);
console.log('chat image compression focused tests passed: 14 cases');
