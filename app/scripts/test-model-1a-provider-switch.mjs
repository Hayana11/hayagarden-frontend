/**
 * MODEL-1A/1B: after provider write, model space follows effective_chat_provider
 * (resolve_provider('chat')), not the raw POST body.
 *
 * MODEL-1B: CC after provider write is unknown until catalog confirms —
 * never invent "默认".
 *
 * Run: npm run test:model-1a-provider-switch
 */
import assert from 'node:assert/strict';

const {
  chatModelSpaceAfterProviderWrite,
  ccChatModelLabel,
} = await import('../src/lib/systemConfig.ts');

function spaceFromProviderResponse(resp, opts = {}) {
  const gw = resp.provider === 'claude_code' ? 'claude_code' : 'api_relay';
  const effective = resp.effective_chat_provider === undefined
    ? gw
    : (resp.effective_chat_provider === 'claude_code' ? 'claude_code' : 'api_relay');
  const afterWrite = chatModelSpaceAfterProviderWrite(effective, opts);
  let chatModelProvider = afterWrite.chatModelProvider;
  let currentModel = afterWrite.currentModel;
  let modelMode = afterWrite.modelMode;
  let refreshFailed = false;
  // MODEL-1B: always drop previous provider catalog on space switch.
  let catalog = [];
  const priorCatalog = opts.priorCatalog || [];

  if (opts.catalogThrows) {
    refreshFailed = true;
    catalog = [];
    // Keep unknown — do not invent default; do not keep prior catalog.
  } else if (opts.catalog) {
    chatModelProvider = opts.catalog.provider || chatModelProvider;
    if (opts.catalog.provider === 'claude_code') {
      modelMode = opts.catalog.model_mode === 'explicit' || opts.catalog.model_mode === 'default'
        ? opts.catalog.model_mode
        : 'unknown';
      currentModel = opts.catalog.configured_model || opts.catalog.current || '';
      catalog = opts.catalog.models || [];
    } else if (opts.catalog.provider === 'api_relay') {
      modelMode = '';
      currentModel = opts.catalog.current || currentModel;
      catalog = opts.catalog.models || [];
    } else {
      refreshFailed = true;
      catalog = [];
    }
  }

  const label = chatModelProvider === 'claude_code'
    ? ccChatModelLabel({
      modelMode,
      configuredModel: currentModel,
      catalog,
      loading: !refreshFailed && modelMode === 'unknown',
    })
    : currentModel;

  return {
    gw,
    effective,
    chatModelProvider,
    currentModel,
    modelMode,
    refreshFailed,
    label,
    catalog,
    priorCatalog,
  };
}

// 1) api_relay → claude_code, catalog GET fails — still enter CC space, NOT fake default
{
  const next = spaceFromProviderResponse(
    { provider: 'claude_code', effective_chat_provider: 'claude_code' },
    {
      catalogThrows: true,
      priorCatalog: [{ id: '[反重力量] claude-opus-4-6-thinking [不补]', label: 'Relay' }],
    },
  );
  assert.equal(next.chatModelProvider, 'claude_code');
  assert.equal(next.currentModel, '');
  assert.equal(next.modelMode, 'unknown');
  assert.equal(next.refreshFailed, true);
  assert.equal(next.label, 'Claude Code · 状态未知');
  assert.notEqual(next.label, 'Claude Code · 默认');
  assert.deepEqual(next.catalog, []);
  assert.equal(next.catalog.length, 0);
}

// 1b) explicit CC → relay → CC + catalog fail — must not show 默认 or Relay models
{
  const next = spaceFromProviderResponse(
    { provider: 'claude_code', effective_chat_provider: 'claude_code' },
    {
      catalogThrows: true,
      priorCatalog: [{ id: '[反重力量] claude-opus-4-6-thinking [不补]', label: 'Relay' }],
    },
  );
  assert.notEqual(next.label, 'Claude Code · 默认');
  assert.match(next.label, /状态未知|读取中/);
  assert.ok(!next.catalog.some((m) => String(m.id || '').includes('[')));
}

// 1b2) CC → Relay + catalog fail — must not keep CC models
{
  const next = spaceFromProviderResponse(
    { provider: 'api_relay', effective_chat_provider: 'api_relay' },
    {
      relayModel: 'claude-sonnet-4-6',
      catalogThrows: true,
      priorCatalog: [{ id: 'claude-sonnet-5', label: 'Sonnet 5' }],
    },
  );
  assert.equal(next.chatModelProvider, 'api_relay');
  assert.deepEqual(next.catalog, []);
  assert.ok(!next.catalog.some((m) => m.id === 'claude-sonnet-5'));
}

// 1c) catalog confirms explicit after provider write
{
  const next = spaceFromProviderResponse(
    { provider: 'claude_code', effective_chat_provider: 'claude_code' },
    {
      catalog: {
        provider: 'claude_code',
        model_mode: 'explicit',
        configured_model: 'claude-opus-4-6',
        current: 'claude-opus-4-6',
      },
    },
  );
  assert.equal(next.modelMode, 'explicit');
  assert.equal(next.currentModel, 'claude-opus-4-6');
  assert.equal(next.label, 'Claude Code · claude-opus-4-6');
}

// 1d) catalog confirms default
{
  const next = spaceFromProviderResponse(
    { provider: 'claude_code', effective_chat_provider: 'claude_code' },
    {
      catalog: {
        provider: 'claude_code',
        model_mode: 'default',
        configured_model: null,
        current: '',
      },
    },
  );
  assert.equal(next.modelMode, 'default');
  assert.equal(next.label, 'Claude Code · 默认');
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
  assert.equal(next.modelMode, 'unknown');
  assert.notEqual(next.label, 'Claude Code · 默认');
}

// Helper unit
{
  assert.deepEqual(
    chatModelSpaceAfterProviderWrite('claude_code', { relayModel: 'claude-opus-4-6' }),
    { chatModelProvider: 'claude_code', currentModel: '', modelMode: 'unknown' },
  );
  assert.deepEqual(
    chatModelSpaceAfterProviderWrite('api_relay', { relayModel: 'claude-opus-4-6' }),
    { chatModelProvider: 'api_relay', currentModel: 'claude-opus-4-6', modelMode: '' },
  );
  assert.equal(
    ccChatModelLabel({ modelMode: 'unknown', loading: true }),
    'Claude Code · 读取中…',
  );
  assert.equal(
    ccChatModelLabel({ modelMode: 'unknown', loading: false }),
    'Claude Code · 状态未知',
  );
  assert.equal(
    ccChatModelLabel({ modelMode: 'default' }),
    'Claude Code · 默认',
  );
}

console.log('test-model-1a-provider-switch: ok');
