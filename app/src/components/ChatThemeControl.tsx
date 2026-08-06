import { useCallback, useEffect, useState, type CSSProperties, type RefObject } from 'react';
import {
  loadChatSettings,
  resolveEffectiveTheme,
  setChatTheme,
  subscribeChatTheme,
  toggleChatThemeQuick,
  type EffectiveTheme,
  type ThemeMode,
} from '../lib/chatTheme';

const IC_MOON =
  'M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z';

function SvgMoon({ size = 16 }: { size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round">
      <path d={IC_MOON} />
    </svg>
  );
}

function SvgSun({ size = 16 }: { size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round">
      <circle cx={12} cy={12} r={4} />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
    </svg>
  );
}

/** Header quick toggle — state is local; DOM theme via data-chat-theme on chat root. */
export function ChatThemeQuickToggle({
  rootRef,
  style,
}: {
  rootRef: RefObject<HTMLElement | null>;
  style?: CSSProperties;
}) {
  const [effective, setEffective] = useState<EffectiveTheme>(
    () => resolveEffectiveTheme(loadChatSettings().theme),
  );

  useEffect(() => subscribeChatTheme(({ effective: e }) => setEffective(e)), []);

  const onClick = useCallback(() => {
    toggleChatThemeQuick(rootRef.current);
  }, [rootRef]);

  return (
    <div onClick={onClick} style={style} role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') onClick(); }}>
      {effective === 'light' ? <SvgMoon /> : <SvgSun />}
    </div>
  );
}

/** Wrench panel light / dark / auto — isolated from ChatScreen parent state. */
export function ChatThemeSegmented({
  rootRef,
  segStyle,
}: {
  rootRef: RefObject<HTMLElement | null>;
  segStyle: (on: boolean) => CSSProperties;
}) {
  const [mode, setMode] = useState<ThemeMode>(() => loadChatSettings().theme);

  useEffect(() => subscribeChatTheme(({ mode: m }) => setMode(m)), []);

  const pick = useCallback((t: ThemeMode) => {
    setChatTheme(rootRef.current, t);
  }, [rootRef]);

  return (
    <div className="hstack hstack-2" style={{ background: 'var(--card2)', borderRadius: 999, padding: 3 }}>
      {(['light', 'dark', 'auto'] as const).map((t) => (
        <div key={t} onClick={() => pick(t)} style={segStyle(mode === t)}>
          {t === 'light' ? '浅色' : t === 'dark' ? '深色' : '跟随系统'}
        </div>
      ))}
    </div>
  );
}
