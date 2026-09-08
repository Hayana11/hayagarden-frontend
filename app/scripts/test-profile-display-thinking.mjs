import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const profile = fs.readFileSync(path.join(root, 'src/screens/ProfileScreen.tsx'), 'utf8');
const css = fs.readFileSync(path.join(root, 'src/screens/ProfileScreen.css'), 'utf8');
const api = fs.readFileSync(path.join(root, 'src/lib/displayThinking.ts'), 'utf8');
const app = fs.readFileSync(path.join(root, '..', 'app.py'), 'utf8');
const indexHtml = fs.readFileSync(path.join(root, 'index.html'), 'utf8');

const persona = profile.indexOf('profile-section-title">PROFILE');
const thinking = profile.indexOf('profile-section-title">think');
const tools = profile.indexOf('profile-section-title">TOOL');
assert.ok(persona >= 0 && thinking > persona && tools > thinking, 'profile order must be PROFILE -> think -> TOOL');
assert.match(profile, /aria-label="可见思绪 prompt"/);
assert.match(profile, /恢复默认/);
assert.match(profile, /完整人设所用的token/);
assert.match(profile, /estimatePersonaTokens/);
assert.match(profile, /aria-label="保存费佳档案"/);
assert.match(profile, /\{saving \? '…' : '✓'\}/);
assert.doesNotMatch(profile, /身份 · 关系 · 工具直觉/);
assert.doesNotMatch(profile, /工具试用/);
assert.doesNotMatch(profile, /profile-tool-card-toggle/);
assert.doesNotMatch(profile, /profile-tool-groups/);
assert.match(profile, /prompt_preview/);
assert.match(profile, /当前注入预览/);
assert.match(api, /DISPLAY_THINKING_PROMPT/);
assert.ok(api.includes('/api/profile/display-thinking-prompt'));
const routeStart = app.indexOf("@app.route('/api/profile/display-thinking-prompt'");
const routeEnd = app.indexOf('# ── User Profile', routeStart);
assert.ok(routeStart >= 0 && routeEnd > routeStart, 'display-thinking profile route must exist');
assert.equal(app.slice(routeStart, routeEnd).includes('persona.md'), false, 'display-thinking route must not edit persona.md');

assert.match(css, /font-family:\s*'JetBrains Mono',\s*monospace/);
assert.match(css, /color:\s*#B76E79/);
assert.match(indexHtml, /family=JetBrains\+Mono/);
console.log('profile display-thinking contract: PASS');
