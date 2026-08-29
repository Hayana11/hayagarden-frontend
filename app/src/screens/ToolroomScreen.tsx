import { type CSSProperties, useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
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

const GROUP_ICONS: Record<string, string> = {
  memory: '🧠',
  web: '⌁',
  pocket: '▣',
  light: '☼',
  shopping: '♢',
  gallery: '▧',
  code_files: '⌘',
  workspace: '◇',
  self_config: '⚙',
  board: '☷',
  life: '◌',
  plans_ledger: '▤',
  desire: '♡',
  triggers: '⌁',
  artifacts: '◇',
  phone: '▯',
  moments: '◫',
};

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
      subtitle: 'RealityPromptProjection',
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
                        {GROUP_ICONS[group.id] || '◇'}
                      </span>
                      <span className="toolroom-group-copy">
                        <strong>{group.label}</strong>
                        <span className="toolroom-badges">
                          <em className={group.available > 0 ? 'is-live' : ''}>
                            {group.available > 0 ? String(group.available) + ' 可用' : '当前不可用'}
                          </em>
                          <em>{group.total} 个工具</em>
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
              <div className="toolroom-card-title"><span aria-hidden="true">▯</span><strong>手机状态</strong></div>
              <dl>
                <div><dt>姿态</dt><dd>{ORIENTATION_LABELS[facts.orientation]}</dd></div>
                <div><dt>动作</dt><dd>{MOTION_LABELS[reality.physical.motion]}</dd></div>
                <div><dt>光线</dt><dd>{LIGHT_LABELS[facts.lightExposure]}</dd></div>
                <div><dt>连接</dt><dd>{statusLabel}</dd></div>
              </dl>
            </article>
            <article className="toolroom-device-card is-muted">
              <div className="toolroom-card-title"><span aria-hidden="true">▣</span><strong>电脑状态</strong></div>
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
    </main>
  );
}
