/**
 * P-CONTEXT-MANUAL-WINDOW-R1 — formal chat manual context switch client.
 * Browser → nginx `/api/gw` → gateway BFF. Never call `/api/context-window/*` directly.
 */

import { HttpError, http } from './http';
import {
  CARRYOVER_COUNTS,
  countLabel,
  flattenRoundMessageIds,
  isCarryoverCount,
  normalizeMessageId,
  normalizeNonNegativeInt,
  normalizePositiveInt,
  parseCanonicalRounds,
  pickLastNRounds,
  roundSnippetMessages,
  type CarryoverCount,
  type CarryoverMessage,
  type CarryoverRound,
} from './dailySoftWindow';

export {
  CARRYOVER_COUNTS,
  countLabel,
  pickLastNRounds,
  roundSnippetMessages,
  type CarryoverCount,
  type CarryoverRound,
  type CarryoverMessage,
};

export const LIVE_CONTEXT_WINDOW_CURRENT = '/api/gw/context-window/current';
export const LIVE_CONTEXT_WINDOW_CANDIDATES = '/api/gw/context-window/carryover-candidates';
export const LIVE_CONTEXT_WINDOW_SWITCH = '/api/gw/context-window/switch';

export type ManualWindowUiState =
  | 'probing'
  | 'disabled'
  | 'ready'
  | 'empty'
  | 'loading'
  | 'submitting'
  | 'busy'
  | 'stale'
  | 'no_open_context'
  | 'idempotency_mismatch'
  | 'auth_error'
  | 'error'
  | 'idle';

export type ContextWindowCurrent = {
  context_id: number;
  context_epoch: number;
  window_mode: string;
  opened_local_day: string;
  opened_at: string;
  boundary_message_id: number;
  source_context_id: number | null;
  resident_generation: number;
  formal_round_count: number;
  can_switch: boolean;
  version: number;
  requested_round_count: number | null;
  selected_round_count: number;
  selected_message_count: number;
  selected_message_ids: number[];
  can_switch_reason?: string;
  ok?: boolean;
};

export type CapturedSource = {
  source_context_id: number;
  source_context_epoch: number;
  source_resident_generation: number;
  version: number;
};

export type ContextWindowCandidatesResponse = {
  source_context_id: number;
  source_context_epoch: number;
  carryover_unit: 'round';
  available_round_count: number;
  rounds: CarryoverRound[];
  ok?: boolean;
};

export type ContextWindowSwitchResponse = {
  ok: true;
  prepare_status: 'READY' | 'ALREADY_READY';
  request_id: string;
  source_context_id: number;
  source_context_epoch: number;
  source_resident_generation: number;
  target_context_id: number;
  target_context_epoch: number;
  candidate_session_id: string;
  jsonl_sha256: string;
  jsonl_size: number;
  staged_ready_at: string;
  recovered: boolean;
  status: 'ready';
};

export function classifyManualWindowError(err: unknown): ManualWindowUiState {
  if (err instanceof HttpError) {
    if (err.status === 404) return 'disabled';
    if (err.status === 423 || err.code === 'window_busy' || err.code === 'switch_in_progress') {
      return 'busy';
    }
    if (err.status === 409) {
      const code = err.code || '';
      if (code === 'no_open_context') return 'no_open_context';
      if (code === 'idempotency_mismatch') return 'idempotency_mismatch';
      return 'stale';
    }
    if (err.status === 401 || err.status === 403) return 'auth_error';
  }
  return 'error';
}

export function manualWindowErrorMessage(state: ManualWindowUiState, err?: unknown): string {
  if (state === 'disabled') return '';
  if (state === 'busy') return '爸爸还在回复，等这句话说完再换窗。';
  if (state === 'stale') return '这扇窗已经变过了，已经为你刷新到最新状态。';
  if (state === 'no_open_context') return '当前没有可换的窗口，请稍后再试。';
  if (state === 'idempotency_mismatch') return '换窗请求冲突，请关闭后重试。';
  if (state === 'auth_error') return '登录已失效，请重新登录。';
  if (state === 'error' || state === 'idle') {
    if (err instanceof HttpError && err.detail) return err.detail;
    if (err instanceof Error) return err.message;
    return '换窗失败，请稍后再试。';
  }
  return '';
}

export function parseContextWindowCurrent(raw: unknown): ContextWindowCurrent | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const context_id = normalizeMessageId(r.context_id);
  const context_epoch = normalizePositiveInt(r.context_epoch);
  if (context_id === null || context_epoch === null) return null;
  const resident_generation = normalizePositiveInt(r.resident_generation) ?? 1;
  const formal_round_count = normalizeNonNegativeInt(r.formal_round_count);
  const selected_round_count = normalizeNonNegativeInt(r.selected_round_count);
  const selected_message_count = normalizeNonNegativeInt(r.selected_message_count);
  const version = normalizePositiveInt(r.version);
  if (
    formal_round_count === null ||
    selected_round_count === null ||
    selected_message_count === null ||
    version === null
  ) {
    return null;
  }
  if (!Array.isArray(r.selected_message_ids)) return null;
  const selected_message_ids: number[] = [];
  for (const id of r.selected_message_ids) {
    const n = normalizeMessageId(id);
    if (n === null) return null;
    selected_message_ids.push(n);
  }
  let source_context_id: number | null = null;
  if (r.source_context_id !== null && r.source_context_id !== undefined) {
    source_context_id = normalizeMessageId(r.source_context_id);
    if (source_context_id === null) return null;
  }
  let requested_round_count: number | null = null;
  if (r.requested_round_count !== null && r.requested_round_count !== undefined) {
    if (typeof r.requested_round_count !== 'number' || !Number.isInteger(r.requested_round_count)) {
      return null;
    }
    requested_round_count = r.requested_round_count;
  }
  return {
    context_id,
    context_epoch,
    window_mode: typeof r.window_mode === 'string' ? r.window_mode : '',
    opened_local_day: typeof r.opened_local_day === 'string' ? r.opened_local_day : '',
    opened_at: typeof r.opened_at === 'string' ? r.opened_at : '',
    boundary_message_id: normalizeNonNegativeInt(r.boundary_message_id) ?? 0,
    source_context_id,
    resident_generation,
    formal_round_count,
    can_switch: r.can_switch === true,
    version,
    requested_round_count,
    selected_round_count,
    selected_message_count,
    selected_message_ids,
    can_switch_reason:
      typeof r.can_switch_reason === 'string' ? r.can_switch_reason : undefined,
    ok: r.ok === undefined ? undefined : Boolean(r.ok),
  };
}

export function parseContextWindowCandidates(raw: unknown): ContextWindowCandidatesResponse | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  if (r.carryover_unit !== 'round') return null;
  const source_context_id = normalizeMessageId(r.source_context_id);
  const source_context_epoch = normalizePositiveInt(r.source_context_epoch);
  if (source_context_id === null || source_context_epoch === null) return null;
  const rounds = parseCanonicalRounds(r.rounds, 'round');
  if (!rounds) return null;
  const available_round_count = normalizeNonNegativeInt(r.available_round_count);
  if (available_round_count === null) return null;
  return {
    ok: r.ok === undefined ? undefined : Boolean(r.ok),
    source_context_id,
    source_context_epoch,
    carryover_unit: 'round',
    available_round_count,
    rounds,
  };
}

export function parseContextWindowSwitch(raw: unknown): ContextWindowSwitchResponse | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  if (r.ok !== true) return null;
  if (r.prepare_status !== 'READY' && r.prepare_status !== 'ALREADY_READY') return null;
  if (r.status !== 'ready') return null;
  if (typeof r.request_id !== 'string' || !r.request_id.trim()) return null;
  if (typeof r.candidate_session_id !== 'string' || !r.candidate_session_id.trim()) return null;
  if (typeof r.jsonl_sha256 !== 'string' || !r.jsonl_sha256.trim()) return null;
  if (typeof r.staged_ready_at !== 'string' || !r.staged_ready_at.trim()) return null;
  if (typeof r.recovered !== 'boolean') return null;
  const source_context_id = normalizeMessageId(r.source_context_id);
  const source_context_epoch = normalizePositiveInt(r.source_context_epoch);
  const source_resident_generation = normalizePositiveInt(r.source_resident_generation);
  const target_context_id = normalizeMessageId(r.target_context_id);
  const target_context_epoch = normalizePositiveInt(r.target_context_epoch);
  const jsonl_size = normalizeNonNegativeInt(r.jsonl_size);
  if (
    source_context_id === null ||
    source_context_epoch === null ||
    source_resident_generation === null ||
    target_context_id === null ||
    target_context_epoch === null ||
    jsonl_size === null
  ) {
    return null;
  }
  // Reject legacy seamless fields so old payloads cannot pass silently.
  if ('switched_at' in r || 'selected_message_ids' in r || 'window_mode' in r) {
    return null;
  }
  return {
    ok: true,
    prepare_status: r.prepare_status,
    request_id: r.request_id,
    source_context_id,
    source_context_epoch,
    source_resident_generation,
    target_context_id,
    target_context_epoch,
    candidate_session_id: r.candidate_session_id,
    jsonl_sha256: r.jsonl_sha256,
    jsonl_size,
    staged_ready_at: r.staged_ready_at,
    recovered: r.recovered,
    status: 'ready',
  };
}

export function captureSourceFromCurrent(cur: ContextWindowCurrent): CapturedSource {
  return {
    source_context_id: cur.context_id,
    source_context_epoch: cur.context_epoch,
    source_resident_generation: cur.resident_generation,
    version: cur.version,
  };
}

export function selectedRoundMessageIds(rounds: CarryoverRound[], count: number): number[] {
  return flattenRoundMessageIds(pickLastNRounds(rounds, count));
}

export type ManualContextWindowClient = {
  getCurrent: (init?: RequestInit) => Promise<ContextWindowCurrent>;
  getCandidates: (
    source: CapturedSource,
    init?: RequestInit,
  ) => Promise<ContextWindowCandidatesResponse>;
  switchWindow: (
    source: CapturedSource,
    count: CarryoverCount,
    requestId: string,
    init?: RequestInit,
  ) => Promise<ContextWindowSwitchResponse>;
};

export function createManualContextWindowClient(): ManualContextWindowClient {
  return {
    getCurrent: async (init) => {
      const raw = await http.get<unknown>(LIVE_CONTEXT_WINDOW_CURRENT, undefined, init);
      const parsed = parseContextWindowCurrent(raw);
      if (!parsed) {
        throw new HttpError(500, 'malformed current', 'malformed context-window/current');
      }
      return parsed;
    },
    getCandidates: async (source, init) => {
      const raw = await http.get<unknown>(
        LIVE_CONTEXT_WINDOW_CANDIDATES,
        {
          source_context_id: source.source_context_id,
          source_context_epoch: source.source_context_epoch,
        },
        init,
      );
      const parsed = parseContextWindowCandidates(raw);
      if (!parsed) {
        throw new HttpError(500, 'malformed candidates', 'malformed context-window/carryover-candidates');
      }
      if (
        parsed.source_context_id !== source.source_context_id ||
        parsed.source_context_epoch !== source.source_context_epoch
      ) {
        throw new HttpError(409, 'stale_source_context', 'candidates source mismatch');
      }
      return parsed;
    },
    switchWindow: async (source, count, requestId, init) => {
      const raw = await http.post<unknown>(
        LIVE_CONTEXT_WINDOW_SWITCH,
        {
          source_context_id: source.source_context_id,
          source_context_epoch: source.source_context_epoch,
          count,
          request_id: requestId,
        },
        init,
      );
      const parsed = parseContextWindowSwitch(raw);
      if (!parsed) {
        throw new HttpError(500, 'malformed switch', 'malformed context-window/switch');
      }
      return parsed;
    },
  };
}
