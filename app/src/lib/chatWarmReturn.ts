import type { ChatMsg } from './chat';
import type { TranscriptWindow } from './legacyTranscriptWindow';

export const CHAT_WARM_RETURN_TTL_MS = 5 * 60 * 1000;

export interface ChatWarmReturnSnapshot {
  version: 1;
  capturedAt: number;
  legacyCompat: boolean;
  messages: ChatMsg[];
  hasMoreBefore: boolean;
  txWin: TranscriptWindow;
  followLatest: boolean;
  scrollTop: number;
}
export interface ChatWarmReturnInput {
  legacyCompat: boolean;
  messages: ChatMsg[];
  hasMoreBefore: boolean;
  txWin: TranscriptWindow;
  followLatest: boolean;
  scrollTop: number;
}
export interface ChatWarmReturnPage {
  messages: ChatMsg[];
  hasMoreBefore: boolean;
}
let snapshot: ChatWarmReturnSnapshot | null = null;
function copySnapshot(value: ChatWarmReturnSnapshot): ChatWarmReturnSnapshot {
  return { ...value, messages: [...value.messages], txWin: { ...value.txWin } };
}
export function readChatWarmReturn(legacyCompat: boolean): ChatWarmReturnSnapshot | null {
  if (!snapshot) return null;
  if (
    snapshot.version !== 1
    || snapshot.messages.length === 0
    || snapshot.legacyCompat !== legacyCompat
    || Date.now() - snapshot.capturedAt >= CHAT_WARM_RETURN_TTL_MS
  ) {
    snapshot = null;
    return null;
  }
  return copySnapshot(snapshot);
}
export function writeChatWarmReturn(input: ChatWarmReturnInput): void {
  if (input.messages.length === 0) return;
  snapshot = {
    version: 1,
    capturedAt: Date.now(),
    legacyCompat: input.legacyCompat,
    messages: [...input.messages],
    hasMoreBefore: input.hasMoreBefore,
    txWin: { ...input.txWin },
    followLatest: input.followLatest,
    scrollTop: input.scrollTop,
  };
}
export function clearChatWarmReturn(): void {
  snapshot = null;
}
export function reconcileChatWarmReturn(
  cached: Pick<ChatWarmReturnSnapshot, 'messages' | 'hasMoreBefore'>,
  fresh: ChatWarmReturnPage,
): ChatWarmReturnPage {
  if (fresh.messages.length === 0 || !fresh.hasMoreBefore) {
    return { messages: [...fresh.messages], hasMoreBefore: fresh.hasMoreBefore };
  }
  const overlapIndex = cached.messages.findIndex(
    (message) => message.id === fresh.messages[0].id,
  );
  if (overlapIndex < 0) {
    return { messages: [...fresh.messages], hasMoreBefore: fresh.hasMoreBefore };
  }
  return {
    messages: [...cached.messages.slice(0, overlapIndex), ...fresh.messages],
    hasMoreBefore: cached.hasMoreBefore,
  };
}
export function __resetChatWarmReturnForTests(): void {
  snapshot = null;
}
