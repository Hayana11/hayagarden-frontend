import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ReactMarkdown from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';
import { installObjectHasOwnCompat } from '../src/lib/objectHasOwnCompat.ts';

function isSafeHref(href) {
  const value = href.trim();
  if (!value) return false;
  try {
    const url = new URL(value, 'https://chat.example.test/dash/chat');
    return ['http:', 'https:', 'mailto:', 'tel:'].includes(url.protocol);
  } catch {
    return false;
  }
}

const components = {
  a({ href, children, node: _node, ...props }) {
    const safeHref = typeof href === 'string' && isSafeHref(href) ? href : null;
    if (!safeHref) return React.createElement('span', { className: 'blocked-link' }, children);
    return React.createElement('a', { ...props, href: safeHref }, children);
  },
};

function render(source) {
  return renderToStaticMarkup(
    React.createElement(
      ReactMarkdown,
      {
        remarkPlugins: [remarkGfm, remarkBreaks],
        disallowedElements: ['img'],
        components,
      },
      source,
    ),
  );
}

const fence = String.fromCharCode(96).repeat(3);
const originalObjectHasOwn = Object.getOwnPropertyDescriptor(Object, 'hasOwn');
try {
  Object.defineProperty(Object, 'hasOwn', {
    configurable: true,
    enumerable: false,
    writable: true,
    value: undefined,
  });
  installObjectHasOwnCompat();
  assert.equal(typeof Object.hasOwn, 'function');
  assert.equal(Object.hasOwn({ a: 1 }, 'a'), true);
  assert.equal(Object.hasOwn({ a: 1 }, 'b'), false);
  const proto = { inherited: 1 };
  const obj = Object.create(proto);
  obj.own = 1;
  assert.equal(Object.hasOwn(obj, 'own'), true);
  assert.equal(Object.hasOwn(obj, 'inherited'), false);
  assert.doesNotThrow(() => render('测试消息'));
  const chrome78Rich = render([
    '**bold**',
    '',
    fence + 'js',
    'const answer = 42',
    fence,
    '',
    '[link](https://example.com)',
    '',
    '| A | B |',
    '| - | - |',
    '| 1 | 2 |',
  ].join('\n'));
  assert.match(chrome78Rich, /<strong>bold<\/strong>/);
  assert.match(chrome78Rich, /<pre><code class="language-js">/);
  assert.match(chrome78Rich, /<a href="https:\/\/example.com">link<\/a>/);
  assert.match(chrome78Rich, /<table>/);
} finally {
  if (originalObjectHasOwn) {
    Object.defineProperty(Object, 'hasOwn', originalObjectHasOwn);
  } else {
    delete Object.hasOwn;
  }
}

const rich = render([
  '**bold** *italic* ~~strike~~ `const x = 1`',
  '',
  `${fence}js`,
  'const x = 1',
  fence,
  '',
  '| A | B |',
  '| - | - |',
  '| 1 | 2 |',
].join('\n'));
assert.match(rich, /<strong>bold<\/strong>/);
assert.match(rich, /<em>italic<\/em>/);
assert.match(rich, /<del>strike<\/del>/);
assert.match(rich, /<code>const x = 1<\/code>/);
assert.match(rich, /<pre><code class="language-js">/);
assert.match(rich, /<table>/);

const singleLine = render('第一行\n第二行\n第三行');
assert.equal((singleLine.match(/<br/g) || []).length, 2);
assert.ok(singleLine.indexOf('第一行') < singleLine.indexOf('第二行'));
assert.ok(singleLine.indexOf('第二行') < singleLine.indexOf('第三行'));

const rawHtml = render('<script>alert(1)</script><img src="x" onerror="alert(1)">');
assert.doesNotMatch(rawHtml, /<(?:script|img)\b|onerror="/i);
assert.match(rawHtml, /&lt;script&gt;/);

const dangerousLinks = render('[javascript](javascript:alert(1)) [data](data:text/html,x)');
assert.doesNotMatch(dangerousLinks, /<a[^>]+href=/i);

const markdownImage = render('![remote image](https://example.com/image.png)');
assert.doesNotMatch(markdownImage, /<img/i);

assert.doesNotThrow(() => render('**unfinished `code [link](https://example.com'));

const chatSource = readFileSync(
  fileURLToPath(new URL('../src/screens/ChatScreen.tsx', import.meta.url)),
  'utf8',
);
const userStart = chatSource.indexOf('function renderUserMsg');
const assistantStart = chatSource.indexOf('function renderAssistantMsg');
assert.ok(userStart >= 0 && assistantStart > userStart);
const userRenderer = chatSource.slice(userStart, assistantStart);
assert.match(userRenderer, /whiteSpace: ['"]pre-wrap['"]/);
assert.doesNotMatch(userRenderer, /renderMarkdown\(/);
assert.match(chatSource, /installObjectHasOwnCompat\(\)/);
assert.doesNotMatch(chatSource, /dangerouslySetInnerHTML|innerHTML/);
const compatSource = readFileSync(
  fileURLToPath(new URL('../src/lib/objectHasOwnCompat.ts', import.meta.url)),
  'utf8',
);
assert.doesNotMatch(compatSource, /Object\.hasOwn\s*=/);
assert.doesNotMatch(compatSource, /\?\?=|\|\|=|&&=|replaceAll|\.at\(|Promise\.any|structuredClone|crypto\.randomUUID/);

console.log('chat markdown focused checks: PASS');

