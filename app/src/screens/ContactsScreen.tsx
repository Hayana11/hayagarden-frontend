// 通讯录 — the front door for all chat destinations: Fyodor's own window,
// Codex's own window, and the group room, plus a 游戏室 module that's an
// honest locked placeholder (no backend for 谁是卧底/飞行棋 exists yet).
import { useEffect, useState, type CSSProperties } from 'react';
import { Link } from 'react-router-dom';
import { BottomNav } from '../components/BottomNav';
import { getGroupStatus, type AgentStatus } from '../lib/groupChat';

const SETTINGS_KEY = 'fyodor-chat-settings';
const SERIF = "'Noto Serif SC', serif";
const DISPLAY = "'Bodoni Moda', serif";
const LOCKED = '这个游戏还没有做，以后一起玩~';

const LIGHT_VARS: Record<string, string> = {
  '--bg': '#F7F1EE', '--card': '#FFFFFF', '--card2': '#F6EFEC',
  '--ink': '#4A3F3C', '--ink2': '#6B5A55', '--mut': '#8C7B76', '--faint': '#A99590', '--ghost': '#C4B4AF',
  '--line': '#F0E6E2', '--rose': '#B76E79', '--deep': '#9C3B4A',
  '--shadow': 'rgba(183,110,121,0.10)', '--shadow2': 'rgba(183,110,121,0.20)', '--ok': '#7A9B6D',
};
const DARK_VARS: Record<string, string> = {
  '--bg': '#211A18', '--card': '#2B2220', '--card2': '#362B28',
  '--ink': '#EFE5E1', '--ink2': '#D9C9C3', '--mut': '#B4A19B', '--faint': '#93817C', '--ghost': '#6E5F5A',
  '--line': '#3B302D', '--rose': '#C98A93', '--deep': '#D89AA2',
  '--shadow': 'rgba(0,0,0,0.28)', '--shadow2': 'rgba(0,0,0,0.45)', '--ok': '#8FAF80',
};

interface Settings { theme: 'light' | 'dark' | 'auto' }

function loadSettings(): Settings {
  try {
    const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
    return { theme: ['light', 'dark', 'auto'].includes(s.theme) ? s.theme : 'light' };
  } catch {
    return { theme: 'light' };
  }
}

const EMPTY_CODEX: AgentStatus = { ready: false, color: 'blue', detail: '检查中…' };

function ContactRow({ to, avatar, avatarBg, name, subtitle, dot }: {
  to: string; avatar: React.ReactNode; avatarBg: string; name: string; subtitle: string; dot?: 'ok' | 'off';
}) {
  return (
    <Link
      to={to}
      className="hstack hstack-14"
      style={{ textDecoration: 'none', padding: '14px 16px', borderRadius: 18, background: 'var(--card)', boxShadow: '0 6px 16px var(--shadow)' }}
    >
      <div style={{ width: 46, height: 46, borderRadius: '50%', background: avatarBg, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, boxShadow: '0 6px 14px var(--shadow2)' }}>
        {avatar}
      </div>
      <div className="vstack vstack-3" style={{ minWidth: 0, flex: 1 }}>
        <div className="hstack hstack-7">
          <span style={{ fontFamily: DISPLAY, fontSize: 15.5, fontWeight: 600, letterSpacing: 1, color: 'var(--ink)' }}>{name}</span>
          {dot && <span style={{ width: 6, height: 6, borderRadius: '50%', background: dot === 'ok' ? 'var(--ok)' : 'var(--ghost)', flexShrink: 0 }} />}
        </div>
        <span style={{ fontSize: 12, color: 'var(--faint)', letterSpacing: 0.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{subtitle}</span>
      </div>
      <span style={{ color: 'var(--ghost)', fontSize: 17, flexShrink: 0 }}>›</span>
    </Link>
  );
}

function GameCard({ title, hint, onLocked }: { title: string; hint: string; onLocked: () => void }) {
  return (
    <button
      type="button"
      onClick={onLocked}
      className="vstack vstack-8"
      style={{ cursor: 'pointer', border: 'none', textAlign: 'left', padding: '16px 16px 14px', borderRadius: 18, background: 'var(--card2)', opacity: 0.82 }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span style={{ fontFamily: DISPLAY, fontSize: 14.5, fontWeight: 600, letterSpacing: 1, color: 'var(--ink2)' }}>{title}</span>
        <span style={{ fontSize: 10.5, color: 'var(--ghost)', border: '1px solid var(--line)', borderRadius: 999, padding: '2px 9px' }}>敬请期待</span>
      </div>
      <span style={{ fontSize: 12, color: 'var(--faint)', lineHeight: 1.6 }}>{hint}</span>
    </button>
  );
}

export function ContactsScreen() {
  const [settings, setSettings] = useState<Settings>(loadSettings);
  const [sysDark, setSysDark] = useState(() => window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false);
  const [codexStatus, setCodexStatus] = useState<AgentStatus>(EMPTY_CODEX);
  const [toast, setToast] = useState('');

  const effTheme = settings.theme === 'auto' ? (sysDark ? 'dark' : 'light') : settings.theme;
  const vars = effTheme === 'dark' ? DARK_VARS : LIGHT_VARS;

  const patchSettings = (p: Partial<Settings>) => {
    setSettings((s) => {
      const next = { ...s, ...p };
      try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(next)); } catch { /* quota */ }
      return next;
    });
  };

  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onMq = () => setSysDark(mq.matches);
    mq.addEventListener?.('change', onMq);
    return () => mq.removeEventListener?.('change', onMq);
  }, []);

  useEffect(() => {
    getGroupStatus().then((s) => setCodexStatus(s.agents.codex)).catch(() => undefined);
  }, []);

  const showLocked = () => {
    setToast(LOCKED);
    window.setTimeout(() => setToast(''), 2200);
  };

  return (
    <div
      className="dash-fullscreen-page"
      style={{
        ...(vars as CSSProperties),
        display: 'flex', flexDirection: 'column',
        background: 'var(--bg)', color: 'var(--ink)', fontFamily: SERIF,
      }}
    >
      <div style={{ flexShrink: 0, background: 'rgba(255,255,255,0.97)', boxShadow: '0 6px 18px var(--shadow)' }}>
        <div className="hstack hstack-10" style={{ maxWidth: 430, margin: '0 auto', padding: '14px 16px 12px' }}>
          <span style={{ fontFamily: DISPLAY, fontSize: 18, fontWeight: 600, letterSpacing: 2, color: 'var(--ink)' }}>通讯录</span>
          <button type="button" onClick={() => patchSettings({ theme: effTheme === 'dark' ? 'light' : 'dark' })} aria-label="切换主题" style={{ marginLeft: 'auto', cursor: 'pointer', border: 'none', background: 'transparent', width: 34, height: 34, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--mut)' }}>
            {effTheme === 'light' ? (
              <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" /></svg>
            ) : (
              <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round"><circle cx={12} cy={12} r={4} /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" /></svg>
            )}
          </button>
        </div>
      </div>

      <div className="hide-scrollbar" style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
        <div className="vstack vstack-26" style={{ maxWidth: 430, margin: '0 auto', padding: '20px 16px 24px' }}>
          <div className="vstack vstack-12">
            <div style={{ fontFamily: DISPLAY, fontSize: 11, letterSpacing: 3, color: 'var(--ghost)', padding: '0 2px' }}>聊天 · CHAT</div>
            <div className="vstack vstack-10">
              <ContactRow
                to="/chat"
                avatar={<span style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 19, color: '#F7F1EE' }}>Θ</span>}
                avatarBg="linear-gradient(135deg,#B76E79,#9C3B4A)"
                name="Fyodor"
                subtitle="暖色 · 单独聊天"
                dot="ok"
              />
              <ContactRow
                to="/codex-chat"
                avatar={<span style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 17, color: '#EEF2F6' }}>C</span>}
                avatarBg="linear-gradient(135deg,#5C8AC0,#2F5A87)"
                name="Codex"
                subtitle={codexStatus.detail || (codexStatus.ready ? '可以回复' : '待接入')}
                dot={codexStatus.ready ? 'ok' : 'off'}
              />
              <ContactRow
                to="/group-chat"
                avatar={
                  <div style={{ display: 'flex', alignItems: 'center' }}>
                    <i style={{ width: 15, height: 15, borderRadius: '50%', background: '#F0DFDB', border: '2px solid #F7F1EE' }} />
                    <i style={{ width: 15, height: 15, borderRadius: '50%', background: '#DCE6F2', border: '2px solid #F7F1EE', marginLeft: -6 }} />
                  </div>
                }
                avatarBg="linear-gradient(135deg,#91AD93,#8EACCF)"
                name="一起"
                subtitle="三个人的房间"
              />
            </div>
          </div>

          <div className="vstack vstack-12">
            <div style={{ fontFamily: DISPLAY, fontSize: 11, letterSpacing: 3, color: 'var(--ghost)', padding: '0 2px' }}>游戏室 · GAMES</div>
            <div className="vstack vstack-10">
              <Link
                to="/monopoly/new"
                className="vstack vstack-9 contacts-monopoly-card"
                style={{
                  position: 'relative', overflow: 'hidden', textDecoration: 'none',
                  padding: '17px 16px 15px', borderRadius: 18,
                  boxShadow: '0 8px 20px var(--shadow)',
                }}
              >
                <div className="hstack hstack-12" style={{ justifyContent: 'space-between' }}>
                  <span style={{ fontFamily: DISPLAY, fontSize: 15.5, fontWeight: 600, letterSpacing: 1.2, color: 'var(--ink)' }}>葡萄海大富翁</span>
                  <span className="contacts-monopoly-badge" style={{ fontSize: 10.5, color: 'var(--deep)', borderRadius: 999, padding: '3px 9px' }}>可以玩了</span>
                </div>
                <span style={{ fontSize: 12, color: 'var(--mut)', lineHeight: 1.65 }}>双人棋局 · 三人聊天。骰子、任务和两位房间伙伴都在等你。</span>
                <span style={{ alignSelf: 'flex-end', color: 'var(--rose)', fontSize: 12, letterSpacing: 1 }}>进入游戏室 →</span>
              </Link>
              <GameCard title="谁是卧底" hint="以后一起玩，等我们都得空的时候。" onLocked={showLocked} />
              <GameCard title="飞行棋" hint="骰子和棋盘都还没做，先记在这里。" onLocked={showLocked} />
            </div>
          </div>
        </div>
      </div>

      <BottomNav embedded />

      {toast && (
        <div style={{ position: 'fixed', left: 0, right: 0, bottom: 28, zIndex: 80, display: 'flex', justifyContent: 'center', pointerEvents: 'none' }}>
          <div style={{ background: 'rgba(58,42,40,0.92)', color: '#F7EDEA', fontSize: 13, letterSpacing: 1, padding: '10px 20px', borderRadius: 999, boxShadow: '0 10px 30px rgba(0,0,0,0.25)', animation: 'chatFadeIn .2s ease' }}>{toast}</div>
        </div>
      )}
    </div>
  );
}
