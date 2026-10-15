import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { flowStudioStatusTone, flowSwitchClassName } from '../src/lib/flowStudio/flowStudioStatus.ts';

const active = { state: 'active', stage: 'stage-3', stageTurn: 2 };
const idle = { state: 'idle', stage: null, stageTurn: null };
const unknown = { state: 'unknown', stage: null, stageTurn: null };

assert.equal(flowStudioStatusTone({ engineEnabled: null, activeFlow: unknown }), 'unknown');
assert.equal(flowStudioStatusTone({ engineEnabled: true, activeFlow: idle }), 'idle');
assert.equal(flowStudioStatusTone({ engineEnabled: true, activeFlow: active }), 'running');
assert.equal(flowStudioStatusTone({ engineEnabled: false, activeFlow: idle }), 'off');
assert.equal(flowSwitchClassName(true, 'enabled'), 'flow-switch is-on is-enabled');
assert.equal(flowSwitchClassName(false, 'enabled'), 'flow-switch is-enabled');

const workspace = await readFile(new URL('../src/screens/FlowStudioWorkspace.tsx', import.meta.url), 'utf8');
const css = await readFile(new URL('../src/screens/FlowStudioSoftGlowScreen.css', import.meta.url), 'utf8');
assert.match(workspace, /<Switch checked=\{draft\.enabled\} tone="enabled"/);
assert.match(css, /\.flow-switch\.is-enabled\.is-on\{background:var\(--flow-on\)\}/);
assert.match(css, /\.flow-switch\{[^}]*background:#decfd2/);
assert.match(css, /\.flow-console-card\.is-idle \.flow-state-dot i[^}]*background:var\(--flow-on\)/);
assert.match(css, /\.flow-console-card\.is-off \.flow-state-dot i[^}]*background:var\(--flow-off\)/);
assert.match(css, /\.flow-state-dot i\{[^}]*background:var\(--flow-rose-gray\)/);

console.log('flow studio status semantics: 4 runtime cases + toggle color contract passed');
