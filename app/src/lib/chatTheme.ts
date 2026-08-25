/** Fyodor Chat theme — DOM + localStorage only; must not live in ChatScreen React state. */

import { beginThemePerfProbe, flushThemePerfProbe } from './themePerfProbe';

export const CHAT_SETTINGS_KEY = 'fyodor-chat-settings';

export type ThemeMode = 'light' | 'dark' | 'blue' | 'auto';
export type EffectiveTheme = 'light' | 'dark' | 'blue';

export interface FyodorChatSettings {
  theme: ThemeMode;
  fontStep: number;
  thinkMode: 'auto' | 'drawer' | 'inline';
}

export interface ChatThemeState {
  mode: ThemeMode;
  effective: EffectiveTheme;
}

const DEFAULTS: FyodorChatSettings = {
  theme: 'light',
  fontStep: 2,
  thinkMode: 'auto',
};

type ChatThemeListener = (state: ChatThemeState) => void;
const listeners = new Set<ChatThemeListener>();

export function loadChatSettings(): FyodorChatSettings {
  try {
    const s = JSON.parse(localStorage.getItem(CHAT_SETTINGS_KEY) || '{}');
    return {
      theme: (['light', 'dark', 'blue', 'auto'] as const).includes(s.theme) ? s.theme : DEFAULTS.theme,
      fontStep:
        typeof s.fontStep === 'number' && s.fontStep >= 0 && s.fontStep <= 4 ? s.fontStep : DEFAULTS.fontStep,
      thinkMode: (['auto', 'drawer', 'inline'] as const).includes(s.thinkMode) ? s.thinkMode : DEFAULTS.thinkMode,
    };
  } catch {
    return { ...DEFAULTS };
  }
}

/** Merge patch onto latest persisted settings (never drop theme when patching font/think). */
export function patchChatSettings(patch: Partial<FyodorChatSettings>): FyodorChatSettings {
  const next = { ...loadChatSettings(), ...patch };
  try {
    localStorage.setItem(CHAT_SETTINGS_KEY, JSON.stringify(next));
  } catch {
    /* quota */
  }
  return next;
}

export function resolveEffectiveTheme(theme: ThemeMode = loadChatSettings().theme): EffectiveTheme {
  if (theme === 'auto') {
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  return theme;
}

function getChatThemeState(): ChatThemeState {
  const mode = loadChatSettings().theme;
  return { mode, effective: resolveEffectiveTheme(mode) };
}

function emitChatThemeChange() {
  const state = getChatThemeState();
  for (const listener of listeners) {
    listener(state);
  }
}

/** Subscribe to theme mode/effective changes (setChatTheme, system auto, central matchMedia). */
export function subscribeChatTheme(listener: ChatThemeListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function applyChatThemeToRoot(root: HTMLElement, theme?: ThemeMode) {
  const mode = theme ?? loadChatSettings().theme;
  const effective = resolveEffectiveTheme(mode);
  root.setAttribute('data-chat-theme', effective);
  root.dataset.chatThemeMode = mode;
  document.documentElement.setAttribute('data-theme', effective);
}

type Detach = () => void;
const detachByRoot = new WeakMap<HTMLElement, Detach>();
const attachedRoots = new Set<HTMLElement>();

let systemThemeMq: MediaQueryList | null = null;

function onSystemThemeChange() {
  if (loadChatSettings().theme !== 'auto') return;
  for (const root of attachedRoots) {
    applyChatThemeToRoot(root);
  }
  emitChatThemeChange();
}

function ensureSystemThemeListener() {
  if (systemThemeMq) return;
  systemThemeMq = window.matchMedia('(prefers-color-scheme: dark)');
  systemThemeMq.addEventListener?.('change', onSystemThemeChange);
}

export function attachChatTheme(root: HTMLElement): Detach {
  detachByRoot.get(root)?.();

  applyChatThemeToRoot(root);
  attachedRoots.add(root);
  ensureSystemThemeListener();

  const detach = () => {
    attachedRoots.delete(root);
    detachByRoot.delete(root);
  };
  detachByRoot.set(root, detach);
  return detach;
}

export function setChatTheme(root: HTMLElement | null, theme: ThemeMode): EffectiveTheme {
  beginThemePerfProbe();
  patchChatSettings({ theme });
  if (root) applyChatThemeToRoot(root, theme);
  emitChatThemeChange();
  if (root) flushThemePerfProbe(root);
  return resolveEffectiveTheme(theme);
}

export function toggleChatThemeQuick(root: HTMLElement | null): EffectiveTheme {
  const effective = resolveEffectiveTheme();
  const next: EffectiveTheme = effective === 'light' ? 'dark' : effective === 'dark' ? 'blue' : 'light';
  return setChatTheme(root, next);
}
