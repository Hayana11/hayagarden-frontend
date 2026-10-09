import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  createFlowStudioApiAdapter,
  FlowStudioConflictError,
} from '../src/lib/flowStudio/flowStudioApi.ts';

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

const workspace = readFileSync(new URL('../src/screens/FlowStudioWorkspace.tsx', import.meta.url), 'utf8');
assert.match(workspace, /draftRef\.current/);
assert.match(workspace, /newerEditsExist/);
assert.match(workspace, /disabled=\{saving \|\| Boolean\(conflict\)\}/);
assert.match(workspace, /以服务器版本为基线保留本地修改/);

const formal = readFileSync(new URL('../src/screens/FlowStudioScreen.tsx', import.meta.url), 'utf8');
const preview = readFileSync(new URL('../src/screens/FlowStudioSoftGlowScreen.tsx', import.meta.url), 'utf8');
assert.match(formal, /createFlowStudioApiAdapter/);
assert.doesNotMatch(formal, /createFlowStudioMockAdapter/);
assert.match(preview, /backPath="\/dash\/chat"/);
assert.match(preview, /backPath="\/dash\/chat"/);

console.log('flow studio persistence, conflict, revision, race, and preview isolation checks passed');
