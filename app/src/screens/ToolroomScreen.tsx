import { type CSSProperties, type FormEvent, useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { getActivitySemanticConfidence, lightSemanticLabel, orientationSemanticLabel, type RealityPromptSegment } from '../lib/reality/realityContextCompiler';
import { useNavigate } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { http } from '../lib/http';
import { fetchToolCompanionHints, patchToolCompanionHint, type ToolCompanionHints, type ToolCompanionTool } from '../lib/toolCompanionHints';
import { realityPromptProjection } from '../lib/reality/realityPromptProjection';
import { realityStore } from '../lib/reality/realityRuntime';
import { getActivityFreshness, getRealityFreshness } from '../lib/reality/realityStore';
import './ToolroomScreen.css';

type ElpisNativeBridge = {
  hasUsageAccess?: () => unknown;
  openUsageAccessSettings?: () => unknown;
  getScreenTime?: () => unknown;
  isIgnoringBatteryOptimizations?: () => unknown;
  requestIgnoreBatteryOptimizations?: () => unknown;
};

type ElpisNotificationsBridge = {
  hasNotificationPermission?: () => unknown;
  requestNotificationPermission?: () => unknown;
  showTestNotification?: () => unknown;
};

declare global {
  interface Window {
    ElpisNative?: ElpisNativeBridge;
    ElpisNotifications?: ElpisNotificationsBridge;
  }
}

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

function companionToolForInventory(hints: ToolCompanionHints | null, tool: InventoryTool): ToolCompanionTool | null {
  if (!hints) return null;
  for (const group of hints.groups) {
    const exactId = group.tools.find((candidate) => candidate.capability_id === tool.tool_name);
    if (exactId) return exactId;
    const labelMatches = group.tools.filter((candidate) => (
      candidate.display_label.trim().toLocaleLowerCase() === tool.display_label.trim().toLocaleLowerCase()
    ));
    if (labelMatches.length === 1) return labelMatches[0];
  }
  return null;
}

function promptTextForTool(
  tool: InventoryTool,
  hints: ToolCompanionHints | null,
  overrides: Record<string, string>,
): string {
  if (overrides[tool.tool_name] !== undefined) return overrides[tool.tool_name];
  const linked = companionToolForInventory(hints, tool);
  return linked?.companion_hint || `${tool.status_label}。本页只展示真实清单，不执行任何工具。`;
}

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
      {icon === 'default' ? <circle cx="18" cy="6" r="1.2" fill="currentColor" stroke="none" /> : null}
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

const ACTIVITY_LABELS: Record<string, string> = {
  still: '静止',
  walking: '步行',
  running: '跑步',
  cycling: '骑行',
  in_vehicle: '车载',
  unknown: '未知',
};

function sourceLabel(provider: string | null): string {
  if (!provider) return 'Legacy';
  if (provider.indexOf('mcp__home__') === 0) return 'Home MCP';
  if (provider.indexOf('mcp__codebase') === 0) return 'Codebase MCP';
  if (provider.indexOf('Claude Code') === 0) return 'Claude Code';
  return provider;
}


function formatActivityAge(status: 'fresh' | 'stale' | 'unknown', sampledAt: number | null, nowMs: number): string {
  if (sampledAt === null || !Number.isFinite(sampledAt)) return 'unknown';
  const ageSeconds = Math.max(0, Math.floor((nowMs - sampledAt) / 1000));
  return status === 'stale' ? 'stale · ' + ageSeconds + 's' : ageSeconds + 's';
}

function formatObservedAt(observedAt: number | null): string {
  if (observedAt === null || !Number.isFinite(observedAt)) return '暂无观测';
  return new Date(observedAt).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function renderToolroomPromptSegment(segment: RealityPromptSegment, index: number) {
  if (segment.kind === 'dynamic') {
    return <strong key={segment.key + '-' + index}>{segment.text}</strong>;
  }
  return <span key={'literal-' + index}>{segment.text}</span>;
}

function formatRelativeTime(observedAt: number | null): string {
  if (observedAt === null || !Number.isFinite(observedAt)) return '暂无';
  const ageSeconds = Math.max(0, Math.floor((Date.now() - observedAt) / 1000));
  if (ageSeconds < 5) return '刚刚';
  if (ageSeconds < 60) return ageSeconds + '秒前';
  const ageMinutes = Math.floor(ageSeconds / 60);
  if (ageMinutes < 60) return ageMinutes + '分钟前';
  return Math.floor(ageMinutes / 60) + '小时前';
}

function asRawRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function rawValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '未知';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
  if (typeof value === 'boolean') return value ? '是' : '否';
  return String(value);
}

function sensorState(value: unknown): { available: boolean; label: string } {
  const sensor = asRawRecord(value);
  const available = sensor?.available === true && sensor?.ready !== false;
  return { available, label: available ? '可用' : '未连接' };
}

function sensorSampleAge(value: unknown, fallback: number | null): string {
  const sensor = asRawRecord(value);
  const sampledAt = typeof sensor?.sampledAt === 'number' ? sensor.sampledAt : fallback;
  return formatRelativeTime(sampledAt);
}

function rawJson(value: unknown): string {
  try {
    return JSON.stringify(value ?? null, null, 2) || 'null';
  } catch {
    return '不可序列化';
  }
}

function formatNativeScreenTime(value: unknown): string {
  let parsed = value;
  if (typeof parsed === 'string') {
    try { parsed = JSON.parse(parsed); } catch { return '读取失败'; }
  }
  const record = asRawRecord(parsed);
  if (record?.error === 'no_permission') return '未授权';
  if (record?.error === 'unavailable') return '暂不可用';
  if (record?.error) return '读取失败';
  const minutes = typeof record?.totalMinutes === 'number'
    ? record.totalMinutes
    : typeof record?.minutes === 'number'
      ? record.minutes
      : typeof record?.totalSeconds === 'number'
        ? Math.round(record.totalSeconds / 60)
        : null;
  if (minutes === null) return '暂不可用';
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder > 0 ? `${hours} 小时 ${remainder} 分钟` : `${hours} 小时`;
}

type NativeDiagnosticState = {
  bridgeConnected: boolean;
  usage: { value: string; canAuthorize: boolean };
  screenTime: string;
  doze: { value: string; canAuthorize: boolean };
  notifications: {
    bridgeConnected: boolean;
    capability: string;
    canRequest: boolean;
    canTest: boolean;
  };
  notice: string;
};

const DEFAULT_NATIVE_DIAGNOSTIC: NativeDiagnosticState = {
  bridgeConnected: false,
  usage: { value: '未接入', canAuthorize: false },
  screenTime: '未接入',
  doze: { value: '未接入', canAuthorize: false },
  notifications: { bridgeConnected: false, capability: '未接入', canRequest: false, canTest: false },
  notice: '',
};

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
  const [companionHints, setCompanionHints] = useState<ToolCompanionHints | null>(null);
  const [promptOverrides, setPromptOverrides] = useState<Record<string, string>>({});
  const [promptEditorTool, setPromptEditorTool] = useState<string | null>(null);
  const [promptDraft, setPromptDraft] = useState('');
  const [promptSaving, setPromptSaving] = useState(false);
  const [promptNotice, setPromptNotice] = useState('');
  const [nativeDiag, setNativeDiag] = useState<NativeDiagnosticState>(DEFAULT_NATIVE_DIAGNOSTIC);
  const [rawJsonFrozen, setRawJsonFrozen] = useState(false);
  const [rawJsonSnapshot, setRawJsonSnapshot] = useState<string | null>(null);
  const [rawJsonNotice, setRawJsonNotice] = useState('');
  const [activityNow, setActivityNow] = useState(() => Date.now());

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

  const loadCompanionHints = useCallback(async () => {
    try {
      setCompanionHints(await fetchToolCompanionHints());
    } catch {
      setCompanionHints(null);
    }
  }, []);

  const refreshNativeDiagnostics = useCallback(async () => {
    const native = window.ElpisNative;
    const has = (value: unknown): value is (...args: never[]) => unknown => typeof value === 'function';
    const bridgeConnected = Boolean(native && (
      has(native.hasUsageAccess) || has(native.getScreenTime)
      || has(native.isIgnoringBatteryOptimizations)
    ));
    let usage = { value: '未接入', canAuthorize: false };
    let screenTime = '未接入';
    let doze = { value: '未接入', canAuthorize: false };
    if (native && has(native.hasUsageAccess)) {
      try {
        const allowed = Boolean(await Promise.resolve(native.hasUsageAccess()));
        usage = { value: allowed ? '已授权' : '未授权', canAuthorize: !allowed && has(native.openUsageAccessSettings) };
        if (allowed && has(native.getScreenTime)) {
          screenTime = formatNativeScreenTime(await Promise.resolve(native.getScreenTime()));
        } else if (!allowed) {
          screenTime = '等待授权';
        }
      } catch {
        usage = { value: '读取失败', canAuthorize: has(native.openUsageAccessSettings) };
        screenTime = '读取失败';
      }
    } else if (native && has(native.getScreenTime)) {
      try { screenTime = formatNativeScreenTime(await Promise.resolve(native.getScreenTime())); } catch { screenTime = '读取失败'; }
    }
    if (native && has(native.isIgnoringBatteryOptimizations)) {
      try {
        const ignoring = Boolean(await Promise.resolve(native.isIgnoringBatteryOptimizations()));
        doze = { value: ignoring ? '已豁免' : '未豁免', canAuthorize: !ignoring && has(native.requestIgnoreBatteryOptimizations) };
      } catch {
        doze = { value: '读取失败', canAuthorize: has(native.requestIgnoreBatteryOptimizations) };
      }
    }
    const notifications = window.ElpisNotifications;
    const notificationBridgeConnected = Boolean(notifications && (
      has(notifications.hasNotificationPermission)
      || has(notifications.requestNotificationPermission)
      || has(notifications.showTestNotification)
    ));
    let notificationState: NativeDiagnosticState['notifications'] = {
      bridgeConnected: notificationBridgeConnected,
      capability: notificationBridgeConnected ? '未开启' : '未接入',
      canRequest: Boolean(notificationBridgeConnected && has(notifications?.requestNotificationPermission)),
      canTest: false,
    };
    if (notifications && has(notifications.hasNotificationPermission)) {
      try {
        const allowed = Boolean(await Promise.resolve(notifications.hasNotificationPermission()));
        notificationState = {
          bridgeConnected: true,
          capability: allowed ? '可用' : '未开启',
          canRequest: !allowed && has(notifications.requestNotificationPermission),
          canTest: allowed && has(notifications.showTestNotification),
        };
      } catch {
        notificationState = { bridgeConnected: true, capability: '不可用', canRequest: has(notifications.requestNotificationPermission), canTest: false };
      }
    }
    setNativeDiag((current) => ({ ...current, bridgeConnected, usage, screenTime, doze, notifications: notificationState }));
  }, []);

  useEffect(() => {
    void refreshNativeDiagnostics();
    const handleRefresh = () => { void refreshNativeDiagnostics(); };
    window.addEventListener('focus', handleRefresh);
    document.addEventListener('visibilitychange', handleRefresh);
    return () => {
      window.removeEventListener('focus', handleRefresh);
      document.removeEventListener('visibilitychange', handleRefresh);
    };
  }, [refreshNativeDiagnostics]);

  const authorizeUsage = async () => {
    if (!window.ElpisNative?.openUsageAccessSettings) return;
    try {
      await Promise.resolve(window.ElpisNative.openUsageAccessSettings());
      setNativeDiag((current) => ({ ...current, notice: '已打开屏幕时间权限设置。' }));
    } catch {
      setNativeDiag((current) => ({ ...current, notice: '无法打开屏幕时间权限设置。' }));
    }
    window.setTimeout(() => { void refreshNativeDiagnostics(); }, 180);
  };

  const authorizeDoze = async () => {
    if (!window.ElpisNative?.requestIgnoreBatteryOptimizations) return;
    try {
      await Promise.resolve(window.ElpisNative.requestIgnoreBatteryOptimizations());
      setNativeDiag((current) => ({ ...current, notice: '已打开电池优化设置。' }));
    } catch {
      setNativeDiag((current) => ({ ...current, notice: '无法打开电池优化设置。' }));
    }
    window.setTimeout(() => { void refreshNativeDiagnostics(); }, 180);
  };

  const requestNotifications = async () => {
    if (!window.ElpisNotifications?.requestNotificationPermission) return;
    try {
      await Promise.resolve(window.ElpisNotifications.requestNotificationPermission());
      setNativeDiag((current) => ({ ...current, notice: '已请求通知权限。' }));
    } catch {
      setNativeDiag((current) => ({ ...current, notice: '通知权限请求失败。' }));
    }
    window.setTimeout(() => { void refreshNativeDiagnostics(); }, 180);
  };

  const sendTestNotification = async () => {
    if (!window.ElpisNotifications?.showTestNotification) return;
    try {
      const result = await Promise.resolve(window.ElpisNotifications.showTestNotification());
      setNativeDiag((current) => ({ ...current, notice: result === false ? '当前无法发送通知。' : '已发送，请查看通知栏。' }));
    } catch {
      setNativeDiag((current) => ({ ...current, notice: '当前无法发送通知。' }));
    }
  };

  useEffect(() => {
    let activityUiTimer: number | null = null;
    const refreshActivityUi = () => {
      if (document.visibilityState === 'visible') setActivityNow(Date.now());
    };
    const stopActivityUiPolling = () => {
      if (activityUiTimer !== null) {
        window.clearInterval(activityUiTimer);
        activityUiTimer = null;
      }
    };
    const startActivityUiPolling = () => {
      stopActivityUiPolling();
      refreshActivityUi();
      if (document.visibilityState === 'visible') {
        activityUiTimer = window.setInterval(refreshActivityUi, 1000);
      }
    };
    startActivityUiPolling();
    document.addEventListener('visibilitychange', startActivityUiPolling);
    return () => {
      stopActivityUiPolling();
      document.removeEventListener('visibilitychange', startActivityUiPolling);
    };
  }, []);

  useEffect(() => {
    void loadInventory();
    void loadCompanionHints();
  }, [loadCompanionHints, loadInventory]);

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

  const openPromptEditor = (tool: InventoryTool) => {
    setPromptEditorTool(tool.tool_name);
    setPromptDraft(promptTextForTool(tool, companionHints, promptOverrides));
    setPromptNotice('');
  };

  const closePromptEditor = () => {
    if (promptSaving) return;
    setPromptEditorTool(null);
    setPromptDraft('');
    setPromptNotice('');
  };

  const savePrompt = async (tool: InventoryTool) => {
    const linked = companionToolForInventory(companionHints, tool);
    setPromptSaving(true);
    setPromptNotice('');
    try {
      if (linked) {
        const nextHints = await patchToolCompanionHint({
          capability_id: linked.capability_id,
          companion_hint: promptDraft,
        });
        setCompanionHints(nextHints);
      }
      setPromptOverrides((current) => ({ ...current, [tool.tool_name]: promptDraft }));
      setPromptNotice(linked ? '已更新费佳档案工具区中的 Prompt。' : '未找到对应档案工具，仅保留当前页面预览。');
    } catch (error) {
      setPromptNotice(error instanceof Error ? error.message : 'Prompt 保存失败，请稍后重试。');
    } finally {
      setPromptSaving(false);
    }
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

  const liveRawJson = rawJson(reality.physical.raw);
  const displayedRawJson = rawJsonFrozen && rawJsonSnapshot !== null ? rawJsonSnapshot : liveRawJson;

  const copyRawJson = async () => {
    try {
      await navigator.clipboard.writeText(displayedRawJson);
      setRawJsonNotice('已复制原始 JSON。');
    } catch {
      setRawJsonNotice('复制失败，请检查浏览器剪贴板权限。');
    }
  };

  const toggleRawJsonFreeze = () => {
    if (rawJsonFrozen) {
      setRawJsonFrozen(false);
      setRawJsonSnapshot(null);
      setRawJsonNotice('已恢复实时 JSON。');
    } else {
      setRawJsonSnapshot(liveRawJson);
      setRawJsonFrozen(true);
      setRawJsonNotice('已冻结当前 JSON。');
    }
  };

  const freshness = getRealityFreshness(reality, Date.now());
  const activityFreshness = getActivityFreshness(reality, activityNow);
  const activity = reality.activity;
  const activityConfidence = activity.source === 'hms'
    && activityFreshness.status === 'fresh'
    ? getActivitySemanticConfidence(activity.possibility)
    : 'hidden';
  const activitySemanticReady = activityConfidence !== 'hidden'
    && activity.userActivity !== 'unknown';
  const facts = reality.physical.facts;
  const statusLabel = freshness.status === 'fresh'
    ? '实时'
    : freshness.status === 'stale'
      ? '已过期'
      : '未连接';

  const nativeRows = [
    { label: '原生桥', value: nativeDiag.bridgeConnected ? '已连接' : '仅 App 可用' },
    { label: '屏幕时间权限', value: nativeDiag.usage.value, action: nativeDiag.usage.canAuthorize ? { label: '去授权', onClick: authorizeUsage } : undefined },
    { label: '今日屏幕时间', value: nativeDiag.screenTime },
    { label: '电池优化', value: nativeDiag.doze.value, action: nativeDiag.doze.canAuthorize ? { label: '去设置', onClick: authorizeDoze } : undefined },
    { label: '通知桥', value: nativeDiag.notifications.bridgeConnected ? '已连接' : '仅 App 可用' },
    { label: '通知能力', value: nativeDiag.notifications.capability, action: nativeDiag.notifications.canRequest ? { label: '申请通知权限', onClick: requestNotifications } : undefined },
  ];

  const nativePanels = [
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


  const physicalRaw = asRawRecord(reality.physical.raw);
  const sensorPanels = [
    {
      id: 'battery',
      title: '电池',
      subtitle: 'Battery',
      raw: physicalRaw?.battery,
      ...sensorState(physicalRaw?.battery),
      rows: [
        ['状态', sensorState(physicalRaw?.battery).label],
        ['电量', facts.batteryLevel === null ? '未知' : String(facts.batteryLevel) + '%'],
        ['充电', facts.charging === null ? '未知' : facts.charging ? '是' : '否'],
        ['样本年龄', sensorSampleAge(physicalRaw?.battery, reality.physical.observedAt)],
      ],
    },
    {
      id: 'accelerometer',
      title: '加速度计',
      subtitle: 'Accelerometer',
      raw: physicalRaw?.accelerometer,
      ...sensorState(physicalRaw?.accelerometer),
      rows: [
        ['状态', sensorState(physicalRaw?.accelerometer).label],
        ['x / y / z', (() => {
          const sensor = asRawRecord(physicalRaw?.accelerometer);
          return rawValue(sensor?.x) + ' / ' + rawValue(sensor?.y) + ' / ' + rawValue(sensor?.z);
        })()],
        ['姿态', orientationSemanticLabel(facts.orientation) || '—'],
        ['样本年龄', sensorSampleAge(physicalRaw?.accelerometer, reality.physical.observedAt)],
      ],
    },
    {
      id: 'gyroscope',
      title: '陀螺仪',
      subtitle: 'Gyroscope',
      raw: physicalRaw?.gyroscope,
      ...sensorState(physicalRaw?.gyroscope),
      rows: [
        ['状态', sensorState(physicalRaw?.gyroscope).label],
        ['x / y / z', (() => {
          const sensor = asRawRecord(physicalRaw?.gyroscope);
          return rawValue(sensor?.x) + ' / ' + rawValue(sensor?.y) + ' / ' + rawValue(sensor?.z);
        })()],
        ['样本年龄', sensorSampleAge(physicalRaw?.gyroscope, reality.physical.observedAt)],
      ],
    },
    {
      id: 'proximity',
      title: '距离',
      subtitle: 'Proximity',
      raw: physicalRaw?.proximity,
      ...sensorState(physicalRaw?.proximity),
      rows: [
        ['状态', sensorState(physicalRaw?.proximity).label],
        ['value / maxRange', (() => {
          const sensor = asRawRecord(physicalRaw?.proximity);
          return rawValue(sensor?.value) + ' / ' + rawValue(sensor?.maxRange);
        })()],
        ['距离状态', PROXIMITY_LABELS[facts.proximity]],
        ['样本年龄', sensorSampleAge(physicalRaw?.proximity, reality.physical.observedAt)],
      ],
    },
    {
      id: 'light',
      title: '光线',
      subtitle: 'Ambient Light',
      raw: physicalRaw?.light,
      ...sensorState(physicalRaw?.light),
      rows: [
        ['状态', sensorState(physicalRaw?.light).label],
        ['lux', rawValue(asRawRecord(physicalRaw?.light)?.lux)],
        ['光线状态', LIGHT_LABELS[facts.lightExposure]],
        ['样本年龄', sensorSampleAge(physicalRaw?.light, reality.physical.observedAt)],
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
                                  <div className="toolroom-detail-kicker-row">
                                    <span className="toolroom-detail-kicker">Prompt / Usage</span>
                                    <button
                                      type="button"
                                      className="toolroom-prompt-edit-button"
                                      aria-label={'编辑 ' + tool.tool_name + ' Prompt'}
                                      onClick={() => openPromptEditor(tool)}
                                    >
                                      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                                        <path d="m4.5 17.2-.8 3.1 3.1-.8L18.4 8a2 2 0 0 0-2.8-2.8L4.5 17.2Z" />
                                        <path d="m13.9 6.9 3.2 3.2" />
                                      </svg>
                                    </button>
                                  </div>
                                  {promptEditorTool === tool.tool_name ? (
                                    <div className="toolroom-prompt-editor">
                                      <textarea
                                        value={promptDraft}
                                        onChange={(event) => setPromptDraft(event.target.value)}
                                        aria-label={tool.tool_name + ' Prompt 编辑器'}
                                        rows={4}
                                      />
                                      <div className="toolroom-prompt-editor-actions">
                                        <button type="button" onClick={closePromptEditor} disabled={promptSaving}>取消</button>
                                        <button type="button" onClick={() => void savePrompt(tool)} disabled={promptSaving}>
                                          {promptSaving ? '保存中…' : '保存'}
                                        </button>
                                      </div>
                                      {promptNotice ? <small>{promptNotice}</small> : null}
                                    </div>
                                  ) : (
                                    <p>{promptTextForTool(tool, companionHints, promptOverrides)}</p>
                                  )}
                                  <details className="toolroom-boundary-disclosure">
                                    <summary>真实能力边界</summary>
                                    <p className="toolroom-boundary-copy">
                                      {companionToolForInventory(companionHints, tool)?.physical_boundary
                                        || '当前清单未提供真实能力边界。'}
                                    </p>
                                  </details>
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
                <div><dt>姿态</dt><dd>{orientationSemanticLabel(facts.orientation) || '—'} <small>（{formatRelativeTime(reality.physical.observedAt)}）</small></dd></div>
                <div><dt>动作</dt><dd>{MOTION_LABELS[reality.physical.motion]} <small>（{formatRelativeTime(reality.physical.observedAt)}）</small></dd></div>
                <div><dt>光线</dt><dd>{lightSemanticLabel(facts.lightExposure) || '—'} <small>（{formatRelativeTime(reality.physical.observedAt)}）</small></dd></div>
                <div><dt>距离传感器</dt><dd>{PROXIMITY_LABELS[facts.proximity]} <small>（{formatRelativeTime(reality.physical.observedAt)}）</small></dd></div>
                <div><dt>电量</dt><dd>{facts.batteryLevel === null ? '未知' : String(facts.batteryLevel) + '%'} <small>（{formatRelativeTime(reality.physical.observedAt)}）</small></dd></div>
                <div><dt>充电</dt><dd>{facts.charging === null ? '未知' : facts.charging ? '是' : '否'} <small>（{formatRelativeTime(reality.physical.observedAt)}）</small></dd></div>
                <div><dt>观测时间</dt><dd>{formatObservedAt(reality.physical.observedAt)} <small>（{formatRelativeTime(reality.physical.observedAt)}）</small></dd></div>
                <div><dt>连接</dt><dd>{statusLabel} <small>（{formatRelativeTime(reality.physical.observedAt)}）</small></dd></div>
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
              <span>系统配置 · Reality Prompt 预览 · {Array.from(prompt.text).length} chars</span>
            </div>
          </div>
          <article className="toolroom-prompt-card">
            {prompt.text ? <p className="toolroom-prompt-text">{prompt.segments.map(renderToolroomPromptSegment)}</p> : <p>暂无可用的设备现实状态。</p>}
            <small>只显示当前 RealityPromptProjection；不在这里写入长期记忆。</small>
          </article>

          <div className="toolroom-section-heading">
            <div><strong>原生信息栏</strong></div>
          </div>
          <div className="toolroom-native-panels">
            <article className="toolroom-native-panel">
              <button
                type="button"
                aria-expanded={Boolean(openPanels['native:native'])}
                onClick={() => setOpenPanels((current) => ({ ...current, 'native:native': !current['native:native'] }))}
              >
                <span><strong>原生能力诊断</strong><small>Elpis Canary · NativeBridge Lite</small></span>
                <em className={nativeDiag.bridgeConnected ? 'is-live' : undefined}>{nativeDiag.bridgeConnected ? '已连接' : '仅 App 可用'}</em>
                <span className={'toolroom-chevron' + (openPanels['native:native'] ? ' is-open' : '')} aria-hidden="true">⌄</span>
              </button>
              {openPanels['native:native'] ? (
                <div className="toolroom-native-detail">
                  <dl>
                    {nativeRows.map((row) => (
                      <div key={row.label}>
                        <dt>{row.label}</dt>
                        <dd>
                          <span>{row.value}</span>
                          {row.action ? (
                            <button
                              type="button"
                              className="toolroom-native-action"
                              onClick={(event) => { event.stopPropagation(); void row.action?.onClick(); }}
                            >{row.action.label}</button>
                          ) : null}
                        </dd>
                      </div>
                    ))}
                    <div>
                      <dt>发送测试通知</dt>
                      <dd>
                        <button
                          type="button"
                          className="toolroom-native-action"
                          disabled={!nativeDiag.notifications.canTest}
                          onClick={(event) => { event.stopPropagation(); void sendTestNotification(); }}
                        >发送测试通知</button>
                      </dd>
                    </div>
                  </dl>
                  {nativeDiag.notice ? <p className="toolroom-native-notice">{nativeDiag.notice}</p> : null}
                  <small className="toolroom-native-hint">测试通知仅在本机显示，不访问消息服务器。</small>
                </div>
              ) : null}
            </article>
            {nativePanels.map((panel) => {
              const key = 'native:' + panel.id;
              const open = Boolean(openPanels[key]);
              return (
                <article className="toolroom-native-panel" key={panel.id}>
                  <button
                    type="button"
                    aria-expanded={open}
                    onClick={() => setOpenPanels((current) => ({ ...current, [key]: !current[key] }))}
                  >
                    <span><strong>{panel.title}</strong><small>{panel.subtitle}</small></span>
                    <em>{panel.status}</em>
                    <span className={'toolroom-chevron' + (open ? ' is-open' : '')} aria-hidden="true">⌄</span>
                  </button>
                  {open ? <dl>{panel.rows.map((row) => <div key={row[0]}><dt>{row[0]}</dt><dd>{row[1]}</dd></div>)}</dl> : null}
                </article>
              );
            })}
          </div>

          <div className="toolroom-section-heading">
            <div><strong>显示传感器</strong><span>Physical Reality 与 HMS Activity 只读观测</span></div>
          </div>
          <div className="toolroom-sensor-panels">
            {sensorPanels.map((sensor) => (
              <article className="toolroom-native-panel toolroom-sensor-panel" key={sensor.id}>
                <div className="toolroom-sensor-heading">
                  <span>
                    <strong>{sensor.title}</strong>
                    <small>{sensor.subtitle}</small>
                  </span>
                  <em className={sensor.available ? 'is-live' : undefined}>{sensor.label}</em>
                </div>
                <div className="toolroom-sensor-body">
                  <dl>
                    {sensor.rows.map((row) => (
                      <div key={row[0]}><dt>{row[0]}</dt><dd>{row[1]}</dd></div>
                    ))}
                  </dl>
                </div>
              </article>
            ))}
          </div>

          <div className="toolroom-section-heading">
            <div>
              <strong>HMS Activity</strong>
              <span>实时状态 · 复用既有 ElpisActivity bridge</span>
            </div>
            <em className={activityFreshness.status === 'fresh' ? 'is-live' : undefined}>{activityFreshness.status}</em>
          </div>
          <article className="toolroom-hms-activity toolroom-native-panel">
            <div className="toolroom-hms-activity-head">
              <span>
                <strong>Semantic</strong>
                <small>只读；stale / unknown 不进入 prompt</small>
              </span>
              <em className={activitySemanticReady ? 'is-live' : undefined}>
                {activitySemanticReady ? 'fresh' : activityFreshness.status}
              </em>
            </div>
            <dl>
              <div><dt>设备推断活动</dt><dd>{activitySemanticReady ? ACTIVITY_LABELS[activity.userActivity] + (activityConfidence === 'low' ? '（低置信）' : '') : '—'}</dd></div>
              <div><dt>置信度</dt><dd>{activity.possibility === null ? '—' : String(activity.possibility) + '%'}</dd></div>
              <div><dt>activity age</dt><dd>{formatActivityAge(activityFreshness.status, activity.activitySampledAt, activityNow)}</dd></div>
              <div><dt>activity source</dt><dd>{activity.source}</dd></div>
            </dl>
            <div className="toolroom-hms-activity-divider" aria-hidden="true" />
            <div className="toolroom-hms-activity-label">Technical diagnostic</div>
            <dl>
              <div><dt>registration</dt><dd>{activity.registration}</dd></div>
              <div><dt>lastErrorCode</dt><dd>{activity.lastErrorCode || '—'}</dd></div>
              <div><dt>callbackReceived</dt><dd>{activity.callbackReceived ? 'yes' : 'no'}</dd></div>
              <div><dt>intentHasExtras</dt><dd>{activity.intentHasExtras ? 'yes' : 'no'}</dd></div>
              <div><dt>responsePresent</dt><dd>{activity.responsePresent ? 'yes' : 'no'}</dd></div>
              <div><dt>activityDataCount</dt><dd>{String(activity.activityDataCount)}</dd></div>
              <div><dt>raw activity code</dt><dd>{activity.rawCandidate === null ? '—' : String(activity.rawCandidate)}</dd></div>
            </dl>
          </article>

          <details className="toolroom-raw-json toolroom-raw-json-all">
            <summary>
              <span>原始JSON</span>
              <span className="toolroom-raw-json-actions">
                <button
                  type="button"
                  className="toolroom-raw-json-action"
                  onClick={(event) => { event.preventDefault(); event.stopPropagation(); void copyRawJson(); }}
                >复制</button>
                <button
                  type="button"
                  className={'toolroom-raw-json-action' + (rawJsonFrozen ? ' is-active' : '')}
                  onClick={(event) => { event.preventDefault(); event.stopPropagation(); toggleRawJsonFreeze(); }}
                >{rawJsonFrozen ? '解冻' : '冻结'}</button>
              </span>
            </summary>
            <pre>{displayedRawJson}</pre>
            {rawJsonNotice ? <small className="toolroom-raw-json-notice" role="status">{rawJsonNotice}</small> : null}
          </details>
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
