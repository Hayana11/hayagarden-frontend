import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const appRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const read = (relativePath) => fs.readFileSync(path.join(appRoot, 'src', relativePath), 'utf8');
const moments = read('screens/MomentsScreen.tsx');
const capabilityStates = read('lib/capabilityStates.ts');
const canonicalStart = moments.indexOf('/* canonical capability controls:');
const legacyStart = moments.indexOf('/* legacy inventory:');
assert.ok(canonicalStart >= 0 && legacyStart > canonicalStart);
const canonical = moments.slice(canonicalStart, legacyStart);
const legacy = moments.slice(legacyStart);

assert.match(capabilityStates, /fetchCapabilityStates/);
assert.match(capabilityStates, /patchCapabilityState/);
assert.match(capabilityStates, /\/api\/capabilities\/\$\{encodedId\}\/state/);
assert.match(capabilityStates, /typeof enabled !== 'boolean'/);
assert.match(capabilityStates, /isRecord\(payload\.state\)/);
assert.match(capabilityStates, /parseCapabilityState\(payload\.state\)/);

assert.match(moments, /fetchToolCompanionHints/);
assert.match(moments, /fetchCapabilityStates/);
assert.match(moments, /Promise\.allSettled/);
assert.match(moments, /ensureOwner/);
assert.match(moments, /patchCapabilityState/);
assert.match(moments, /err instanceof HttpError && err\.status === 401/);
assert.match(moments, /authenticated: false/);
assert.match(moments, /setCapabilityStates\(\(current\) => current\.map/);

assert.match(canonical, /capability_id/);
assert.match(canonical, /tool\.capability_id/);
assert.doesNotMatch(canonical, /tool_name/);
assert.match(canonical, /data-capability-id/);
assert.match(canonical, /effective_enabled/);
for (const label of ['默认开启', '已开启', '已关闭', '不可用', '状态未知', '当前不可确认']) {
  assert.match(canonical, new RegExp(label));
}
assert.match(canonical, /disabled=\{!canToggle \|\| pending\}/);
assert.match(canonical, /role="switch"/);
assert.match(canonical, /toggleCapability/);

assert.match(legacy, /tool\.tool_name/);
assert.doesNotMatch(legacy, /toggleCapability|patchCapabilityState|role="switch"/);

console.log('test-capability-state-moments: ok');
