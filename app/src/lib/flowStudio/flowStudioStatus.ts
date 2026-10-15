import type { FlowStudioRuntimeStatus } from './flowStudioMock';

export type FlowSwitchTone = 'default' | 'enabled';
export type FlowStudioStatusTone = 'off' | 'idle' | 'running' | 'unknown';

export function flowSwitchClassName(checked: boolean, tone: FlowSwitchTone = 'default'): string {
  return [
    'flow-switch',
    checked ? 'is-on' : '',
    tone === 'enabled' ? 'is-enabled' : '',
  ].filter(Boolean).join(' ');
}

/** Map only the runtime reading to its display tone; editor config is separate. */
export function flowStudioStatusTone(runtimeStatus: FlowStudioRuntimeStatus): FlowStudioStatusTone {
  if (runtimeStatus.engineEnabled === false) return 'off';
  if (runtimeStatus.engineEnabled === null || runtimeStatus.activeFlow.state === 'unknown') return 'unknown';
  return runtimeStatus.activeFlow.state === 'active' ? 'running' : 'idle';
}
