import { http } from './http';

export const RUNTIME_CAPABILITY_STATES = ['INHERIT', 'ON', 'OFF', 'DENY'] as const;
export type RuntimeCapabilityState = (typeof RUNTIME_CAPABILITY_STATES)[number];

export type CapabilityState = {
  capability_id: string;
  static_enabled: boolean;
  runtime_state: RuntimeCapabilityState;
  effective_enabled: boolean;
  writable: boolean;
};

export type CapabilityStateResponse = {
  ok: true;
  version: number;
  states: CapabilityState[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object';
}

function parseCapabilityState(value: unknown): CapabilityState {
  if (!isRecord(value)) throw new Error('能力状态响应无效');
  const runtimeState = value.runtime_state;
  if (
    typeof value.capability_id !== 'string'
    || typeof value.static_enabled !== 'boolean'
    || typeof value.effective_enabled !== 'boolean'
    || typeof value.writable !== 'boolean'
    || typeof runtimeState !== 'string'
    || !(RUNTIME_CAPABILITY_STATES as readonly string[]).includes(runtimeState)
  ) {
    throw new Error('能力状态响应无效');
  }
  return {
    capability_id: value.capability_id,
    static_enabled: value.static_enabled,
    runtime_state: runtimeState as RuntimeCapabilityState,
    effective_enabled: value.effective_enabled,
    writable: value.writable,
  };
}

export function fetchCapabilityStates(): Promise<CapabilityStateResponse> {
  return http.get<unknown>('/api/capabilities/states').then((payload) => {
    if (
      !isRecord(payload)
      || payload.ok !== true
      || typeof payload.version !== 'number'
      || !Array.isArray(payload.states)
    ) {
      throw new Error('能力状态响应无效');
    }
    return {
      ok: true,
      version: payload.version,
      states: payload.states.map(parseCapabilityState),
    };
  });
}
