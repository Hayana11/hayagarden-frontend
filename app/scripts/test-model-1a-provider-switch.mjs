/**
 * MODEL-1A: after provider write, model space follows effective_chat_provider
 * (resolve_provider('chat')), not the raw POST body.
 *
 * Run: npm run test:model-1a-provider-switch
 */
import assert from 'node:assert/strict';

const {
  chatModelSpaceAfterProviderWrite,
  // normalize is not exported; re-implement the response→space contract here.
} = await import('../src/lib/systemConfig.ts');

function spaceFromProviderResponse(resp, opts = {}) {
  const gw = resp.provider === 'claude_code' ? 'claude_code' : 'api_relay';
  const effective = resp.effective_chat_provider === undefined
    ? gw
    : (resp.effective_chat_provider === 'claude_code' ? 'claude_code' : 'api_relay');
  const afterWrite = chatModelSpaceAfterProviderWrite(effective, opts);
  let chatModelProvider = afterWrite.chatModelProvider;
  let currentModel = afterWrite.currentModel;
  let refreshFailed = false;

  if (opts.catalogThrows) {
    refreshFailed = true;
  } else if (opts.catalog) {
    chatModelProvider = opts.catalog.provider || chatModelProvider;
    currentModel = opts.catalog.provider === 'claude_code'
      ? ''
      : (opts.catalog.current || currentModel);
  }

  return { gw, effective, chatModelProvider, currentModel, refreshFailed };
}

// 1) api_relay → claude_code, catalog GET fails — still enter CC space
{
  const next = spaceFromProviderResponse(
    { provider: 'claude_code', effective_chat_provider: 'claude_code' },
    { catalogThrows: true },
  );
  assert.equal(next.chatModelProvider, 'claude_code');
  assert.equal(next.currentModel, '');
  assert.equal(next.refreshFailed, true);
}

// 2) claude_code → api_relay, catalog GET fails — enter relay space when effective says so
{
  const next = spaceFromProviderResponse(
    { provider: 'api_relay', effective_chat_provider: 'api_relay' },
    { relayModel: 'claude-sonnet-4-6', catalogThrows: true },
  );
  assert.equal(next.chatModelProvider, 'api_relay');
  assert.equal(next.currentModel, 'claude-sonnet-4-6');
  assert.equal(next.refreshFailed, true);
}

// 3) CHAT_PROVIDER=claude_code still wins after POST GW_PROVIDER=api_relay
{
  const next = spaceFromProviderResponse(
    {
      provider: 'api_relay',
      effective_chat_provider: 'claude_code',
    },
    { relayModel: 'claude-opus-4-6', catalogThrows: true },
  );
  assert.equal(next.gw, 'api_relay');
  assert.equal(next.effective, 'claude_code');
  assert.equal(next.chatModelProvider, 'claude_code');
  assert.equal(next.currentModel, '');
}

// Helper unit
{
  assert.deepEqual(
    chatModelSpaceAfterProviderWrite('claude_code', { relayModel: 'claude-opus-4-6' }),
    { chatModelProvider: 'claude_code', currentModel: '' },
  );
  assert.deepEqual(
    chatModelSpaceAfterProviderWrite('api_relay', { relayModel: 'claude-opus-4-6' }),
    { chatModelProvider: 'api_relay', currentModel: 'claude-opus-4-6' },
  );
}

console.log('test-model-1a-provider-switch: ok');
