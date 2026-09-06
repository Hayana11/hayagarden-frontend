import type { ChatMsg, ChatToolCall } from './chat';
import type { LiveSegment } from './chatLiveTimeline';

export type ChatHandoffKind = 'send' | 'choice' | 'regen' | 'edit' | 'confirmation';

export interface ChatStreamHandoff {
  operationId: string;
  presentationKey: string;
  kind: ChatHandoffKind;
  userMessageId: number | null;
  sourceAssistantId: number | null;
  finalMessageId: number | null;
  finalizationConfirmed: boolean;
  terminal: 'live' | 'success' | 'failed';
  freshSegmentIds: Set<number>;
}

export function createChatStreamHandoff(args: {
  kind: ChatHandoffKind;
  userMessageId?: number | null;
  rewriteId?: string | null;
  operationIdentity?: string | null;
  sourceAssistantId?: number | null;
}): ChatStreamHandoff {
  const identity = args.operationIdentity || args.rewriteId || (
    args.userMessageId == null ? 'unknown' : String(args.userMessageId)
  );
  const operationId = args.kind + ':' + identity;
  return {
    operationId,
    presentationKey: 'chat-presentation-' + operationId,
    kind: args.kind,
    userMessageId: args.userMessageId ?? null,
    sourceAssistantId: args.sourceAssistantId ?? null,
    finalMessageId: null,
    finalizationConfirmed: false,
    terminal: 'live',
    freshSegmentIds: new Set<number>(),
  };
}

export function markChatHandoffFinalized(handoff: ChatStreamHandoff): void {
  handoff.finalizationConfirmed = true;
  handoff.terminal = 'success';
}

export function markChatHandoffFailed(handoff: ChatStreamHandoff): void {
  handoff.finalizationConfirmed = false;
  handoff.finalMessageId = null;
  handoff.terminal = 'failed';
  handoff.freshSegmentIds.clear();
}

export function clearChatStreamHandoff(_handoff: ChatStreamHandoff | null): null {
  return null;
}

export function findAssistantAfter(messages: ChatMsg[], sourceMessageId: number | null): number | null {
  if (sourceMessageId == null) return null;
  const sourceIndex = messages.findIndex((message) => message.id === sourceMessageId);
  if (sourceIndex < 0) return null;
  const candidate = messages.slice(sourceIndex + 1).find((message) => message.role === 'assistant');
  return candidate?.id ?? null;
}

export function presentationSegmentKey(handoff: ChatStreamHandoff, segmentIndex: number): string {
  return handoff.presentationKey + '-segment-' + segmentIndex;
}

export function thinkingStateKey(handoff: ChatStreamHandoff, segmentIndex: number): string {
  return presentationSegmentKey(handoff, segmentIndex) + '-thinking';
}

export function isLiveSegmentFresh(handoff: ChatStreamHandoff, segment: LiveSegment): boolean {
  return handoff.terminal === 'live' && handoff.freshSegmentIds.has(segment.id);
}

export function canHandoffToFinal(handoff: ChatStreamHandoff): boolean {
  return handoff.terminal === 'success' && handoff.finalizationConfirmed && handoff.finalMessageId !== null;
}

export function finalOnlyContentMayAnimate(liveSegments: LiveSegment[], finalContentWasLive: boolean): boolean {
  return !finalContentWasLive || liveSegments.length === 0;
}

function normalizedToolName(tool: ChatToolCall | undefined): string {
  return tool?.name || '';
}

function persistedSegments(message: ChatMsg): Array<
  { type: 'thinking' | 'text'; text: string } |
  { type: 'tool'; toolIndex: number; tool: ChatToolCall | undefined }
> {
  if (message.displaySegments?.length) {
    return message.displaySegments.map((segment) => (
      segment.type === 'tool'
        ? { type: 'tool', toolIndex: segment.toolIndex, tool: message.toolCalls[segment.toolIndex] }
        : { type: segment.type, text: segment.text }
    ));
  }
  const result: Array<
    { type: 'thinking' | 'text'; text: string } |
    { type: 'tool'; toolIndex: number; tool: ChatToolCall | undefined }
  > = [];
  if (message.thinking) result.push({ type: 'thinking', text: message.thinking });
  if (message.text) result.push({ type: 'text', text: message.text });
  message.toolCalls.forEach((tool, toolIndex) => result.push({ type: 'tool', toolIndex, tool }));
  return result;
}

function liveMatchesPersisted(message: ChatMsg, liveSegments: LiveSegment[]): boolean {
  const persisted = persistedSegments(message);
  if (persisted.length !== liveSegments.length) return false;
  return liveSegments.every((segment, index) => {
    const candidate = persisted[index];
    if (!candidate) return false;
    if (segment.type === 'tool') {
      if (candidate.type !== 'tool') return false;
      return candidate.toolIndex === segment.idx
        && normalizedToolName(candidate.tool) === normalizedToolName(segment.tool);
    }
    if (candidate.type === 'tool') return false;
    return candidate.text === segment.text;
  });
}

export function findPersistedAssistantForHandoff(
  messages: ChatMsg[],
  handoff: ChatStreamHandoff,
  liveSegments: LiveSegment[],
): number | null {
  if (!handoff.finalizationConfirmed || handoff.terminal === 'failed') return null;
  if (handoff.finalMessageId !== null) {
    return messages.some((message) => message.id === handoff.finalMessageId)
      ? handoff.finalMessageId
      : null;
  }
  if (handoff.sourceAssistantId !== null) {
    return messages.some((message) => (
      message.id === handoff.sourceAssistantId && message.role === 'assistant'
    )) ? handoff.sourceAssistantId : null;
  }
  if (handoff.userMessageId === null) return null;
  const sourceIndex = messages.findIndex((message) => message.id === handoff.userMessageId);
  if (sourceIndex < 0) return null;
  const candidates = messages.slice(sourceIndex + 1).filter((message) => message.role === 'assistant');
  const matched = candidates.find((message) => (
    liveSegments.length === 0 || liveMatchesPersisted(message, liveSegments)
  ));
  return matched?.id ?? null;
}
