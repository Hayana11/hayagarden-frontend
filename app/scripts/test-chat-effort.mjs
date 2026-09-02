import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const api = fs.readFileSync(path.join(root, 'src/lib/api.ts'), 'utf8');
const screen = fs.readFileSync(path.join(root, 'src/screens/ChatScreen.tsx'), 'utf8');

assert.match(api, /export type ChatEffortMode = 'default' \| 'explicit' \| 'unavailable' \| 'unknown';/);
assert.match(api, /getChatEffort\(\)/);
assert.match(api, /setChatEffort\(effort: string \| null\)/);
assert.match(api, /'\/api\/config\/effort'/);
assert.match(api, /effortMode: 'unknown'/);
assert.match(screen, /思考强度/);
assert.match(screen, /不传 --effort/);
assert.match(screen, /--effort \{effort\}/);
assert.match(screen, /chatProvider === 'claude_code'/);

for (const forbidden of ['Array.prototype.at', 'Object.hasOwn', 'replaceAll', 'structuredClone']) {
  assert.equal(api.includes(forbidden) || screen.includes(forbidden), false, `Chrome 78 builtin: ${forbidden}`);
}

console.log('chat effort frontend contract: PASS');

