import { CHAT_WARM_RETURN_TTL_MS } from './chatWarmReturn';

export const CHAT_FOLLOW_LATEST_THRESHOLD_PX = 32;
export const CHAT_COMPOSER_DRAFT_STORAGE_KEY = 'elpis.chat.composer-draft.v1';

export interface ChatScrollGeometry {
  scrollHeight: number;
  scrollTop: number;
  clientHeight: number;
}

export function distanceFromBottom(geometry: ChatScrollGeometry): number {
  return Math.max(
    0,
    geometry.scrollHeight - geometry.scrollTop - geometry.clientHeight,
  );
}

export function followLatestFromGeometry(
  geometry: ChatScrollGeometry,
  logicalWindowIsLatest = true,
): boolean {
  return logicalWindowIsLatest
    && distanceFromBottom(geometry) <= CHAT_FOLLOW_LATEST_THRESHOLD_PX;
}

interface ChatComposerDraft {
  version: 1;
  text: string;
  updatedAt: number;
}

function getSessionStorage(): Storage | null {
  try {
    if (typeof window === 'undefined') return null;
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function discardDraft(storage: Storage): void {
  try {
    storage.removeItem(CHAT_COMPOSER_DRAFT_STORAGE_KEY);
  } catch {
    /* storage can be unavailable */
  }
}

export function readChatComposerDraft(): string {
  const storage = getSessionStorage();
  if (!storage) return '';

  let raw: string | null;
  try {
    raw = storage.getItem(CHAT_COMPOSER_DRAFT_STORAGE_KEY);
  } catch {
    return '';
  }
  if (raw === null) return '';

  let parsed: ChatComposerDraft;
  try {
    parsed = JSON.parse(raw) as ChatComposerDraft;
  } catch {
    discardDraft(storage);
    return '';
  }

  if (
    parsed === null
    || parsed.version !== 1
    || typeof parsed.text !== 'string'
    || typeof parsed.updatedAt !== 'number'
    || !Number.isFinite(parsed.updatedAt)
    || Date.now() - parsed.updatedAt >= CHAT_WARM_RETURN_TTL_MS
  ) {
    discardDraft(storage);
    return '';
  }
  return parsed.text;
}

export function writeChatComposerDraft(text: string): void {
  const storage = getSessionStorage();
  if (!storage) return;
  if (text === '') {
    discardDraft(storage);
    return;
  }
  try {
    storage.setItem(
      CHAT_COMPOSER_DRAFT_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        text,
        updatedAt: Date.now(),
      }),
    );
  } catch {
    /* storage can be unavailable */
  }
}

export function clearChatComposerDraft(): void {
  const storage = getSessionStorage();
  if (!storage) return;
  discardDraft(storage);
}
