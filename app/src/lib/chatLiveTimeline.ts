import { normalizeToolCall, type ChatToolCall } from './chat';
import { lastItem } from './lastItem';

export type LiveSegment =
  | { id: number; type: 'thinking'; text: string }
  | { id: number; type: 'text'; text: string }
  | { id: number; type: 'tool'; idx: number; tool: ChatToolCall };

export type LiveEvent = 'connecting' | 'thinking' | 'text' | 'tool';

export interface LiveState {
  segments: LiveSegment[];
  nextSegmentId: number;
  lastEvent: LiveEvent;
}

export function createLiveState(): LiveState {
  return { segments: [], nextSegmentId: 0, lastEvent: 'connecting' };
}

export function isTextCaretActive(state: LiveState): boolean {
  return state.lastEvent === 'text' && lastItem(state.segments)?.type === 'text';
}

function appendDelta(state: LiveState, type: 'thinking' | 'text', delta: string): LiveState {
  if (!delta) return state;
  const segments = [...state.segments];
  const last = segments[segments.length - 1];
  if (last?.type === type) {
    segments[segments.length - 1] = { ...last, text: last.text + delta };
    return { ...state, segments, lastEvent: type };
  }
  segments.push({ id: state.nextSegmentId, type, text: delta });
  return { ...state, segments, nextSegmentId: state.nextSegmentId + 1, lastEvent: type };
}

export function appendThinkingDelta(state: LiveState, delta: string): LiveState {
  return appendDelta(state, 'thinking', delta);
}

export function appendTextDelta(state: LiveState, delta: string): LiveState {
  return appendDelta(state, 'text', delta);
}

export function upsertToolUse(state: LiveState, idx: number, tool: ChatToolCall): LiveState {
  const existingIndex = state.segments.findIndex((segment) => segment.type === 'tool' && segment.idx === idx);
  if (existingIndex >= 0) {
    const existing = state.segments[existingIndex];
    if (existing.type !== 'tool') return state;
    const segments = [...state.segments];
    segments[existingIndex] = {
      ...existing,
      tool: normalizeToolCall({ ...existing.tool, ...tool, running: true }),
    };
    return { ...state, segments, lastEvent: 'tool' };
  }

  return {
    ...state,
    segments: [...state.segments, {
      id: state.nextSegmentId,
      type: 'tool',
      idx,
      tool: normalizeToolCall({ ...tool, running: true }),
    }],
    nextSegmentId: state.nextSegmentId + 1,
    lastEvent: 'tool',
  };
}

export function applyToolResult(state: LiveState, idx: number, result: ChatToolCall): LiveState {
  const existingIndex = state.segments.findIndex((segment) => segment.type === 'tool' && segment.idx === idx);
  if (existingIndex < 0) return state;
  const existing = state.segments[existingIndex];
  if (existing.type !== 'tool') return state;
  const segments = [...state.segments];
  segments[existingIndex] = {
    ...existing,
    tool: normalizeToolCall({ ...existing.tool, ...result, running: false }),
  };
  return { ...state, segments, lastEvent: 'tool' };
}
