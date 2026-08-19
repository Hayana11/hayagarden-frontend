import { CHAT_WARM_RETURN_TTL_MS } from './chatWarmReturn';

export const CHAT_FOLLOW_LATEST_THRESHOLD_PX = 32;
export const CHAT_COMPOSER_DRAFT_STORAGE_KEY = 'elpis.chat.composer-draft.v1';

export interface ChatScrollGeometry {
  scrollHeight: number;
  scrollTop: number;
  clientHeight: number;
}

export function isChatFollowLatest(geometry: ChatScrollGeometry, legacyCompat: boolean, logicalWindowAtLatest: boolean): boolean {
  const distanceFromBottom = Math.max(0, geometry.scrollHeight - geometry.scrollTop - geometry.clientHeight);
  return distanceFromBottom <= CHAT_FOLLOW_LATEST_THRESHOLD_PX && (!legacyCompat || logicalWindowAtLatest);
}

interface ChatComposerDraft {
  version: 1;
  text: string;
  updatedAt: number;
}

function readChatComposerStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try { return window.sessionStorage; } catch { return null; }
}

export function readChatComposerDraft(): string {
  const storage = readChatComposerStorage();
  if (!storage) return '';
  try {
    const raw = storage.getItem(CHAT_COMPOSER_DRAFT_STORAGE_KEY);
    if (raw === null) return '';
    const parsed = JSON.parse(raw) as Partial<ChatComposerDraft>;
    if (
      parsed.version !== 1
      || typeof parsed.text !== 'string'
      || typeof parsed.updatedAt !== 'number'
      || !Number.isFinite(parsed.updatedAt)
      || Date.now() - parsed.updatedAt >= CHAT_WARM_RETURN_TTL_MS
    ) {
      storage.removeItem(CHAT_COMPOSER_DRAFT_STORAGE_KEY);
      return '';
    }
    return parsed.text;
  } catch {
    try { storage.removeItem(CHAT_COMPOSER_DRAFT_STORAGE_KEY); } catch { /* optional storage */ }
    return '';
  }
}

export function writeChatComposerDraft(text: string): void {
  const storage = readChatComposerStorage();
  if (!storage) return;
  try {
    if (text === '') { storage.removeItem(CHAT_COMPOSER_DRAFT_STORAGE_KEY); return; }
    storage.setItem(CHAT_COMPOSER_DRAFT_STORAGE_KEY, JSON.stringify({ version: 1, text, updatedAt: Date.now() }));
  } catch { /* optional storage; typing must remain functional */ }
}

export function clearChatComposerDraft(): void {
  const storage = readChatComposerStorage();
  if (!storage) return;
  try { storage.removeItem(CHAT_COMPOSER_DRAFT_STORAGE_KEY); } catch { /* optional storage */ }
}
