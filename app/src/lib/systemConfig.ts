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
  capabilities: EndpointCapabilities;
}

export interface RelayDraft {
  name: string;
  url: string;
  key: string;
  defaultModel: string;
  capabilities: EndpointCapabilities;
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
      capabilities?: Partial<EndpointCapabilities>;
    }>;
  }>('/api/config/relay-presets');

  return (data.presets || []).map((endpoint) => ({
    id: endpoint.id,
    name: endpoint.name || `中转站 ${endpoint.id}`,
    url: endpoint.url || '',
    active: Boolean(endpoint.active),
    defaultModel: endpoint.default_model || '',
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
    capabilities: draft.capabilities,
  });
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

function monthKey(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;
}

function isoDate(date: Date): string {
  return `${monthKey(date)}-${String(date.getDate()).padStart(2, '0')}`;
}

export async function getDailyUsage(days: number): Promise<DailyUsage[]> {
  const dates = Array.from({ length: days }, (_, index) => {
    const date = new Date();
    date.setHours(12, 0, 0, 0);
    date.setDate(date.getDate() - (days - index - 1));
    return date;
  });
  const months = [...new Set(dates.map(monthKey))];
  const responses = await Promise.all(months.map(async (month) => {
    const data = await http.get<{ days?: Array<{ day?: number; count?: number }> }>('/api/messages/heatmap', { month });
    return [month, data.days || []] as const;
  }));
  const counts = new Map<string, number>();
  for (const [month, rows] of responses) {
    for (const row of rows) {
      if (!row.day) continue;
      counts.set(`${month}-${String(row.day).padStart(2, '0')}`, Number(row.count || 0));
    }
  }
  return dates.map((date) => ({ date: isoDate(date), count: counts.get(isoDate(date)) || 0 }));
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
