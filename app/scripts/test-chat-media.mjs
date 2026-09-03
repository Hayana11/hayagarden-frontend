import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  chatMediaLayoutForCount,
  chatMediaExitX,
  chatMediaSwipeDirection,
  clampChatMediaIndex,
  imageUrlsFromToolValue,
  nextChatMediaIndex,
  visibleChatMediaIndices,
} from '../src/lib/chatMedia.ts';

assert.equal(chatMediaLayoutForCount(1), 'single');
assert.equal(chatMediaLayoutForCount(2), 'double');
assert.equal(chatMediaLayoutForCount(3), 'collage');
assert.equal(chatMediaLayoutForCount(4), 'stack');
assert.equal(chatMediaLayoutForCount(9), 'stack');

assert.deepEqual(visibleChatMediaIndices(7, 0), [0, 1, 2]);
assert.deepEqual(visibleChatMediaIndices(7, 3), [2, 3, 4, 5]);
assert.ok(visibleChatMediaIndices(100, 50).length <= 4);
assert.equal(clampChatMediaIndex(-3, 7), 0);
assert.equal(clampChatMediaIndex(99, 7), 6);
assert.equal(nextChatMediaIndex(0, 7, -1), 0);
assert.equal(nextChatMediaIndex(6, 7, 1), 6);
assert.equal(nextChatMediaIndex(2, 7, 1), 3);
assert.equal(chatMediaExitX(1, 300), -300);
assert.equal(chatMediaExitX(-1, 300), 300);
assert.equal(chatMediaSwipeDirection(-40), 1);
assert.equal(chatMediaSwipeDirection(40), -1);

const here = dirname(fileURLToPath(import.meta.url));
const chatScreen = readFileSync(join(here, '../src/screens/ChatScreen.tsx'), 'utf8');
const mediaGroup = readFileSync(join(here, '../src/components/ChatMediaGroup.tsx'), 'utf8');
const userRenderer = chatScreen.slice(chatScreen.indexOf('function renderUserMsg'), chatScreen.indexOf('function renderOrderedAssistantContent'));
assert.match(userRenderer, /chat-message-content-user[\s\S]*ChatMediaGroup[\s\S]*chat-message-bubble-user/);
assert.doesNotMatch(userRenderer, /chat-message-bubble-user[\s\S]*<img/);
assert.match(mediaGroup, /data-testid="chat-photo-stack"/);
assert.match(mediaGroup, /onTransitionEnd/);
assert.doesNotMatch(mediaGroup, /280/);
assert.doesNotMatch(chatScreen.slice(chatScreen.indexOf('function renderMarkdown'), chatScreen.indexOf('function renderUserMsg')), /chat-message-bubble-assistant/);

assert.deepEqual(
  imageUrlsFromToolValue({ images: [{ image_url: '/one.png', source_url: 'https://example.test/not-an-image.jpg' }, { imageUrl: 'https://example.test/two.jpg' }], url: '/ordinary-link' }),
  ['/one.png', 'https://example.test/two.jpg'],
);
assert.deepEqual(imageUrlsFromToolValue('{"type":"image","source":"/three.webp"}'), ['/three.webp']);
const manyImageUrls = Array.from({ length: 24 }, (_, index) => `/image-${index}.png`);
assert.deepEqual(imageUrlsFromToolValue({ images: manyImageUrls }), manyImageUrls);

console.log('chat media focused tests passed');

