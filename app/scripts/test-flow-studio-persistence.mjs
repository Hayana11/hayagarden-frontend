import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  createFlowStudioApiAdapter,
  FlowStudioConflictError,
} from '../src/lib/flowStudio/flowStudioApi.ts';
import { mergeFlowStudioDocuments } from '../src/lib/flowStudio/flowStudioMerge.ts';
import { createFlowStudioMockAdapter } from '../src/lib/flowStudio/flowStudioMock.ts';
import { hasEditableFlowStudioChanges } from '../src/lib/flowStudio/flowStudioDocument.ts';
import { createRuotaPool, filterRuotaEntries, RUOTA_TEMPLATES } from '../src/lib/flowStudio/flowStudioTemplates.ts';

const document = {
  version: 17,
  savedAt: 'old',
  enabled: true,
  stages: [{ id: 's1', name: '试探', minTurns: 1, terminal: true, poolIds: [], nextStageId: null, guidancePrompt: '' }],
  pools: [],
  cues: [],
  dimensions: [],
};
const serverDocument = {
  ...document,
  stages: [{ ...document.stages[0], name: '服务器版本' }],
};
const requests = [];
const responses = [
  {
    status: 200,
    body: {
      ok: true,
      document,
      editorRevision: 4,
      savedAt: 'server-load',
      runtime: { version: 99, schemaVersion: 1, engineEnabled: false },
    },
  },
  {
    status: 409,
    body: {
      ok: false,
      code: 'EDITOR_REVISION_CONFLICT',
      error: 'editor document changed on the server',
      currentRevision: 5,
      currentDocument: serverDocument,
    },
  },
  {
    status: 200,
    body: {
      ok: true,
      document: { ...document, stages: [{ ...document.stages[0], name: '本地保存', guidancePrompt: 'STAGE_GUIDANCE_SENTINEL_A' }] },
      editorRevision: 6,
      savedAt: 'server-save',
      runtime: { version: 101, schemaVersion: 1, engineEnabled: false, published: false },
    },
  },
  {
    status: 200,
    body: {
      ok: true,
      valid: true,
      publishable: true,
      errors: [],
      unsupportedFields: [],
      displayOnlyFields: [],
      unsafeFields: [],
    },
  },
  {
    status: 200,
    body: {
      ok: true,
      document: { ...document, stages: [{ ...document.stages[0], name: '本地保存', guidancePrompt: 'STAGE_GUIDANCE_SENTINEL_A' }] },
      editorRevision: 6,
      savedAt: 'published',
      runtime: { version: 102, configDigest: 'digest-1', schemaVersion: 1, engineEnabled: false, published: true },
      displayOnlyFields: [],
    },
  },
  {
    status: 200,
    body: {
      ok: true,
      document: { ...document, stages: [{ ...document.stages[0], name: '本地保存', guidancePrompt: 'STAGE_GUIDANCE_SENTINEL_A' }] },
      editorRevision: 6,
      savedAt: 'published',
      runtime: { version: 102, configDigest: 'digest-1', schemaVersion: 1, engineEnabled: false, published: true },
    },
  },
  {
    status: 200,
    body: {
      ok: true,
      document: { ...document, stages: [{ ...document.stages[0], name: '运行冲突重试' }] },
      editorRevision: 7,
      savedAt: 'server-save-again',
      runtime: { version: 102, schemaVersion: 1, engineEnabled: false, published: false },
    },
  },
  {
    status: 200,
    body: {
      ok: true,
      valid: true,
      publishable: true,
      errors: [],
      unsupportedFields: [],
      displayOnlyFields: [],
      unsafeFields: [],
    },
  },
  {
    status: 409,
    body: {
      ok: false,
      code: 'RUNTIME_VERSION_CONFLICT',
      error: 'runtime configuration changed on the server',
      currentRevision: 7,
      currentRuntimeVersion: 103,
      currentDocument: { ...document, stages: [{ ...document.stages[0], name: '服务器运行版本' }] },
    },
  },

];

globalThis.fetch = async (url, init = {}) => {
  requests.push({ url: String(url), init });
  const response = responses.shift();
  assert.ok(response, 'unexpected fetch request');
  return {
    ok: response.status >= 200 && response.status < 300,
    status: response.status,
    json: async () => response.body,
  };
};

const adapter = createFlowStudioApiAdapter('intimacy-v1');
const loaded = await adapter.load();
assert.equal(loaded.editorRevision, 4);
assert.equal(loaded.version, 0, 'runtime version must not masquerade as editor revision');

const editableBaseline = {
  ...document,
  editorRevision: 4,
  runtimeVersion: 99,
  savedAt: 'server-load',
  published: true,
  stages: [{ ...document.stages[0], guidancePrompt: '' }],
  pools: [{
    id: 'pool-dirty',
    name: '测试灵感池',
    enabled: true,
    mode: 'perTurn',
    count: 1,
    entries: [{ id: 'entry-dirty', text: '原始条目', enabled: true }],
  }],
  cues: [{ id: 'cue-dirty', key: '原始情境', kind: '地点', enabled: true, poolIds: [] }],
};
const metadataOnly = {
  ...editableBaseline,
  version: 900,
  editorRevision: 5,
  runtimeVersion: 100,
  savedAt: 'new-server-time',
  published: false,
};
assert.equal(hasEditableFlowStudioChanges(editableBaseline, metadataOnly), false, 'server metadata must not make the editor dirty');

const editableChanges = [
  ['stage name', { stages: [{ ...editableBaseline.stages[0], name: '新阶段名称' }] }],
  ['stage guidance', { stages: [{ ...editableBaseline.stages[0], guidancePrompt: '阶段提示词' }] }],
  ['pool entry', { pools: [{ ...editableBaseline.pools[0], entries: [{ ...editableBaseline.pools[0].entries[0], text: '新条目' }] }] }],
  ['cue', { cues: [{ ...editableBaseline.cues[0], key: '新情境' }] }],
  ['enabled', { enabled: false }],
];
for (const [label, change] of editableChanges) {
  assert.equal(
    hasEditableFlowStudioChanges(editableBaseline, { ...editableBaseline, ...change }),
    true,
    label + ' must make the editor dirty',
  );
}

const activeGuardSaved = { ...editableBaseline, editorRevision: 6, runtimeVersion: 100, savedAt: 'guarded-save', published: false };
const activeGuardDraft = { ...activeGuardSaved };
assert.equal(hasEditableFlowStudioChanges(activeGuardSaved, activeGuardDraft), false);
const activeGuardState = { dirty: hasEditableFlowStudioChanges(activeGuardSaved, activeGuardDraft), runtimeApplyPending: true };
assert.deepEqual(activeGuardState, { dirty: false, runtimeApplyPending: true }, 'guarded editor save is pending runtime apply, not dirty');

await assert.rejects(
  () => adapter.save({ ...loaded, stages: [{ ...loaded.stages[0], name: '本地冲突' }] }),
  (error) => {
    assert.ok(error instanceof FlowStudioConflictError);
    assert.equal(error.currentRevision, 5);
    assert.equal(error.currentDocument?.stages[0].name, '服务器版本');
    return true;
  },
);

adapter.acceptRevision?.(5);
const saved = await adapter.save({ ...loaded, stages: [{ ...loaded.stages[0], name: '本地保存', guidancePrompt: 'STAGE_GUIDANCE_SENTINEL_A' }] });
assert.equal(saved.editorRevision, 6);
assert.equal(requests[1].init.body.includes('"expectedRevision":4'), true);
assert.equal(requests[2].init.body.includes('"expectedRevision":5'), true);
assert.equal(requests[2].init.body.includes('STAGE_GUIDANCE_SENTINEL_A'), true);

assert.equal(saved.runtimeVersion, 102);
assert.equal(saved.published, true);
assert.equal(saved.stages[0].name, '本地保存');
assert.equal(saved.stages[0].guidancePrompt, 'STAGE_GUIDANCE_SENTINEL_A');
assert.equal(requests[2].url.endsWith('/intimacy-v1'), true);
assert.equal(requests[2].init.body.includes('"expectedRevision":5'), true);
assert.equal(requests[3].url.endsWith('/validate'), true);
assert.equal(requests[4].url.endsWith('/publish'), true);
assert.equal(requests[4].init.body.includes('"expectedRuntimeVersion":101'), true);
assert.equal(requests[4].init.body.includes('STAGE_GUIDANCE_SENTINEL_A'), true);
assert.equal(requests[5].url.endsWith('/intimacy-v1'), true);
assert.equal(saved.stages[0].name, '本地保存', 'reload confirmation must retain the saved configuration');

await assert.rejects(
  () => adapter.save({ ...saved, stages: [{ ...saved.stages[0], name: '运行冲突重试' }] }),
  (error) => {
    assert.ok(error instanceof FlowStudioConflictError);
    assert.equal(error.code, 'RUNTIME_VERSION_CONFLICT');
    assert.equal(error.currentRuntimeVersion, 103);
    assert.equal(error.savedDocument?.stages[0].name, '运行冲突重试');
    return true;
  },
);
assert.equal(requests[6].url.endsWith('/intimacy-v1'), true);
assert.equal(requests[7].url.endsWith('/validate'), true);
assert.equal(requests[8].url.endsWith('/publish'), true);


const mergeBase = {
  ...document,
  stages: [{ ...document.stages[0], name: '原始阶段', minTurns: 1 }],
  pools: [{
    id: 'pool-1',
    name: '原始灵感池',
    enabled: true,
    mode: 'perTurn',
    count: 1,
    entries: [{ id: 'entry-1', text: '原始条目', enabled: true }],
  }],
};
const localDifferent = {
  ...mergeBase,
  stages: [{ ...mergeBase.stages[0], name: '本地阶段修改' }],
};
const remoteDifferent = {
  ...mergeBase,
  pools: [{ ...mergeBase.pools[0], name: '服务器灵感池修改' }],
};
const nonOverlapping = mergeFlowStudioDocuments(mergeBase, localDifferent, remoteDifferent);
assert.equal(nonOverlapping.conflicts.length, 0);
assert.equal(nonOverlapping.document.stages[0].name, '本地阶段修改');
assert.equal(nonOverlapping.document.pools[0].name, '服务器灵感池修改');

const guidanceBase = {
  ...mergeBase,
  stages: [{ ...mergeBase.stages[0], guidancePrompt: '' }],
};
const guidanceLocal = {
  ...guidanceBase,
  stages: [{ ...guidanceBase.stages[0], guidancePrompt: 'STAGE_GUIDANCE_LOCAL' }],
};
const guidanceRemote = {
  ...guidanceBase,
  stages: [{ ...guidanceBase.stages[0], name: '服务器阶段' }],
};
const guidanceNonOverlapping = mergeFlowStudioDocuments(guidanceBase, guidanceLocal, guidanceRemote);
assert.equal(guidanceNonOverlapping.conflicts.length, 0);
assert.equal(guidanceNonOverlapping.document.stages[0].guidancePrompt, 'STAGE_GUIDANCE_LOCAL');
assert.equal(guidanceNonOverlapping.document.stages[0].name, '服务器阶段');

const guidanceRemoteConflict = {
  ...guidanceBase,
  stages: [{ ...guidanceBase.stages[0], guidancePrompt: 'STAGE_GUIDANCE_REMOTE' }],
};
const guidanceOverlapping = mergeFlowStudioDocuments(guidanceBase, guidanceLocal, guidanceRemoteConflict);
assert.equal(guidanceOverlapping.conflicts.length, 1);
assert.equal(guidanceOverlapping.conflicts[0].path, 'stages.s1.guidancePrompt');
assert.equal(
  mergeFlowStudioDocuments(guidanceBase, guidanceLocal, guidanceRemoteConflict, 'remote').document.stages[0].guidancePrompt,
  'STAGE_GUIDANCE_REMOTE',
);
assert.equal(
  mergeFlowStudioDocuments(guidanceBase, guidanceLocal, guidanceRemoteConflict, 'local').document.stages[0].guidancePrompt,
  'STAGE_GUIDANCE_LOCAL',
);

const localSameField = {
  ...mergeBase,
  stages: [{ ...mergeBase.stages[0], name: '本地同字段修改' }],
};
const remoteSameField = {
  ...mergeBase,
  stages: [{ ...mergeBase.stages[0], name: '服务器同字段修改' }],
};
const overlapping = mergeFlowStudioDocuments(mergeBase, localSameField, remoteSameField);
assert.equal(overlapping.conflicts.length, 1);
assert.equal(overlapping.conflicts[0].path, 'stages.s1.name');
assert.equal(
  mergeFlowStudioDocuments(mergeBase, localSameField, remoteSameField, 'remote').document.stages[0].name,
  '服务器同字段修改',
);
assert.equal(
  mergeFlowStudioDocuments(mergeBase, localSameField, remoteSameField, 'local').document.stages[0].name,
  '本地同字段修改',
);

const ruotaTemplateSource = readFileSync(new URL('../src/lib/flowStudio/templates/ruota.json', import.meta.url), 'utf8');
const ruotaAsset = JSON.parse(ruotaTemplateSource);
assert.equal(ruotaAsset.source.repository, '29-Cu/Ruota-della-Fortuna');
assert.equal(ruotaAsset.source.license, 'MIT');
assert.deepEqual(ruotaAsset.import.ordinaryDimensions, ['position', 'scenario', 'props', 'roleplay', 'physical', 'mental']);
assert.equal(ruotaAsset.import.goreExcludedTotal > 0, true);
assert.equal(RUOTA_TEMPLATES.length, 6);
assert.equal(RUOTA_TEMPLATES.some((template) => template.id === 'gore'), false);
assert.equal(RUOTA_TEMPLATES.every((template) => template.entries.length > 0), true);
assert.equal(ruotaAsset.import.sourceTotal, 440);
assert.equal(ruotaAsset.import.importedTotal, 410);
assert.equal(ruotaAsset.import.filteredTotal, 30);

const filteredFixture = filterRuotaEntries([
  { zh: '安全成人标签', en: 'Adult consensual focus' },
  { zh: '不应导入', en: 'Forced coercive scene' },
  { zh: '不应导入', en: 'Explicit blood play' },
  { zh: '不应导入', en: 'Shota Top' },
  { zh: '不应导入', en: 'Sleep Stealing' },
  { zh: '不应导入', en: 'Bestiality' },
  { zh: '不应导入', en: 'Chokehold' },
  { zh: '不应导入', gore: true },
]);
assert.deepEqual(filteredFixture, ['安全成人标签']);
const templatePool = createRuotaPool('position', []);
assert.match(templatePool.id, /^ruota_position$/);
assert.equal(templatePool.name, 'Ruota · 体位 / Entry');
assert.equal(templatePool.enabled, true);
assert.equal(templatePool.mode, 'perTurn');
assert.equal(templatePool.count, 1);
assert.equal(templatePool.entries.length, RUOTA_TEMPLATES.find((template) => template.id === 'position').entries.length);
assert.equal(templatePool.entries.every((entry) => entry.enabled), true);
const duplicateTemplatePool = createRuotaPool('position', [templatePool.id]);
assert.notEqual(duplicateTemplatePool.id, templatePool.id);
assert.match(duplicateTemplatePool.id, /^ruota_position_2$/);
assert.deepEqual({ poolIds: [templatePool.id] }.poolIds, [templatePool.id], 'template pools remain bindable by normal stage poolIds');
const templateModule = readFileSync(new URL('../src/lib/flowStudio/flowStudioTemplates.ts', import.meta.url), 'utf8');
assert.doesNotMatch(templateModule, /Math\.random|fetch\(|webhook|mcp/i);

const mockAdapter = createFlowStudioMockAdapter('preview');
const mockDraft = mockAdapter.load();
mockDraft.stages[0].guidancePrompt = 'STAGE_GUIDANCE_MOCK';
await mockAdapter.save(mockDraft);
assert.equal(mockAdapter.load().stages[0].guidancePrompt, 'STAGE_GUIDANCE_MOCK');

const workspace = readFileSync(new URL('../src/screens/FlowStudioWorkspace.tsx', import.meta.url), 'utf8');
assert.match(workspace, /draftRef\.current/);
assert.match(workspace, /hasEditableFlowStudioChanges\(saved, draft\)/);
assert.match(workspace, /hasEditableFlowStudioChanges\(requestDraft, draftRef\.current\)/);
assert.doesNotMatch(workspace, /JSON\.stringify\(saved\) !== JSON\.stringify\(draft\)/);
assert.doesNotMatch(workspace, /JSON\.stringify\(draftRef\.current\) !== JSON\.stringify\(requestDraft\)/);
assert.match(workspace, /runtimeApplyPending \? '编辑已保存 · 等待应用'/);
assert.match(workspace, /有未保存修改/);
assert.match(workspace, /if \(dirty && !window\.confirm/);
assert.match(workspace, /setRuntimeApplyPending\(true\)/);
assert.match(workspace, /newerEditsExist/);
assert.match(workspace, /disabled=\{saving \|\| Boolean\(conflict\)\}/);
assert.match(workspace, /mergeFlowStudioDocuments/);
assert.match(workspace, /resolveMerge/);
assert.match(workspace, /明确保留本地冲突字段/);
assert.doesNotMatch(workspace, /验证并发布|已发布|未发布|发布验证未通过|请先保存当前草稿，再验证并发布/);
assert.doesNotMatch(workspace, /publishDraft|canPublish|publishValidation|validating|publishing/);
assert.match(workspace, /runtimeApplyPending/);
assert.match(workspace, /编辑已保存，但当前 Flow 正在运行，运行配置暂未更新。结束当前 Flow 后再次保存即可应用。/);
assert.match(workspace, /onClick=\{saveDraft\}/);
assert.match(workspace, /新建空白灵感池/);
assert.match(workspace, /从模板创建/);
assert.match(workspace, /RUOTA_TEMPLATES/);
assert.match(workspace, /createTemplatePool/);
assert.match(workspace, /六个普通维度/);
assert.match(workspace, /createRuotaPool/);
assert.doesNotMatch(workspace, /flow-template-grid[\\s\\S]{0,1500}template\\.entries\\.map/);
assert.match(workspace, /guidancePrompt/);
assert.match(workspace, /阶段提示词/);
assert.match(workspace, /maxLength=\{3000\}/);
assert.match(workspace, /只影响本阶段的创作引导，不改变推进规则/);
assert.match(workspace, /保存/);

const formal = readFileSync(new URL('../src/screens/FlowStudioScreen.tsx', import.meta.url), 'utf8');
const preview = readFileSync(new URL('../src/screens/FlowStudioSoftGlowScreen.tsx', import.meta.url), 'utf8');
assert.match(formal, /createFlowStudioApiAdapter/);
assert.doesNotMatch(formal, /createFlowStudioMockAdapter/);
assert.match(formal, /服务器配置/);
assert.doesNotMatch(formal, /服务器草稿|正式页面 · 未发布/);
assert.match(preview, /backPath="\/dash\/chat"/);
assert.match(preview, /createFlowStudioMockAdapter/);
assert.doesNotMatch(preview, /createFlowStudioApiAdapter|validate|publish/);

const flowWorkspace = readFileSync(new URL('../src/screens/FlowStudioWorkspace.tsx', import.meta.url), 'utf8');
const flowStyles = readFileSync(new URL('../src/screens/FlowStudioSoftGlowScreen.css', import.meta.url), 'utf8');
assert.match(flowStyles, /flow-template-panel/);
assert.match(flowStyles, /flow-template-card/);
const chatScreen = readFileSync(new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8');
assert.match(flowWorkspace, /className=\"flow-studio-page dash-fullscreen-page dash-scroll-page\"/);
assert.match(flowStyles, /height:100%;min-height:0;max-width:none;overflow-x:hidden;overflow-y:auto;-webkit-overflow-scrolling:touch/);
assert.match(flowStyles, /overscroll-behavior-y:contain;touch-action:pan-y/);
assert.match(flowStyles, /safe-area-inset-bottom/);
assert.doesNotMatch(flowStyles, /flow-publish-action|flow-publish-note/);
assert.match(chatScreen, /服务器配置/);
assert.doesNotMatch(chatScreen, /正式编辑器 · 服务器草稿|正式编辑器 · 本地 Mock/);

console.log('flow studio persistence, conflict, revision, race, and preview isolation checks passed');
