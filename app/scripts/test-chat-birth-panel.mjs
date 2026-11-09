import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { normalizeBirth, normalizeCacheInfo } from '../src/lib/chat.ts';
import { ChatBirthPanel } from '../src/components/ChatBirthPanel.tsx';

const raw = {
  total_ms: 12400,
  turn_kind: 'hot',
  phases: [
    { key: 'persona', label: '载入人设', ms: 300, detail: '人设 4.1k 字 · 系统提示 9.1k 字' },
    { key: 'proof', label: '上下文核对', ms: 8000, detail: '通过' },
    { key: 'first_word', label: '模型开口', ms: 3500, detail: null },
    { key: 'junk', label: '', ms: 5 },
  ],
};

// normalize: snake_case in, empty labels dropped, nothing for an empty breakdown
const birth = normalizeBirth(raw);
assert.equal(birth.totalMs, 12400);
assert.equal(birth.turnKind, 'hot');
assert.deepEqual(birth.phases.map((p) => p.key), ['persona', 'proof', 'first_word']);
assert.equal(birth.phases[2].detail, undefined);
assert.equal(normalizeBirth({ total_ms: 0, phases: raw.phases }), null);
assert.equal(normalizeBirth({ total_ms: 100, phases: [] }), null);
assert.equal(normalizeBirth(null), null);

// history: the breakdown kept in cache_info comes back on the message
assert.deepEqual(normalizeCacheInfo({ input_tokens: 1, birth: raw }).birth, birth);
assert.equal(normalizeCacheInfo({ input_tokens: 1 }).birth, undefined);

// collapsed: one line, no rows
const closed = renderToStaticMarkup(React.createElement(ChatBirthPanel, { birth }));
assert.match(closed, /出生/);
assert.match(closed, /12\.4s/);
assert.match(closed, /热回合 · 3 段/);
assert.match(closed, /aria-expanded="false"/);
assert.doesNotMatch(closed, /chat-birth-row/);

// open: rows in order, slowest marked, sizes shown
const open = renderToStaticMarkup(React.createElement(ChatBirthPanel, { birth, defaultOpen: true }));
const order = ['载入人设', '上下文核对', '模型开口'].map((label) => open.indexOf(label));
assert.ok(order.every((i) => i >= 0) && order[0] < order[1] && order[1] < order[2], 'rows keep their order');
assert.match(open, /aria-expanded="true"/);
assert.match(open, /is-slowest[^>]*><span class="chat-birth-label">上下文核对/);
assert.match(open, /人设 4\.1k 字/);
assert.match(open, /0\.30s/);
assert.match(open, /8\.00s/);

// nothing to show: no panel
assert.equal(renderToStaticMarkup(React.createElement(ChatBirthPanel, { birth: undefined })), '');

// Chrome78 contract: no flex gap / inset / clamp in the panel's stylesheet
const css = readFileSync(fileURLToPath(new URL('../src/components/ChatBirthPanel.css', import.meta.url)), 'utf8');
assert.doesNotMatch(css.replace(/\/\*[\s\S]*?\*\//g, ''), /(^|[\s;{])gap\s*:|\binset\s*:|clamp\(|100dvh|aspect-ratio/);

console.log('chat birth panel: ok');
