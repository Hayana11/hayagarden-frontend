import { http } from './http';

export type ChatProvider = 'api_relay' | 'claude_code';

export interface ProviderConfig {
  provider: ChatProvider;
  ccTokenSet: boolean;
}

export interface KeyStatus {
  maskedKey: string;
  source: string;
  todayMessages: number;
}

export interface EndpointCapabilities {
  thinking: boolean;
  cache: boolean;
  tools: boolean;
}

export interface RelayEndpoint {
  id: number;
  name: string;
  url: string;
  active: boolean;
  defaultModel: string;
  statusUrl: string;
  accountBalanceConfigured: boolean;
  accountCredentialKind: 'access_token' | 'session_cookie' | '';
  capabilities: EndpointCapabilities;
}

export interface RelayDraft {
  name: string;
  url: string;
  key: string;
  defaultModel: string;
  statusUrl: string;
  capabilities: EndpointCapabilities;
}

export interface RelayModelHealth {
  name: string;
  group: string;
  status: number | null;
  time: string;
  message: string;
  ping: number | null;
}

export interface RelayModelInsight {
  id: string;
  name?: string;
  route?: string;
  groups?: string[];
  group?: string;
  group_ratio?: string | number;
  base_price?: string;
  price?: string;
  status?: RelayModelHealth;
}

export interface RelayIntelligence {
  models: string[];
  modelOptions: RelayModelInsight[];
  statusSummary: RelayModelHealth[];
  pricingRequiresAuth: boolean;
  pricingSource: string;
  statusSource: string;
  cached: boolean;
  sourceProject: string;
}

export interface RelayBalance {
  supported: boolean;
  error: 'missing_key' | 'unauthorized' | 'unsupported' | 'unavailable' | 'invalid_response' | '';
  source: string;
  name: string;
  unlimited: boolean;
  expiresAt: number;
  totalGrantedUsd: number | null;
  totalUsedUsd: number | null;
  totalAvailableUsd: number | null;
}

export interface RelayAccountBalance {
  supported: boolean;
  error: 'missing_credentials' | 'unauthorized' | 'unsupported' | 'unavailable' | 'invalid_response' | '';
  source: string;
  credentialKind: 'access_token' | 'session_cookie' | '';
  remainingUsd: number | null;
  usedUsd: number | null;
  totalUsd: number | null;
}

export interface ConfigModel {
  id: string;
  label: string;
  description: string;
  thinking: string;
  primary: boolean;
  dot: string;
}

export interface ConfigUsageSummary {
  win5Pct: number;
  win7Pct: number;
  todayMessages: number;
  todayTokens: number;
}

export interface DailyUsage {
  date: string;
  count: number;
  cost: number | null;
}

export interface DailyUsageRelayTotal {
  id: number;
  name: string;
  totalCost: number;
  todayCost: number;
  dailyCosts: Record<string, number>;
}

export interface DailyUsageResult {
  mode: 'cost' | 'requests';
  days: DailyUsage[];
  relays: DailyUsageRelayTotal[];
  totalCost: number | null;
  totalCount: number;
  monthMessages: number;
}

export interface PlaygroundResult {
  content: string;
  thinking: string;
  error: string;
  latencyMs: number;
  inputTokens: number;
  outputTokens: number;
  provider: string;
}

export async function getProviderConfig(): Promise<ProviderConfig> {
  const data = await http.get<{ provider?: string; cc_token_set?: boolean }>('/api/config/provider');
  return {
    provider: data.provider === 'claude_code' ? 'claude_code' : 'api_relay',
    ccTokenSet: Boolean(data.cc_token_set),
  };
}

export async function updateProvider(provider: ChatProvider): Promise<void> {
  await http.post('/api/config/provider', { provider });
}

export async function getKeyStatus(): Promise<KeyStatus> {
  const data = await http.get<{ masked_key?: string; source?: string; today_msgs?: number }>('/api/config/key-status');
  return {
    maskedKey: data.masked_key || '—',
    source: data.source || '—',
    todayMessages: Number(data.today_msgs || 0),
  };
}

export async function getRelayEndpoints(): Promise<RelayEndpoint[]> {
  const data = await http.get<{
    presets?: Array<{
      id: number;
      name?: string;
      url?: string;
      active?: boolean;
      default_model?: string;
      status_url?: string;
      account_balance_configured?: boolean;
      account_credential_kind?: 'access_token' | 'session_cookie' | '';
      capabilities?: Partial<EndpointCapabilities>;
    }>;
  }>('/api/config/relay-presets');

  return (data.presets || []).map((endpoint) => ({
    id: endpoint.id,
    name: endpoint.name || `中转站 ${endpoint.id}`,
    url: endpoint.url || '',
    active: Boolean(endpoint.active),
    defaultModel: endpoint.default_model || '',
    statusUrl: endpoint.status_url || '',
    accountBalanceConfigured: Boolean(endpoint.account_balance_configured),
    accountCredentialKind: endpoint.account_credential_kind || '',
    capabilities: {
      thinking: endpoint.capabilities?.thinking !== false,
      cache: endpoint.capabilities?.cache !== false,
      tools: endpoint.capabilities?.tools !== false,
    },
  }));
}

export async function createRelayEndpoint(draft: RelayDraft): Promise<void> {
  await http.post('/api/config/relay-presets', {
    name: draft.name,
    url: draft.url,
    key: draft.key,
    default_model: draft.defaultModel,
    status_url: draft.statusUrl,
    capabilities: draft.capabilities,
  });
}

export async function getRelayIntelligence(id: number, refresh = false): Promise<RelayIntelligence> {
  const query = refresh ? '?refresh=1' : '';
  const data = await http.get<{
    models?: string[];
    model_options?: RelayModelInsight[];
    status_summary?: RelayModelHealth[];
    pricing_requires_auth?: boolean;
    pricing_source?: string | null;
    status_source?: string | null;
    cached?: boolean;
    source_project?: string;
  }>(`/api/config/relay-presets/${id}/intelligence${query}`);
  return {
    models: data.models || [],
    modelOptions: data.model_options || [],
    statusSummary: data.status_summary || [],
    pricingRequiresAuth: Boolean(data.pricing_requires_auth),
    pricingSource: data.pricing_source || '',
    statusSource: data.status_source || '',
    cached: Boolean(data.cached),
    sourceProject: data.source_project || '',
  };
}

export async function getRelayBalance(id: number): Promise<RelayBalance> {
  const data = await http.get<{
    balance?: {
      supported?: boolean;
      error?: RelayBalance['error'];
      source?: string;
      name?: string;
      unlimited?: boolean;
      expires_at?: number;
      total_granted_usd?: number | null;
      total_used_usd?: number | null;
      total_available_usd?: number | null;
    };
  }>(`/api/config/relay-presets/${id}/balance`);
  const balance = data.balance || {};
  return {
    supported: Boolean(balance.supported),
    error: balance.error || '',
    source: balance.source || '',
    name: balance.name || '',
    unlimited: Boolean(balance.unlimited),
    expiresAt: Number(balance.expires_at || 0),
    totalGrantedUsd: balance.total_granted_usd ?? null,
    totalUsedUsd: balance.total_used_usd ?? null,
    totalAvailableUsd: balance.total_available_usd ?? null,
  };
}

export async function getRelayAccountBalance(id: number): Promise<RelayAccountBalance> {
  const data = await http.get<{
    balance?: {
      supported?: boolean;
      error?: RelayAccountBalance['error'];
      source?: string;
      credential_kind?: 'access_token' | 'session_cookie';
      remaining_usd?: number | null;
      used_usd?: number | null;
      total_usd?: number | null;
    };
  }>(`/api/config/relay-presets/${id}/account-balance`);
  const balance = data.balance || {};
  return {
    supported: Boolean(balance.supported),
    error: balance.error || '',
    source: balance.source || '',
    credentialKind: balance.credential_kind || '',
    remainingUsd: balance.remaining_usd ?? null,
    usedUsd: balance.used_usd ?? null,
    totalUsd: balance.total_usd ?? null,
  };
}

export async function saveRelayAccountCredentials(
  id: number,
  userId: string,
  credentialKind: 'access_token' | 'session_cookie',
  credentialSecret: string,
): Promise<void> {
  await http.put(`/api/config/relay-presets/${id}/account-credentials`, {
    user_id: userId,
    credential_kind: credentialKind,
    credential_secret: credentialSecret,
  });
}

export async function clearRelayAccountCredentials(id: number): Promise<void> {
  await http.del(`/api/config/relay-presets/${id}/account-credentials`);
}

export async function activateRelayEndpoint(id: number): Promise<{ model: string }> {
  const data = await http.post<{ model_switched?: string }>(`/api/config/relay-presets/${id}/activate`);
  return { model: data.model_switched || '' };
}

export async function removeRelayEndpoint(id: number): Promise<void> {
  await http.del(`/api/config/relay-presets/${id}`);
}

export async function getModelCatalog(): Promise<{ models: ConfigModel[]; current: string }> {
  const data = await http.get<{
    models?: Array<{
      id?: string;
      label?: string;
      desc?: string;
      thinking?: string;
      primary?: boolean;
      dot?: string;
    }>;
    current?: string;
  }>('/api/config/model-catalog');

  return {
    current: data.current || '',
    models: (data.models || []).flatMap((model) => {
      const id = model.id?.trim();
      if (!id) return [];
      return [{
        id,
        label: model.label || id,
        description: model.desc || '',
        thinking: model.thinking || 'none',
        primary: Boolean(model.primary),
        dot: model.dot || '#B76E79',
      }];
    }),
  };
}

export async function getAvailableModels(): Promise<string[]> {
  const data = await http.get<{ ok?: boolean; models?: string[] }>('/api/config/models');
  return data.ok ? (data.models || []).filter(Boolean) : [];
}

export async function updateCurrentModel(model: string): Promise<void> {
  await http.post('/api/config/model', { model });
}

export async function getConfigUsageSummary(): Promise<ConfigUsageSummary> {
  const data = await http.get<{
    win5Pct?: number;
    win7Pct?: number;
    msgToday?: number;
    tokenToday?: number;
  }>('/api/usage/summary');
  return {
    win5Pct: Number(data.win5Pct || 0),
    win7Pct: Number(data.win7Pct || 0),
    todayMessages: Number(data.msgToday || 0),
    todayTokens: Number(data.tokenToday || 0),
  };
}

export async function getDailyUsage(days: number): Promise<DailyUsageResult> {
  const data = await http.get<{
    mode?: 'cost' | 'requests';
    days?: Array<{ date?: string; count?: number; cost?: number | null }>;
    relays?: Array<{ id?: number; name?: string; total_cost?: number; today_cost?: number; daily_costs?: Record<string, number> }>;
    total_cost?: number | null;
    total_count?: number;
    month_messages?: number;
  }>('/api/usage/daily-cost', { days });

  return {
    mode: data.mode === 'cost' ? 'cost' : 'requests',
    days: (data.days || []).map((row) => ({
      date: row.date || '',
      count: Number(row.count || 0),
      cost: row.cost == null ? null : Number(row.cost),
    })),
    relays: (data.relays || []).map((relay) => ({
      id: Number(relay.id || 0),
      name: relay.name || '',
      totalCost: Number(relay.total_cost || 0),
      todayCost: Number(relay.today_cost || 0),
      dailyCosts: relay.daily_costs && typeof relay.daily_costs === 'object' ? relay.daily_costs : {},
    })),
    totalCost: data.total_cost == null ? null : Number(data.total_cost),
    totalCount: Number(data.total_count || 0),
    monthMessages: Number(data.month_messages || 0),
  };
}

export async function runPlayground(message: string, injectMemory: boolean): Promise<PlaygroundResult> {
  const data = await http.post<{
    content?: string;
    thinking?: string;
    error?: string;
    latency_ms?: number;
    input_tokens?: number;
    output_tokens?: number;
    provider?: string;
  }>('/api/gw/test', { message, inject_memory: injectMemory });
  return {
    content: data.content || '',
    thinking: data.thinking || '',
    error: data.error || '',
    latencyMs: Number(data.latency_ms || 0),
    inputTokens: Number(data.input_tokens || 0),
    outputTokens: Number(data.output_tokens || 0),
    provider: data.provider || '',
  };
}
