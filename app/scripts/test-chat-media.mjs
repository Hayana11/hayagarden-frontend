import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  chatMediaLayoutForCount,
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

const here = dirname(fileURLToPath(import.meta.url));
const chatScreen = readFileSync(join(here, '../src/screens/ChatScreen.tsx'), 'utf8');
const mediaGroup = readFileSync(join(here, '../src/components/ChatMediaGroup.tsx'), 'utf8');
const userRenderer = chatScreen.slice(chatScreen.indexOf('function renderUserMsg'), chatScreen.indexOf('function renderOrderedAssistantContent'));
assert.match(userRenderer, /chat-message-content-user[\s\S]*ChatMediaGroup[\s\S]*chat-message-bubble-user/);
assert.doesNotMatch(userRenderer, /chat-message-bubble-user[\s\S]*<img/);
assert.match(mediaGroup, /data-testid="chat-photo-stack"/);

assert.deepEqual(
  imageUrlsFromToolValue({ images: [{ image_url: '/one.png' }, { imageUrl: 'https://example.test/two.jpg' }], url: '/ordinary-link' }),
  ['/one.png', 'https://example.test/two.jpg'],
);

console.log('chat media focused tests passed');

