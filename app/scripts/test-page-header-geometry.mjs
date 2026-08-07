import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const src = path.join(root, 'src');

function read(rel) {
  return fs.readFileSync(path.join(src, rel), 'utf8');
}

function toolbarOpenLine(screenPath) {
  const body = read(screenPath);
  const idx = body.indexOf('page-header-toolbar');
  assert.ok(idx >= 0, `${screenPath} must use page-header-toolbar`);
  return body.slice(idx, body.indexOf('\n', idx));
}

// 1. PageHeader — explicit back→title spacing survives title margin reset
{
  const css = fs.readFileSync(path.join(src, 'index.css'), 'utf8');
  const block = css.slice(css.indexOf('/* Canonical page header'));
  const titleRuleIdx = block.indexOf('.page-header__title');
  const backTitleRuleIdx = block.indexOf('.page-header > .page-header__back + .page-header__title');
  assert.ok(titleRuleIdx >= 0, 'expected .page-header__title rule');
  assert.ok(backTitleRuleIdx > titleRuleIdx, 'back→title spacing must come after title margin reset');
  assert.match(
    block,
    /\.page-header > \.page-header__back \+ \.page-header__title\s*\{[\s\S]*?margin-left:\s*14px/,
    'back→title contract must use explicit semantic selector',
  );
  assert.doesNotMatch(
    block,
    /\.page-header\s*>\s*\*\s*\+\s*\*/,
    'generic sibling selector must not define page-header spacing',
  );
  assert.doesNotMatch(block, /\.page-header\s*\{[^}]*\bgap\s*:/);
  assert.match(block, /\.page-header__back[\s\S]*?width:\s*38px/);
  assert.match(block, /\.page-header__title\s*\{[\s\S]*?margin:\s*0/);
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

// 5. Chat / Codex toolbar — Chrome78-safe sibling margins (Codex compat fix)
{
  const css = fs.readFileSync(path.join(src, 'index.css'), 'utf8');
  const block = css.slice(css.indexOf('.page-header-toolbar'));
  assert.match(block, /\.page-header-toolbar\s*>\s*\*\s*\+\s*\*\s*\{[\s\S]*?margin-left:\s*10px/);
  assert.doesNotMatch(block, /\.page-header-toolbar\s*\{[^}]*\bgap\s*:/);
  assert.match(block, /\.page-header-toolbar\s*\{[\s\S]*?padding:\s*10px 12px 9px/);
}

for (const screen of ['screens/ChatScreen.tsx', 'screens/CodexChatScreen.tsx']) {
  const line = toolbarOpenLine(screen);
  assert.doesNotMatch(line, /\bgap\s*:/, `${screen} toolbar row must not use inline flex gap`);
}

// 6. Contacts — unchanged geometry; still hstack-10, not shared toolbar
{
  const contacts = read('screens/ContactsScreen.tsx');
  assert.doesNotMatch(contacts, /page-header-toolbar/);
  assert.match(contacts, /hstack hstack-10/);
  assert.match(contacts, /padding:\s*'14px 16px 12px'/);
}

console.log('test-page-header-geometry: ok');
