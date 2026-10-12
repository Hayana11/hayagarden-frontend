import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createFlowStudioApiAdapter } from '../src/lib/flowStudio/flowStudioApi.ts';
import {
  createFlowStudioMockAdapter,
  FLOW_STUDIO_GLOBAL_GUIDANCE_MAX_CHARS,
} from '../src/lib/flowStudio/flowStudioMock.ts';
import {
  clampDockTop,
  clampDragLeft,
  defaultDockPosition,
  defaultPreviewStageId,
  dockedLeft,
  drawablePoolsForStage,
  nearestDockSide,
  parseStoredDock,
  previewDraw,
  serializeDock,
} from '../src/lib/flowStudio/flowStudioPreviewDraw.ts';

// ── Preview draw: draft only, disabled pools skipped, never mutates ──
const draft = {
  version: 0,
  savedAt: '',
  enabled: true,
  initialStage: 's1',
  guidancePrompt: '',
  stages: [
    { id: 's0', name: '停用', minTurns: 1, terminal: false, poolIds: ['p1'], nextStageId: null, enabled: false },
    { id: 's1', name: '起点', minTurns: 1, terminal: false, poolIds: ['p1', 'p2', 'p1', 'missing'], nextStageId: null },
    { id: 's2', name: '空', minTurns: 1, terminal: false, poolIds: ['p2'], nextStageId: null },
    { id: 's3', name: '终', minTurns: 1, terminal: true, poolIds: ['p3'], nextStageId: null },
  ],
  pools: [
    { id: 'p1', name: 'MENTAL', enabled: true, mode: 'perTurn', count: 2, entries: [
      { id: 'a', text: 'A', enabled: true },
      { id: 'b', text: 'B', enabled: true },
      { id: 'c', text: 'C', enabled: false },
      { id: 'd', text: '   ', enabled: true },
    ] },
    { id: 'p2', name: 'OFF', enabled: false, mode: 'perTurn', count: 1, entries: [{ id: 'x', text: 'X', enabled: true }] },
    { id: 'p3', name: 'PROPS', enabled: true, mode: 'perStage', count: 5, entries: [{ id: 'y', text: 'Y', enabled: true }] },
  ],
  cues: [{ id: 'cue', key: 'k', kind: '地点', enabled: false, poolIds: ['p3'] }],
  dimensions: [],
};
const before = JSON.stringify(draft);

assert.deepEqual(drawablePoolsForStage(draft, 's1').map((pool) => pool.id), ['p1']);
const first = previewDraw(draft, 's1', 'seed-1');
assert.equal(first.groups.length, 1);
assert.equal(first.groups[0].poolName, 'MENTAL');
assert.deepEqual([...first.groups[0].picks].sort(), ['A', 'B']);
assert.deepEqual(first.groups[0].candidates, ['A', 'B']);
assert.deepEqual(previewDraw(draft, 's1', 'seed-1'), first, 'same preview seed is reproducible');
assert.equal(previewDraw(draft, 's2', 'seed').groups.length, 0, 'only disabled pools → graceful empty');
assert.equal(previewDraw(draft, 'nope', 'seed').groups.length, 0);
assert.deepEqual(previewDraw(draft, 's3', 'seed').groups[0].picks, ['Y'], 'count never exceeds entries');
assert.equal(JSON.stringify(draft), before, 'preview draw must not mutate the draft');

assert.equal(defaultPreviewStageId(draft, null), 's1', 'idle → initial stage');
assert.equal(defaultPreviewStageId(draft, 's3'), 's3', 'active → active stage');
assert.equal(defaultPreviewStageId(draft, 's0'), 's1', 'disabled active stage falls back');
assert.equal(defaultPreviewStageId({ ...draft, initialStage: null }, null), 's1');

// ── Dock math ──
const bounds = {
  viewportWidth: 360,
  viewportHeight: 740,
  chipWidth: 64,
  chipHeight: 40,
  safeTop: 24,
  safeBottom: 20,
  reservedBottom: 92,
  margin: 10,
};
assert.equal(nearestDockSide(20, bounds), 'left');
assert.equal(nearestDockSide(250, bounds), 'right');
assert.equal(dockedLeft('left', bounds), 10);
assert.equal(dockedLeft('right', bounds), 360 - 64 - 10);
assert.equal(clampDockTop(-500, bounds), 34, 'respects safe-area top');
assert.equal(clampDockTop(5000, bounds), 740 - 20 - 92 - 10 - 40, 'never covers save bar or safe area');
assert.equal(clampDragLeft(-50, bounds), 10);
assert.equal(clampDragLeft(9999, bounds), 360 - 64 - 10);
const fallback = defaultDockPosition(bounds);
assert.equal(fallback.side, 'right');
assert.ok(fallback.top > 740 / 2, 'default sits in the lower half');
const stored = serializeDock({ side: 'left', top: 300 }, bounds);
const restored = parseStoredDock(stored, bounds);
assert.equal(restored.side, 'left');
assert.ok(Math.abs(restored.top - 300) < 1, 'ratio round-trip is within a pixel');
assert.equal(parseStoredDock('{"side":"up","topRatio":0.3}', bounds), null);
assert.equal(parseStoredDock('{"side":"left","topRatio":7}', bounds), null);
assert.equal(parseStoredDock('not json', bounds), null);
assert.equal(parseStoredDock(null, bounds), null);

// ── API adapter: runtime summary, progress phase, guidance default, no model calls ──
const requests = [];
const editorPayload = (extra = {}) => ({
  ok: true,
  document: {
    enabled: true,
    stages: [{ id: 's1', name: '起点', minTurns: 1, terminal: true, poolIds: [], nextStageId: null }],
    pools: [],
    cues: [],
    dimensions: [],
  },
  editorRevision: 3,
  savedAt: 'now',
  runtime: { version: 9, engineEnabled: true, published: true, configDigest: 'd', ...extra },
});
const queue = [
  editorPayload({ activeFlow: { state: 'active', stage: 's1', stageTurn: 2, contextKeys: ['leak'] } }),
  editorPayload({ published: false }),
  { ok: true, valid: true, publishable: true, errors: [] },
  { ok: true, runtime: { version: 10, configDigest: 'd2' } },
  editorPayload({ version: 10, configDigest: 'd2', activeFlow: { state: 'idle' } }),
];
globalThis.fetch = async (url, init = {}) => {
  requests.push({ url: String(url), method: init.method || 'GET' });
  const body = queue.shift();
  return { ok: true, status: 200, json: async () => body };
};
const adapter = createFlowStudioApiAdapter('flow-x');
const loaded = await adapter.load();
assert.equal(loaded.guidancePrompt, '', 'missing guidance defaults to empty');
assert.deepEqual(adapter.getRuntimeStatus(), {
  engineEnabled: true,
  activeFlow: { state: 'active', stage: 's1', stageTurn: 2 },
});
assert.equal(JSON.stringify(adapter.getRuntimeStatus()).includes('leak'), false);

const phases = [];
await adapter.save({ ...loaded, guidancePrompt: '基础' }, {
  onProgress: (phase) => phases.push([phase, requests.length]),
});
assert.deepEqual(phases, [['editorSaved', 2]], 'progress fires right after PUT, before validate/publish');
assert.equal(adapter.getRuntimeStatus().activeFlow.state, 'idle');
assert.deepEqual(requests.map((item) => item.method), ['GET', 'PUT', 'POST', 'POST', 'GET']);
assert.ok(requests.every((item) => item.url.startsWith('/api/flow-studio/editor/')), 'no model or runtime-store requests');

// ── Mock adapter (preview) reports progress and stays local ──
const mock = createFlowStudioMockAdapter('preview');
const mockPhases = [];
await mock.save(mock.load(), { onProgress: (phase) => mockPhases.push(phase) });
assert.deepEqual(mockPhases, ['editorSaved']);
assert.equal(mock.getRuntimeStatus().engineEnabled, true);
assert.equal(typeof mock.load().guidancePrompt, 'string');
assert.equal(FLOW_STUDIO_GLOBAL_GUIDANCE_MAX_CHARS, 5000);

// ── Source contracts ──
const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const workspace = read('../src/screens/FlowStudioWorkspace.tsx');
const dock = read('../src/screens/FlowStudioDrawDock.tsx');
const drawLib = read('../src/lib/flowStudio/flowStudioPreviewDraw.ts');
const styles = read('../src/screens/FlowStudioSoftGlowScreen.css');

for (const oldCopy of [
  'CURRENT SERVER STATE', 'CURRENT MOCK STATE', 'Standby', '本地 Mock', '不调用模型', 'Draft v',
  '正式页面', '生产隔离', '会话记录', '没有连接真实聊天', '只改变本地 Mock 草稿', '编辑修订',
]) {
  assert.equal(workspace.includes(oldCopy), false, `old overview copy must be gone: ${oldCopy}`);
}
assert.match(workspace, /flow-tab-pill/);
assert.match(workspace, /aria-selected=\{tab === key\}/);
assert.match(workspace, /已关闭/);
assert.match(workspace, /空闲/);
assert.match(workspace, /运行中/);
assert.match(workspace, /is-current/);
assert.match(workspace, /is-flowing/);
assert.match(workspace, /全局提示词/);
assert.match(workspace, /应用于所有阶段的基础引导/);
assert.match(workspace, /useState\(false\);\n  const \[guidanceEditing/, 'guidance is collapsed by default');
assert.match(workspace, /maxLength=\{FLOW_STUDIO_GLOBAL_GUIDANCE_MAX_CHARS\}/);
assert.match(workspace, /next\.guidancePrompt = event\.target\.value/, 'guidance edits go through the shared draft/undo');
assert.equal((workspace.match(/onClick=\{saveDraft\}/g) || []).length, 1, 'exactly one global save');
assert.match(workspace, /<FlowStudioDrawDock/);
const overviewJsx = workspace.slice(workspace.indexOf("{tab === 'overview' ?"), workspace.indexOf("{tab === 'stages' ?"));
assert.ok(overviewJsx.length > 200);
assert.doesNotMatch(overviewJsx, /configDigest|editorRevision|runtimeVersion|\bCAS\b|lease|snapshot|token/i, 'no technical state on the overview');

for (const source of [dock, drawLib]) {
  assert.doesNotMatch(source, /fetch\(|\/api\/|XMLHttpRequest|sendBeacon/, 'test draw never calls the server or model');
  assert.doesNotMatch(source, /hidden_flow_runtime|message_snapshots|fixedDraws|FlowState/);
}
assert.match(dock, /localStorage/);
assert.match(dock, /prefers-reduced-motion/);
assert.match(dock, /仅预览 · 不影响真实 Flow/);
assert.match(dock, /这个阶段目前没有可抽取的灵感/);
assert.match(dock, /setPointerCapture/);
assert.match(styles, /prefers-reduced-motion:reduce/);
assert.match(styles, /\.flow-draw-chip\{position:fixed/);
const consoleCss = styles.slice(styles.indexOf('Overview Console R1'));
assert.doesNotMatch(consoleCss, /inset:|[^-]min\(/, 'Chrome 78 WebView compatible CSS');
assert.doesNotMatch(consoleCss, /background:#fff[;}]|background:white/, 'no pure white blocks');

console.log('flow studio overview console, preview draw, dock and save-progress checks passed');
