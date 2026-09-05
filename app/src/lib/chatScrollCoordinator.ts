export type ChatScrollIntent = 'follow-latest' | 'preserve-position' | 'explicit-target';
export type ChatScrollSource =
  | 'initial-history'
  | 'warm-restore'
  | 'stream-follow'
  | 'background-poll'
  | 'search-jump'
  | 'history-window'
  | 'textarea-resize'
  | 'explicit-scroll-bottom';

export type ChatScrollProbeRecord = {
  timestamp: number;
  source: ChatScrollSource;
  intent: ChatScrollIntent;
  beforeScrollTop: number;
  afterScrollTop: number;
  targetScrollTop: number;
  clientHeight: number;
  scrollHeight: number;
  followLatest: boolean;
  textareaHeight?: number;
};

type ChatScrollContainer = Pick<HTMLElement, 'scrollHeight' | 'clientHeight'>;
type ChatScrollWriter = ChatScrollContainer & {
  scrollTop: number;
  scrollTo: (options: { top: number; behavior?: ScrollBehavior }) => void;
};

export function clampChatScrollTop(container: ChatScrollContainer, value: number): number {
  const maxScrollTop = Math.max(0, container.scrollHeight - container.clientHeight);
  const safeValue = Number.isFinite(value) ? value : 0;
  return Math.min(maxScrollTop, Math.max(0, safeValue));
}

export function writeChatScroll(
  container: ChatScrollWriter,
  request: {
    source: ChatScrollSource;
    intent: ChatScrollIntent;
    followLatest: boolean;
    targetScrollTop?: number;
    behavior?: ScrollBehavior;
    textareaHeight?: number;
  },
  probe?: (record: ChatScrollProbeRecord) => void,
): number {
  const beforeScrollTop = container.scrollTop;
  const targetScrollTop = request.intent === 'follow-latest'
    ? clampChatScrollTop(container, container.scrollHeight)
    : clampChatScrollTop(container, request.targetScrollTop ?? beforeScrollTop);

  if (request.behavior === 'smooth') {
    container.scrollTo({ top: targetScrollTop, behavior: 'smooth' });
  } else {
    container.scrollTop = targetScrollTop;
  }

  probe?.({
    timestamp: Date.now(),
    source: request.source,
    intent: request.intent,
    beforeScrollTop,
    afterScrollTop: container.scrollTop,
    targetScrollTop,
    clientHeight: container.clientHeight,
    scrollHeight: container.scrollHeight,
    followLatest: request.followLatest,
    textareaHeight: request.textareaHeight,
  });
  return targetScrollTop;
}

export function resizeChatTextarea(
  textarea: Pick<HTMLTextAreaElement, 'style' | 'scrollHeight'>,
  scrollContainer: (ChatScrollWriter & HTMLElement) | null,
  followLatest: boolean,
  probe?: (record: ChatScrollProbeRecord) => void,
): number {
  const snapshot = scrollContainer
    ? {
      followLatest,
      scrollTop: clampChatScrollTop(scrollContainer, scrollContainer.scrollTop),
    }
    : null;

  textarea.style.height = 'auto';
  const nextHeight = Math.min(textarea.scrollHeight, 120);
  textarea.style.height = `${nextHeight}px`;

  if (scrollContainer && snapshot) {
    writeChatScroll(
      scrollContainer,
      {
        source: 'textarea-resize',
        intent: snapshot.followLatest ? 'follow-latest' : 'preserve-position',
        followLatest: snapshot.followLatest,
        targetScrollTop: snapshot.scrollTop,
        textareaHeight: nextHeight,
      },
      probe,
    );
  }
  return nextHeight;
}