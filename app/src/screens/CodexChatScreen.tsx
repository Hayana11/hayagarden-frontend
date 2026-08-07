// Codex's own chat window — visually mirrors ChatScreen's shell (header,
// theme/font toggle, sidebar) for parity with Fyodor's page, but the
// conversation itself is wired to the real, already-working room='codex'
// backend in groupChat.ts (the same one GroupChatScreen's "蓝色" tab uses).
// No edit/branch/regen/model-catalog/file-upload here — that backend surface
// doesn't exist for the solo codex room, so this screen doesn't pretend it does.
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { MixedSectionLabel } from '../components/MixedSectionLabel';
import {
  clearGroupRoom,
  getGroupMessages,
  getGroupStatus,
  sendGroupMessage,
  streamGroupReply,
  type AgentStatus,
  type GroupMessage,
} from '../lib/groupChat';
import { FONT_CN, FONT_DISPLAY, fontFamilyForText, hasCJK } from '../lib/typography';

const SETTINGS_KEY = 'fyodor-chat-settings';
const FONT_SIZES = [13.5, 14.5, 16, 17.5, 19];

const LIGHT_VARS: Record<string, string> = {
  '--bg': '#EEF2F6', '--card': '#FFFFFF', '--card2': '#EAF0F6', '--bubble': '#DCE6F2',
  '--ink': '#33404D', '--ink2': '#4E5D6C', '--mut': '#78899A', '--faint': '#9AABBC', '--ghost': '#B9C6D3',
  '--line': '#E4EBF2', '--blue': '#4E7FB0', '--deep': '#2F5A87', '--bluebg': 'rgba(78,127,176,0.10)',
  '--shadow': 'rgba(78,127,176,0.10)', '--shadow2': 'rgba(78,127,176,0.20)', '--ok': '#7A9B6D', '--err': '#C25450',
};
const DARK_VARS: Record<string, string> = {
  '--bg': '#151C24', '--card': '#1D2731', '--card2': '#26313D', '--bubble': '#2C3B4A',
  '--ink': '#E4EBF2', '--ink2': '#C7D3DE', '--mut': '#9CACBB', '--faint': '#7C8D9E', '--ghost': '#5D6E80',
  '--line': '#2B3947', '--blue': '#7FA6D0', '--deep': '#9DBEE0', '--bluebg': 'rgba(127,166,208,0.16)',
  '--shadow': 'rgba(0,0,0,0.28)', '--shadow2': 'rgba(0,0,0,0.45)', '--ok': '#8FAF80', '--err': '#D97B76',
};

interface Settings {
  theme: 'light' | 'dark' | 'auto';
  fontStep: number;
}

function loadSettings(): Settings {
  try {
    const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
    return {
      theme: ['light', 'dark', 'auto'].includes(s.theme) ? s.theme : 'light',
      fontStep: typeof s.fontStep === 'number' && s.fontStep >= 0 && s.fontStep <= 4 ? s.fontStep : 2,
    };
  } catch {
    return { theme: 'light', fontStep: 2 };
  }
}

const iconBtn: CSSProperties = {
  cursor: 'pointer', width: 35, height: 35, borderRadius: '50%',
  display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--mut)', transition: 'background .2s',
};

function BubbleText({ text }: { text: string }) {
  const pieces = text.trim().split(/\n\s*\n/).filter(Boolean);
  return <>{pieces.map((piece, index) => (
    <p key={`${index}-${piece.slice(0, 12)}`} style={{ margin: 0, fontSize: '1em', lineHeight: 1.85, letterSpacing: 0.3 }}>{piece}</p>
  ))}</>;
}

function timeLabel(value: string) {
  const parsed = new Date(value.replace(' ', 'T') + '+08:00');
  if (Number.isNaN(parsed.getTime())) return '';
  return parsed.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
}

const EMPTY_STATUS: AgentStatus = { ready: false, color: 'blue', detail: '检查中…' };

export function CodexChatScreen() {
  const navigate = useNavigate();
  const [settings, setSettings] = useState<Settings>(loadSettings);
  const [sysDark, setSysDark] = useState(() => window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false);

  const [messages, setMessages] = useState<GroupMessage[]>([]);
  const [status, setStatus] = useState<AgentStatus>(EMPTY_STATUS);
  const [loading, setLoading] = useState(true);
  const [draft, setDraft] = useState('');
  const [streamingText, setStreamingText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [confirmClear, setConfirmClear] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [navOpen, setNavOpen] = useState<null | 'font' | 'search'>(null);
  const [searchQ, setSearchQ] = useState('');

  const bottomRef = useRef<HTMLDivElement>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  const effTheme = settings.theme === 'auto' ? (sysDark ? 'dark' : 'light') : settings.theme;
  const vars = effTheme === 'dark' ? DARK_VARS : LIGHT_VARS;

  const patchSettings = (p: Partial<Settings>) => {
    setSettings((s) => {
      const next = { ...s, ...p };
      try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(next)); } catch { /* quota */ }
      return next;
    });
  };

  const scrollDown = useCallback((behavior: ScrollBehavior = 'smooth') => {
    requestAnimationFrame(() => bottomRef.current?.scrollIntoView({ behavior }));
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await getGroupMessages('codex');
      setMessages(result.messages);
      scrollDown('auto');
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '消息加载失败');
    } finally {
      setLoading(false);
    }
  }, [scrollDown]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    getGroupStatus().then((s) => setStatus(s.agents.codex)).catch(() => {
      setNotice('线路状态暂时取不到，仍可以稍后重试');
    });
    return () => controllerRef.current?.abort();
  }, []);

  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onMq = () => setSysDark(mq.matches);
    mq.addEventListener?.('change', onMq);
    return () => mq.removeEventListener?.('change', onMq);
  }, []);

  useEffect(() => scrollDown(), [messages, streamingText, scrollDown]);

  const runReply = async (messageId: number | null) => {
    if (busy) return;
    setBusy(true);
    setNotice('');
    setStreamingText('');
    const controller = new AbortController();
    controllerRef.current = controller;
    const result = await streamGroupReply('codex', messageId, ['codex'], {
      onStart: () => setStreamingText(''),
      onText: (_agent, text) => setStreamingText((cur) => (cur || '') + text),
      onDone: (_agent, message) => {
        setMessages((cur) => (cur.some((m) => m.id === message.id) ? cur : [...cur, message]));
        setStreamingText(null);
      },
      onStatus: (_agent, ready, detail) => {
        setStatus((cur) => ({ ...cur, ready, detail }));
        if (!ready) setNotice(detail);
      },
      onAgentError: (_agent, detail) => {
        setStreamingText(null);
        setNotice(`蓝色线路：${detail}`);
      },
    }, controller);
    if (!result.ok && result.error) setNotice(result.error);
    setStreamingText(null);
    setBusy(false);
    controllerRef.current = null;
  };

  const send = async () => {
    const content = draft.trim();
    if (!content || busy) return;
    setDraft('');
    if (taRef.current) taRef.current.style.height = 'auto';
    setNotice('');
    try {
      const result = await sendGroupMessage('codex', content);
      setMessages((cur) => [...cur, result.message]);
      await runReply(result.message.id);
    } catch (error) {
      setDraft(content);
      setNotice(error instanceof Error ? error.message : '发送失败');
    }
  };

  const doClear = async () => {
    if (!confirmClear) {
      setConfirmClear(true);
      window.setTimeout(() => setConfirmClear(false), 3500);
      return;
    }
    try {
      await clearGroupRoom('codex');
      setMessages([]);
      setConfirmClear(false);
      setNotice('这个房间已经清空');
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '清空失败');
    }
  };

  const searchResults = useMemo(() => {
    const q = searchQ.trim().toLowerCase();
    if (!q) return [];
    return messages
      .filter((m) => m.content.toLowerCase().includes(q))
      .slice(-30)
      .reverse()
      .map((m) => {
        const i = m.content.toLowerCase().indexOf(q);
        const start = Math.max(0, i - 12);
        return { id: m.id, who: m.author === 'user' ? '哈娅' : 'Codex', snippet: (start > 0 ? '…' : '') + m.content.slice(start, start + 60) };
      });
  }, [searchQ, messages]);

  const canSend = Boolean(draft.trim()) && !busy;
  const placeholder = status.ready ? '跟蓝色线路说点什么…' : '蓝色线路还没就绪，消息会先留在这里…';
  const statusText = status.detail || '蓝色线路';
  const statusFontStyle = hasCJK(statusText) ? 'normal' : 'italic';

  return (
    <div
      className="chat-root app-frame__page"
      style={{
        ...(vars as CSSProperties),
        display: 'flex', flexDirection: 'column',
        background: 'var(--bg)', color: 'var(--ink)', fontFamily: FONT_CN,
        fontSize: FONT_SIZES[settings.fontStep],
      }}
    >
      {/* top nav */}
      <div style={{ flexShrink: 0, position: 'relative', zIndex: 40 }}>
        <div style={{ background: 'var(--card)', boxShadow: '0 6px 18px var(--shadow)', position: 'relative', zIndex: 3 }}>
          <div className="page-header-toolbar">
            <div onClick={() => setSidebarOpen(true)} style={{ cursor: 'pointer', width: 38, height: 38, borderRadius: '50%', background: 'linear-gradient(135deg,#5C8AC0,#2F5A87)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, boxShadow: '0 6px 14px var(--shadow2)' }}>
              <span style={{ fontFamily: FONT_DISPLAY, fontStyle: 'italic', fontSize: 15, color: '#EEF2F6' }}>C</span>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0, flexShrink: 1 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                <span style={{ fontFamily: FONT_DISPLAY, fontSize: 17, fontWeight: 600, letterSpacing: 1, color: 'var(--ink)' }}>Codex</span>
                <span style={{ width: 6, height: 6, borderRadius: '50%', background: status.ready ? 'var(--ok)' : 'var(--ghost)', flexShrink: 0 }} />
              </div>
              <span style={{ fontFamily: fontFamilyForText(statusText), fontStyle: statusFontStyle, fontSize: 10.5, letterSpacing: 1, color: 'var(--faint)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{statusText}</span>
            </div>
            <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 1, flexShrink: 0 }}>
              <div onClick={() => patchSettings({ theme: effTheme === 'dark' ? 'light' : 'dark' })} style={iconBtn}>
                {effTheme === 'light' ? (
                  <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" /></svg>
                ) : (
                  <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round"><circle cx={12} cy={12} r={4} /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" /></svg>
                )}
              </div>
              <div onClick={() => setNavOpen(navOpen === 'font' ? null : 'font')} style={{ ...iconBtn, background: navOpen === 'font' ? 'var(--bluebg)' : 'transparent' }}>
                <span style={{ fontFamily: FONT_DISPLAY, fontSize: 14, letterSpacing: 0.5 }}>Aa</span>
              </div>
              <div onClick={() => setNavOpen(navOpen === 'search' ? null : 'search')} style={{ ...iconBtn, background: navOpen === 'search' ? 'var(--bluebg)' : 'transparent' }}>
                <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round"><circle cx={11} cy={11} r={7} /><path d="M20 20l-3.5-3.5" /></svg>
              </div>
              <div onClick={() => void doClear()} style={{ ...iconBtn, color: confirmClear ? 'var(--err)' : 'var(--mut)', width: 'auto', borderRadius: 999, padding: '0 10px', fontSize: 12.5 }}>
                {confirmClear ? '再点一次' : '清空'}
              </div>
            </div>
          </div>
        </div>

        {navOpen && (
          <>
            <div onClick={() => setNavOpen(null)} style={{ position: 'fixed', inset: 0, zIndex: 1, background: 'rgba(21,28,36,0.30)', animation: 'chatFadeIn .2s ease' }} />
            <div style={{ position: 'absolute', top: '100%', left: 0, right: 0, zIndex: 2, animation: 'chatDropIn .22s ease' }}>
              <div style={{ maxWidth: 430, margin: '0 auto', padding: '0 10px' }}>
                <div style={{ background: 'var(--card)', borderRadius: '0 0 26px 26px', boxShadow: '0 30px 70px var(--shadow2)', padding: '20px 20px 22px', maxHeight: '72vh', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 18 }}>
                  {navOpen === 'font' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                        <MixedSectionLabel cn="字号" en="TEXT SIZE" />
                        <span style={{ fontFamily: FONT_DISPLAY, fontSize: 12, color: 'var(--blue)' }}>{['XS', 'S', 'M', 'L', 'XL'][settings.fontStep]}</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                        <span style={{ fontSize: 12, color: 'var(--ghost)' }}>字</span>
                        <input type="range" min={0} max={4} step={1} value={settings.fontStep} onChange={(e) => patchSettings({ fontStep: Number(e.target.value) })} style={{ flex: 1, accentColor: 'var(--blue)' }} />
                        <span style={{ fontSize: 19, color: 'var(--ghost)' }}>字</span>
                      </div>
                    </div>
                  )}
                  {navOpen === 'search' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                      <MixedSectionLabel cn="聊天记录" en="HISTORY" />
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, background: 'var(--card2)', borderRadius: 999, padding: '11px 16px' }}>
                        <svg viewBox="0 0 24 24" width={15} height={15} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" style={{ color: 'var(--ghost)', flexShrink: 0 }}><circle cx={11} cy={11} r={7} /><path d="M20 20l-3.5-3.5" /></svg>
                        <input value={searchQ} onChange={(e) => setSearchQ(e.target.value)} placeholder="搜索已加载的对话…" style={{ flex: 1, border: 'none', background: 'transparent', fontSize: 14, color: 'var(--ink)', minWidth: 0, fontFamily: FONT_CN, outline: 'none' }} />
                      </div>
                      {searchResults.map((r) => (
                        <div key={r.id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 12px', borderRadius: 14, background: 'var(--card2)' }}>
                          <span style={{ fontSize: 11, padding: '3px 9px', borderRadius: 999, background: 'var(--bluebg)', color: 'var(--deep)', flexShrink: 0 }}>{r.who}</span>
                          <span style={{ flex: 1, fontSize: 13, color: 'var(--ink2)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.snippet}</span>
                        </div>
                      ))}
                      {searchQ.trim() && !searchResults.length && <div style={{ fontSize: 13, color: 'var(--faint)', padding: '2px 12px' }}>没有找到相关消息</div>}
                    </div>
                  )}
                </div>
              </div>
            </div>
          </>
        )}
      </div>

      {notice && (
        <div style={{ maxWidth: 430, margin: '0 auto', width: '100%', padding: '0 12px' }}>
          <button type="button" onClick={() => setNotice('')} style={{ width: '100%', textAlign: 'left', border: 'none', cursor: 'pointer', marginTop: 8, background: 'var(--bluebg)', color: 'var(--deep)', borderRadius: 14, padding: '10px 14px', fontSize: 12.5, display: 'flex', justifyContent: 'space-between', gap: 10 }}>
            <span>{notice}</span><span>×</span>
          </button>
        </div>
      )}

      {/* message stream */}
      <div className="hide-scrollbar" style={{ flex: 1, minHeight: 0, overflowY: 'auto', position: 'relative' }}>
        <div style={{ maxWidth: 430, margin: '0 auto', padding: '20px 16px 26px', display: 'flex', flexDirection: 'column', gap: 16 }}>
          {loading && <div style={{ textAlign: 'center', color: 'var(--faint)', fontSize: 13, padding: '30px 0' }}>正在推开蓝色的门…</div>}
          {!loading && !messages.length && streamingText === null && (
            <div style={{ textAlign: 'center', color: 'var(--faint)', fontSize: 13, padding: '30px 0', display: 'flex', flexDirection: 'column', gap: 8, alignItems: 'center' }}>
              <strong style={{ color: 'var(--ink2)', fontSize: 14, fontWeight: 500 }}>这里只属于你们两个</strong>
              <span>{status.ready ? '说点什么开始吧。' : '蓝色线路还在等接入，消息会先好好留在这里。'}</span>
            </div>
          )}
          {messages.map((m) => (
            <div key={m.id} style={{ display: 'flex', flexDirection: 'column', alignItems: m.author === 'user' ? 'flex-end' : 'flex-start', gap: 6 }}>
              <div style={{ maxWidth: '82%', background: m.author === 'user' ? 'var(--bubble)' : 'var(--card)', borderRadius: m.author === 'user' ? '18px 18px 6px 18px' : '18px 18px 18px 6px', padding: '12px 16px', boxShadow: '0 6px 16px var(--shadow)', display: 'flex', flexDirection: 'column', gap: 6 }}>
                <BubbleText text={m.content} />
              </div>
              <span style={{ fontFamily: FONT_DISPLAY, fontSize: 11, color: 'var(--ghost)', letterSpacing: 1, padding: '0 4px' }}>{timeLabel(m.created_at)}</span>
            </div>
          ))}
          {streamingText !== null && (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 6 }}>
              <div style={{ maxWidth: '82%', background: 'var(--card)', borderRadius: '18px 18px 18px 6px', padding: '12px 16px', boxShadow: '0 6px 16px var(--shadow)' }}>
                {streamingText ? <BubbleText text={streamingText} /> : (
                  <span style={{ display: 'flex', gap: 5 }}>
                    <i style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--ghost)', animation: 'chatBreathe 1.2s ease-in-out infinite' }} />
                    <i style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--ghost)', animation: 'chatBreathe 1.2s ease-in-out .15s infinite' }} />
                    <i style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--ghost)', animation: 'chatBreathe 1.2s ease-in-out .3s infinite' }} />
                  </span>
                )}
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      {/* input area */}
      <div style={{ flexShrink: 0, position: 'relative', zIndex: 30, padding: '8px 12px 14px' }}>
        <div style={{ maxWidth: 430, margin: '0 auto' }}>
          <div style={{ background: 'var(--card)', borderRadius: 26, boxShadow: '0 14px 40px var(--shadow2)', padding: '12px 12px 10px' }}>
            <textarea
              ref={taRef}
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                const ta = e.target;
                ta.style.height = 'auto';
                ta.style.height = `${Math.min(ta.scrollHeight, 120)}px`;
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              rows={1}
              placeholder={placeholder}
              disabled={busy}
              style={{ width: '100%', border: 'none', background: 'transparent', fontSize: '1em', lineHeight: 1.6, color: 'var(--ink)', resize: 'none', maxHeight: 120, padding: '4px 8px 8px', display: 'block', overflowY: 'auto', fontFamily: FONT_CN, outline: 'none' }}
            />
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 2 }}>
              <span style={{ fontSize: 11.5, color: 'var(--ghost)', letterSpacing: 1 }}>蓝色线路 · Codex</span>
              <div
                onClick={canSend ? () => void send() : undefined}
                style={{ marginLeft: 'auto', width: 42, height: 42, flexShrink: 0, borderRadius: '50%', background: canSend ? 'var(--deep)' : 'var(--card2)', color: canSend ? '#EEF2F6' : 'var(--ghost)', display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: canSend ? 'pointer' : 'default', boxShadow: canSend ? '0 8px 20px var(--shadow2)' : 'none', transition: 'background .15s ease' }}
              >
                {busy ? <span style={{ width: 15, height: 15, borderRadius: '50%', border: '2px solid var(--bluebg)', borderTopColor: 'var(--blue)', animation: 'chatSpin .8s linear infinite' }} /> : (
                  <svg viewBox="0 0 24 24" width={17} height={17} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><path d="M12 19V5" /><path d="M5 12l7-7 7 7" /></svg>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* sidebar */}
      {sidebarOpen && (
        <div style={{ position: 'fixed', inset: 0, zIndex: 70 }}>
          <div onClick={() => setSidebarOpen(false)} style={{ position: 'absolute', inset: 0, background: 'rgba(21,28,36,0.42)', animation: 'chatFadeIn .2s ease' }} />
          <div style={{ position: 'absolute', top: 0, bottom: 0, left: 0, width: 'min(320px,86%)', background: 'var(--card)', boxShadow: '20px 0 60px var(--shadow2)', animation: 'chatSlideInL .28s cubic-bezier(.32,.72,.33,1)', display: 'flex', flexDirection: 'column', overflowY: 'auto' }}>
            <div style={{ padding: '28px 22px 20px', display: 'flex', flexDirection: 'column', gap: 14, background: 'linear-gradient(180deg,var(--bluebg),transparent)' }}>
              <div style={{ width: 64, height: 64, borderRadius: '50%', background: 'linear-gradient(135deg,#5C8AC0,#2F5A87)', display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 10px 24px var(--shadow2)' }}>
                <span style={{ fontFamily: FONT_DISPLAY, fontStyle: 'italic', fontSize: 24, color: '#EEF2F6' }}>C</span>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontFamily: FONT_DISPLAY, fontSize: 22, fontWeight: 600, letterSpacing: 1, color: 'var(--ink)' }}>Codex</span>
                  <span style={{ width: 7, height: 7, borderRadius: '50%', background: status.ready ? 'var(--ok)' : 'var(--ghost)' }} />
                </div>
                <span style={{ fontFamily: fontFamilyForText(statusText), fontStyle: statusFontStyle, fontSize: 12, letterSpacing: 1.5, color: 'var(--faint)' }}>{statusText}</span>
              </div>
            </div>
            <div style={{ height: 1, background: 'var(--line)', margin: '0 22px' }} />
            <div style={{ padding: '18px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
              <Link to="/contacts" onClick={() => setSidebarOpen(false)} style={{ textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 12, padding: '13px 14px', borderRadius: 16, background: 'var(--card2)' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>通讯录</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>费奥多尔 · Codex · 群聊</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </Link>
              <a href="/chat" style={{ textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 12, padding: '13px 14px', borderRadius: 16, background: 'linear-gradient(135deg,rgba(220,232,217,.55),rgba(245,222,179,.4))' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>暖色 · Fyodor</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>单独聊天</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </a>
              <Link to="/group-chat" onClick={() => setSidebarOpen(false)} style={{ textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 12, padding: '13px 14px', borderRadius: 16, background: 'var(--card2)' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>群聊房间</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>三个人的房间</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </Link>
              <Link to="/profile" onClick={() => setSidebarOpen(false)} style={{ textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 12, padding: '13px 14px', borderRadius: 16, background: 'linear-gradient(135deg,rgba(92,138,192,.14),rgba(220,232,245,.55))' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>费佳档案</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>身份 · 关系 · 语言风格</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </Link>
              <Link to="/settings" onClick={() => setSidebarOpen(false)} style={{ textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 12, padding: '13px 14px', borderRadius: 16, background: 'var(--card2)' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>系统配置</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>官方端点 · 用量统计</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </Link>
            </div>
            <div style={{ marginTop: 'auto', padding: '16px 22px 22px' }}>
              <button type="button" onClick={() => navigate('/contacts')} style={{ width: '100%', border: 'none', cursor: 'pointer', background: 'transparent', color: 'var(--faint)', fontSize: 12.5, textAlign: 'center', padding: '8px 0' }}>‹ 返回通讯录</button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}
