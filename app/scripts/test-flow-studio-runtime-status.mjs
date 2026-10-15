import assert from 'node:assert/strict';
import { createFlowStudioApiAdapter } from '../src/lib/flowStudio/flowStudioApi.ts';

const document = {
  version: 0,
  savedAt: '',
  enabled: true,
  stages: [],
  pools: [],
  cues: [],
  dimensions: [],
};

async function readStatus(runtime) {
  globalThis.fetch = async () => new Response(
    JSON.stringify({
      ok: true,
      flowId: 'intimacy-v1',
      document,
      editorRevision: 0,
      savedAt: null,
      runtime,
    }),
    { status: 200, headers: { 'content-type': 'application/json' } },
  );
  const adapter = createFlowStudioApiAdapter('intimacy-v1');
  await adapter.load();
  return adapter.getRuntimeStatus();
}

assert.deepEqual(
  await readStatus({
    engineEnabled: true,
    activeFlow: { state: 'active', stage: 's2', stageTurn: 3 },
  }),
  {
    engineEnabled: true,
    activeFlow: { state: 'active', stage: 's2', stageTurn: 3 },
  },
);

assert.deepEqual(
  await readStatus({
    engineEnabled: true,
    activeFlow: { state: 'idle', stage: null, stageTurn: null },
  }),
  {
    engineEnabled: true,
    activeFlow: { state: 'idle', stage: null, stageTurn: null },
  },
);

assert.deepEqual(
  await readStatus({
    engineEnabled: true,
    activeFlow: { state: 'active', stage: '', stageTurn: 3 },
  }),
  {
    engineEnabled: true,
    activeFlow: { state: 'unknown', stage: null, stageTurn: null },
  },
);

assert.deepEqual(
  await readStatus({
    engineEnabled: true,
  }),
  {
    engineEnabled: true,
    activeFlow: { state: 'unknown', stage: null, stageTurn: null },
  },
);

console.log('flow studio runtime parser: active, idle and fail-closed unknown cases passed');
