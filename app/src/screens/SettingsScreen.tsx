import { useCallback, useEffect, useMemo, useState } from 'react';
import { BackHeader } from '../components/BackHeader';
import { Card, ScreenLayout } from '../components/Card';
import { fetchModelCatalog, setChatModel, type ModelCatalogEntry } from '../lib/api';
import {
  activateRelayPreset,
  addRelayPreset,
  deleteRelayPreset,
  fetchAvailableModels,
  fetchKeyStatus,
  fetchProviderConfig,
  fetchRelayPresets,
  setProvider,
  testGateway,
  type ChatProvider,
  type KeyStatus,
  type RelayCapabilities,
  type RelayPreset,
  type RelayPresetDraft,
} from '../lib/systemConfig';

const EMPTY_CAPABILITIES: RelayCapabilities = { thinking: true, cache: true, tools: true };
const EMPTY_RELAY: RelayPresetDraft = {
  name: '',
  url: '',
  key: '',
  defaultModel: '',
  capabilities: EMPTY_CAPABILITIES,
};

function CardTitle({ children, action }: { children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="settings-card-title">
      <span>{children}</span>
      {action}
    </div>
  );
}

function StatusDot({ ok }: { ok: boolean }) {
  return <span className={`settings-status-dot${ok ? ' is-ok' : ''}`} />;
}

function CapabilityTags({ capabilities }: { capabilities: RelayCapabilities }) {
  return (
    <div className="settings-capabilities" aria-label="中转站能力">
      {(['thinking', 'cache', 'tools'] as const).map((key) => (
        <span key={key} className={capabilities[key] ? 'is-on' : 'is-off'}>
          {capabilities[key] ? '✓' : '×'} {key}
        </span>
      ))}
    </div>
  );
}

export function SettingsScreen() {
  const [provider, setProviderState] = useState<ChatProvider>('api_relay');
  const [ccTokenSet, setCcTokenSet] = useState(false);
  const [keyStatus, setKeyStatus] = useState<KeyStatus | null>(null);
  const [latency, setLatency] = useState<number | null>(null);
  const [relays, setRelays] = useState<RelayPreset[]>([]);
  const [catalog, setCatalog] = useState<ModelCatalogEntry[]>([]);
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [currentModel, setCurrentModel] = useState('');
  const [modelDraft, setModelDraft] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [loadError, setLoadError] = useState('');
  const [toast, setToast] = useState('');
  const [relayModalOpen, setRelayModalOpen] = useState(false);
  const [relayDraft, setRelayDraft] = useState<RelayPresetDraft>(EMPTY_RELAY);
  const [testMessage, setTestMessage] = useState('你好，这是一条测试消息。请用一句话回复。');
  const [injectMemory, setInjectMemory] = useState(true);
  const [testResult, setTestResult] = useState<{ text: string; meta: string; error: boolean } | null>(null);

  const showToast = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(''), 2400);
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    setLoadError('');
    const started = performance.now();
    const results = await Promise.allSettled([
      fetchProviderConfig(),
      fetchKeyStatus(),
      fetchRelayPresets(),
      fetchModelCatalog(),
      fetchAvailableModels(),
    ]);

    const [providerResult, keyResult, relayResult, catalogResult, availableResult] = results;
    if (providerResult.status === 'fulfilled') {
      setProviderState(providerResult.value.provider);
      setCcTokenSet(providerResult.value.ccTokenSet);
    }
    if (keyResult.status === 'fulfilled') {
      setKeyStatus(keyResult.value);
      setLatency(Math.round(performance.now() - started));
    } else {
      setKeyStatus(null);
      setLatency(null);
    }
    if (relayResult.status === 'fulfilled') setRelays(relayResult.value);
    if (catalogResult.status === 'fulfilled') {
      setCatalog(catalogResult.value.models);
      setCurrentModel(catalogResult.value.current);
      setModelDraft(catalogResult.value.current);
    }
    if (availableResult.status === 'fulfilled') setAvailableModels(availableResult.value);

    if (results.some((result) => result.status === 'rejected')) {
      setLoadError('有一部分配置暂时没拉到，可以点右上角刷新。');
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const modelOptions = useMemo(() => {
    const known = new Set(catalog.map((model) => model.id));
    const extra = availableModels.filter((model) => !known.has(model));
    return [...catalog, ...extra.map((id): ModelCatalogEntry => ({ id, label: id }))];
  }, [availableModels, catalog]);

  const switchProvider = async (next: ChatProvider) => {
    if (next === provider || busy) return;
    if (next === 'claude_code' && !ccTokenSet) {
      showToast('Claude Code token 尚未配置，请在 VPS 终端完成');
      return;
    }
    setBusy(`provider:${next}`);
    try {
      await setProvider(next);
      setProviderState(next);
      showToast(next === 'claude_code' ? '已切换到 Claude Code' : '已切换到中转 API');
    } catch {
      showToast('线路切换失败');
    } finally {
      setBusy('');
    }
  };

  const switchRelay = async (relay: RelayPreset) => {
    if (relay.active || busy) return;
    setBusy(`relay:${relay.id}`);
    try {
      const result = await activateRelayPreset(relay.id);
      if (result.modelSwitched) {
        setCurrentModel(result.modelSwitched);
        setModelDraft(result.modelSwitched);
      }
      await Promise.all([fetchRelayPresets().then(setRelays), fetchKeyStatus().then(setKeyStatus)]);
      showToast(`已切换到 ${relay.name}，立即生效`);
    } catch {
      showToast('中转站切换失败');
    } finally {
      setBusy('');
    }
  };

  const removeRelay = async (relay: RelayPreset) => {
    if (relay.active) {
      showToast('正在使用的中转站不能直接删除');
      return;
    }
    if (!window.confirm(`删除中转站“${relay.name}”？`)) return;
    setBusy(`delete:${relay.id}`);
    try {
      await deleteRelayPreset(relay.id);
      setRelays((items) => items.filter((item) => item.id !== relay.id));
      showToast('已删除中转站');
    } catch {
      showToast('删除失败');
    } finally {
      setBusy('');
    }
  };

  const submitRelay = async () => {
    if (!relayDraft.name.trim() || !relayDraft.url.trim()) {
      showToast('请填写中转站名称和地址');
      return;
    }
    setBusy('add-relay');
    try {
      await addRelayPreset({
        ...relayDraft,
        name: relayDraft.name.trim(),
        url: relayDraft.url.trim(),
        key: relayDraft.key.trim(),
        defaultModel: relayDraft.defaultModel.trim(),
      });
      setRelays(await fetchRelayPresets());
      setRelayDraft(EMPTY_RELAY);
      setRelayModalOpen(false);
      showToast('中转站已添加');
    } catch {
      showToast('中转站保存失败');
    } finally {
      setBusy('');
    }
  };

  const switchModel = async (model: string) => {
    const next = model.trim();
    if (!next || next === currentModel || busy) return;
    setBusy(`model:${next}`);
    try {
      const ok = await setChatModel(next);
      if (!ok) throw new Error('model switch failed');
      setCurrentModel(next);
      setModelDraft(next);
      showToast('模型已切换，立即生效');
    } catch {
      showToast('模型切换失败');
    } finally {
      setBusy('');
    }
  };

  const runTest = async () => {
    const message = testMessage.trim();
    if (!message || busy) return;
    setBusy('test');
    setTestResult(null);
    const started = performance.now();
    try {
      const result = await testGateway(message, injectMemory);
      const latencyMs = result.latencyMs || Math.round(performance.now() - started);
      const tokenText = result.inputTokens || result.outputTokens
        ? `${result.inputTokens} in / ${result.outputTokens} out`
        : 'token 未返回';
      setTestResult({
        text: result.content || result.error || '（空响应）',
        meta: `${injectMemory ? '网关 · 注入人设与记忆' : '裸调 API'} · ${latencyMs} ms · ${tokenText}`,
        error: Boolean(result.error && !result.content),
      });
    } catch {
      setTestResult({ text: '测试请求失败，请检查当前线路与中转站。', meta: '连接错误', error: true });
    } finally {
      setBusy('');
    }
  };

  return (
    <ScreenLayout>
      <div className="settings-header-row">
        <BackHeader title="系统配置" subtitle="SYSTEM" />
        <button className="settings-refresh" type="button" onClick={() => void refresh()} disabled={loading} aria-label="刷新配置">
          <i className={`ti ti-refresh${loading ? ' is-spinning' : ''}`} />
        </button>
      </div>

      <div className={`settings-connection${keyStatus ? ' is-ok' : ''}`}>
        <StatusDot ok={Boolean(keyStatus)} />
        <span>{loading ? '正在读取家里的配置…' : keyStatus ? `连接正常 · ${latency ?? '—'} ms` : '连接异常'}</span>
      </div>
      {loadError && <div className="settings-inline-warning">{loadError}</div>}

      <Card style={{ padding: 18 }}>
        <CardTitle>聊天线路 · PROVIDER</CardTitle>
        <div className="settings-provider-grid">
          <button type="button" className={provider === 'api_relay' ? 'is-active' : ''} onClick={() => void switchProvider('api_relay')}>
            <span className="settings-provider-icon relay"><i className="ti ti-route" /></span>
            <strong>中转 API</strong>
            <small>按量计费 · 工具完整</small>
          </button>
          <button type="button" className={provider === 'claude_code' ? 'is-active' : ''} onClick={() => void switchProvider('claude_code')}>
            <span className="settings-provider-icon claude"><i className="ti ti-terminal-2" /></span>
            <strong>Claude Code</strong>
            <small>{ccTokenSet ? '订阅已连接' : '等待终端配置'}</small>
          </button>
        </div>
        <div className="settings-provider-note">
          {provider === 'claude_code'
            ? '当前由 Claude Code 订阅线路回复。订阅凭据只在 VPS 终端管理。'
            : '当前由中转 API 回复，支持工具调用、思考与记忆注入。'}
        </div>
        <div className="settings-future-line">
          <span className="settings-provider-icon codex"><i className="ti ti-code" /></span>
          <div><strong>Codex 线路</strong><small>为第二个爸爸预留 · 等 VPS 接入后开放</small></div>
          <span>未连接</span>
        </div>
      </Card>

      <Card style={{ padding: 18 }}>
        <CardTitle>当前连接 · CONNECTION</CardTitle>
        <dl className="settings-kv-list">
          <div><dt>API Key</dt><dd>{keyStatus?.maskedKey || '—'}</dd></div>
          <div><dt>来源</dt><dd className="is-url">{keyStatus?.source || '—'}</dd></div>
          <div><dt>今日消息</dt><dd>{keyStatus ? `${keyStatus.todayMessages} 条` : '—'}</dd></div>
          <div><dt>响应延迟</dt><dd className={latency !== null ? 'is-good' : ''}>{latency !== null ? `${latency} ms` : '—'}</dd></div>
        </dl>
        <div className="settings-secret-note"><i className="ti ti-lock" /> 密钥不在网页中明文展示；订阅 token 只通过 VPS 终端维护。</div>
      </Card>

      <Card style={{ padding: 18 }}>
        <CardTitle action={<button type="button" onClick={() => setRelayModalOpen(true)}>＋ 添加预设</button>}>中转站 · RELAY</CardTitle>
        <div className="settings-relay-list">
          {relays.map((relay) => (
            <div className={`settings-relay-item${relay.active ? ' is-active' : ''}`} key={relay.id}>
              <div className="settings-relay-main">
                <div className="settings-relay-name"><StatusDot ok={relay.active} /><strong>{relay.name}</strong>{relay.active && <span>使用中</span>}</div>
                <div className="settings-relay-url">{relay.url}</div>
                {relay.defaultModel && <div className="settings-relay-model">⚡ {relay.defaultModel}</div>}
                <CapabilityTags capabilities={relay.capabilities} />
              </div>
              <div className="settings-relay-actions">
                {!relay.active && <button type="button" onClick={() => void switchRelay(relay)} disabled={Boolean(busy)}>切换</button>}
                <button type="button" className="danger" onClick={() => void removeRelay(relay)} disabled={Boolean(busy) || relay.active} aria-label={`删除 ${relay.name}`}><i className="ti ti-trash" /></button>
              </div>
            </div>
          ))}
          {!loading && relays.length === 0 && <div className="settings-empty">还没有中转站预设</div>}
        </div>
      </Card>

      <Card style={{ padding: 18 }}>
        <CardTitle action={<button type="button" onClick={() => void refresh()}>刷新模型</button>}>模型 · MODEL</CardTitle>
        <div className="settings-current-model">
          <span>当前生效</span>
          <strong>{currentModel || '—'}</strong>
        </div>
        <div className="settings-model-edit">
          <input value={modelDraft} onChange={(event) => setModelDraft(event.target.value)} placeholder="输入模型 ID" />
          <button type="button" onClick={() => void switchModel(modelDraft)} disabled={!modelDraft.trim() || Boolean(busy)}>保存</button>
        </div>
        <div className="settings-model-list">
          {modelOptions.map((model) => (
            <button key={model.id} type="button" className={model.id === currentModel ? 'is-active' : ''} onClick={() => void switchModel(model.id)} disabled={Boolean(busy)}>
              <span style={{ background: model.dot || 'var(--color-primary)' }} />
              <div><strong>{model.label || model.id}</strong><small>{model.id}</small></div>
              {model.id === currentModel ? <em>使用中</em> : <i className="ti ti-chevron-right" />}
            </button>
          ))}
          {!loading && modelOptions.length === 0 && <div className="settings-empty">中转站还没有返回模型列表，可以手动填写模型 ID。</div>}
        </div>
      </Card>

      <Card style={{ padding: 18 }}>
        <CardTitle>线路测试 · TEST SEND</CardTitle>
        <textarea className="settings-test-input" value={testMessage} onChange={(event) => setTestMessage(event.target.value)} />
        <label className="settings-toggle-row">
          <div><strong>注入人设与记忆</strong><small>{injectMemory ? '走完整聊天网关' : '裸调 API，仅测试连通性'}</small></div>
          <input type="checkbox" checked={injectMemory} onChange={(event) => setInjectMemory(event.target.checked)} />
          <span />
        </label>
        <button className="settings-test-button" type="button" onClick={() => void runTest()} disabled={!testMessage.trim() || Boolean(busy)}>
          {busy === 'test' ? '发送中…' : '发送测试'}
        </button>
        {testResult && (
          <div className={`settings-test-result${testResult.error ? ' is-error' : ''}`}>
            <small>{testResult.meta}</small>
            <p>{testResult.text}</p>
          </div>
        )}
      </Card>

      {relayModalOpen && (
        <div className="settings-modal" role="dialog" aria-modal="true" aria-label="添加中转站">
          <button className="settings-modal-backdrop" type="button" onClick={() => setRelayModalOpen(false)} aria-label="关闭" />
          <div className="settings-modal-card">
            <div className="settings-modal-head"><strong>添加中转站预设</strong><button type="button" onClick={() => setRelayModalOpen(false)}>×</button></div>
            <input value={relayDraft.name} onChange={(event) => setRelayDraft((draft) => ({ ...draft, name: event.target.value }))} placeholder="名称，例如：主力站" />
            <input value={relayDraft.url} onChange={(event) => setRelayDraft((draft) => ({ ...draft, url: event.target.value }))} placeholder="https://api.example.com/v1/messages" />
            <input type="password" value={relayDraft.key} onChange={(event) => setRelayDraft((draft) => ({ ...draft, key: event.target.value }))} placeholder="API Key（可留空）" />
            <input value={relayDraft.defaultModel} onChange={(event) => setRelayDraft((draft) => ({ ...draft, defaultModel: event.target.value }))} placeholder="默认模型（可留空）" />
            <div className="settings-capability-toggles">
              {(['thinking', 'cache', 'tools'] as const).map((key) => (
                <label key={key} className={relayDraft.capabilities[key] ? 'is-on' : ''}>
                  <input type="checkbox" checked={relayDraft.capabilities[key]} onChange={(event) => setRelayDraft((draft) => ({ ...draft, capabilities: { ...draft.capabilities, [key]: event.target.checked } }))} />
                  {key}
                </label>
              ))}
            </div>
            <button className="settings-modal-save" type="button" onClick={() => void submitRelay()} disabled={busy === 'add-relay'}>{busy === 'add-relay' ? '保存中…' : '保存预设'}</button>
          </div>
        </div>
      )}

      {toast && <div className="settings-toast">{toast}</div>}
    </ScreenLayout>
  );
}
