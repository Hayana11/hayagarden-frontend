import assert from 'node:assert/strict';
import fs from 'node:fs';

const css = fs.readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

function blockFor(selector) {
  const escaped = selector.replace(/[.:]/g, '\\$&');
  const pattern = new RegExp('(?:^|\\n)\\s*' + escaped + '\\s*\\{');
  const match = pattern.exec(css);
  assert.ok(match, 'missing CSS selector: ' + selector);

  let depth = 0;
  const start = match.index + match[0].lastIndexOf('{');
  for (let index = start; index < css.length; index += 1) {
    if (css[index] === '{') depth += 1;
    if (css[index] === '}') {
      depth -= 1;
      if (depth === 0) return css.slice(start, index + 1);
    }
  }
  assert.fail('unterminated CSS block: ' + selector);
}

const chatRoot = blockFor('.chat-root');
const haze = blockFor('.chat-root::before');

assert.match(chatRoot, /position\s*:\s*relative\b/);
assert.match(chatRoot, /z-index\s*:\s*0\b/);
assert.match(haze, /z-index\s*:\s*-1\b/);
assert.match(haze, /pointer-events\s*:\s*none\b/);
assert.match(haze, /background\s*:/);

for (const variable of [
  '--haze-left-x',
  '--haze-left-y',
  '--haze-right-x',
  '--haze-right-y',
  '--haze-low-x',
  '--haze-low-y',
]) {
  assert.ok(css.includes(variable), 'missing haze variable: ' + variable);
}

console.log('test:chat-haze-stacking — all checks passed');