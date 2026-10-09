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
  stages: [{ id: 's1', name: '试探', minTurns: 1, terminal: true, poolIds: [], nextStageId: null }],
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
      document: { ...document, stages: [{ ...document.stages[0], name: '本地保存' }] },
      editorRevision: 6,
      savedAt: 'server-save',
      runtime: { version: 101, schemaVersion: 1, engineEnabled: false },
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
const saved = await adapter.save({ ...loaded, stages: [{ ...loaded.stages[0], name: '本地保存' }] });
assert.equal(saved.editorRevision, 6);
assert.equal(requests[1].init.body.includes('"expectedRevision":4'), true);
assert.equal(requests[2].init.body.includes('"expectedRevision":5'), true);

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
assert.match(overlapping.conflicts[0].path, /stages\\.s1\\.name/);
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

const formal = readFileSync(new URL('../src/screens/FlowStudioScreen.tsx', import.meta.url), 'utf8');
const preview = readFileSync(new URL('../src/screens/FlowStudioSoftGlowScreen.tsx', import.meta.url), 'utf8');
assert.match(formal, /createFlowStudioApiAdapter/);
assert.doesNotMatch(formal, /createFlowStudioMockAdapter/);
assert.match(preview, /backPath="\/dash\/chat"/);
assert.match(preview, /backPath="\/dash\/chat"/);

console.log('flow studio persistence, conflict, revision, race, and preview isolation checks passed');
