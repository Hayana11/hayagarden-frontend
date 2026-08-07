import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const src = path.join(root, 'src');

function read(rel) {
  return fs.readFileSync(path.join(src, rel), 'utf8');
}

// 1. PageHeader contract — Chrome78-safe sibling margins, no flex gap
{
  const css = fs.readFileSync(path.join(src, 'index.css'), 'utf8');
  const block = css.slice(css.indexOf('/* Canonical page header'));
  assert.match(block, /\.page-header\s*\{[\s\S]*?display:\s*flex/);
  assert.match(block, /\.page-header\s*>\s*\*\s*\+\s*\*\s*\{[\s\S]*?margin-left:\s*14px/);
  assert.doesNotMatch(block, /\.page-header\s*\{[^}]*\bgap\s*:/);
  assert.match(block, /\.page-header__back[\s\S]*?width:\s*38px/);
}

// 2. BackHeader delegates to PageHeader
{
  const back = read('components/BackHeader.tsx');
  assert.match(back, /from '\.\/PageHeader'/);
  assert.match(back, /<PageHeader/);
  assert.doesNotMatch(back, /gap:\s*14/);
}

// 3. Usage quota title row — dot slot / title block / status pill
{
  const usage = read('screens/UsageScreen.tsx');
  const css = fs.readFileSync(path.join(src, 'index.css'), 'utf8');
  const headingBlock = css.slice(css.indexOf('.usage-agent-heading'));
  assert.match(usage, /usage-agent-dot-slot/);
  assert.match(usage, /usage-agent-title-block/);
  assert.match(usage, /usage-agent-heading[\s\S]*<em className=/);
  assert.doesNotMatch(headingBlock, /\.usage-agent-heading\s*\{[^}]*\bgap\s*:/);
  assert.match(headingBlock, /\.usage-agent-heading\s*>\s*\*\s*\+\s*\*\s*\{[\s\S]*?margin-left:\s*11px/);
}

// 4. Settings uses shared PageHeader
{
  const settings = read('screens/SettingsScreen.tsx');
  assert.match(settings, /<PageHeader[\s\S]*title="系统配置"/);
}

console.log('test-page-header-geometry: ok');
