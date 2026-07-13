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

export interface RelayCapabilities {
  thinking: boolean;
  cache: boolean;
  tools: boolean;
}

export interface RelayPreset {
  id: number;
  name: string;
  url: string;
  active: boolean;
  defaultModel: string;
  capabilities: RelayCapabilities;
}

export interface RelayPresetDraft {
  name: string;
  url: string;
  key: string;
  defaultModel: string;
  capabilities: RelayCapabilities;
}

export interface GatewayTestResult {
  content: string;
  error: string;
  latencyMs: number;
  inputTokens: number;
  outputTokens: number;
}

export async function fetchProviderConfig(): Promise<ProviderConfig> {
  const data = await http.get<{ provider?: ChatProvider; cc_token_set?: boolean }>('/api/config/provider');
  return {
    provider: data.provider === 'claude_code' ? 'claude_code' : 'api_relay',
    ccTokenSet: Boolean(data.cc_token_set),
  };
}

export async function setProvider(provider: ChatProvider): Promise<void> {
  await http.post('/api/config/provider', { provider });
}

export async function fetchKeyStatus(): Promise<KeyStatus> {
  const data = await http.get<{ masked_key?: string; source?: string; today_msgs?: number }>('/api/config/key-status');
  return {
    maskedKey: data.masked_key || '—',
    source: data.source || '—',
    todayMessages: Number(data.today_msgs || 0),
  };
}

export async function fetchRelayPresets(): Promise<RelayPreset[]> {
  const data = await http.get<{
    presets?: Array<{
      id: number;
      name?: string;
      url?: string;
      active?: boolean;
      default_model?: string;
      capabilities?: Partial<RelayCapabilities>;
    }>;
  }>('/api/config/relay-presets');

  return (data.presets || []).map((preset) => ({
    id: preset.id,
    name: preset.name || `relay ${preset.id}`,
    url: preset.url || '',
    active: Boolean(preset.active),
    defaultModel: preset.default_model || '',
    capabilities: {
      thinking: preset.capabilities?.thinking !== false,
      cache: preset.capabilities?.cache !== false,
      tools: preset.capabilities?.tools !== false,
    },
  }));
}

export async function addRelayPreset(draft: RelayPresetDraft): Promise<void> {
  await http.post('/api/config/relay-presets', {
    name: draft.name,
    url: draft.url,
    key: draft.key,
    default_model: draft.defaultModel,
    capabilities: draft.capabilities,
  });
}

export async function activateRelayPreset(id: number): Promise<{ modelSwitched: string }> {
  const data = await http.post<{ model_switched?: string }>(`/api/config/relay-presets/${id}/activate`);
  return { modelSwitched: data.model_switched || '' };
}

export async function deleteRelayPreset(id: number): Promise<void> {
  await http.del(`/api/config/relay-presets/${id}`);
}

export async function fetchAvailableModels(): Promise<string[]> {
  const data = await http.get<{ ok?: boolean; models?: string[] }>('/api/config/models');
  return data.ok ? (data.models || []) : [];
}

export async function testGateway(message: string, injectMemory: boolean): Promise<GatewayTestResult> {
  const data = await http.post<{
    content?: string;
    error?: string;
    latency_ms?: number;
    input_tokens?: number;
    output_tokens?: number;
  }>('/api/gw/test', { message, inject_memory: injectMemory });

  return {
    content: data.content || '',
    error: data.error || '',
    latencyMs: Number(data.latency_ms || 0),
    inputTokens: Number(data.input_tokens || 0),
    outputTokens: Number(data.output_tokens || 0),
  };
}
