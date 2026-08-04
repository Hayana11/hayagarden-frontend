// Nexus — the coding-agent console. Ported from the Claude Design prototype
// `Nexus.dc.html` (project 「Android聊天软件仪表板设计」).
//
// The prototype is a self-contained mock: two agents (Claude Code / Codex),
// each with its own seeded transcript, and a command bar whose buttons drive
// local state only. This port keeps that contract — no backend calls — so the
// page can be wired to a real runner later without reshaping the UI. Every
// colour, size and copy string below comes straight from the design source.
import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react';
import { useNavigate } from 'react-router-dom';

const SERIF = "'Noto Serif SC', serif";
const MONO = "'JetBrains Mono', monospace";

/** Scoped copy of the prototype's :root block. Prefixed so it can't leak into
 *  or collide with the other fullscreen screens' own --ink/--rose vars. */
const VARS: Record<string, string> = {
  '--nx-pagebg': '#FCEBEF',
  '--nx-cardbg': '#2A2120',
  '--nx-cardtext': '#F3E9E6',
  '--nx-cardmut': '#B7A29C',
  '--nx-cardghost': '#7C6A65',
  '--nx-ink': '#4A3F3C',
  '--nx-ghost': '#C9B8B4',
  '--nx-navmut': '#AC969F',
  '--nx-rose': '#E8A5B5',
  '--nx-rosepale': '#F6DCE3',
  '--nx-rosedeep': '#C1657E',
  '--nx-mist': '#9FB6C7',
  '--nx-mistpale': '#E3EDF2',
  '--nx-mistdeep': '#5E7F98',
  '--nx-ok': '#8FBB86',
  '--nx-shadow': 'rgba(196,120,140,0.22)',
};

type ModelKey = 'claude' | 'codex';

type LogEntry =
  | { id: number; type: 'sys'; kind?: 'think' | 'chat'; text: string }
  | { id: number; type: 'patch'; name: string; diffText: string; expanded: boolean }
  | { id: number; type: 'text'; text: string }
  | { id: number; type: 'cmd'; text: string }
  | { id: number; type: 'image'; text: string }
  | { id: number; type: 'status'; text: string };

interface ModelMeta {
  accent: string;
  add: number;
  del: number;
  statusLabel: string;
  ctxPct: number;
  ctxLabel: string;
}

const META: Record<ModelKey, ModelMeta> = {
  claude: { accent: 'var(--nx-rose)', add: 82, del: 19, statusLabel: '运行中', ctxPct: 62, ctxLabel: '62% 上下文' },
  codex: { accent: 'var(--nx-mist)', add: 46, del: 8, statusLabel: '已完成', ctxPct: 38, ctxLabel: '38% 上下文' },
};

let uid = 0;
const nextId = () => ++uid;

function seedLogs(): Record<ModelKey, LogEntry[]> {
  return {
    claude: [
      { id: nextId(), type: 'sys', kind: 'think', text: 'Thought for 44s, searched for 1 pattern, read 3 files' },
      {
        id: nextId(),
        type: 'patch',
        name: 'edit_layout.diff',
        expanded: false,
        diffText: '-  display: block;\n+  display: flex;\n+  align-items: center;',
      },
      { id: nextId(), type: 'text', text: '找到问题所在——排版组件里 flex 顺序写反了\n改完之后卡片对齐会顺很多。' },
      { id: nextId(), type: 'image', text: '[Image #1] 截图.png' },
      { id: nextId(), type: 'status', text: 'Cooked for 1m 51s' },
    ],
    codex: [
      { id: nextId(), type: 'sys', kind: 'think', text: 'Reasoning for 12s, ran 2 tests, opened 1 diff' },
      {
        id: nextId(),
        type: 'patch',
        name: 'rename_fields.patch',
        expanded: false,
        diffText: '-  const user_id = req.id;\n+  const userId = req.id;',
      },
      { id: nextId(), type: 'text', text: '测试全部通过。顺手把命名统一成 camelCase 了。' },
      { id: nextId(), type: 'image', text: '[Image #1] diff.png' },
      { id: nextId(), type: 'status', text: 'Finished in 38s' },
    ],
  };
}

/* ── icons (traced from the prototype's inline SVGs) ── */

function ThinkIcon() {
  return (
    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="var(--nx-rose)" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 5a3 3 0 0 0-5.9.6A3.5 3.5 0 0 0 4 9a3.5 3.5 0 0 0 .6 5.4A3.2 3.2 0 0 0 8 19c.6 0 1.2-.2 1.7-.5.6.9 1.4 1.5 2.3 1.5" />
      <path d="M12 5a3 3 0 0 1 5.9.6A3.5 3.5 0 0 1 20 9a3.5 3.5 0 0 1-.6 5.4A3.2 3.2 0 0 1 16 19c-.6 0-1.2-.2-1.7-.5-.6.9-1.4 1.5-2.3 1.5" />
      <path d="M12 5v15" />
    </svg>
  );
}

function BubbleIcon({ size = 13 }: { size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="var(--nx-rose)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 4h16v12H8l-4 4V4Z" />
    </svg>
  );
}

function NavBubbleIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="var(--nx-navmut)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 4h16v12H8l-4 4V4Z" />
    </svg>
  );
}

function HomeIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="var(--nx-navmut)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 11.5 12 4l8 7.5" />
      <path d="M6 10v9a1 1 0 0 0 1 1h4v-6h2v6h4a1 1 0 0 0 1-1v-9" />
    </svg>
  );
}

function CatIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="var(--nx-navmut)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 4 5 8" />
      <path d="M16 4l3 4" />
      <circle cx="12" cy="13" r="7" />
      <path d="M9.5 13h.01" />
      <path d="M14.5 13h.01" />
    </svg>
  );
}

function BookIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="var(--nx-navmut)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 5c3-1 6-1 8 1c2-2 5-2 8-1v14c-3-1-6-1-8 1c-2-2-5-2-8-1V5Z" />
    </svg>
  );
}

/* ── log rows ── */

function LogRow({ log, onToggle }: { log: LogEntry; onToggle: (id: number) => void }) {
  if (log.type === 'sys') {
    return (
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
        <span style={{ flexShrink: 0, marginTop: 1, display: 'flex' }}>
          {log.kind === 'think' ? <ThinkIcon /> : null}
          {log.kind === 'chat' ? <BubbleIcon /> : null}
          {!log.kind ? <span style={{ color: 'var(--nx-ok)', fontSize: 12 }}>✓</span> : null}
        </span>
        {log.kind !== 'chat' ? (
          <span style={{ fontFamily: MONO, fontSize: 11.5, lineHeight: 1.7, color: '#CBBBB7', whiteSpace: 'normal' }}>{log.text}</span>
        ) : null}
      </div>
    );
  }

  if (log.type === 'patch') {
    return (
      <div>
        <div onClick={() => onToggle(log.id)} style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 7 }}>
          <span
            style={{
              flexShrink: 0,
              display: 'flex',
              transform: log.expanded ? 'rotate(180deg)' : 'rotate(0deg)',
              transition: 'transform .15s',
            }}
          >
            <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="var(--nx-mist)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M6 9l6 6 6-6" />
            </svg>
          </span>
          <span style={{ fontFamily: MONO, fontSize: 11, color: 'var(--nx-cardghost)' }}>patch · {log.name}</span>
        </div>
        {log.expanded ? (
          <div
            style={{
              margin: '6px 0 0 19px',
              padding: '8px 10px',
              background: 'rgba(255,255,255,0.04)',
              borderRadius: 8,
              fontFamily: MONO,
              fontSize: 10.5,
              lineHeight: 1.6,
              color: 'var(--nx-cardmut)',
              whiteSpace: 'pre-wrap',
            }}
          >
            {log.diffText}
          </div>
        ) : null}
      </div>
    );
  }

  if (log.type === 'text') {
    return (
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
        <span style={{ flexShrink: 0, marginTop: 2, display: 'flex' }}>
          <BubbleIcon />
        </span>
        <div style={{ flex: 1, minWidth: 0, fontSize: 13, lineHeight: 1.45, color: '#B7A29C', whiteSpace: 'pre-wrap', fontFamily: "'Trebuchet MS', sans-serif" }}>
          {log.text}
        </div>
      </div>
    );
  }

  if (log.type === 'image') {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, paddingLeft: 2 }}>
        <span style={{ color: 'var(--nx-cardghost)', fontFamily: MONO, fontSize: 11, flexShrink: 0 }}>└</span>
        <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="var(--nx-cardghost)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="3" />
          <circle cx="9" cy="9" r="2" />
          <path d="M21 15l-5-5-9 9" />
        </svg>
        <span style={{ fontSize: 11, color: 'var(--nx-cardghost)' }}>{log.text}</span>
      </div>
    );
  }

  if (log.type === 'status') {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--nx-cardghost)' }}>
        <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7v5l3 2" />
        </svg>
        {log.text}
      </div>
    );
  }

  return null;
}

/* ── page ── */

const keyBtn: CSSProperties = {
  flex: 1,
  textAlign: 'center',
  cursor: 'pointer',
  padding: '10px 0',
  borderRadius: 14,
  color: 'var(--nx-ink)',
  border: '1px solid rgba(0,0,0,0.08)',
  fontFamily: MONO,
  fontWeight: 500,
  backgroundColor: '#FFF9FA',
};

const navItem: CSSProperties = {
  flex: 1,
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  gap: 3,
  padding: '5px 0',
  cursor: 'pointer',
};

const navIconWrap: CSSProperties = {
  width: 38,
  height: 28,
  borderRadius: 14,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
};

const navLabel: CSSProperties = { fontSize: 10, color: 'var(--nx-navmut)' };

export function NexusScreen({
  defaultModel = 'claude',
  showStatsBar = true,
}: {
  defaultModel?: ModelKey;
  showStatsBar?: boolean;
}) {
  const navigate = useNavigate();
  const logRef = useRef<HTMLDivElement>(null);
  const inpRef = useRef<HTMLInputElement>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const submitTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [model, setModel] = useState<ModelKey>(defaultModel === 'codex' ? 'codex' : 'claude');
  const [logs, setLogs] = useState<Record<ModelKey, LogEntry[]>>(seedLogs);
  const [input, setInput] = useState('');
  const [history, setHistory] = useState<string[]>([]);
  const [historyIdx, setHistoryIdx] = useState(-1);
  const [toast, setToast] = useState<string | null>(null);

  useEffect(
    () => () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
      if (submitTimer.current) clearTimeout(submitTimer.current);
    },
    [],
  );

  const showToast = useCallback((txt: string) => {
    setToast(txt);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 1800);
  }, []);

  const scrollLog = useCallback(() => {
    const c = logRef.current;
    if (c) c.scrollTop = c.scrollHeight;
  }, []);

  const pushLog = useCallback((key: ModelKey, entry: LogEntry) => {
    setLogs((prev) => ({ ...prev, [key]: prev[key].concat([entry]) }));
  }, []);

  const submit = useCallback(() => {
    const txt = input.trim();
    if (!txt) return;
    const key = model;
    pushLog(key, { id: nextId(), type: 'cmd', text: txt });
    setInput('');
    setHistory((prev) => prev.concat([txt]));
    setHistoryIdx(-1);
    if (submitTimer.current) clearTimeout(submitTimer.current);
    submitTimer.current = setTimeout(() => {
      pushLog(key, { id: nextId(), type: 'status', text: '已收到指令，处理完毕' });
      setTimeout(scrollLog, 30);
    }, 500);
    setTimeout(scrollLog, 30);
  }, [input, model, pushLog, scrollLog]);

  const recallUp = useCallback(() => {
    if (!history.length) return;
    const i = historyIdx < 0 ? history.length - 1 : Math.max(0, historyIdx - 1);
    setHistoryIdx(i);
    setInput(history[i]);
  }, [history, historyIdx]);

  const recallDown = useCallback(() => {
    if (historyIdx < 0) return;
    const i = historyIdx + 1;
    if (i >= history.length) {
      setHistoryIdx(-1);
      setInput('');
    } else {
      setHistoryIdx(i);
      setInput(history[i]);
    }
  }, [history, historyIdx]);

  /** Drops the most recent command and everything logged after it. */
  const rewind = useCallback(() => {
    setLogs((prev) => {
      const list = prev[model];
      for (let i = list.length - 1; i >= 0; i--) {
        if (list[i].type === 'cmd') return { ...prev, [model]: list.slice(0, i) };
      }
      return prev;
    });
    showToast('已回退上一步');
  }, [model, showToast]);

  const togglePatch = useCallback(
    (id: number) => {
      setLogs((prev) => ({
        ...prev,
        [model]: prev[model].map((lg) => (lg.id === id && lg.type === 'patch' ? { ...lg, expanded: !lg.expanded } : lg)),
      }));
    },
    [model],
  );

  const meta = META[model];
  const isClaude = model === 'claude';

  return (
    <div
      className="nexus-root dash-fullscreen-page"
      style={{
        ...(VARS as CSSProperties),
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--nx-pagebg)',
        fontFamily: SERIF,
        color: 'var(--nx-ink)',
      }}
    >
      {/* ══ 深棕控制台卡片 ══ */}
      <div style={{ flex: 1, minHeight: 0, display: 'flex', justifyContent: 'center', padding: '16px 14px 10px' }}>
        <div style={{ width: '100%', maxWidth: 430, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          <div
            style={{
              background: 'var(--nx-cardbg)',
              borderRadius: 24,
              boxShadow: '0 24px 60px var(--nx-shadow)',
              overflow: 'hidden',
              display: 'flex',
              flexDirection: 'column',
              letterSpacing: '-0.2px',
              flex: 1,
              minHeight: 0,
            }}
          >
            {/* title bar */}
            <div style={{ display: 'flex', alignItems: 'center', padding: '14px 14px 10px', gap: 10, width: '100%' }}>
              <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                {['#EF6157', '#F4BF4F', '#61C454'].map((c) => (
                  <span key={c} style={{ width: 10, height: 10, borderRadius: '50%', background: c, display: 'block' }} />
                ))}
              </div>
              <div style={{ flex: 1, display: 'flex', justifyContent: 'center', gap: 4 }}>
                <div
                  onClick={() => setModel('claude')}
                  style={{
                    cursor: 'pointer',
                    padding: '6px 13px',
                    borderRadius: 999,
                    fontSize: 11.5,
                    fontWeight: 600,
                    letterSpacing: '0.2px',
                    whiteSpace: 'nowrap',
                    transition: 'all .2s',
                    background: isClaude ? 'var(--nx-rosepale)' : 'transparent',
                    color: isClaude ? 'var(--nx-rosedeep)' : 'var(--nx-cardmut)',
                  }}
                >
                  Claude Code
                </div>
                <div
                  onClick={() => setModel('codex')}
                  style={{
                    cursor: 'pointer',
                    padding: '6px 13px',
                    borderRadius: 999,
                    fontSize: 11.5,
                    fontWeight: 600,
                    letterSpacing: '0.2px',
                    whiteSpace: 'nowrap',
                    transition: 'all .2s',
                    background: !isClaude ? 'var(--nx-mistpale)' : 'transparent',
                    color: !isClaude ? 'var(--nx-mistdeep)' : 'var(--nx-cardmut)',
                  }}
                >
                  Codex
                </div>
              </div>
              <div
                onClick={() => showToast('已刷新')}
                style={{
                  cursor: 'pointer',
                  width: 26,
                  height: 26,
                  borderRadius: '50%',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  color: 'var(--nx-cardmut)',
                  flexShrink: 0,
                }}
              >
                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M3 12a9 9 0 1 0 3-6.7" />
                  <path d="M3 4v5h5" />
                </svg>
              </div>
            </div>

            {/* stats strip */}
            {showStatsBar ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '0 16px 13px', borderBottom: '1px solid rgba(255,255,255,0.07)' }}>
                <span style={{ display: 'flex', alignItems: 'center', gap: 5, fontFamily: MONO, fontSize: 12 }}>
                  <span style={{ color: '#8FBB86' }}>+{meta.add}</span>
                  <span style={{ color: 'var(--nx-cardghost)' }}>/</span>
                  <span style={{ color: '#D97B76' }}>-{meta.del}</span>
                </span>
                <span style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11.5, color: 'var(--nx-cardmut)' }}>
                  <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#61C454', flexShrink: 0 }} />
                  {meta.statusLabel}
                </span>
                <span
                  style={{
                    marginLeft: 'auto',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 7,
                    fontSize: 11.5,
                    color: 'var(--nx-cardmut)',
                    whiteSpace: 'nowrap',
                    flexShrink: 0,
                  }}
                >
                  <span
                    style={{
                      width: 44,
                      height: 4,
                      borderRadius: 99,
                      background: 'rgba(255,255,255,0.12)',
                      overflow: 'hidden',
                      display: 'inline-block',
                      flexShrink: 0,
                    }}
                  >
                    <span style={{ display: 'block', height: '100%', width: `${meta.ctxPct}%`, background: meta.accent }} />
                  </span>
                  <span style={{ whiteSpace: 'nowrap' }}>{meta.ctxLabel}</span>
                </span>
              </div>
            ) : null}

            {/* transcript */}
            <div
              ref={logRef}
              className="hide-scrollbar"
              style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '15px 16px', display: 'flex', flexDirection: 'column', gap: 13 }}
            >
              {logs[model].map((lg) => (
                <div key={lg.id} style={{ animation: 'nexusFadeIn .25s ease' }}>
                  {lg.type === 'cmd' ? (
                    <div style={{ fontFamily: MONO, fontSize: 12.5, color: meta.accent, display: 'flex', gap: 6 }}>
                      <span>&gt;</span>
                      <span>{lg.text}</span>
                    </div>
                  ) : (
                    <LogRow log={lg} onToggle={togglePatch} />
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* ══ 命令对话框 ══ */}
      <div style={{ flexShrink: 0, display: 'flex', justifyContent: 'center', padding: '0 14px 10px' }}>
        <div style={{ width: '100%', maxWidth: 430, display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div
            style={{
              background: '#FFF9FA',
              borderRadius: 999,
              boxShadow: '0 10px 26px rgba(232,165,181,0.20)',
              padding: '12px 18px',
              display: 'flex',
              alignItems: 'center',
              gap: 10,
            }}
          >
            <span style={{ flexShrink: 0, display: 'flex' }}>
              <svg viewBox="0 0 24 24" width="16" height="16" fill="var(--nx-rosedeep)">
                <path d="M12 2l1.6 5.4L19 9l-5.4 1.6L12 16l-1.6-5.4L5 9l5.4-1.6L12 2Z" />
              </svg>
            </span>
            <input
              ref={inpRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  submit();
                } else if (e.key === 'Escape') {
                  e.preventDefault();
                  setInput('');
                } else if (e.key === 'ArrowUp') {
                  e.preventDefault();
                  recallUp();
                } else if (e.key === 'ArrowDown') {
                  e.preventDefault();
                  recallDown();
                }
              }}
              placeholder="命令..."
              style={{
                flex: 1,
                border: 'none',
                background: 'transparent',
                fontSize: 13,
                color: '#28211F',
                minWidth: 0,
                fontFamily: 'Verdana, sans-serif',
                outline: 'none',
              }}
            />
            <span style={{ fontFamily: MONO, fontSize: 15, color: '#CD657E', flexShrink: 0 }}>&gt;_</span>
          </div>

          <div style={{ display: 'flex', gap: 6 }}>
            <div
              onClick={() => {
                setInput('');
                inpRef.current?.focus();
              }}
              style={{ ...keyBtn, fontSize: 12 }}
            >
              Esc
            </div>
            <div onClick={rewind} style={{ ...keyBtn, fontSize: 11 }}>
              Rewind
            </div>
            <div onClick={recallUp} style={{ ...keyBtn, fontSize: 13 }}>
              ↑
            </div>
            <div onClick={recallDown} style={{ ...keyBtn, fontSize: 13 }}>
              ↓
            </div>
            <div
              onClick={submit}
              style={{
                flex: 1,
                textAlign: 'center',
                cursor: 'pointer',
                padding: '10px 0',
                borderRadius: 14,
                background: 'var(--nx-rosepale)',
                color: 'var(--nx-rosedeep)',
                border: '1px solid var(--nx-rose)',
                fontFamily: MONO,
                fontSize: 12,
                fontWeight: 700,
              }}
            >
              Enter
            </div>
            <div
              onClick={() => {
                setLogs((prev) => ({ ...prev, [model]: [] }));
                setInput('');
                showToast('已清空');
              }}
              style={{ ...keyBtn, fontSize: 12 }}
            >
              Clear
            </div>
          </div>
        </div>
      </div>

      {/* ══ 底部导航 ══ */}
      <div
        style={{
          flexShrink: 0,
          background: 'rgba(255,255,255,0.86)',
          backdropFilter: 'blur(10px)',
          borderTop: '1px solid rgba(0,0,0,0.045)',
        }}
      >
        <div style={{ maxWidth: 430, margin: '0 auto', display: 'flex', padding: '6px 4px 10px' }}>
          <div onClick={() => showToast('Sanctum 尚未搭建')} style={navItem}>
            <div style={navIconWrap}>
              <HomeIcon />
            </div>
            <span style={navLabel}>Sanctum</span>
          </div>
          <div onClick={() => navigate('/chat')} style={navItem}>
            <div style={navIconWrap}>
              <NavBubbleIcon />
            </div>
            <span style={navLabel}>Chat</span>
          </div>
          <div onClick={() => showToast('Catroom 尚未搭建')} style={navItem}>
            <div style={navIconWrap}>
              <CatIcon />
            </div>
            <span style={navLabel}>Catroom</span>
          </div>
          <div style={{ ...navItem, cursor: 'default' }}>
            <div style={{ ...navIconWrap, background: 'var(--nx-rosepale)' }}>
              <span style={{ fontFamily: MONO, fontSize: 14, fontWeight: 700, color: 'var(--nx-rosedeep)' }}>&gt;_</span>
            </div>
            <span style={{ fontSize: 10, color: 'var(--nx-rosedeep)', fontWeight: 700 }}>Nexus</span>
          </div>
          <div onClick={() => showToast('Arcana 尚未搭建')} style={navItem}>
            <div style={navIconWrap}>
              <BookIcon />
            </div>
            <span style={navLabel}>Arcana</span>
          </div>
        </div>
      </div>

      {toast ? (
        <div style={{ position: 'fixed', left: 0, right: 0, bottom: 96, zIndex: 80, display: 'flex', justifyContent: 'center', pointerEvents: 'none' }}>
          <div
            style={{
              background: 'rgba(42,33,32,0.92)',
              color: 'var(--nx-cardtext)',
              fontSize: 12.5,
              letterSpacing: '0.5px',
              padding: '9px 18px',
              borderRadius: 999,
              boxShadow: '0 10px 30px rgba(0,0,0,0.2)',
              animation: 'nexusFadeIn .2s ease',
            }}
          >
            {toast}
          </div>
        </div>
      ) : null}
    </div>
  );
}
