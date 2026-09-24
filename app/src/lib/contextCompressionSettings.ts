export type CompressionSettingsConfig = {
  length: number
  turns: number
  provider: string
  model: string
  prompt: string
}

export type CompressionRegistryModel = {
  id: string
  label: string
  enabled: boolean
  disabledReason: string | null
}

export type CompressionRegistryProvider = {
  id: string
  label: string
  enabled: boolean
  disabledReason: string | null
  models: CompressionRegistryModel[]
}

export type ContinuitySettingsView = {
  activeRevision: string
  activeSource: string | null
  pendingRevision: string | null
  currentBlockRevision: string
  activeConfig: CompressionSettingsConfig
  pendingConfig: CompressionSettingsConfig | null
  editableConfig: CompressionSettingsConfig
  providers: CompressionRegistryProvider[]
  defaults: CompressionSettingsConfig
}

type JsonRecord = Record<string, unknown>

const API_ROOT = new URL('__continuity/', window.location.origin + import.meta.env.BASE_URL).pathname.replace(/\/$/, '')

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function firstString(record: JsonRecord, keys: string[]) {
  for (const key of keys) {
    const value = record[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return null
}

function firstNumber(record: JsonRecord, keys: string[]) {
  for (const key of keys) {
    const value = record[key]
    if (typeof value === 'number' && Number.isFinite(value)) return value
  }
  return null
}

function nestedRecords(value: unknown) {
  if (!isRecord(value)) return []
  const records = [value]
  for (const key of ['config', 'settings', 'value', 'policy', 'snapshot']) {
    if (isRecord(value[key])) records.push(value[key] as JsonRecord)
  }
  return records
}

function normalizeConfig(value: unknown): CompressionSettingsConfig | null {
  for (const record of nestedRecords(value)) {
    const length = firstNumber(record, ['length', 'tokens', 'size_target', 'logical_size_target'])
    const turns = firstNumber(record, ['turns', 'rounds', 'turn_target', 'completed_turn_target'])
    const provider = firstString(record, ['provider', 'provider_id'])
    const model = firstString(record, ['model', 'model_id'])
    const promptValue = record.prompt
    const prompt = typeof promptValue === 'string'
      ? promptValue
      : isRecord(promptValue)
        ? firstString(promptValue, ['body', 'text', 'value'])
        : firstString(record, ['prompt_body', 'prompt_text'])
    if (length !== null && turns !== null && provider && model && prompt) {
      return { length, turns, provider, model, prompt }
    }
  }
  return null
}

function revisionLabel(value: unknown) {
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  if (!isRecord(value)) return '—'
  return firstString(value, ['revision_id', 'revision', 'id', 'version', 'sha256'])
    || (typeof value.version === 'number' ? String(value.version) : '—')
}

function displayName(id: string) {
  return id
    .replace(/^explicit:/, '')
    .replace(/^claude-/, 'Claude ')
    .replace(/^deepseek-/, 'DeepSeek ')
    .replace(/[-_]/g, ' ')
    .replace(/\b\w/g, character => character.toUpperCase())
}

function normalizeModel(value: unknown, fallbackId: string, providerEnabled: boolean, providerReason: string | null): CompressionRegistryModel | null {
  if (typeof value === 'string') {
    return { id: value, label: displayName(value), enabled: providerEnabled, disabledReason: providerReason }
  }
  if (!isRecord(value)) return null
  const id = firstString(value, ['model', 'model_id', 'id', 'value']) || fallbackId
  if (!id) return null
  const enabled = providerEnabled && value.enabled !== false && value.disabled !== true
  const disabledReason = firstString(value, ['disabled_reason', 'reason', 'unavailable_reason']) || (enabled ? null : providerReason || '当前不可用')
  return {
    id,
    label: firstString(value, ['label', 'display_name', 'title', 'name']) || displayName(id),
    enabled,
    disabledReason,
  }
}

function modelEntries(value: unknown): Array<[string, unknown]> {
  if (Array.isArray(value)) return value.map((item, index) => [String(index), item])
  if (isRecord(value)) return Object.entries(value)
  return []
}

function normalizeProvider(value: unknown, fallbackId: string): CompressionRegistryProvider | null {
  const record = isRecord(value) ? value : {}
  const id = firstString(record, ['provider', 'provider_id', 'id', 'value']) || fallbackId
  if (!id) return null
  const enabled = record.enabled !== false && record.disabled !== true
  const disabledReason = firstString(record, ['disabled_reason', 'reason', 'unavailable_reason']) || (enabled ? null : '当前不可用')
  const rawModels = record.models ?? record.model_catalog ?? record.options ?? []
  const models = modelEntries(rawModels)
    .map(([key, item]) => normalizeModel(item, key, enabled, disabledReason))
    .filter((item): item is CompressionRegistryModel => item !== null)
  return {
    id,
    label: firstString(record, ['label', 'display_name', 'title', 'name']) || (id === 'claude_code' ? 'Claude' : id === 'api_relay' ? 'API Relay' : displayName(id)),
    enabled,
    disabledReason,
    models,
  }
}

function normalizeProviders(value: unknown) {
  const source = isRecord(value) && value.providers !== undefined ? value.providers : value
  const entries = Array.isArray(source)
    ? source.map((item, index): [string, unknown] => [String(index), item])
    : isRecord(source)
      ? Object.entries(source)
      : []
  return entries
    .map(([key, item]) => normalizeProvider(item, key))
    .filter((item): item is CompressionRegistryProvider => item !== null)
}

function normalizeResponse(value: unknown): ContinuitySettingsView {
  if (!isRecord(value) || value.ok !== true || !isRecord(value.authority)) {
    throw new Error('压缩设置返回格式无效')
  }
  const authority = value.authority
  const defaults = normalizeConfig(value.defaults)
  const activeConfig = normalizeConfig(authority.active_revision)
    || normalizeConfig(authority.active)
    || normalizeConfig(authority)
    || defaults
  if (!defaults || !activeConfig) throw new Error('压缩设置缺少真实默认值或当前配置')
  const hasPending = authority.pending_revision !== null && authority.pending_revision !== undefined
  const pendingConfig = hasPending
    ? normalizeConfig(authority.pending_revision) || normalizeConfig(authority.pending)
    : null
  const providers = normalizeProviders(value.registry)
  if (!providers.length) throw new Error('压缩设置缺少 provider registry')
  return {
    activeRevision: revisionLabel(authority.active_revision),
    activeSource: isRecord(authority.active_revision) ? firstString(authority.active_revision, ['source']) : null,
    pendingRevision: hasPending ? revisionLabel(authority.pending_revision) : null,
    currentBlockRevision: revisionLabel(authority.current_block),
    activeConfig,
    pendingConfig,
    editableConfig: pendingConfig || activeConfig,
    providers,
    defaults,
  }
}

async function settingsRequest(method: 'GET' | 'POST', body?: CompressionSettingsConfig, signal?: AbortSignal) {
  const response = await fetch(API_ROOT + '/settings', {
    method,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    signal,
  })
  const value: unknown = await response.json()
  if (!response.ok) {
    const message = isRecord(value) && typeof value.error === 'string' ? value.error : '压缩设置请求失败'
    throw new Error(message)
  }
  return normalizeResponse(value)
}

export function fetchContinuitySettings(signal?: AbortSignal) {
  return settingsRequest('GET', undefined, signal)
}

export function saveContinuitySettings(value: CompressionSettingsConfig, signal?: AbortSignal) {
  return settingsRequest('POST', value, signal)
}
