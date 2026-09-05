import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const profile = fs.readFileSync(path.join(root, 'src/screens/ProfileScreen.tsx'), 'utf8');
const api = fs.readFileSync(path.join(root, 'src/lib/displayThinking.ts'), 'utf8');
const app = fs.readFileSync(path.join(root, '..', 'app.py'), 'utf8');

const persona = profile.indexOf('费佳的完整人设');
const thinking = profile.indexOf('可见思绪');
const tools = profile.indexOf('费佳的工具直觉');
assert.ok(persona >= 0 && thinking > persona && tools > thinking, 'profile order must be persona -> thinking -> tools');
assert.match(profile, /aria-label="可见思绪 prompt"/);
assert.match(profile, /恢复默认/);
assert.match(api, /DISPLAY_THINKING_PROMPT/);
assert.ok(api.includes('/api/profile/display-thinking-prompt'));
const routeStart = app.indexOf("@app.route('/api/profile/display-thinking-prompt'");
const routeEnd = app.indexOf('# ── User Profile', routeStart);
assert.ok(routeStart >= 0 && routeEnd > routeStart, 'display-thinking profile route must exist');
assert.equal(app.slice(routeStart, routeEnd).includes('persona.md'), false, 'display-thinking route must not edit persona.md');
console.log('profile display-thinking contract: PASS');
