import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

function read(relativePath) {
  return fs.readFileSync(path.join(root, 'src', relativePath), 'utf8');
}

const client = read('lib/capabilityStates.ts');
const profile = read('screens/ProfileScreen.tsx');
const hints = read('lib/toolCompanionHints.ts');
const css = fs.readFileSync(path.join(root, 'src/screens/ProfileScreen.css'), 'utf8');

assert.match(client, /export function fetchCapabilityStates/);
assert.match(client, /\/api\/capabilities\/states/);
for (const field of [
  'capability_id',
  'static_enabled',
  'runtime_state',
  'effective_enabled',
  'writable',
]) {
  assert.match(client, new RegExp(field));
}
for (const state of ['INHERIT', 'ON', 'OFF', 'DENY']) {
  assert.match(client, new RegExp(state));
}

assert.match(profile, /fetchToolCompanionHints/);
assert.match(profile, /Promise\.allSettled/);
assert.match(profile, /费佳的工具直觉/);
assert.match(profile, /注入预览/);
assert.doesNotMatch(profile, /fetchCapabilityStates/);
assert.doesNotMatch(profile, /profile-tool-card/);
assert.doesNotMatch(profile, /setOpenTools/);
assert.doesNotMatch(profile, /patchCapabilityState|\/api\/capabilities\/[^']+\/state|http\.patch/);
assert.doesNotMatch(profile, /patchToolCompanionHint/);

const persistStart = profile.indexOf('const persist =');
const persistEnd = profile.indexOf('return (');
assert.ok(persistStart >= 0 && persistEnd > persistStart);
assert.doesNotMatch(profile.slice(persistStart, persistEnd), /capabilityState|patchToolCompanionHint/);

assert.match(hints, /\/api\/tools\/companion-hints/);
assert.match(hints, /patchToolCompanionHint/);
assert.doesNotMatch(css, /profile-capability-state-badge/);
assert.doesNotMatch(css, /profile-tool-card/);

console.log('test-capability-state-profile: ok');
