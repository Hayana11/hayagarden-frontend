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

// 5. Shared page-header-toolbar CSS — sibling margins, no flex gap
{
  const css = fs.readFileSync(path.join(src, 'index.css'), 'utf8');
  const block = css.slice(css.indexOf('.page-header-toolbar'));
  assert.match(block, /\.page-header-toolbar\s*>\s*\*\s*\+\s*\*\s*\{[\s\S]*?margin-left:\s*10px/);
  assert.doesNotMatch(block, /\.page-header-toolbar\s*\{[^}]*\bgap\s*:/);
  assert.match(block, /\.page-header-toolbar\s*\{[\s\S]*?padding:\s*10px 12px 9px/);
}

// 6. Chat / Codex / Contacts — toolbar adoption; top row must not inline flex gap
for (const screen of ['screens/ChatScreen.tsx', 'screens/CodexChatScreen.tsx', 'screens/ContactsScreen.tsx']) {
  const line = toolbarOpenLine(screen);
  assert.doesNotMatch(line, /\bgap\s*:/, `${screen} toolbar row must not use inline flex gap`);
}

{
  const contacts = read('screens/ContactsScreen.tsx');
  assert.match(contacts, /page-header-toolbar__title[\s\S]*通讯录/);
  assert.match(contacts, /page-header-toolbar__aside/);
}

// 7. Profile header intentional geometry contract (38px buttons / 44px min-height)
{
  const profileCss = read('screens/ProfileScreen.css');
  assert.match(profileCss, /\.profile-header\s*\{[\s\S]*?grid-template-columns:\s*38px 1fr 38px/);
  assert.match(profileCss, /\.profile-header\s*\{[\s\S]*?min-height:\s*44px/);
  assert.match(
    profileCss,
    /\.profile-round-button[\s\S]*?width:\s*38px[\s\S]*?height:\s*38px/,
  );
}

console.log('test-page-header-geometry: ok');
