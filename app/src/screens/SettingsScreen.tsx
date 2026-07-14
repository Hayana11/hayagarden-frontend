import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { HttpError } from '../lib/http';
import { getGroupStatus, type AgentStatus } from '../lib/groupChat';
import {
  activateRelayEndpoint,
  clearRelayAccountCredentials,
  createRelayEndpoint,
  getAvailableModels,
  getConfigUsageSummary,
  getDailyUsage,
  getKeyStatus,
  getModelCatalog,
  getProviderConfig,
  getRelayAccountBalance,
  getRelayBalance,
  getRelayEndpoints,
  getRelayIntelligence,
  removeRelayEndpoint,
  runPlayground,
  saveRelayAccountCredentials,
  updateCurrentModel,
  updateProvider,
  type ConfigModel,
  type ConfigUsageSummary,
  type DailyUsage,
  type EndpointCapabilities,
  type KeyStatus,
  type RelayBalance,
  type RelayAccountBalance,
  type RelayDraft,
  type RelayEndpoint,
  type RelayIntelligence,
} from '../lib/systemConfig';

const EMPTY_CAPS: EndpointCapabilities = { thinking: true, cache: true, tools: true };
const EMPTY_RELAY: RelayDraft = { name: '', url: '', key: '', defaultModel: '', statusUrl: '', capabilities: EMPTY_CAPS };

function SectionLabel({ children, aside }: { children: React.ReactNode; aside?: React.ReactNode }) {
  return <div className="config-section-label"><span>{children}</span>{aside}</div>;
}

function CapabilityChips({ caps }: { caps: EndpointCapabilities }) {
  const labels: Array<[keyof EndpointCapabilities, string]> = [['thinking', '思考'], ['cache', '缓存'], ['tools', '工具']];
  return <div className="config-cap-chips">{labels.map(([key, label]) => <span key={key} className={caps[key] ? 'on' : 'off'}>{caps[key] ? '✓' : '×'} {label}</span>)}</div>;
}

function fmtTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}K`;
  return String(value);
}

function shortDate(value: string): string {
  return `${Number(value.slice(5, 7))}-${Number(value.slice(8, 10))}`;
}

function fmtUsd(value: number | null): string {
  return value === null ? '—' : `$${value.toFixed(value >= 100 ? 1 : 2)}`;
}

function balanceErrorText(error: RelayBalance['error']): string {
  if (error === 'missing_key') return '没有保存 API Key';
  if (error === 'unauthorized') return 'Key 无效或没有查询权限';
  if (error === 'unsupported') return '这个中转站不支持令牌余额接口';
  if (error === 'invalid_response') return '站点返回了无法识别的余额格式';
  return '余额接口暂时不可用';
}

function accountBalanceErrorText(error: RelayAccountBalance['error']): string {
  if (error === 'missing_credentials') return '还没有配置后台账户凭据';
  if (error === 'unauthorized') return 'Session Cookie 或用户 ID 已失效';
  if (error === 'unsupported') return '这个中转站不支持控制台余额接口';
  if (error === 'invalid_response') return '控制台返回了无法识别的余额格式';
  return '控制台余额暂时不可用';
}

async function getKeyStatusWithHostRtt(): Promise<{ status: KeyStatus; hostRttMs: number }> {
  const started = performance.now();
  const status = await getKeyStatus();
  return { status, hostRttMs: Math.round(performance.now() - started) };
}

export function SettingsScreen() {
  const navigate = useNavigate();
  const [provider, setProvider] = useState<'api_relay' | 'claude_code'>('api_relay');
  const [ccTokenSet, setCcTokenSet] = useState(false);
  const [codexStatus, setCodexStatus] = useState<AgentStatus | null>(null);
  const [keyStatus, setKeyStatus] = useState<KeyStatus | null>(null);
  const [hostRtt, setHostRtt] = useState<number | null>(null);
  const [relays, setRelays] = useState<RelayEndpoint[]>([]);
  const [catalog, setCatalog] = useState<ConfigModel[]>([]);
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [currentModel, setCurrentModel] = useState('');
  const [usage, setUsage] = useState<ConfigUsageSummary | null>(null);
  const [daily, setDaily] = useState<DailyUsage[]>([]);
  const [range, setRange] = useState<7 | 30>(30);
  const [selectedDay, setSelectedDay] = useState('');
  const [officialExpanded, setOfficialExpanded] = useState(false);
  const [expandedRelay, setExpandedRelay] = useState<number | null>(null);
  const [relayFilter, setRelayFilter] = useState('');
  const [relayInsights, setRelayInsights] = useState<Record<number, RelayIntelligence>>({});
  const [relayBalances, setRelayBalances] = useState<Record<number, RelayBalance>>({});
  const [relayAccountBalances, setRelayAccountBalances] = useState<Record<number, RelayAccountBalance>>({});
  const [accountAuthRelay, setAccountAuthRelay] = useState<number | null>(null);
  const [accountUserId, setAccountUserId] = useState('');
  const [accountCredentialKind, setAccountCredentialKind] = useState<'access_token' | 'session_cookie'>('access_token');
  const [accountCredentialSecret, setAccountCredentialSecret] = useState('');
  const [confirmCredentialClear, setConfirmCredentialClear] = useState<number | null>(null);
  const [modelFilter, setModelFilter] = useState('');
  const [busy, setBusy] = useState('');
  const [warning, setWarning] = useState('');
  const [toast, setToast] = useState('');
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [relayDraft, setRelayDraft] = useState<RelayDraft>(EMPTY_RELAY);
  const [sheetResult, setSheetResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [testInput, setTestInput] = useState('');
  const [injectMemory, setInjectMemory] = useState(true);
  const [testResult, setTestResult] = useState<{ meta: string; thinking: string; text: string; error: boolean } | null>(null);

  const showToast = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(''), 2400);
  }, []);

  const loadAll = useCallback(async () => {
    setBusy('load');
    setWarning('');
    const results = await Promise.allSettled([
      getProviderConfig(),
      getKeyStatusWithHostRtt(),
      getRelayEndpoints(),
      getModelCatalog(),
      getAvailableModels(),
      getConfigUsageSummary(),
      getDailyUsage(30),
      getGroupStatus(),
    ]);
    const [providerResult, keyResult, relayResult, catalogResult, availableResult, usageResult, dailyResult, groupStatusResult] = results;
    if (providerResult.status === 'fulfilled') {
      setProvider(providerResult.value.provider);
      setCcTokenSet(providerResult.value.ccTokenSet);
    }
    if (keyResult.status === 'fulfilled') {
      setKeyStatus(keyResult.value.status);
      setHostRtt(keyResult.value.hostRttMs);
    }
    if (relayResult.status === 'fulfilled') {
      setRelays(relayResult.value);
      const configured = relayResult.value.filter((relay) => relay.accountBalanceConfigured);
      void Promise.allSettled(configured.map(async (relay) => ({
        id: relay.id,
        balance: await getRelayAccountBalance(relay.id),
      }))).then((balanceResults) => {
        setRelayAccountBalances((current) => {
          const next = { ...current };
          for (const result of balanceResults) {
            if (result.status === 'fulfilled') next[result.value.id] = result.value.balance;
          }
          return next;
        });
      });
    }
    if (catalogResult.status === 'fulfilled') {
      setCatalog(catalogResult.value.models);
      setCurrentModel(catalogResult.value.current);
    }
    if (availableResult.status === 'fulfilled') setAvailableModels(availableResult.value);
    if (usageResult.status === 'fulfilled') setUsage(usageResult.value);
    if (dailyResult.status === 'fulfilled') {
      setDaily(dailyResult.value);
      setSelectedDay((current) => current || dailyResult.value.at(-1)?.date || '');
    }
    if (groupStatusResult.status === 'fulfilled') setCodexStatus(groupStatusResult.value.agents.codex);
    if (results.some((result) => result.status === 'rejected')) setWarning('部分实时数据暂时不可用，已保留成功读取的配置。');
    setBusy('');
  }, []);

  useEffect(() => { void loadAll(); }, [loadAll]);

  const activeRelay = relays.find((relay) => relay.active) || null;
  const currentEndpointName = provider === 'claude_code' ? 'Claude Code 订阅' : activeRelay?.name || '未选择中转站';
  const currentReady = provider === 'claude_code' ? ccTokenSet : Boolean(activeRelay && keyStatus);
  const shownDaily = range === 7 ? daily.slice(-7) : daily;
  const selectedUsage = shownDaily.find((item) => item.date === selectedDay) || shownDaily.at(-1);
  const maxDaily = Math.max(1, ...shownDaily.map((item) => item.count));
  const totalRequests = shownDaily.reduce((sum, item) => sum + item.count, 0);

  const unifiedModels = useMemo(() => {
    const byId = new Map<string, ConfigModel>();
    for (const model of catalog) byId.set(model.id, model);
    for (const id of availableModels) {
      if (!byId.has(id)) byId.set(id, { id, label: id, description: '', thinking: 'unknown', primary: false, dot: '#7A9B6D' });
    }
    const query = modelFilter.trim().toLowerCase();
    return [...byId.values()].filter((model) => !query || model.id.toLowerCase().includes(query) || model.label.toLowerCase().includes(query));
  }, [availableModels, catalog, modelFilter]);

  const activeRelayModels = useMemo(() => {
    const catalogById = new Map(catalog.map((model) => [model.id, model]));
    const query = relayFilter.trim().toLowerCase();
    return availableModels
      .map((id) => catalogById.get(id) || { id, label: id, description: '', thinking: 'unknown', primary: false, dot: '#7A9B6D' })
      .filter((model) => !query || model.id.toLowerCase().includes(query) || model.label.toLowerCase().includes(query));
  }, [availableModels, catalog, relayFilter]);

  const switchToClaude = async () => {
    if (!ccTokenSet) { showToast('Claude Code token 尚未在 VPS 配置'); return; }
    setBusy('provider');
    try {
      await updateProvider('claude_code');
      setProvider('claude_code');
      showToast('已切换：Claude Code 订阅');
    } catch { showToast('切换失败'); } finally { setBusy(''); }
  };

  const switchRelay = async (relay: RelayEndpoint) => {
    setBusy(`relay:${relay.id}`);
    try {
      const result = await activateRelayEndpoint(relay.id);
      await updateProvider('api_relay');
      setProvider('api_relay');
      setRelays(await getRelayEndpoints());
      if (result.model) setCurrentModel(result.model);
      const keyCheck = await getKeyStatusWithHostRtt();
      setKeyStatus(keyCheck.status);
      setHostRtt(keyCheck.hostRttMs);
      setAvailableModels(await getAvailableModels().catch(() => []));
      showToast(`已切换：${relay.name}`);
    } catch { showToast('中转站切换失败'); } finally { setBusy(''); }
  };

  const switchModel = async (model: ConfigModel) => {
    if (model.id === currentModel) return;
    setBusy(`model:${model.id}`);
    try {
      await updateCurrentModel(model.id);
      setCurrentModel(model.id);
      showToast(`已切换：${model.label}`);
    } catch { showToast('模型切换失败'); } finally { setBusy(''); }
  };

  const refreshModels = async () => {
    setBusy('models');
    try {
      setAvailableModels(await getAvailableModels());
      showToast('模型列表已刷新');
    } catch { showToast('当前中转站没有返回模型列表'); } finally { setBusy(''); }
  };

  const inspectRelay = async (relay: RelayEndpoint) => {
    setBusy(`inspect:${relay.id}`);
    try {
      const insight = await getRelayIntelligence(relay.id, true);
      setRelayInsights((items) => ({ ...items, [relay.id]: insight }));
      showToast(`已读取 ${relay.name} 的价格与状态`);
    } catch (error) {
      const detail = error instanceof HttpError && error.detail
        ? error.detail
        : '读取失败，请检查中转站地址、密钥或状态页地址';
      showToast(detail);
    } finally { setBusy(''); }
  };

  const queryRelayBalance = async (relay: RelayEndpoint) => {
    setBusy(`balance:${relay.id}`);
    try {
      const balance = await getRelayBalance(relay.id);
      setRelayBalances((items) => ({ ...items, [relay.id]: balance }));
      showToast(balance.supported ? `已读取 ${relay.name} 的密钥限额` : balanceErrorText(balance.error));
    } catch (error) {
      const detail = error instanceof HttpError && error.detail ? error.detail : '余额查询失败';
      showToast(detail);
    } finally { setBusy(''); }
  };

  const queryRelayAccountBalance = async (relay: RelayEndpoint) => {
    if (!relay.accountBalanceConfigured) {
      setAccountAuthRelay(relay.id);
      setAccountCredentialKind('access_token');
      setAccountCredentialSecret('');
      return;
    }
    setBusy(`account-balance:${relay.id}`);
    try {
      const balance = await getRelayAccountBalance(relay.id);
      setRelayAccountBalances((items) => ({ ...items, [relay.id]: balance }));
      if (balance.supported) {
        showToast(`已刷新 ${relay.name} 的账户余额`);
      } else {
        showToast(accountBalanceErrorText(balance.error));
      }
    } catch (error) {
      const detail = error instanceof HttpError && error.detail ? error.detail : '账户余额查询失败';
      showToast(detail);
    } finally { setBusy(''); }
  };

  const saveAccountCredentials = async (relay: RelayEndpoint) => {
    const userId = accountUserId.trim();
    const secret = accountCredentialSecret.trim();
    if (!/^\d+$/.test(userId) || !secret) {
      showToast('请填写数字用户 ID 和控制台凭据');
      return;
    }
    setAccountCredentialSecret('');
    setBusy(`account-credentials:${relay.id}`);
    let saved = false;
    try {
      await saveRelayAccountCredentials(relay.id, userId, accountCredentialKind, secret);
      saved = true;
      setRelays((items) => items.map((item) => item.id === relay.id ? {
        ...item,
        accountBalanceConfigured: true,
        accountCredentialKind,
      } : item));
      setAccountAuthRelay(null);
      setConfirmCredentialClear(null);
      const balance = await getRelayAccountBalance(relay.id);
      setRelayAccountBalances((items) => ({ ...items, [relay.id]: balance }));
      showToast(balance.supported ? `${relay.name} 的后台保险箱已配置` : accountBalanceErrorText(balance.error));
    } catch (error) {
      const fallback = saved ? '凭据已保存，但余额暂时读取失败' : '后台凭据保存失败';
      const detail = error instanceof HttpError && error.detail ? error.detail : fallback;
      showToast(detail);
    } finally { setBusy(''); }
  };

  const clearAccountCredentials = async (relay: RelayEndpoint) => {
    if (confirmCredentialClear !== relay.id) {
      setConfirmCredentialClear(relay.id);
      return;
    }
    setBusy(`clear-account-credentials:${relay.id}`);
    try {
      await clearRelayAccountCredentials(relay.id);
      setRelays((items) => items.map((item) => item.id === relay.id ? {
        ...item,
        accountBalanceConfigured: false,
        accountCredentialKind: '',
      } : item));
      setRelayAccountBalances((items) => {
        const next = { ...items };
        delete next[relay.id];
        return next;
      });
      setConfirmCredentialClear(null);
      showToast(`${relay.name} 的后台凭据已清除`);
    } catch { showToast('后台凭据清除失败'); } finally { setBusy(''); }
  };

  const deleteRelay = async (relay: RelayEndpoint) => {
    if (relay.active) { showToast('正在使用的中转站不能删除'); return; }
    if (confirmDelete !== relay.id) { setConfirmDelete(relay.id); return; }
    setBusy(`delete:${relay.id}`);
    try {
      await removeRelayEndpoint(relay.id);
      setRelays((items) => items.filter((item) => item.id !== relay.id));
      setExpandedRelay(null);
      setConfirmDelete(null);
      showToast(`已删除 ${relay.name}`);
    } catch { showToast('删除失败'); } finally { setBusy(''); }
  };

  const validateRelayDraft = () => {
    if (!relayDraft.url.trim()) { setSheetResult({ ok: false, text: '请先填写 Base URL' }); return; }
    try {
      const url = new URL(relayDraft.url.trim());
      if (!['http:', 'https:'].includes(url.protocol)) throw new Error('protocol');
      setSheetResult({ ok: true, text: '地址格式正常 · 保存并切换后可拉取模型验证连通性' });
    } catch { setSheetResult({ ok: false, text: 'Base URL 格式不正确' }); }
  };

  const saveRelay = async () => {
    if (!relayDraft.name.trim() || !relayDraft.url.trim()) { setSheetResult({ ok: false, text: '名称与 Base URL 为必填' }); return; }
    setBusy('save-relay');
    try {
      await createRelayEndpoint({ ...relayDraft, name: relayDraft.name.trim(), url: relayDraft.url.trim(), key: relayDraft.key.trim(), defaultModel: relayDraft.defaultModel.trim(), statusUrl: relayDraft.statusUrl.trim() });
      setRelays(await getRelayEndpoints());
      setSheetOpen(false);
      setRelayDraft(EMPTY_RELAY);
      setSheetResult(null);
      showToast(`已添加中转站 ${relayDraft.name.trim()}`);
    } catch { setSheetResult({ ok: false, text: '保存失败，请检查地址和权限' }); } finally { setBusy(''); }
  };

  const sendTest = async () => {
    if (!testInput.trim()) { showToast('先输入一句话'); return; }
    setBusy('test');
    setTestResult(null);
    try {
      const result = await runPlayground(testInput.trim(), injectMemory);
      const tokens = `${fmtTokens(result.inputTokens)} in / ${fmtTokens(result.outputTokens)} out`;
      setTestResult({
        meta: `${currentEndpointName} · ${currentModel || '默认模型'} · ${result.latencyMs || '—'}ms · ${tokens}`,
        thinking: result.thinking,
        text: result.content || result.error || '（空响应）',
        error: Boolean(result.error && !result.content),
      });
    } catch (error) {
      const detail = error instanceof HttpError && error.detail
        ? error.detail
        : '模型测试请求失败，请检查当前调用配置。';
      setTestResult({ meta: '连接失败', thinking: '', text: detail, error: true });
    } finally { setBusy(''); }
  };

  return (
    <div className="config-root dash-fullscreen-page">
      <main className="config-screen hide-scrollbar">
        <header className="config-header">
          <button type="button" onClick={() => navigate('/chat')}>‹</button>
          <h1>系统配置</h1>
          <button type="button" className="config-back-chat" onClick={() => navigate('/chat')}>返回聊天</button>
        </header>

        {warning && <div className="config-warning">{warning}</div>}

        <section className="config-card config-current">
          <div className="config-card-kicker"><span>CURRENT · 当前调用</span><span className={currentReady ? 'ready' : 'bad'}><i />{currentReady ? '就绪' : '配置不完整'}</span></div>
          <dl>
            <div><dt>端点</dt><dd>{currentEndpointName}</dd></div>
            <div><dt>模型</dt><dd>{currentModel || '—'}</dd></div>
            <div><dt>主站往返</dt><dd className="latency">{hostRtt === null ? '—' : `${hostRtt} ms`}</dd></div>
          </dl>
          <p>主站往返仅测网页到 VPS，不代表中转站延迟 · 聊天与下方「模型测试」均走此配置</p>
        </section>

        <SectionLabel>USAGE · 用量统计</SectionLabel>
        <section className="config-card config-usage">
          <div className="config-card-heading"><h2>用量日历</h2><span>按天 · 对话请求</span></div>
          <div className="config-segmented">
            <button type="button" className={range === 7 ? 'active' : ''} onClick={() => { setRange(7); setSelectedDay(daily.at(-1)?.date || ''); }}>一周</button>
            <button type="button" className={range === 30 ? 'active' : ''} onClick={() => { setRange(30); setSelectedDay(daily.at(-1)?.date || ''); }}>一个月</button>
          </div>
          {range === 7 ? (
            <div className="config-week-bars">
              {shownDaily.map((item) => <button type="button" key={item.date} onClick={() => setSelectedDay(item.date)}><span>{item.count}</span><i className={selectedUsage?.date === item.date ? 'selected' : ''} style={{ height: `${Math.max(8, Math.round((item.count / maxDaily) * 118))}px` }} /><small>{shortDate(item.date)}</small></button>)}
            </div>
          ) : (
            <div className="config-month-grid">
              {shownDaily.map((item) => <button type="button" key={item.date} onClick={() => setSelectedDay(item.date)}><span className={selectedUsage?.date === item.date ? 'selected' : ''}><i style={{ height: `${Math.max(5, Math.round((item.count / maxDaily) * 100))}%` }} /><em>{item.count}</em></span><small>{shortDate(item.date)}</small></button>)}
            </div>
          )}
          <div className="config-day-detail">{selectedUsage ? `${shortDate(selectedUsage.date)} · ${selectedUsage.count} 次请求` : '暂无用量数据'}</div>
          <div className="config-usage-summary"><span>合计 <b>{totalRequests}</b> 次请求</span><span>今日 <b>{usage?.todayMessages ?? 0}</b> 条消息</span><span>约 <b>{fmtTokens(usage?.todayTokens ?? 0)}</b> token</span></div>
        </section>

        <SectionLabel>OFFICIAL · 官方端点</SectionLabel>
        <section className={`config-card config-endpoint${provider === 'claude_code' ? ' current' : ''}`}>
          <button className="config-endpoint-summary" type="button" onClick={() => setOfficialExpanded((value) => !value)}>
            <i className={ccTokenSet ? 'online' : 'offline'} />
            <span><strong>Claude Code 订阅</strong><small>VPS 终端凭据 · 官方原生</small></span>
            <em>{ccTokenSet ? '已连接' : '未配置'}<small>{catalog.length} 个策展模型</small></em>
            <b className={officialExpanded ? 'open' : ''}>▾</b>
          </button>
          <div className="config-endpoint-row"><CapabilityChips caps={{ thinking: true, cache: true, tools: false }} />{provider === 'claude_code' ? <span className="config-current-badge">使用中</span> : <button type="button" onClick={() => void switchToClaude()} disabled={Boolean(busy)}>切换</button>}</div>
          <div className="config-quota"><div><span>5 小时窗 <b>已用 {usage?.win5Pct ?? 0}%</b></span><i><em style={{ width: `${usage?.win5Pct ?? 0}%` }} /></i></div><div><span>周额度 <b>已用 {usage?.win7Pct ?? 0}%</b></span><i><em className="rose" style={{ width: `${usage?.win7Pct ?? 0}%` }} /></i></div></div>
          <div className="config-effort"><span>Effort</span><div><button type="button" disabled>LOW</button><button type="button" disabled>MED</button><button type="button" disabled>HIGH</button></div><small>后端尚未接入</small></div>
          {officialExpanded && <div className="config-endpoint-expanded"><div className="config-expanded-title"><strong>订阅配置</strong><span>凭据仅在 VPS 终端管理</span></div><div className="config-model-chips">{catalog.slice(0, 6).map((model) => <span key={model.id}>{model.label}</span>)}</div><div className="config-key-row"><span>OAUTH TOKEN</span><b>{ccTokenSet ? '已配置 · 不回传网页' : '未设置'}</b></div></div>}
        </section>

        <section className="config-card config-endpoint">
          <div className="config-endpoint-summary" style={{ cursor: 'default' }}>
            <i className={codexStatus?.ready ? 'online' : 'offline'} />
            <span><strong>Codex 官方订阅</strong><small>本地 CLI · 群聊蓝色线路</small></span>
            <em>{codexStatus === null ? '读取中…' : codexStatus.ready ? '已连接' : codexStatus.installed ? '未登录' : '未安装'}<small>{codexStatus?.detail || ''}</small></em>
          </div>
          <div className="config-endpoint-row">
            <small>不接入 Fyodor 的官方端点切换 · 只服务群聊里的蓝色线路</small>
            {codexStatus?.ready
              ? <Link to="/codex-chat" className="config-current-badge" style={{ textDecoration: 'none' }}>去聊天</Link>
              : <button type="button" disabled>{codexStatus?.installed ? '等待登录' : '未安装'}</button>}
          </div>
        </section>

        <SectionLabel aside={<span>{relays.length} 个预设</span>}>RELAYS · 中转站</SectionLabel>
        <section className="config-card config-failover">
          <div><strong>自动故障转移</strong><span>后端尚未接入 · 当前仍为手动切换</span></div>
          <button type="button" aria-disabled="true" onClick={() => showToast('自动故障转移后端尚未接入')}><i /></button>
        </section>

        {relays.map((relay, index) => {
          const expanded = expandedRelay === relay.id;
          const relayModels = relay.active ? activeRelayModels : [];
          const insight = relayInsights[relay.id];
          const balance = relayBalances[relay.id];
          const accountBalance = relayAccountBalances[relay.id];
          const query = relayFilter.trim().toLowerCase();
          const inspectedModels = (insight?.modelOptions || []).filter((model) => !query || model.id.toLowerCase().includes(query) || model.name?.toLowerCase().includes(query));
          const isCurrent = provider === 'api_relay' && relay.active;
          return <section className={`config-card config-endpoint${isCurrent ? ' current' : ''}`} key={relay.id}>
            <button className="config-endpoint-summary" type="button" onClick={() => { setExpandedRelay(expanded ? null : relay.id); setConfirmDelete(null); setConfirmCredentialClear(null); setRelayFilter(''); setAccountAuthRelay(null); setAccountUserId(''); setAccountCredentialSecret(''); }}>
              <i className={relay.active ? 'online' : 'saved'} />
              <span><strong>{relay.name} {index < 5 && <small className="config-role">{index === 0 ? '主' : `备${index}`}</small>}</strong><small>{relay.url}</small></span>
              <em>{relay.active ? '已激活' : '已保存'}<small>{relay.defaultModel || '未指定默认模型'}</small></em>
              <b className={expanded ? 'open' : ''}>▾</b>
            </button>
            <div className="config-endpoint-row"><CapabilityChips caps={relay.capabilities} />{isCurrent ? <span className="config-current-badge">使用中</span> : <button type="button" onClick={() => void switchRelay(relay)} disabled={Boolean(busy)}>切换</button>}</div>
            {expanded && <div className="config-endpoint-expanded">
              {accountBalance && <div className={`config-balance config-account-balance${accountBalance.supported ? '' : ' unavailable'}`}>
                {accountBalance.supported ? <>
                  <div><span>控制台账户余额</span><strong>{fmtUsd(accountBalance.remainingUsd)}</strong></div>
                  <dl><div><dt>当前余额</dt><dd>{fmtUsd(accountBalance.remainingUsd)}</dd></div><div><dt>累计已用</dt><dd>{fmtUsd(accountBalance.usedUsd)}</dd></div><div><dt>历史合计</dt><dd>{fmtUsd(accountBalance.totalUsd)}</dd></div></dl>
                  <small>后台保险箱：{relay.accountCredentialKind === 'access_token' ? '控制台访问令牌' : 'Session Cookie'} · 已加密保存</small>
                </> : <><span>账户余额暂不可查</span><strong>{accountBalanceErrorText(accountBalance.error)}</strong></>}
              </div>}
              {balance && <div className={`config-balance${balance.supported ? '' : ' unavailable'}`}>
                {balance.supported ? <>
                  <div><span>API Key 限额</span><strong>{balance.unlimited ? '密钥不限额' : fmtUsd(balance.totalAvailableUsd)}</strong></div>
                  <dl><div><dt>密钥总限额</dt><dd>{balance.unlimited ? '不限额' : fmtUsd(balance.totalGrantedUsd)}</dd></div><div><dt>密钥已使用</dt><dd>{fmtUsd(balance.totalUsedUsd)}</dd></div><div><dt>有效期</dt><dd>{balance.expiresAt ? new Date(balance.expiresAt * 1000).toLocaleDateString('zh-CN') : '永不过期'}</dd></div></dl>
                  <small>{balance.unlimited ? '这里只代表 API Key 没有单独上限，不代表控制台账户余额无限' : ''}{balance.name ? `${balance.unlimited ? ' · ' : ''}令牌：${balance.name}` : ''}</small>
                </> : <><span>密钥限额暂不可查</span><strong>{balanceErrorText(balance.error)}</strong></>}
              </div>}
              {accountAuthRelay === relay.id && <form className="config-account-auth" onSubmit={(event) => { event.preventDefault(); void saveAccountCredentials(relay); }}>
                <strong>{relay.accountBalanceConfigured ? '更新后台账户凭据' : '配置后台账户保险箱'}</strong>
                <p>凭据会加密保存；网页以后只能看到“已配置”，不能取回原文。访问令牌可单独撤销，比登录 Cookie 更适合长期使用。</p>
                <div className="config-credential-kind"><button type="button" className={accountCredentialKind === 'access_token' ? 'active' : ''} onClick={() => { setAccountCredentialKind('access_token'); setAccountCredentialSecret(''); }}>访问令牌 · 推荐</button><button type="button" className={accountCredentialKind === 'session_cookie' ? 'active' : ''} onClick={() => { setAccountCredentialKind('session_cookie'); setAccountCredentialSecret(''); }}>Session Cookie · 兼容</button></div>
                <div className="config-account-fields"><label>用户 ID<input inputMode="numeric" autoComplete="off" value={accountUserId} onChange={(event) => setAccountUserId(event.target.value)} placeholder="New-Api-User" /></label><label>{accountCredentialKind === 'access_token' ? '控制台访问令牌' : 'Session Cookie'}<input type="password" autoComplete="new-password" value={accountCredentialSecret} onChange={(event) => setAccountCredentialSecret(event.target.value)} placeholder={accountCredentialKind === 'access_token' ? '访问令牌' : 'session=…'} /></label></div>
                <footer><button type="submit" disabled={busy === `account-credentials:${relay.id}`}>{busy === `account-credentials:${relay.id}` ? '加密保存中…' : '加密保存并查询'}</button><button type="button" onClick={() => { setAccountAuthRelay(null); setAccountCredentialSecret(''); }}>取消</button></footer>
              </form>}
              <div className="config-expanded-title"><strong>模型 · {insight ? insight.modelOptions.length : relay.active ? relayModels.length : '尚未读取'}</strong><span>{insight ? '已单独读取此端点' : relay.active ? '当前端点返回列表' : '点“价格与状态”即可读取'}</span></div>
              {(insight || relay.active) && <input value={relayFilter} onChange={(event) => setRelayFilter(event.target.value)} placeholder="筛选模型…" />}
              {!insight && <div className="config-model-chips">{relay.active ? relayModels.slice(0, 24).map((model) => <span key={model.id}>{model.label}</span>) : <span>无需切换，点下方按钮即可检查</span>}</div>}
              {insight && <div className="config-intel">
                <div className="config-intel-summary">
                  <span><i className="ok" />{insight.modelOptions.length} 个模型</span>
                  <span><i className={insight.statusSource ? 'ok' : 'unknown'} />{insight.statusSource ? '已找到状态页' : '暂无状态源'}</span>
                  <span><i className={insight.pricingSource && !insight.pricingRequiresAuth ? 'ok' : 'unknown'} />{insight.pricingRequiresAuth ? '价格页需登录' : insight.pricingSource ? '已读取价格' : '暂无价格源'}</span>
                </div>
                <div className="config-intel-models">
                  {inspectedModels.slice(0, 30).map((model) => {
                    const state = model.status?.status === 1 ? 'online' : model.status?.status === 0 ? 'offline' : 'unknown';
                    const stateLabel = state === 'online' ? '正常' : state === 'offline' ? '异常' : '未监控';
                    return <div className="config-intel-model" key={model.id}>
                      <i className={state} />
                      <span><strong>{model.name || model.id}</strong><small>{model.group || model.route || '默认分组'}{model.price ? ` · ${model.price}` : ' · 未公开价格'}</small></span>
                      <em>{stateLabel}{model.status?.message ? <small>{model.status.message}</small> : model.status?.ping != null ? <small>{model.status.ping} ms</small> : null}</em>
                    </div>;
                  })}
                  {!inspectedModels.length && <p>没有匹配的模型</p>}
                </div>
                <p className="config-intel-note">状态只做精确模型名匹配，不会把相似名字误判成同一个模型。{insight.cached ? ' · 使用一分钟缓存' : ''}</p>
              </div>}
              <div className="config-key-row"><span>API KEY</span><b>{relay.active ? keyStatus?.maskedKey || '—' : '已保存 · 不回传网页'}</b></div>
              {relay.statusUrl && <div className="config-key-row"><span>状态页</span><b>{relay.statusUrl}</b></div>}
              <div className="config-endpoint-actions"><button type="button" onClick={() => void queryRelayAccountBalance(relay)} disabled={busy === `account-balance:${relay.id}`}>{busy === `account-balance:${relay.id}` ? '刷新中…' : relay.accountBalanceConfigured ? '刷新账户余额' : '设置账户余额'}</button>{relay.accountBalanceConfigured && <button type="button" onClick={() => { setAccountAuthRelay(accountAuthRelay === relay.id ? null : relay.id); setAccountUserId(''); setAccountCredentialKind(relay.accountCredentialKind || 'access_token'); setAccountCredentialSecret(''); }}>{accountAuthRelay === relay.id ? '收起凭据设置' : '更新账户凭据'}</button>}{relay.accountBalanceConfigured && <button type="button" className="danger" onClick={() => void clearAccountCredentials(relay)} disabled={busy === `clear-account-credentials:${relay.id}`}>{confirmCredentialClear === relay.id ? '确认清除凭据？' : '清除账户凭据'}</button>}<button type="button" onClick={() => void queryRelayBalance(relay)} disabled={busy === `balance:${relay.id}`}>{busy === `balance:${relay.id}` ? '查询中…' : balance ? '刷新密钥限额' : '查密钥限额'}</button><button type="button" onClick={() => void inspectRelay(relay)} disabled={busy === `inspect:${relay.id}`}>{busy === `inspect:${relay.id}` ? '读取中…' : insight ? '刷新价格与状态' : '价格与状态'}</button><button type="button" onClick={() => relay.active ? void refreshModels() : showToast('价格与状态可以直接读取；普通模型池需先切换')}>拉取模型</button><button type="button" className="danger" onClick={() => void deleteRelay(relay)}>{confirmDelete === relay.id ? '确认删除？' : '删除'}</button></div>
              <div className="config-priority"><span>启用</span><button type="button" className="locked-switch" aria-disabled="true" onClick={() => showToast('端点启停后端尚未接入')}><i /></button><em>启停与优先级后端尚未接入</em><button type="button" disabled>↑</button><button type="button" disabled>↓</button></div>
            </div>}
          </section>;
        })}
        <button type="button" className="config-add-relay" onClick={() => { setSheetOpen(true); setRelayDraft(EMPTY_RELAY); setSheetResult(null); }}>＋ 添加中转站</button>

        <SectionLabel>MODELS · 统一模型池</SectionLabel>
        <section className="config-card config-model-pool">
          <div className="config-card-heading"><h2>模型池</h2><button type="button" onClick={() => void refreshModels()}>{busy === 'models' ? '拉取中…' : '⟳ 统一拉取'}</button></div>
          <p>当前端点实时模型 + models.json 策展清单 · 共 {unifiedModels.length} 个</p>
          <h3>常用预设</h3>
          <div className="config-preset-list">{catalog.filter((model) => model.primary).map((model) => <button type="button" key={model.id} onClick={() => void switchModel(model)}><i style={{ background: model.dot }} /><span><strong>{model.label}</strong><small>{activeRelay?.name || currentEndpointName} · {model.id}</small></span>{model.id === currentModel ? <em>使用中</em> : <b>切换</b>}</button>)}</div>
          <input value={modelFilter} onChange={(event) => setModelFilter(event.target.value)} placeholder="搜索全部模型…" />
          <div className="config-unified-list">{unifiedModels.map((model) => <button type="button" key={model.id} onClick={() => void switchModel(model)} disabled={Boolean(busy)}><i style={{ background: model.dot }} /><span>{activeRelay?.name || '策展'}</span><strong>{model.id}</strong>{model.id === currentModel ? <em>使用中</em> : <b>切换</b>}</button>)}{!unifiedModels.length && <div>当前端点没有返回模型</div>}</div>
        </section>

        <SectionLabel>PLAYGROUND · 模型测试</SectionLabel>
        <section className="config-card config-playground">
          <div className="config-card-heading"><h2>模型测试</h2><span>不写入正式对话</span></div>
          <div className="config-playground-current"><i className={currentReady ? 'ready' : ''} /><span>当前：{currentEndpointName} · {currentModel || '默认模型'}</span></div>
          <div className="config-playground-chips"><button type="button" className={injectMemory ? 'active memory' : ''} onClick={() => setInjectMemory((value) => !value)}>{injectMemory ? '✓ ' : ''}注入记忆</button><span>思考由当前线路决定</span></div>
          <div className="config-playground-input"><input value={testInput} onChange={(event) => setTestInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void sendTest(); }} placeholder="说点什么试试……" /><button type="button" onClick={() => void sendTest()} disabled={busy === 'test'}>↑</button></div>
          {busy === 'test' && <div className="config-thinking">费佳思考中…</div>}
          {testResult && <div className={`config-test-result${testResult.error ? ' error' : ''}`}><small>{testResult.meta}</small>{testResult.thinking && <div>{testResult.thinking}</div>}<p>{testResult.text}</p></div>}
        </section>

        <div className="config-api-note">实时接口：usage · provider · relay presets · model catalog · gateway test</div>
      </main>

      {sheetOpen && <div className="config-sheet-wrap" role="dialog" aria-modal="true" aria-label="添加中转站"><button type="button" className="config-sheet-backdrop" onClick={() => setSheetOpen(false)} /><div className="config-sheet"><i /><h2>添加中转站</h2><label>名称<input value={relayDraft.name} onChange={(event) => setRelayDraft((draft) => ({ ...draft, name: event.target.value }))} placeholder="例如：OpenRouter" /></label><label>Base URL<input value={relayDraft.url} onChange={(event) => setRelayDraft((draft) => ({ ...draft, url: event.target.value }))} placeholder="https://…/v1/messages" /></label><label>API Key（可选）<input type="password" value={relayDraft.key} onChange={(event) => setRelayDraft((draft) => ({ ...draft, key: event.target.value }))} placeholder="sk-…" /></label><label>状态页地址（可选）<input value={relayDraft.statusUrl} onChange={(event) => setRelayDraft((draft) => ({ ...draft, statusUrl: event.target.value }))} placeholder="Uptime Kuma 或模型监控页" /></label><label>默认模型（可选）<input value={relayDraft.defaultModel} onChange={(event) => setRelayDraft((draft) => ({ ...draft, defaultModel: event.target.value }))} placeholder="model id" /></label>{sheetResult && <p className={sheetResult.ok ? 'ok' : 'bad'}>{sheetResult.text}</p>}<div><button type="button" onClick={validateRelayDraft}>测试连接</button><button type="button" onClick={() => void saveRelay()} disabled={busy === 'save-relay'}>{busy === 'save-relay' ? '保存中…' : '保存'}</button></div></div></div>}
      {toast && <div className="config-toast">{toast}</div>}
    </div>
  );
}
