/** Fyodor Chat theme — DOM + localStorage only; must not live in ChatScreen React state. */

export const CHAT_SETTINGS_KEY = 'fyodor-chat-settings';

export type ThemeMode = 'light' | 'dark' | 'auto';
export type EffectiveTheme = 'light' | 'dark';

export interface FyodorChatSettings {
  theme: ThemeMode;
  fontStep: number;
  thinkMode: 'auto' | 'drawer' | 'inline';
}

const DEFAULTS: FyodorChatSettings = {
  theme: 'light',
  fontStep: 2,
  thinkMode: 'auto',
};

export function loadChatSettings(): FyodorChatSettings {
  try {
    const s = JSON.parse(localStorage.getItem(CHAT_SETTINGS_KEY) || '{}');
    return {
      theme: (['light', 'dark', 'auto'] as const).includes(s.theme) ? s.theme : DEFAULTS.theme,
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

export function applyChatThemeToRoot(root: HTMLElement, theme?: ThemeMode) {
  const mode = theme ?? loadChatSettings().theme;
  root.setAttribute('data-chat-theme', resolveEffectiveTheme(mode));
  root.dataset.chatThemeMode = mode;
}

type Detach = () => void;
const detachByRoot = new WeakMap<HTMLElement, Detach>();

export function attachChatTheme(root: HTMLElement): Detach {
  applyChatThemeToRoot(root);
  detachByRoot.get(root)?.();

  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const onMq = () => {
    if (loadChatSettings().theme === 'auto') applyChatThemeToRoot(root);
  };
  mq.addEventListener?.('change', onMq);

  const detach = () => {
    mq.removeEventListener?.('change', onMq);
    detachByRoot.delete(root);
  };
  detachByRoot.set(root, detach);
  return detach;
}

export function setChatTheme(root: HTMLElement | null, theme: ThemeMode): EffectiveTheme {
  patchChatSettings({ theme });
  if (root) applyChatThemeToRoot(root, theme);
  return resolveEffectiveTheme(theme);
}

export function toggleChatThemeQuick(root: HTMLElement | null): EffectiveTheme {
  const effective = resolveEffectiveTheme();
  const next: EffectiveTheme = effective === 'dark' ? 'light' : 'dark';
  return setChatTheme(root, next);
}
