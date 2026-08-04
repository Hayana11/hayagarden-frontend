/**
 * MODEL-1A: provider write success must enter the new model space even when
 * subsequent model-catalog refresh fails.
 *
 * Run: npm run test:model-1a-provider-switch
 */
import assert from 'node:assert/strict';

const { chatModelSpaceAfterProviderWrite } = await import('../src/lib/systemConfig.ts');

function simulateSwitch(prev, writeProvider, opts = {}) {
  // Contract: after provider POST success, apply local space BEFORE catalog.
  const afterWrite = chatModelSpaceAfterProviderWrite(writeProvider, opts);
  let chatModelProvider = afterWrite.chatModelProvider;
  let currentModel = afterWrite.currentModel;
  let toast = '';
  let refreshFailed = false;

  if (opts.catalogThrows) {
    refreshFailed = true;
    // Must NOT roll back to prev model space.
  } else if (opts.catalog) {
    chatModelProvider = opts.catalog.provider || chatModelProvider;
    currentModel = opts.catalog.provider === 'claude_code'
      ? ''
      : (opts.catalog.current || currentModel);
  }

  toast = refreshFailed
    ? (writeProvider === 'claude_code'
      ? '已切换到 Claude Code，部分状态刷新失败'
      : '已切换到 relay，部分状态刷新失败')
    : (writeProvider === 'claude_code'
      ? '已切换：Claude Code 订阅'
      : '已切换：relay');

  return { prev, chatModelProvider, currentModel, toast, refreshFailed };
}

// 1) api_relay → claude_code, catalog GET fails
{
  const prev = {
    chatModelProvider: 'api_relay',
    currentModel: 'claude-opus-4-6',
  };
  const next = simulateSwitch(prev, 'claude_code', { catalogThrows: true });
  assert.equal(next.chatModelProvider, 'claude_code');
  assert.equal(next.currentModel, '');
  assert.equal(next.refreshFailed, true);
  assert.match(next.toast, /已切换到 Claude Code/);
  assert.notEqual(next.chatModelProvider, prev.chatModelProvider);
  assert.notEqual(next.currentModel, prev.currentModel);
}

// 2) claude_code → api_relay, catalog GET fails
{
  const prev = {
    chatModelProvider: 'claude_code',
    currentModel: '',
  };
  const next = simulateSwitch(prev, 'api_relay', {
    relayModel: 'claude-sonnet-4-6',
    catalogThrows: true,
  });
  assert.equal(next.chatModelProvider, 'api_relay');
  assert.equal(next.currentModel, 'claude-sonnet-4-6');
  assert.equal(next.refreshFailed, true);
  assert.match(next.toast, /已切换到 relay/);
  assert.notEqual(next.chatModelProvider, prev.chatModelProvider);
}

// Helper unit: CC clears relay model; relay keeps activated model
{
  const cc = chatModelSpaceAfterProviderWrite('claude_code', {
    relayModel: 'claude-opus-4-6',
  });
  assert.deepEqual(cc, {
    chatModelProvider: 'claude_code',
    currentModel: '',
  });
  const relay = chatModelSpaceAfterProviderWrite('api_relay', {
    relayModel: 'claude-opus-4-6',
  });
  assert.deepEqual(relay, {
    chatModelProvider: 'api_relay',
    currentModel: 'claude-opus-4-6',
  });
}

console.log('test-model-1a-provider-switch: ok');
