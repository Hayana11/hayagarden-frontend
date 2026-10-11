import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  createFlowStudioApiAdapter,
  FlowStudioConflictError,
} from '../src/lib/flowStudio/flowStudioApi.ts';
import { mergeFlowStudioDocuments } from '../src/lib/flowStudio/flowStudioMerge.ts';

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
      document: { ...document, stages: [{ ...document.stages[0], name: '本地保存' }] },
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

assert.equal(saved.runtimeVersion, 102);
assert.equal(saved.published, true);
assert.equal(saved.stages[0].name, '本地保存');
assert.equal(saved.stages[0].guidancePrompt, 'STAGE_GUIDANCE_SENTINEL_A');
assert.equal(requests[2].url.endsWith('/intimacy-v1'), true);
assert.equal(requests[2].init.body.includes('"expectedRevision":5'), true);
assert.equal(requests[3].url.endsWith('/validate'), true);
assert.equal(requests[4].url.endsWith('/publish'), true);
assert.equal(requests[4].init.body.includes('"expectedRuntimeVersion":101'), true);
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

const workspace = readFileSync(new URL('../src/screens/FlowStudioWorkspace.tsx', import.meta.url), 'utf8');
assert.match(workspace, /draftRef\.current/);
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
const chatScreen = readFileSync(new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8');
assert.match(flowWorkspace, /className=\"flow-studio-page dash-fullscreen-page dash-scroll-page\"/);
assert.match(flowStyles, /height:100%;min-height:0;max-width:none;overflow-x:hidden;overflow-y:auto;-webkit-overflow-scrolling:touch/);
assert.match(flowStyles, /overscroll-behavior-y:contain;touch-action:pan-y/);
assert.match(flowStyles, /safe-area-inset-bottom/);
assert.doesNotMatch(flowStyles, /flow-publish-action|flow-publish-note/);
assert.match(chatScreen, /服务器配置/);
assert.doesNotMatch(chatScreen, /正式编辑器 · 服务器草稿|正式编辑器 · 本地 Mock/);

console.log('flow studio persistence, conflict, revision, race, and preview isolation checks passed');
