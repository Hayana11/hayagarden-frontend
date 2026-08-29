import { type CSSProperties, type FormEvent, useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { http } from '../lib/http';
import { realityPromptProjection } from '../lib/reality/realityPromptProjection';
import { realityStore } from '../lib/reality/realityRuntime';
import { getRealityFreshness } from '../lib/reality/realityStore';
import './ToolroomScreen.css';

type InventoryTool = {
  tool_name: string;
  display_label: string;
  available: boolean;
  status_label: string;
  reason_code: string;
  provider: string | null;
};

type InventoryGroup = {
  id: string;
  label: string;
  total: number;
  available: number;
  tools: InventoryTool[];
};

type InventoryResponse = {
  ok: boolean;
  version: string;
  total: number;
  available_count: number;
  unavailable_count: number;
  groups: InventoryGroup[];
  error?: string;
};

type ToolroomTab = 'tools' | 'activity';

const GROUP_ICON_PATHS: Record<string, string> = {
  memory: 'M9.4 4.2c-2.2 0-3.8 1.6-3.8 3.7 0 .4.1.8.2 1.1-1.1.6-1.8 1.7-1.8 3 0 1.8 1.5 3.3 3.3 3.3h.4v2.1c0 1.3 1 2.4 2.4 2.4 1 0 1.8-.6 2.2-1.5.5.9 1.4 1.5 2.4 1.5 1.4 0 2.5-1.1 2.5-2.5v-1.9h.3c1.8 0 3.2-1.4 3.2-3.2 0-1.2-.6-2.2-1.6-2.8.1-.3.2-.7.2-1.1 0-2-1.5-3.5-3.5-3.5-.7 0-1.4.2-1.9.6-.7-.7-1.6-1.1-2.3-1.1Z M8.2 10.3h2.1m3.4 0h2.1m-5.5 3h2.8',
  web: 'M8.4 12.8 6.7 14.5a2.8 2.8 0 0 1-4-4l2.2-2.2a2.8 2.8 0 0 1 4 0m2.4 2.4 1.7-1.7a2.8 2.8 0 0 1 4 4l-2.2 2.2a2.8 2.8 0 0 1-4 0m-3.7-1.8 5.5-5.5',
  pocket: 'M4 4h16v16H4z M8 8h8v8H8z',
  light: 'M12 4.5a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9Zm0-3v2m0 12v2m9-6h-2m-14 0H3m15.4-6.4-1.4 1.4m-10 10-1.4 1.4m0-12.8 1.4 1.4m10 10 1.4 1.4',
  shopping: 'm12 3 8 9-8 9-8-9 8-9Z',
  gallery: 'M4 5h16v14H4z M7 16l3.5-3.5 2.5 2.5 2-2 2 3 M8 9.2h.01',
  code_files: 'M7 12h10 M12 7v10 M5 5h.01M19 5h.01M5 19h.01M19 19h.01',
  workspace: 'M12 3 20 12 12 21 4 12 12 3Z M8.5 12h7',
  self_config: 'M12 3v3m0 12v3M3 12h3m12 0h3m-3.4-6.6-2.1 2.1m-7 7-2.1 2.1m0-11.2 2.1 2.1m7 7 2.1 2.1 M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z',
  board: 'M5 5h14v14H5z M8 9h8M8 12h8M8 15h5',
  life: 'M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10Z',
  plans_ledger: 'M5 4h12a2 2 0 0 1 2 2v14H7a2 2 0 0 1-2-2V4Zm0 0v14a2 2 0 0 0 2 2m3-11h6m-6 4h6',
  desire: 'M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10Z',
  triggers: 'M13 2 5 13h6l-1 9 8-11h-6l1-9Z',
  artifacts: 'M12 3 19.8 7.5v9L12 21l-7.8-4.5v-9L12 3Z',
  phone: 'M7 3h10v18H7z M10 18h4',
  moments: 'M4 5h10v12H4z M10 8h10v11H10z',
};

function ToolroomGroupIcon({ groupId }: { groupId: string }) {
  return (
    <svg viewBox="0 0 24 24" role="presentation" focusable="false">
      <path d={GROUP_ICON_PATHS[groupId] || GROUP_ICON_PATHS.workspace} />
    </svg>
  );
}

function ToolroomDeviceIcon({ kind }: { kind: 'phone' | 'computer' }) {
  const path = kind === 'phone'
    ? 'M7 3h10v18H7z M10 18h4'
    : 'M4 5h16v11H4z M9 21h6m-3-5v5';
  return (
    <svg viewBox="0 0 24 24" role="presentation" focusable="false">
      <path d={path} />
    </svg>
  );
}

type ExternalMcpAuthScheme = 'none' | 'bearer';

type ExternalMcpIcon = 'default' | 'server' | 'globe' | 'plug' | 'spark';

type ExternalMcpForm = {
  icon: ExternalMcpIcon;
  name: string;
  description: string;
  url: string;
  auth: ExternalMcpAuthScheme;
};

const DEFAULT_EXTERNAL_MCP_FORM: ExternalMcpForm = {
  icon: 'default',
  name: '',
  description: '',
  url: '',
  auth: 'none',
};

const EXTERNAL_MCP_ICON_PATHS: Record<Exclude<ExternalMcpIcon, 'default'>, string> = {
  server: 'M4 5h16v14H4z M8 9h8M8 13h5 M7 19v2m10-2v2',
  globe: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm-8.4 6h16.8M3.6 15h16.8M12 3c2.1 2.4 3.2 5.4 3.2 9S14.1 18.6 12 21c-2.1-2.4-3.2-5.4-3.2-9S9.9 5.4 12 3Z',
  plug: 'M9 3v6m6-6v6m-8 0h10v2a5 5 0 0 1-10 0V9Zm5 7v5',
  spark: 'M12 3l1.8 6.2L20 11l-6.2 1.8L12 19l-1.8-6.2L4 11l6.2-1.8L12 3Z',
};

const EXTERNAL_MCP_ICON_OPTIONS: ExternalMcpIcon[] = ['default', 'server', 'globe', 'plug', 'spark'];
const EXTERNAL_MCP_ICON_LABELS: Record<ExternalMcpIcon, string> = {
  default: '默认图标（按名称匹配）',
  server: '服务器图标',
  globe: '地球图标',
  plug: '插头图标',
  spark: '星芒图标',
};

function externalMcpIconForName(name: string): Exclude<ExternalMcpIcon, 'default'> {
  const source = Array.from(name.trim() || 'mcp');
  const score = source.reduce((total, character) => total + character.charCodeAt(0), 0);
  const icons: Array<Exclude<ExternalMcpIcon, 'default'>> = ['server', 'globe', 'plug', 'spark'];
  return icons[score % icons.length];
}

function ExternalMcpIconView({ icon, name = '' }: { icon: ExternalMcpIcon; name?: string }) {
  const resolvedIcon = icon === 'default' ? externalMcpIconForName(name) : icon;
  return (
    <svg viewBox="0 0 24 24" role="presentation" focusable="false">
      <path d={EXTERNAL_MCP_ICON_PATHS[resolvedIcon]} />
    </svg>
  );
}

const DISABLED_TOOL_ACCENT = '#C7B9B5';

const GROUP_ACCENT_COLORS: Record<string, string> = {
  memory: '#8A7AB5',
  web: '#9FB6C7',
  pocket: '#C08497',
  light: '#7FA98F',
  shopping: '#C08497',
  gallery: '#8EA5B8',
  code_files: '#8EA5B8',
  workspace: '#8EA5B8',
  self_config: '#8EA5B8',
  board: '#B76E79',
  life: '#5E7F98',
  plans_ledger: '#D9A441',
  desire: '#B76E79',
  triggers: '#D9A441',
  artifacts: '#8EA5B8',
  phone: '#5E7F98',
  moments: '#B76E79',
};

function toolAccent(groupId: string, available: boolean): string {
  if (!available) return DISABLED_TOOL_ACCENT;
  return GROUP_ACCENT_COLORS[groupId] || '#8EA5B8';
}

function transportLabel(group: InventoryGroup): string {
  const providers = group.tools.map((tool) => tool.provider || '').join(' ');
  if (/\bsse\b/i.test(providers)) return 'SSE';
  if (/\bhttps?:\/\//i.test(providers) || /\bhttp\b/i.test(providers)) return 'HTTP';
  if (/\bcanary\b/i.test(providers)) return 'Canary';
  return '其他';
}

const ORIENTATION_LABELS = {
  face_up: '正面朝上',
  face_down: '背面朝上',
  vertical: '竖直',
  tilted: '倾斜',
  unknown: '未知',
} as const;

const MOTION_LABELS = {
  still: '静止',
  moving: '移动中',
  unknown: '未知',
} as const;

const PROXIMITY_LABELS = {
  near: '已遮挡',
  far: '未遮挡',
  unknown: '未知',
} as const;

const LIGHT_LABELS = {
  dark: '黑暗',
  dim: '较暗',
  moderate: '适中',
  bright: '明亮',
  unknown: '未知',
} as const;

function sourceLabel(provider: string | null): string {
  if (!provider) return 'Legacy';
  if (provider.indexOf('mcp__home__') === 0) return 'Home MCP';
  if (provider.indexOf('mcp__codebase') === 0) return 'Codebase MCP';
  if (provider.indexOf('Claude Code') === 0) return 'Claude Code';
  return provider;
}

function formatObservedAt(observedAt: number | null): string {
  if (observedAt === null || !Number.isFinite(observedAt)) return '暂无观测';
  return new Date(observedAt).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function toolMatches(tool: InventoryTool, query: string): boolean {
  return [
    tool.tool_name,
    tool.display_label,
    tool.provider || '',
    tool.reason_code,
    tool.status_label,
  ].some((value) => value.toLocaleLowerCase().includes(query));
}

export function ToolroomScreen() {
  const navigate = useNavigate();
  const [tab, setTab] = useState<ToolroomTab>('tools');
  const [inventory, setInventory] = useState<InventoryResponse | null>(null);
  const [inventoryError, setInventoryError] = useState('');
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({});
  const [openTools, setOpenTools] = useState<Record<string, boolean>>({});
  const [openPanels, setOpenPanels] = useState<Record<string, boolean>>({});
  const [addMcpOpen, setAddMcpOpen] = useState(false);
  const [externalMcpForm, setExternalMcpForm] = useState<ExternalMcpForm>(DEFAULT_EXTERNAL_MCP_FORM);
  const [externalMcpNotice, setExternalMcpNotice] = useState('');

  const reality = useSyncExternalStore(
    (listener) => realityStore.subscribe(listener),
    () => realityStore.getSnapshot(),
    () => realityStore.getSnapshot(),
  );
  const prompt = useSyncExternalStore(
    (listener) => realityPromptProjection.subscribe(listener),
    () => realityPromptProjection.getSnapshot(),
    () => realityPromptProjection.getSnapshot(),
  );

  const loadInventory = useCallback(async () => {
    setLoading(true);
    setInventoryError('');
    try {
      const result = await http.get<InventoryResponse>('/api/tools/inventory');
      if (!result.ok) throw new Error(result.error || '工具清单读取失败');
      setInventory(result);
    } catch (error) {
      setInventory(null);
      setInventoryError(error instanceof Error ? error.message : '工具清单读取失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadInventory();
  }, [loadInventory]);

  useEffect(() => {
    if (!addMcpOpen) return undefined;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setAddMcpOpen(false);
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [addMcpOpen]);

  const openAddMcpDialog = () => {
    setExternalMcpForm(DEFAULT_EXTERNAL_MCP_FORM);
    setExternalMcpNotice('');
    setAddMcpOpen(true);
  };

  const handleExternalMcpPreviewSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setExternalMcpNotice('已保留在当前页面预览中；尚未接入 External MCP Registry。');
  };

  const normalizedSearch = search.trim().toLocaleLowerCase();
  const filteredGroups = useMemo(() => {
    if (!inventory) return [];
    if (!normalizedSearch) return inventory.groups;
    const groups: InventoryGroup[] = [];
    for (const group of inventory.groups) {
      const groupMatches = group.label.toLocaleLowerCase().includes(normalizedSearch)
        || group.id.toLocaleLowerCase().includes(normalizedSearch);
      const tools = groupMatches
        ? group.tools
        : group.tools.filter((tool) => toolMatches(tool, normalizedSearch));
      if (tools.length > 0) {
        groups.push({ ...group, tools });
      }
    }
    return groups;
  }, [inventory, normalizedSearch]);

  const freshness = getRealityFreshness(reality, Date.now());
  const facts = reality.physical.facts;
  const statusLabel = freshness.status === 'fresh'
    ? '实时'
    : freshness.status === 'stale'
      ? '已过期'
      : '未连接';

  const activityPanels = [
    {
      id: 'physical',
      title: '现实传感器诊断',
      subtitle: 'ElpisPhysical · schema v1',
      status: statusLabel,
      rows: [
        ['姿态', ORIENTATION_LABELS[facts.orientation]],
        ['动作', MOTION_LABELS[reality.physical.motion]],
        ['环境光线', LIGHT_LABELS[facts.lightExposure]],
        ['距离传感器', PROXIMITY_LABELS[facts.proximity]],
        ['电量', facts.batteryLevel === null ? '未知' : String(facts.batteryLevel) + '%'],
        ['充电', facts.charging === null ? '未知' : facts.charging ? '是' : '否'],
        ['观测时间', formatObservedAt(reality.physical.observedAt)],
      ],
    },
    {
      id: 'prompt',
      title: '实际注入 Prompt',
      subtitle: '系统配置 · Reality Prompt 预览',
      status: prompt.text ? '有内容' : '空',
      rows: [
        ['字符数', String(Array.from(prompt.text).length)],
        ['数据来源', prompt.text ? '设备现实状态' : '无可用状态'],
        ['更新语义', '状态变化时刷新'],
      ],
    },
    {
      id: 'boundary',
      title: '数据边界',
      subtitle: 'Read-only source contract',
      status: '只读',
      rows: [
        ['工具', '/api/tools/inventory'],
        ['手机', 'ElpisPhysical'],
        ['桌面观测', '未接入'],
        ['通知内容', '未接入'],
      ],
    },
  ];

  const meta = inventory
    ? String(inventory.groups.length) + ' 组 · ' + String(inventory.total) + ' 个工具'
    : loading ? '正在读取' : '读取失败';

  return (
    <main className="toolroom-root dash-fullscreen-page">
      <div className="toolroom-header-wrap">
        <PageHeader
          title="Toolroom"
          subtitle={'工具室 · ' + meta}
          onBack={() => navigate(-1)}
          backLabel="返回聊天"
        />
      </div>

      <div className="toolroom-tabs page-tab-bar" role="tablist" aria-label="工具室页面">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'tools'}
          className={'toolroom-tab page-tab-item' + (tab === 'tools' ? ' act' : '')}
          onClick={() => setTab('tools')}
        >
          工具室
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'activity'}
          className={'toolroom-tab page-tab-item' + (tab === 'activity' ? ' act' : '')}
          onClick={() => setTab('activity')}
        >
          活动页
        </button>
      </div>

      {tab === 'tools' ? (
        <section className="toolroom-scroll" aria-label="工具清单">
          <div className="toolroom-search">
            <span aria-hidden="true">⌕</span>
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索工具名称或来源…"
              aria-label="搜索工具"
            />
            {search ? (
              <button type="button" onClick={() => setSearch('')} aria-label="清空搜索">×</button>
            ) : null}
            <button
              type="button"
              className="toolroom-add-button"
              onClick={openAddMcpDialog}
              aria-label="添加外部 MCP"
            >
              +
            </button>
          </div>

          {loading ? <div className="toolroom-state">正在读取真实工具清单…</div> : null}
          {inventoryError ? (
            <div className="toolroom-state toolroom-state--error" role="alert">
              <span>{inventoryError}</span>
              <button type="button" onClick={() => void loadInventory()}>重新读取</button>
            </div>
          ) : null}

          {!loading && !inventoryError ? (
            <div className="toolroom-groups">
              {filteredGroups.map((group) => {
                const open = Boolean(openGroups[group.id]) || Boolean(normalizedSearch);
                return (
                  <article className="toolroom-group" key={group.id}>
                    <button
                      type="button"
                      className="toolroom-group-toggle"
                      aria-expanded={open}
                      onClick={() => setOpenGroups((current) => ({
                        ...current,
                        [group.id]: !current[group.id],
                      }))}
                    >
                      <span className="toolroom-group-icon" data-group={group.id} aria-hidden="true">
                        <ToolroomGroupIcon groupId={group.id} />
                      </span>
                      <span className="toolroom-group-copy">
                        <strong>{group.label}</strong>
                        <span className="toolroom-badges">
                          <em className={'toolroom-connection-badge' + (group.available > 0 ? ' is-live' : '')}>
                            {group.available > 0 ? '已连接' : '当前不可用'}
                          </em>
                          <em className="toolroom-transport-badge">{transportLabel(group)}</em>
                          <em>工具：{group.available}/{group.total}</em>
                        </span>
                      </span>
                      <span className={'toolroom-chevron' + (open ? ' is-open' : '')} aria-hidden="true">⌄</span>
                    </button>

                    {open ? (
                      <div className="toolroom-tool-list">
                        {group.tools.map((tool) => {
                          const toolOpen = Boolean(openTools[tool.tool_name]);
                          return (
                            <div className="toolroom-tool" key={tool.tool_name}>
                              <button
                                type="button"
                                className="toolroom-tool-toggle"
                                aria-expanded={toolOpen}
                                onClick={() => setOpenTools((current) => ({
                                  ...current,
                                  [tool.tool_name]: !current[tool.tool_name],
                                }))}
                              >
                                <span
                                  className={'toolroom-status-dot' + (tool.available ? ' is-live' : '')}
                                  style={{ '--toolroom-accent': toolAccent(group.id, tool.available) } as CSSProperties}
                                  aria-hidden="true"
                                />
                                <span className="toolroom-tool-copy">
                                  <strong>{tool.tool_name}</strong>
                                  <span>{tool.display_label}</span>
                                </span>
                                <span className={'toolroom-chevron' + (toolOpen ? ' is-open' : '')} aria-hidden="true">⌄</span>
                              </button>
                              {toolOpen ? (
                                <div
                                  className="toolroom-tool-detail"
                                  style={{ '--toolroom-accent': toolAccent(group.id, tool.available) } as CSSProperties}
                                >
                                  <span className="toolroom-detail-kicker">Prompt / Usage</span>
                                  <p>{tool.status_label}。本页只展示真实清单，不执行任何工具。</p>
                                  <div className="toolroom-detail-divider" aria-hidden="true" />
                                  <span className="toolroom-detail-label">Current binding</span>
                                  <dl>
                                    <div><dt>Provider</dt><dd>{sourceLabel(tool.provider)}</dd></div>
                                    <div><dt>Reason</dt><dd>{tool.reason_code}</dd></div>
                                  </dl>
                                </div>
                              ) : null}
                            </div>
                          );
                        })}
                      </div>
                    ) : null}
                  </article>
                );
              })}
              {filteredGroups.length === 0 ? (
                <div className="toolroom-state">没有找到匹配的工具</div>
              ) : null}
              <div className="toolroom-signoff">Read only.</div>
            </div>
          ) : null}
        </section>
      ) : (
        <section className="toolroom-scroll" aria-label="活动页">
          <div className="toolroom-section-heading">
            <div><strong>当前设备情境</strong><span>更新 {formatObservedAt(reality.physical.observedAt)}</span></div>
            <em>observed</em>
          </div>

          <div className="toolroom-device-grid">
            <article className="toolroom-device-card">
              <div className="toolroom-card-title"><ToolroomDeviceIcon kind="phone" /><strong>手机状态</strong></div>
              <dl>
                <div><dt>姿态</dt><dd>{ORIENTATION_LABELS[facts.orientation]}</dd></div>
                <div><dt>动作</dt><dd>{MOTION_LABELS[reality.physical.motion]}</dd></div>
                <div><dt>光线</dt><dd>{LIGHT_LABELS[facts.lightExposure]}</dd></div>
                <div><dt>距离传感器</dt><dd>{PROXIMITY_LABELS[facts.proximity]}</dd></div>
                <div><dt>电量</dt><dd>{facts.batteryLevel === null ? '未知' : String(facts.batteryLevel) + '%'}</dd></div>
                <div><dt>充电</dt><dd>{facts.charging === null ? '未知' : facts.charging ? '是' : '否'}</dd></div>
                <div><dt>观测时间</dt><dd>{formatObservedAt(reality.physical.observedAt)}</dd></div>
                <div><dt>连接</dt><dd>{statusLabel}</dd></div>
              </dl>
            </article>
            <article className="toolroom-device-card is-muted">
              <div className="toolroom-card-title"><ToolroomDeviceIcon kind="computer" /><strong>电脑状态</strong></div>
              <p>当前网页没有可证明的桌面观测桥接，因此不显示原型里的示例应用或窗口。</p>
            </article>
          </div>

          <div className="toolroom-section-heading">
            <div><strong>重要通知</strong></div>
          </div>
          <article className="toolroom-notice-card">
            <span className="toolroom-status-dot" aria-hidden="true" />
            <div>
              <strong>暂无可显示的真实通知</strong>
              <p>现有页面没有通知内容的只读 owner；原型中的 QQ 与外卖通知不会被当成线上数据。</p>
            </div>
          </article>

          <div className="toolroom-section-heading">
            <div>
              <strong>实际注入 Prompt</strong>
              <span>{Array.from(prompt.text).length} chars</span>
            </div>
          </div>
          <article className="toolroom-prompt-card">
            {prompt.text ? <pre>{prompt.text}</pre> : <p>暂无可用的设备现实状态。</p>}
            <small>只显示当前 RealityPromptProjection；不在这里写入长期记忆。</small>
          </article>

          <div className="toolroom-section-heading">
            <div><strong>原生信息栏</strong></div>
          </div>
          <div className="toolroom-native-panels">
            {activityPanels.map((panel) => {
              const open = Boolean(openPanels[panel.id]);
              return (
                <article className="toolroom-native-panel" key={panel.id}>
                  <button
                    type="button"
                    aria-expanded={open}
                    onClick={() => setOpenPanels((current) => ({
                      ...current,
                      [panel.id]: !current[panel.id],
                    }))}
                  >
                    <span>
                      <strong>{panel.title}</strong>
                      <small>{panel.subtitle}</small>
                    </span>
                    <em>{panel.status}</em>
                    <span className={'toolroom-chevron' + (open ? ' is-open' : '')} aria-hidden="true">⌄</span>
                  </button>
                  {open ? (
                    <dl>
                      {panel.rows.map((row) => (
                        <div key={row[0]}><dt>{row[0]}</dt><dd>{row[1]}</dd></div>
                      ))}
                    </dl>
                  ) : null}
                </article>
              );
            })}
          </div>
          <div className="toolroom-signoff">Still becoming.</div>
        </section>
      )}

      {addMcpOpen ? (
        <div
          className="toolroom-modal-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setAddMcpOpen(false);
          }}
        >
          <section
            className="toolroom-mcp-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="toolroom-mcp-dialog-title"
          >
            <div className="toolroom-mcp-dialog-header">
              <div>
                <span className="toolroom-mcp-eyebrow">External MCP</span>
                <h2 id="toolroom-mcp-dialog-title">添加外部 MCP</h2>
              </div>
              <button
                type="button"
                className="toolroom-mcp-close"
                onClick={() => setAddMcpOpen(false)}
                aria-label="关闭添加外部 MCP"
              >
                ×
              </button>
            </div>

            <p className="toolroom-mcp-preview-note">
              仅前端预览：填写后不会写入服务器，也不会执行任何工具。
            </p>

            <form className="toolroom-mcp-form" onSubmit={handleExternalMcpPreviewSubmit}>
              <div className="toolroom-mcp-field">
                <span className="toolroom-mcp-label">图标 <small>内置线条图标</small></span>
                <div className="toolroom-mcp-icon-grid" role="group" aria-label="选择 MCP 图标">
                  {EXTERNAL_MCP_ICON_OPTIONS.map((icon) => (
                    <button
                      type="button"
                      key={icon}
                      className={'toolroom-mcp-icon-option' + (externalMcpForm.icon === icon ? ' is-selected' : '')}
                      aria-label={'选择' + EXTERNAL_MCP_ICON_LABELS[icon]}
                      title={EXTERNAL_MCP_ICON_LABELS[icon]}
                      aria-pressed={externalMcpForm.icon === icon}
                      onClick={() => setExternalMcpForm((current) => ({ ...current, icon }))}
                    >
                      <ExternalMcpIconView icon={icon} name={externalMcpForm.name} />
                    </button>
                  ))}
                </div>
                <small className="toolroom-mcp-icon-note">选择默认图标后，会根据名称自动匹配。</small>
              </div>

              <label className="toolroom-mcp-field">
                <span className="toolroom-mcp-label">名称 <small>只是给你看的显示名</small></span>
                <input
                  name="name"
                  value={externalMcpForm.name}
                  onChange={(event) => setExternalMcpForm((current) => ({ ...current, name: event.target.value }))}
                  placeholder="例如：我的 MCP"
                  autoComplete="off"
                  required
                />
              </label>

              <label className="toolroom-mcp-field">
                <span className="toolroom-mcp-label">描述 <small>可选备注</small></span>
                <textarea
                  name="description"
                  value={externalMcpForm.description}
                  onChange={(event) => setExternalMcpForm((current) => ({ ...current, description: event.target.value }))}
                  placeholder="用几句话说明它的用途"
                  rows={3}
                />
              </label>

              <label className="toolroom-mcp-field">
                <span className="toolroom-mcp-label">服务器 URL</span>
                <input
                  name="url"
                  type="url"
                  value={externalMcpForm.url}
                  onChange={(event) => setExternalMcpForm((current) => ({ ...current, url: event.target.value }))}
                  placeholder="https://example.com/mcp"
                  autoComplete="url"
                  required
                />
              </label>

              <label className="toolroom-mcp-field">
                <span className="toolroom-mcp-label">身份认证 <small>仅选择认证方式</small></span>
                <select
                  name="auth"
                  value={externalMcpForm.auth}
                  onChange={(event) => setExternalMcpForm((current) => ({ ...current, auth: event.target.value as ExternalMcpAuthScheme }))}
                >
                  <option value="none">none</option>
                  <option value="bearer">bearer</option>
                </select>
              </label>

              <div className="toolroom-mcp-actions">
                <button type="button" className="toolroom-mcp-secondary" onClick={() => setAddMcpOpen(false)}>
                  取消
                </button>
                <button type="submit" className="toolroom-mcp-primary">
                  保存配置（仅预览）
                </button>
              </div>

              {externalMcpNotice ? (
                <p className="toolroom-mcp-status" role="status">{externalMcpNotice}</p>
              ) : null}
            </form>
          </section>
        </div>
      ) : null}
    </main>
  );
}
