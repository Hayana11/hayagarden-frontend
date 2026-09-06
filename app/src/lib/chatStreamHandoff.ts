import type { ChatMsg } from './chat';
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

function handoffPresentationKey(
  handoffOrKey: Pick<ChatStreamHandoff, 'presentationKey'> | string,
): string {
  return typeof handoffOrKey === 'string' ? handoffOrKey : handoffOrKey.presentationKey;
}

export function presentationSegmentKey(
  handoffOrKey: Pick<ChatStreamHandoff, 'presentationKey'> | string,
  segmentIndex: number,
): string {
  return handoffPresentationKey(handoffOrKey) + '-segment-' + segmentIndex;
}

export function thinkingStateKey(
  handoffOrKey: Pick<ChatStreamHandoff, 'presentationKey'> | string,
  segmentIndex: number,
): string {
  return presentationSegmentKey(handoffOrKey, segmentIndex) + '-thinking';
}

export function isLiveSegmentFresh(handoff: ChatStreamHandoff, segment: LiveSegment): boolean {
  return handoff.terminal === 'live' && handoff.freshSegmentIds.has(segment.id);
}

export function canHandoffToFinal(handoff: ChatStreamHandoff): boolean {
  return handoff.terminal === 'success' && handoff.finalizationConfirmed && handoff.finalMessageId !== null;
}

export type ChatPresentationEntry =
  | { kind: 'date'; key: string; dateKey: string; messageId: number }
  | { kind: 'message'; key: string; message: ChatMsg; presentationKey: string }
  | { kind: 'live'; key: string; presentationKey: string };

export function commitChatPresentationBinding(
  handoff: ChatStreamHandoff,
  assistantMessageId: number | null | undefined,
  presentationKeys: Map<number, string>,
): boolean {
  if (!Number.isSafeInteger(assistantMessageId) || Number(assistantMessageId) <= 0) return false;
  const finalMessageId = Number(assistantMessageId);
  handoff.finalMessageId = finalMessageId;
  presentationKeys.set(finalMessageId, handoff.presentationKey);
  return true;
}

export function pruneChatPresentationBindings(
  presentationKeys: Map<number, string>,
  retainedMessages: ChatMsg[],
): void {
  const retainedIds = new Set(retainedMessages.map((message) => message.id));
  for (const messageId of presentationKeys.keys()) {
    if (!retainedIds.has(messageId)) presentationKeys.delete(messageId);
  }
}

export function buildChatPresentationEntries(args: {
  messages: ChatMsg[];
  handoff: Pick<ChatStreamHandoff, 'presentationKey' | 'sourceAssistantId'> | null;
  finalMessageId: number | null;
  liveVisible: boolean;
  presentationKeys?: Map<number, string>;
}): ChatPresentationEntry[] {
  const entries: ChatPresentationEntry[] = [];
  const presentationKeys = args.presentationKeys || new Map<number, string>();
  let lastDate = '';
  let sourceReplaced = false;

  args.messages.forEach((message) => {
    if (message.dateKey && message.dateKey !== lastDate) {
      lastDate = message.dateKey;
      entries.push({
        kind: 'date',
        key: 'd-' + message.dateKey + '-' + message.id,
        dateKey: message.dateKey,
        messageId: message.id,
      });
    }

    if (
      args.liveVisible
      && args.finalMessageId === null
      && args.handoff
      && args.handoff.sourceAssistantId === message.id
      && message.role === 'assistant'
    ) {
      entries.push({
        kind: 'live',
        key: args.handoff.presentationKey,
        presentationKey: args.handoff.presentationKey,
      });
      sourceReplaced = true;
      return;
    }

    const presentationKey = args.finalMessageId === message.id && args.handoff
      ? args.handoff.presentationKey
      : presentationKeys.get(message.id) || String(message.id);
    entries.push({
      kind: 'message',
      key: message.role === 'assistant' ? presentationKey : String(message.id),
      message,
      presentationKey,
    });
  });

  if (args.liveVisible && args.handoff && !sourceReplaced) {
    entries.push({
      kind: 'live',
      key: args.handoff.presentationKey,
      presentationKey: args.handoff.presentationKey,
    });
  }
  return entries;
}

export function findPersistedAssistantForHandoff(
  messages: ChatMsg[],
  handoff: ChatStreamHandoff,
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
  return null;
}
