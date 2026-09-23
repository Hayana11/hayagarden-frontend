export type ContinuityBlockStatus = 'complete' | 'compressing' | 'failed' | 'unavailable'

export type ContinuityBlock = {
  candidate_id: string
  local_day: string
  start_at: string | null
  end_at: string | null
  completed_turn_count: number
  logical_size: number
  original_char_count: number | null
  compressed_size: number | null
  compressed_char_count: number | null
  status: ContinuityBlockStatus
  included: null
  provider: string | null
  model: string | null
  generation_job_status: string | null
  chunk_status: string | null
  materialization_available: boolean
  materialization_error: string | null
}

export type ContinuityMessage = {
  id: string
  role: string
  content: string
  created_at: string | null
  source_ref: string | null
}

export type ContinuityChunk = {
  body: string | null
  [key: string]: unknown
}

export type ContinuityBlocksResponse = {
  ok: true
  count: number
  included_authority: 'unknown'
  blocks: ContinuityBlock[]
}

export type ContinuityBlockDetailResponse = {
  ok: true
  block: ContinuityBlock
  source: unknown
  chunk: ContinuityChunk | null
  messages: ContinuityMessage[]
}

export type ContinuityCurrentResponse = {
  ok: true
  available: boolean
  source_count: number
  completed_turn_count: number
  logical_size: number
  original_char_count: number
  start_at: string | null
  end_at: string | null
  body: string | null
  messages: ContinuityMessage[]
  size_target: number
  turn_target: number
  size_progress: number
  turn_progress: number
  source_refs: string[]
  settings_revision_id: string | null
  chat_id?: string | null
  context_id?: number | null
  context_epoch?: number | null
  threshold_reached?: boolean
  processing_state?: 'waiting' | 'threshold_reached'
  remaining_logical_size?: number
  remaining_completed_turns?: number
  materialization_error?: string | null
}

const API_ROOT = new URL('__continuity/', window.location.origin + import.meta.env.BASE_URL).pathname.replace(/\/$/, '')

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

async function fetchJson(path: string, signal?: AbortSignal): Promise<unknown> {
  const response = await fetch(API_ROOT + path, {
    method: 'GET',
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
    signal,
  })
  if (!response.ok) throw new Error(`Continuity API ${response.status}`)
  return response.json()
}

function assertOkRecord(value: unknown, label: string): asserts value is Record<string, unknown> & { ok: true } {
  if (!isRecord(value) || value.ok !== true) throw new Error(`${label} 返回格式无效`)
}

export async function fetchContinuityBlocks(signal?: AbortSignal): Promise<ContinuityBlocksResponse> {
  const value = await fetchJson('/blocks', signal)
  assertOkRecord(value, '压缩块列表')
  if (!Array.isArray(value.blocks) || typeof value.count !== 'number' || value.included_authority !== 'unknown') {
    throw new Error('压缩块列表返回格式无效')
  }
  return value as ContinuityBlocksResponse
}

export async function fetchContinuityBlockDetail(candidateId: string, signal?: AbortSignal): Promise<ContinuityBlockDetailResponse> {
  const value = await fetchJson('/blocks/' + encodeURIComponent(candidateId), signal)
  assertOkRecord(value, '压缩块详情')
  if (!isRecord(value.block) || !Array.isArray(value.messages)) throw new Error('压缩块详情返回格式无效')
  return value as ContinuityBlockDetailResponse
}

export async function fetchContinuityCurrent(signal?: AbortSignal): Promise<ContinuityCurrentResponse> {
  const value = await fetchJson('/current', signal)
  assertOkRecord(value, '当前块')
  if (typeof value.available !== 'boolean' || !Array.isArray(value.messages)) throw new Error('当前块返回格式无效')
  return value as ContinuityCurrentResponse
}
