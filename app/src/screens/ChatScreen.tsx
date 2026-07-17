// Fyodor Chat — implements Fyodor Chat.dc.html against the production chat
// backend: /api/chat/messages history, send -> /api/gw/chat/stream SSE
// (think/text/tool_use/tool_result/usage/done/err), inline branches
// (branch/switch, regen prepare/finalize), edit-with-truncate, model catalog.
// Mounted at /dash/chat, parallel to the legacy /chat page.
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { Link } from 'react-router-dom';
import { BottomNav } from '../components/BottomNav';
import {
  editChatMessage,
  fetchChatMessages,
  fetchModelCatalog,
  regenFinalize,
  regenPrepare,
  sendChatMessage,
  setChatModel,
  switchChatBranch,
  uploadChatFile,
  type ModelCatalogEntry,
} from '../lib/api';
import {
  cacheLabel,
  chatPlaceholder,
  fmtTokens,
  fetchChatGatewayOnline,
  forceUnlockChatGenLock,
  guessChatErrorHint,
  streamChatReply,
  type ChatMsg,
  type ChatToolCall,
} from '../lib/chat';
import type { ReactElement } from 'react';

const SETTINGS_KEY = 'fyodor-chat-settings';
const FONT_SIZES = [13.5, 14.5, 16, 17.5, 19];
const INPUT_FONT_SIZE = FONT_SIZES[0];
const SERIF = "'Noto Serif SC', serif";
const DISPLAY = "'Bodoni Moda', serif";
const MONO = 'ui-monospace, Menlo, monospace';

const LIGHT_VARS: Record<string, string> = {
  '--bg': '#F7F1EE', '--card': '#FFFFFF', '--card2': '#F6EFEC', '--bubble': '#F0DFDB',
  '--ink': '#4A3F3C', '--ink2': '#6B5A55', '--mut': '#8C7B76', '--faint': '#A99590', '--ghost': '#C4B4AF',
  '--line': '#F0E6E2', '--rose': '#B76E79', '--deep': '#9C3B4A', '--rosebg': 'rgba(183,110,121,0.10)',
  '--shadow': 'rgba(183,110,121,0.10)', '--shadow2': 'rgba(183,110,121,0.20)',
  '--ok': '#7A9B6D', '--err': '#C25450', '--gold': '#D9A441',
};
const DARK_VARS: Record<string, string> = {
  '--bg': '#211A18', '--card': '#2B2220', '--card2': '#362B28', '--bubble': '#3E2E30',
  '--ink': '#EFE5E1', '--ink2': '#D9C9C3', '--mut': '#B4A19B', '--faint': '#93817C', '--ghost': '#6E5F5A',
  '--line': '#3B302D', '--rose': '#C98A93', '--deep': '#D89AA2', '--rosebg': 'rgba(201,138,147,0.16)',
  '--shadow': 'rgba(0,0,0,0.28)', '--shadow2': 'rgba(0,0,0,0.45)',
  '--ok': '#8FAF80', '--err': '#D97B76', '--gold': '#DFB25E',
};

interface Settings {
  theme: 'light' | 'dark' | 'auto';
  fontStep: number;
  thinkMode: 'auto' | 'drawer' | 'inline';
}

function loadSettings(): Settings {
  try {
    const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
    return {
      theme: ['light', 'dark', 'auto'].includes(s.theme) ? s.theme : 'light',
      fontStep: typeof s.fontStep === 'number' && s.fontStep >= 0 && s.fontStep <= 4 ? s.fontStep : 2,
      thinkMode: ['auto', 'drawer', 'inline'].includes(s.thinkMode) ? s.thinkMode : 'auto',
    };
  } catch {
    return { theme: 'light', fontStep: 2, thinkMode: 'auto' };
  }
}

interface LiveState {
  thinking: string;
  text: string;
  tools: ChatToolCall[];
  phase: 'wait' | 'think' | 'tool' | 'text';
}

interface DrawerState {
  text: string;
  label: string;
}

const iconBtn: CSSProperties = {
  cursor: 'pointer', width: 35, height: 35, borderRadius: '50%',
  display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--mut)', transition: 'background .2s',
};

function Svg({ d, size = 16, sw = 1.6 }: { d: string; size?: number; sw?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round">
      {d.split('|').map((p, i) => (
        <path key={i} d={p} />
      ))}
    </svg>
  );
}

const IC = {
  wrench: 'M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z',
  moon: 'M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z',
  clock: 'M12 7v5l3 2',
  edit: 'M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z',
  redo: 'M3 12a9 9 0 1 0 3-6.7|M3 4v5h5',
  refresh: 'M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8|M21 3v5h-5',
  up: 'M12 19V5|M5 12l7-7 7 7',
  down: 'M12 5v14|M19 12l-7 7-7-7',
  plus: 'M12 5v14|M5 12h14',
  chev: 'M6 9l6 6 6-6',
  tool: 'M4 17l6-5-6-5|M12 19h8',
  clip: 'M21 12.5l-8.2 8.2a5 5 0 0 1-7-7l8.7-8.7a3.3 3.3 0 0 1 4.7 4.7l-8.7 8.7a1.66 1.66 0 0 1-2.3-2.3l8-8',
  brain: 'M12 5a3 3 0 0 0-5.9.6A3.5 3.5 0 0 0 4 9a3.5 3.5 0 0 0 .6 5.4A3.2 3.2 0 0 0 8 19c.6 0 1.2-.2 1.7-.5.6.9 1.4 1.5 2.3 1.5|M12 5a3 3 0 0 1 5.9.6A3.5 3.5 0 0 1 20 9a3.5 3.5 0 0 1-.6 5.4A3.2 3.2 0 0 1 16 19c-.6 0-1.2-.2-1.7-.5-.6.9-1.4 1.5-2.3 1.5|M12 5v15',
  thumb: 'M7 10v12H4a1 1 0 0 1-1-1V11a1 1 0 0 1 1-1h3zm0 0l4.5-7a2.4 2.4 0 0 1 2.4 2.4V9h5a2 2 0 0 1 2 2.3l-1.2 8A2 2 0 0 1 17.7 21H7',
};

function CopyIcon({ size = 15 }: { size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15V5a2 2 0 0 1 2-2h10" />
    </svg>
  );
}

export function ChatScreen() {
  const [settings, setSettings] = useState<Settings>(loadSettings);
  const [sysDark, setSysDark] = useState(() => window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false);
  const [wide, setWide] = useState(() => window.innerWidth >= 900);

  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [hasMoreBefore, setHasMoreBefore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [live, setLive] = useState<LiveState | null>(null);
  const [pendingFile, setPendingFile] = useState<{ fileUrl: string; fileName: string } | null>(null);
  const [pendingImage, setPendingImage] = useState<File | null>(null);

  const [navOpen, setNavOpen] = useState<null | 'wrench' | 'font' | 'search' | 'profile'>(null);
  const [searchQ, setSearchQ] = useState('');
  const [modelPopOpen, setModelPopOpen] = useState(false);
  const [attachMenuOpen, setAttachMenuOpen] = useState(false);
  const [models, setModels] = useState<ModelCatalogEntry[]>([]);
  const [currentModel, setCurrentModel] = useState('');

  const [openThink, setOpenThink] = useState<Record<number, boolean>>({});
  const [openTools, setOpenTools] = useState<Record<string, boolean>>({});
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editText, setEditText] = useState('');
  const [drawer, setDrawer] = useState<DrawerState | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [flashId, setFlashId] = useState<number | null>(null);
  const [liked, setLiked] = useState<Record<number, 1 | -1>>({});
  const [endpointOnline, setEndpointOnline] = useState<boolean | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [chatError, setChatError] = useState<{ message: string; hint: string } | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const imgInputRef = useRef<HTMLInputElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const liveRef = useRef<LiveState | null>(null);

  const effTheme = settings.theme === 'auto' ? (sysDark ? 'dark' : 'light') : settings.theme;
  const vars = effTheme === 'dark' ? DARK_VARS : LIGHT_VARS;
  const effThinkMode = settings.thinkMode === 'auto' ? (wide ? 'inline' : 'drawer') : settings.thinkMode;

  const placeholder = useMemo(() => chatPlaceholder(new Date()), []);
  const dateLabel = useMemo(() => {
    const now = new Date();
    const dows = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
    return `今天 · ${now.getMonth() + 1}月${now.getDate()}日 ${dows[now.getDay()]}`;
  }, []);

  const patchSettings = (p: Partial<Settings>) => {
    setSettings((s) => {
      const next = { ...s, ...p };
      try {
        localStorage.setItem(SETTINGS_KEY, JSON.stringify(next));
      } catch {
        // ignore quota errors
      }
      return next;
    });
  };

  const showToast = useCallback((t: string) => {
    setToast(t);
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 2200);
  }, []);

  const scrollBottom = useCallback((smooth = false) => {
    requestAnimationFrame(() => {
      const c = scrollRef.current;
      if (c) c.scrollTo({ top: c.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
    });
  }, []);

  const refetchLatest = useCallback(async (toBottom = true) => {
    const page = await fetchChatMessages({ limit: 80 });
    setMsgs(page.messages);
    setHasMoreBefore(page.hasMoreBefore);
    if (toBottom) scrollBottom();
  }, [scrollBottom]);

  const refreshChat = useCallback(async () => {
    if (refreshing) return;
    setRefreshing(true);
    setNavOpen(null);
    setChatError(null);
    abortRef.current?.abort();
    abortRef.current = null;
    liveRef.current = null;
    setLive(null);
    setSending(false);
    try {
      const unlock = await forceUnlockChatGenLock();
      await refetchLatest();
      const online = await fetchChatGatewayOnline();
      setEndpointOnline(online);
      if (unlock.ok && !unlock.busy) showToast('已刷新');
      else if (unlock.busy) showToast('锁仍占用，消息已刷新');
      else showToast('已刷新（解锁请求失败）');
    } finally {
      setRefreshing(false);
    }
  }, [refreshing, refetchLatest, showToast]);

  // initial load + catalog
  useEffect(() => {
    refetchLatest();
    fetchModelCatalog().then((r) => {
      setModels(r.models);
      setCurrentModel(r.current);
    });
  }, [refetchLatest]);

  // media listeners
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onMq = () => setSysDark(mq.matches);
    mq.addEventListener?.('change', onMq);
    const onRs = () => setWide(window.innerWidth >= 900);
    window.addEventListener('resize', onRs);
    return () => {
      mq.removeEventListener?.('change', onMq);
      window.removeEventListener('resize', onRs);
    };
  }, []);

  // gateway reachability — breathing status under the name
  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      const online = await fetchChatGatewayOnline();
      if (!cancelled) setEndpointOnline(online);
    };
    void check();
    const iv = setInterval(() => { void check(); }, 20000);
    return () => { cancelled = true; clearInterval(iv); };
  }, []);

  // poll for new messages (e.g. wake messages from the api-side) when idle
  useEffect(() => {
    const iv = setInterval(() => {
      if (document.hidden || liveRef.current || sending) return;
      setMsgs((cur) => {
        const newest = cur.length ? cur[cur.length - 1].id : 0;
        fetchChatMessages({ after: newest, limit: 50 }).then((page) => {
          if (page.messages.length) {
            setMsgs((c2) => {
              const known = new Set(c2.map((m) => m.id));
              const fresh = page.messages.filter((m) => !known.has(m.id));
              return fresh.length ? [...c2, ...fresh] : c2;
            });
            scrollBottom(true);
          }
        });
        return cur;
      });
    }, 10000);
    return () => clearInterval(iv);
  }, [sending, scrollBottom]);

  useEffect(() => () => {
    abortRef.current?.abort();
    clearTimeout(toastTimer.current);
  }, []);

  const updateLive = useCallback((fn: (l: LiveState) => LiveState) => {
    liveRef.current = fn(liveRef.current ?? { thinking: '', text: '', tools: [], phase: 'wait' });
    setLive(liveRef.current);
  }, []);

  const runStream = useCallback(
    async (userMessageId: number | null): Promise<boolean> => {
      liveRef.current = { thinking: '', text: '', tools: [], phase: 'wait' };
      setLive(liveRef.current);
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      const res = await streamChatReply(
        userMessageId,
        {
          onThink: (d) => updateLive((l) => ({ ...l, phase: 'think', thinking: l.thinking + d })),
          onText: (d) => {
            updateLive((l) => ({ ...l, phase: 'text', text: l.text + d }));
            scrollBottom();
          },
          onToolUse: (idx, tc) =>
            updateLive((l) => {
              const tools = [...l.tools];
              tools[idx] = tc;
              return { ...l, phase: 'tool', tools };
            }),
          onToolResult: (idx, tc) =>
            updateLive((l) => {
              const tools = [...l.tools];
              tools[idx] = { ...tools[idx], ...tc, running: false };
              return { ...l, tools };
            }),
          onNotice: (s) => showToast(s),
        },
        ctrl,
      );
      liveRef.current = null;
      setLive(null);
      if (!res.ok && res.error) {
        if (!ctrl.signal.aborted) {
          setChatError({ message: res.error, hint: guessChatErrorHint(res.error) });
          scrollBottom(true);
        }
      }
      return res.ok;
    },
    [scrollBottom, showToast, updateLive],
  );

  const send = useCallback(async () => {
    const text = input.trim();
    if ((!text && !pendingFile && !pendingImage) || sending) return;
    setSending(true);
    setChatError(null);
    setInput('');
    if (taRef.current) taRef.current.style.height = 'auto';
    const extra = pendingImage ? { imageFile: pendingImage } : pendingFile ? { fileUrl: pendingFile.fileUrl, fileName: pendingFile.fileName } : {};
    setPendingFile(null);
    setPendingImage(null);
    const messageId = await sendChatMessage(text, extra);
    if (messageId === null) {
      showToast('发送失败');
      setInput(text);
      setSending(false);
      return;
    }
    await refetchLatest();
    await runStream(messageId);
    await refetchLatest();
    setSending(false);
    taRef.current?.focus();
  }, [input, pendingFile, pendingImage, sending, refetchLatest, runStream, showToast]);

  const redo = useCallback(
    async (msgId: number) => {
      if (sending) return;
      setSending(true);
      setChatError(null);
      const old = await regenPrepare(msgId);
      if (old === null) {
        showToast('重答准备失败');
        setSending(false);
        return;
      }
      setMsgs((cur) => cur.filter((m) => m.id !== msgId));
      const ok = await runStream(null);
      if (ok) await regenFinalize(old);
      await refetchLatest();
      setSending(false);
    },
    [sending, refetchLatest, runStream, showToast],
  );

  const saveEdit = useCallback(
    async (msgId: number) => {
      const content = editText.trim();
      if (!content || sending) return;
      setSending(true);
      setEditingId(null);
      const ok = await editChatMessage(msgId, content);
      if (!ok) {
        showToast('修改失败');
        setSending(false);
        return;
      }
      await refetchLatest();
      await runStream(null);
      await refetchLatest();
      setSending(false);
    },
    [editText, sending, refetchLatest, runStream, showToast],
  );

  const branchSwitch = useCallback(
    async (msgId: number, dir: 1 | -1) => {
      const r = await switchChatBranch(msgId, dir);
      if (r) await refetchLatest(false);
    },
    [refetchLatest],
  );

  const copyText = useCallback(
    (t: string) => {
      navigator.clipboard?.writeText(t).catch(() => undefined);
      showToast('已复制');
    },
    [showToast],
  );

  const loadEarlier = useCallback(async () => {
    if (loadingMore || !msgs.length) return;
    setLoadingMore(true);
    const page = await fetchChatMessages({ before: msgs[0].id, limit: 80 });
    setMsgs((cur) => [...page.messages, ...cur]);
    setHasMoreBefore(page.hasMoreBefore);
    setLoadingMore(false);
  }, [loadingMore, msgs]);

  const jumpTo = useCallback((id: number) => {
    setNavOpen(null);
    setFlashId(id);
    setTimeout(() => {
      const el = document.getElementById(`msg-${id}`);
      const c = scrollRef.current;
      if (el && c) c.scrollTo({ top: Math.max(0, el.offsetTop - 80), behavior: 'smooth' });
    }, 250);
    setTimeout(() => setFlashId((f) => (f === id ? null : f)), 2200);
  }, []);

  const searchResults = useMemo(() => {
    const q = searchQ.trim().toLowerCase();
    if (!q) return [];
    return msgs
      .filter((m) => m.text.toLowerCase().includes(q))
      .slice(-30)
      .reverse()
      .map((m) => {
        const i = m.text.toLowerCase().indexOf(q);
        const start = Math.max(0, i - 12);
        return { id: m.id, who: m.role === 'user' ? '哈娅' : 'Fyodor', snippet: (start > 0 ? '…' : '') + m.text.slice(start, start + 60), ts: m.ts };
      });
  }, [searchQ, msgs]);

  const lastAssistantId = useMemo(() => {
    for (let i = msgs.length - 1; i >= 0; i--) if (msgs[i].role === 'assistant') return msgs[i].id;
    return -1;
  }, [msgs]);

  const onAttachFile = useCallback(
    async (f: File | undefined) => {
      setAttachMenuOpen(false);
      if (!f) return;
      const up = await uploadChatFile(f);
      if (up) {
        setPendingFile(up);
        setPendingImage(null);
      } else showToast('上传失败（只收 2MB 内文本类文件）');
    },
    [showToast],
  );

  const segStyle = (on: boolean): CSSProperties => ({
    flex: 1, textAlign: 'center', padding: '8px 0', borderRadius: 999, fontSize: 13, cursor: 'pointer', transition: 'all .2s',
    background: on ? 'var(--card)' : 'transparent', color: on ? 'var(--deep)' : 'var(--mut)',
    boxShadow: on ? '0 4px 10px var(--shadow)' : 'none',
  });

  const sectionCaption: CSSProperties = { fontFamily: DISPLAY, fontSize: 11, letterSpacing: 3, color: 'var(--ghost)' };
  const modelBadge = useMemo(() => {
    const hit = models.find((m) => m.id === currentModel);
    return hit?.label || currentModel.replace(/^.*\]\s*/, '').slice(0, 22) || '模型';
  }, [models, currentModel]);

  const canSend = Boolean(input.trim() || pendingFile || pendingImage) && !sending;

  // ── message block renderers ──

  function renderThinkBlock(m: ChatMsg) {
    if (!m.thinking) return null;
    const label = m.thinkingSummary || `思考了 ${m.thinking.length} 字`;
    const open = Boolean(openThink[m.id]);
    const onClick = () => {
      if (effThinkMode === 'drawer') setDrawer({ text: m.thinking, label });
      else setOpenThink((o) => ({ ...o, [m.id]: !o[m.id] }));
    };
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div onClick={onClick} style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8, color: 'var(--faint)' }}>
          <span style={{ display: 'flex' }}>
            <Svg d={IC.brain} size={17} sw={1.5} />
          </span>
          <span style={{ fontSize: 13, letterSpacing: 1 }}>{label}</span>
          {effThinkMode === 'inline' && (
            <svg viewBox="0 0 24 24" width={12} height={12} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" style={{ transition: 'transform .2s', transform: `rotate(${open ? 180 : 0}deg)` }}>
              <path d="M6 9l6 6 6-6" />
            </svg>
          )}
        </div>
        {open && effThinkMode === 'inline' && (
          <div style={{ borderRadius: 14, background: 'var(--card2)', padding: '14px 16px', fontSize: '0.88em', lineHeight: 1.95, color: 'var(--mut)', whiteSpace: 'pre-wrap', animation: 'chatFadeIn .2s ease' }}>
            {m.thinking}
          </div>
        )}
      </div>
    );
  }

  function renderToolCard(key: string, tc: ChatToolCall) {
    const open = Boolean(openTools[key]);
    const outStr = typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result ?? '', null, 2);
    const inStr = typeof tc.args === 'string' ? tc.args : JSON.stringify(tc.args ?? {}, null, 2);
    return (
      <div key={key} style={{ background: 'var(--card)', borderRadius: 14, boxShadow: '0 6px 16px var(--shadow)', overflow: 'hidden' }}>
        <div onClick={() => setOpenTools((o) => ({ ...o, [key]: !o[key] }))} style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10, padding: '11px 14px' }}>
          <span style={{ color: 'var(--faint)', flexShrink: 0, display: 'flex' }}>
            <Svg d={IC.tool} size={14} sw={1.8} />
          </span>
          <span style={{ fontFamily: MONO, fontSize: 12.5, color: 'var(--ink2)' }}>{tc.name || 'tool'}</span>
          {tc.running && <span style={{ width: 13, height: 13, borderRadius: '50%', border: '2px solid var(--rosebg)', borderTopColor: 'var(--rose)', animation: 'chatSpin .8s linear infinite', flexShrink: 0 }} />}
          {!tc.running && tc.success !== false && <span style={{ color: 'var(--ok)', fontSize: 13 }}>✓</span>}
          {!tc.running && tc.success === false && <span style={{ color: 'var(--err)', fontSize: 13 }}>✗</span>}
          {tc.caption && <span style={{ fontSize: 11.5, color: 'var(--ghost)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{tc.caption}</span>}
          <svg viewBox="0 0 24 24" width={12} height={12} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" style={{ marginLeft: 'auto', color: 'var(--ghost)', transition: 'transform .2s', transform: `rotate(${open ? 180 : 0}deg)`, flexShrink: 0 }}>
            <path d="M6 9l6 6 6-6" />
          </svg>
        </div>
        {open && (
          <div style={{ padding: '0 14px 14px', display: 'flex', flexDirection: 'column', gap: 9, animation: 'chatFadeIn .2s ease' }}>
            <div style={{ height: 1, background: 'var(--line)' }} />
            <div style={{ fontSize: 11, letterSpacing: 2, color: 'var(--ghost)' }}>入参</div>
            <div style={{ background: 'var(--card2)', borderRadius: 10, padding: '10px 12px', fontFamily: MONO, fontSize: 11.5, lineHeight: 1.7, color: 'var(--mut)', whiteSpace: 'pre-wrap', overflowX: 'auto', maxHeight: 200, overflowY: 'auto' }}>{inStr}</div>
            <div style={{ fontSize: 11, letterSpacing: 2, color: 'var(--ghost)' }}>出参</div>
            <div style={{ background: 'var(--card2)', borderRadius: 10, padding: '10px 12px', fontFamily: MONO, fontSize: 11.5, lineHeight: 1.7, color: 'var(--mut)', whiteSpace: 'pre-wrap', overflowX: 'auto', maxHeight: 200, overflowY: 'auto' }}>{String(outStr).slice(0, 4000)}</div>
          </div>
        )}
      </div>
    );
  }

  function renderParas(text: string, caret = false) {
    const paras = text.split('\n').filter((p, i, arr) => p.trim() || (i < arr.length - 1 && arr[i + 1]?.trim()));
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10, maxWidth: 640, padding: '0 2px' }}>
        {paras.map((p, i) => {
          const bullet = /^[-·•]\s+/.test(p.trim());
          const last = i === paras.length - 1;
          if (bullet) {
            return (
              <div key={i} style={{ display: 'flex', gap: 10, paddingLeft: 6 }}>
                <span style={{ color: 'var(--rose)', flexShrink: 0, lineHeight: 1.85, fontSize: '1em' }}>·</span>
                <span style={{ fontSize: '1em', lineHeight: 1.85, letterSpacing: 0.3, color: 'var(--ink)' }}>{p.trim().replace(/^[-·•]\s+/, '')}</span>
              </div>
            );
          }
          return (
            <div key={i} style={{ fontSize: '1em', lineHeight: 1.9, letterSpacing: 0.3, color: 'var(--ink)', textWrap: 'pretty' as CSSProperties['textWrap'] }}>
              {p}
              {caret && last && <span style={{ display: 'inline-block', width: 2, height: '1em', background: 'var(--rose)', verticalAlign: -2, marginLeft: 2, animation: 'chatBlink 1s step-end infinite' }} />}
            </div>
          );
        })}
      </div>
    );
  }

  function renderUserMsg(m: ChatMsg) {
    const editing = editingId === m.id;
    return (
      <div id={`msg-${m.id}`} className={`chat-msg${flashId === m.id ? ' chat-flash' : ''}`} style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 7, borderRadius: 16 }}>
        {editing ? (
          <div style={{ width: '100%', maxWidth: 520, background: 'var(--card)', borderRadius: 18, boxShadow: '0 10px 30px var(--shadow)', padding: 14, display: 'flex', flexDirection: 'column', gap: 10 }}>
            <textarea
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              rows={3}
              style={{ width: '100%', border: 'none', background: 'var(--card2)', borderRadius: 12, padding: 12, fontSize: '1em', lineHeight: 1.7, color: 'var(--ink)', resize: 'none', fontFamily: SERIF }}
            />
            <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 8 }}>
              <span style={{ marginRight: 'auto', fontSize: 11, color: 'var(--ghost)' }}>修改会归档后面的对话，重新生成回复</span>
              <div onClick={() => setEditingId(null)} style={{ cursor: 'pointer', padding: '8px 16px', borderRadius: 999, background: 'var(--card2)', color: 'var(--mut)', fontSize: 13 }}>
                取消
              </div>
              <div onClick={() => saveEdit(m.id)} style={{ cursor: 'pointer', padding: '8px 16px', borderRadius: 999, background: 'var(--deep)', color: '#FBF3F0', fontSize: 13, letterSpacing: 1 }}>
                发送新版本
              </div>
            </div>
          </div>
        ) : (
          <>
            <div style={{ maxWidth: '82%', background: 'var(--bubble)', borderRadius: '18px 18px 6px 18px', padding: '12px 16px', boxShadow: '0 6px 16px var(--shadow)', display: 'flex', flexDirection: 'column', gap: 8 }}>
              {(m.fileName || m.imageUrl) && (
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {m.fileName && (
                    <span style={{ display: 'flex', alignItems: 'center', gap: 6, background: 'var(--card)', borderRadius: 999, padding: '5px 11px', fontSize: 11.5, color: 'var(--ink2)' }}>
                      <Svg d={IC.clip} size={11} sw={1.8} />
                      {m.fileName}
                    </span>
                  )}
                  {m.imageUrl && <img src={m.imageUrl} alt="" style={{ maxWidth: 200, maxHeight: 200, borderRadius: 12, objectFit: 'cover' }} />}
                </div>
              )}
              {m.text && <span style={{ fontSize: '1em', lineHeight: 1.75, letterSpacing: 0.3, color: 'var(--ink)', whiteSpace: 'pre-wrap' }}>{m.text}</span>}
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              <div className="chat-msg-acts" style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <div
                  onClick={() => {
                    setEditingId(m.id);
                    setEditText(m.text);
                  }}
                  title="修改"
                  style={{ cursor: 'pointer', width: 26, height: 26, borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--faint)' }}
                >
                  <Svg d={IC.edit} size={14} sw={1.7} />
                </div>
                <div onClick={() => copyText(m.text)} title="复制" style={{ cursor: 'pointer', width: 26, height: 26, borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--faint)' }}>
                  <CopyIcon size={14} />
                </div>
              </div>
              <span style={{ fontFamily: DISPLAY, fontSize: 11, color: 'var(--ghost)', letterSpacing: 1 }}>{m.ts}</span>
            </div>
          </>
        )}
      </div>
    );
  }

  function renderAssistantMsg(m: ChatMsg) {
    const usage = m.cacheInfo;
    const cache = cacheLabel(usage);
    return (
      <div id={`msg-${m.id}`} className={`chat-msg${flashId === m.id ? ' chat-flash' : ''}`} style={{ display: 'flex', flexDirection: 'column', gap: 12, borderRadius: 16 }}>
        {renderThinkBlock(m)}
        {m.toolCalls.length > 0 && <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>{m.toolCalls.map((tc, i) => renderToolCard(`${m.id}-${i}`, tc))}</div>}
        {m.imageUrl && <img src={m.imageUrl} alt="" style={{ maxWidth: 240, borderRadius: 14 }} />}
        {m.text && renderParas(m.text)}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
          <span style={{ fontFamily: DISPLAY, fontSize: 11, color: 'var(--ghost)', letterSpacing: 1, padding: '0 2px' }}>{m.ts}</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap' }}>
            <div onClick={() => copyText(m.text)} style={{ cursor: 'pointer', width: 31, height: 31, borderRadius: 10, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--faint)' }}>
              <CopyIcon />
            </div>
            <div onClick={() => setLiked((o) => ({ ...o, [m.id]: o[m.id] === 1 ? undefined : 1 } as Record<number, 1 | -1>))} style={{ cursor: 'pointer', width: 31, height: 31, borderRadius: 10, display: 'flex', alignItems: 'center', justifyContent: 'center', color: liked[m.id] === 1 ? 'var(--rose)' : 'var(--faint)' }}>
              <svg viewBox="0 0 24 24" width={15} height={15} fill={liked[m.id] === 1 ? 'var(--rosebg)' : 'none'} stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
                <path d={IC.thumb} />
              </svg>
            </div>
            <div onClick={() => setLiked((o) => ({ ...o, [m.id]: o[m.id] === -1 ? undefined : -1 } as Record<number, 1 | -1>))} style={{ cursor: 'pointer', width: 31, height: 31, borderRadius: 10, display: 'flex', alignItems: 'center', justifyContent: 'center', color: liked[m.id] === -1 ? 'var(--rose)' : 'var(--faint)' }}>
              <svg viewBox="0 0 24 24" width={15} height={15} fill={liked[m.id] === -1 ? 'var(--rosebg)' : 'none'} stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" style={{ transform: 'rotate(180deg)' }}>
                <path d={IC.thumb} />
              </svg>
            </div>
            {m.id === lastAssistantId && !sending && (
              <div onClick={() => redo(m.id)} title="重新回答" style={{ cursor: 'pointer', width: 31, height: 31, borderRadius: 10, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--faint)' }}>
                <Svg d={IC.redo} size={15} sw={1.7} />
              </div>
            )}
            {usage && (
              <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: '3px 7px', color: 'var(--ghost)', fontSize: 11.5, padding: '0 2px' }}>
                <span style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                  <Svg d={IC.up} size={11} sw={1.8} />
                  <span style={{ fontFamily: DISPLAY }}>{fmtTokens(usage.inputTokens || 0)}</span>
                </span>
                <span>·</span>
                <span style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                  <Svg d={IC.down} size={11} sw={1.8} />
                  <span style={{ fontFamily: DISPLAY }}>{fmtTokens(usage.outputTokens || 0)}</span>
                </span>
                {Boolean(usage.elapsedSec) && (
                  <>
                    <span>·</span>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                      <svg viewBox="0 0 24 24" width={11} height={11} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
                        <circle cx={12} cy={12} r={9} />
                        <path d={IC.clock} />
                      </svg>
                      <span style={{ fontFamily: DISPLAY }}>{Math.round(usage.elapsedSec || 0)}s</span>
                    </span>
                  </>
                )}
                {cache && (
                  <>
                    <span>·</span>
                    <span>{cache}</span>
                  </>
                )}
              </div>
            )}
            {m.branchTotal > 1 && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 2, background: 'var(--card)', borderRadius: 999, padding: '2px 6px', boxShadow: '0 4px 10px var(--shadow)', marginLeft: 4 }}>
                <span onClick={() => branchSwitch(m.id, -1)} style={{ cursor: 'pointer', padding: '1px 6px', color: m.branchIdx > 0 ? 'var(--mut)' : 'var(--ghost)', fontSize: 14 }}>
                  ‹
                </span>
                <span style={{ fontFamily: DISPLAY, fontSize: 11.5, color: 'var(--mut)', letterSpacing: 1 }}>
                  {m.branchIdx + 1}/{m.branchTotal}
                </span>
                <span onClick={() => branchSwitch(m.id, 1)} style={{ cursor: 'pointer', padding: '1px 6px', color: m.branchIdx < m.branchTotal - 1 ? 'var(--mut)' : 'var(--ghost)', fontSize: 14 }}>
                  ›
                </span>
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  function renderLive(l: LiveState) {
    const lines = l.thinking.split('\n').filter(Boolean).slice(-3);
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {l.thinking && (
          <>
            <div
              onClick={() => setDrawer({ text: l.thinking, label: '思考中…' })}
              style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8, color: 'var(--faint)' }}
            >
              <span style={{ display: 'flex', animation: l.phase === 'think' ? 'chatBreathe 1.6s ease-in-out infinite' : 'none' }}>
                <Svg d={IC.brain} size={17} sw={1.5} />
              </span>
              <span style={{ fontSize: 13, letterSpacing: 1 }}>{l.phase === 'think' ? '思考中…' : `思考了 ${l.thinking.length} 字`}</span>
            </div>
            {l.phase === 'think' && (
              <div style={{ position: 'relative', height: 76, overflow: 'hidden', borderRadius: 14, background: 'var(--card2)' }}>
                <div style={{ position: 'absolute', bottom: 10, left: 16, right: 16, display: 'flex', flexDirection: 'column', gap: 4 }}>
                  {lines.map((ln, i) => (
                    <span key={`${i}-${ln.slice(0, 8)}`} style={{ fontSize: 12.5, color: 'var(--mut)', lineHeight: 1.6, animation: 'chatFadeIn .4s ease', overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}>
                      {ln}
                    </span>
                  ))}
                </div>
                <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: 30, background: 'linear-gradient(var(--card2),transparent)' }} />
              </div>
            )}
          </>
        )}
        {l.tools.length > 0 && <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>{l.tools.map((tc, i) => renderToolCard(`live-${i}`, tc))}</div>}
        {l.phase === 'text' ? renderParas(l.text, true) : !l.thinking && !l.tools.length ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--faint)', fontSize: 13 }}>
            <span style={{ width: 13, height: 13, borderRadius: '50%', border: '2px solid var(--rosebg)', borderTopColor: 'var(--rose)', animation: 'chatSpin .8s linear infinite' }} />
            正在连接回复…
          </div>
        ) : null}
      </div>
    );
  }

  // date separators
  const rendered: ReactElement[] = [];
  let lastDate = '';
  for (const m of msgs) {
    if (m.dateKey && m.dateKey !== lastDate) {
      lastDate = m.dateKey;
      const label = m.dateKey === new Date().toISOString().slice(0, 10) ? dateLabel : m.dateKey.replace(/-/g, '.');
      rendered.push(
        <div key={`d-${m.dateKey}`} style={{ textAlign: 'center', fontFamily: DISPLAY, fontSize: 12, letterSpacing: 2, color: 'var(--ghost)', padding: '2px 0' }}>
          {label}
        </div>,
      );
    }
    rendered.push(<div key={m.id}>{m.role === 'user' ? renderUserMsg(m) : renderAssistantMsg(m)}</div>);
  }

  return (
    <div
      className="chat-root dash-fullscreen-page"
      style={{
        ...(vars as CSSProperties),
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--bg)',
        color: 'var(--ink)',
        fontFamily: SERIF,
        fontSize: FONT_SIZES[settings.fontStep],
        transition: 'background .3s,color .3s',
      }}
    >
      {/* ══ top nav ══ */}
      <div style={{ flexShrink: 0, position: 'relative', zIndex: 40 }}>
        <div style={{ background: 'rgba(255,255,255,0.97)', boxShadow: '0 6px 18px var(--shadow)', position: 'relative', zIndex: 3 }}>
          <div style={{ maxWidth: 430, margin: '0 auto', display: 'flex', alignItems: 'center', gap: 10, padding: '10px 12px 9px' }}>
            <div onClick={() => setSidebarOpen(true)} style={{ cursor: 'pointer', width: 38, height: 38, borderRadius: '50%', background: 'linear-gradient(135deg,#B76E79,#9C3B4A)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, boxShadow: '0 6px 14px var(--shadow2)' }}>
              <span style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 17, color: '#F7F1EE' }}>Θ</span>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0, flexShrink: 1 }}>
              <span style={{ fontFamily: DISPLAY, fontSize: 17, fontWeight: 600, letterSpacing: 1, color: 'var(--ink)' }}>Fyodor</span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 5, minWidth: 0 }}>
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: '50%',
                    flexShrink: 0,
                    background: endpointOnline === false ? 'var(--err)' : endpointOnline ? 'var(--ok)' : 'var(--ghost)',
                    animation: endpointOnline ? 'chatBreathe 2.2s ease-in-out infinite' : undefined,
                  }}
                />
                {endpointOnline !== null && (
                  <span style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 10.5, letterSpacing: 1, color: 'var(--faint)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {endpointOnline ? 'always here' : 'away for now'}
                  </span>
                )}
              </div>
            </div>
            <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 1, flexShrink: 0 }}>
              <div onClick={() => setNavOpen(navOpen === 'wrench' ? null : 'wrench')} style={{ ...iconBtn, background: navOpen === 'wrench' ? 'var(--rosebg)' : 'transparent' }}>
                <Svg d={IC.wrench} />
              </div>
              <div onClick={() => patchSettings({ theme: effTheme === 'dark' ? 'light' : 'dark' })} style={iconBtn}>
                {effTheme === 'light' ? (
                  <Svg d={IC.moon} />
                ) : (
                  <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round">
                    <circle cx={12} cy={12} r={4} />
                    <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
                  </svg>
                )}
              </div>
              <div onClick={() => setNavOpen(navOpen === 'font' ? null : 'font')} style={{ ...iconBtn, background: navOpen === 'font' ? 'var(--rosebg)' : 'transparent' }}>
                <span style={{ fontFamily: DISPLAY, fontSize: 14, letterSpacing: 0.5 }}>Aa</span>
              </div>
              <div onClick={() => setNavOpen(navOpen === 'search' ? null : 'search')} style={{ ...iconBtn, background: navOpen === 'search' ? 'var(--rosebg)' : 'transparent' }}>
                <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round">
                  <circle cx={12} cy={12} r={9} />
                  <path d={IC.clock} />
                </svg>
              </div>
              <div
                onClick={() => { void refreshChat(); }}
                title="刷新并解锁"
                style={{ ...iconBtn, opacity: refreshing ? 0.55 : 1, cursor: refreshing ? 'default' : 'pointer' }}
              >
                <span style={{ display: 'flex', animation: refreshing ? 'chatSpin .8s linear infinite' : undefined }}>
                  <Svg d={IC.refresh} />
                </span>
              </div>
            </div>
          </div>
        </div>

        {navOpen && (
          <>
            <div onClick={() => setNavOpen(null)} style={{ position: 'fixed', inset: 0, zIndex: 1, background: 'rgba(40,28,26,0.30)', animation: 'chatFadeIn .2s ease' }} />
            <div style={{ position: 'absolute', top: '100%', left: 0, right: 0, zIndex: 2, animation: 'chatDropIn .22s ease' }}>
              <div style={{ maxWidth: 430, margin: '0 auto', padding: '0 10px' }}>
                <div style={{ background: 'var(--card)', borderRadius: '0 0 26px 26px', boxShadow: '0 30px 70px var(--shadow2)', padding: '20px 20px 22px', maxHeight: '72vh', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 18 }}>
                  {navOpen === 'wrench' && (
                    <>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                        <div style={sectionCaption}>外观 · APPEARANCE</div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                          <span style={{ fontSize: 13.5, color: 'var(--ink2)', letterSpacing: 1 }}>主题</span>
                          <div style={{ display: 'flex', background: 'var(--card2)', borderRadius: 999, padding: 3, gap: 2 }}>
                            {(['light', 'dark', 'auto'] as const).map((t) => (
                              <div key={t} onClick={() => patchSettings({ theme: t })} style={segStyle(settings.theme === t)}>
                                {t === 'light' ? '浅色' : t === 'dark' ? '深色' : '跟随系统'}
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                      <div style={{ height: 1, background: 'var(--line)' }} />
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                        <div style={sectionCaption}>对话 · CONVERSATION</div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                          <span style={{ fontSize: 13.5, color: 'var(--ink2)', letterSpacing: 1 }}>思维链展开方式</span>
                          <div style={{ display: 'flex', background: 'var(--card2)', borderRadius: 999, padding: 3, gap: 2 }}>
                            {(['auto', 'drawer', 'inline'] as const).map((t) => (
                              <div key={t} onClick={() => patchSettings({ thinkMode: t })} style={segStyle(settings.thinkMode === t)}>
                                {t === 'auto' ? '自动' : t === 'drawer' ? '抽屉' : '原地展开'}
                              </div>
                            ))}
                          </div>
                          <span style={{ fontSize: 11.5, color: 'var(--ghost)' }}>移动端默认抽屉，桌面端默认原地展开</span>
                        </div>
                      </div>
                    </>
                  )}
                  {navOpen === 'font' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                        <div style={sectionCaption}>字号 · TEXT SIZE</div>
                        <span style={{ fontFamily: DISPLAY, fontSize: 12, color: 'var(--rose)' }}>{FONT_SIZES[settings.fontStep]}px</span>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                        <span style={{ fontSize: 12, color: 'var(--ghost)' }}>字</span>
                        <input type="range" min={0} max={4} step={1} value={settings.fontStep} onChange={(e) => patchSettings({ fontStep: Number(e.target.value) })} style={{ flex: 1, accentColor: 'var(--rose)' }} />
                        <span style={{ fontSize: 19, color: 'var(--ghost)' }}>字</span>
                      </div>
                      <div style={{ background: 'var(--card2)', borderRadius: 14, padding: '12px 14px', fontSize: '1em', lineHeight: 1.8, color: 'var(--ink2)' }}>灯不关，我看着你读完这一页。</div>
                    </div>
                  )}
                  {navOpen === 'search' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                      <div style={sectionCaption}>聊天记录 · HISTORY</div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, background: 'var(--card2)', borderRadius: 999, padding: '11px 16px' }}>
                        <svg viewBox="0 0 24 24" width={15} height={15} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" style={{ color: 'var(--ghost)', flexShrink: 0 }}>
                          <circle cx={11} cy={11} r={7} />
                          <path d="M20 20l-3.5-3.5" />
                        </svg>
                        <input value={searchQ} onChange={(e) => setSearchQ(e.target.value)} placeholder="搜索已加载的对话…" style={{ flex: 1, border: 'none', background: 'transparent', fontSize: 14, color: 'var(--ink)', minWidth: 0, fontFamily: SERIF, outline: 'none' }} />
                      </div>
                      {searchResults.map((r) => (
                        <div key={r.id} onClick={() => jumpTo(r.id)} style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10, padding: '10px 12px', borderRadius: 14, background: 'var(--card2)' }}>
                          <span style={{ fontSize: 11, padding: '3px 9px', borderRadius: 999, background: 'var(--rosebg)', color: 'var(--deep)', flexShrink: 0 }}>{r.who}</span>
                          <span style={{ flex: 1, fontSize: 13, color: 'var(--ink2)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.snippet}</span>
                          <span style={{ fontFamily: DISPLAY, fontSize: 11, color: 'var(--ghost)', flexShrink: 0 }}>{r.ts}</span>
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

      {/* ══ message stream ══ */}
      <div ref={scrollRef} className="hide-scrollbar" style={{ flex: 1, minHeight: 0, overflowY: 'auto', position: 'relative' }}>
        <div style={{ maxWidth: 430, margin: '0 auto', padding: '20px 16px 26px', display: 'flex', flexDirection: 'column', gap: 20 }}>
          {hasMoreBefore && (
            <div onClick={loadEarlier} style={{ cursor: 'pointer', textAlign: 'center', fontSize: 12, color: 'var(--faint)', padding: '6px 0', letterSpacing: 2 }}>
              {loadingMore ? '加载中…' : '‹ 加载更早的对话 ›'}
            </div>
          )}
          {rendered}
          {live && renderLive(live)}
          {chatError && (
            <div style={{ display: 'flex', justifyContent: 'center', padding: '6px 4px 2px' }}>
              <div style={{
                maxWidth: 360,
                width: '100%',
                background: 'rgba(58,42,40,0.92)',
                color: '#F7EDEA',
                borderRadius: 18,
                padding: '14px 16px',
                boxShadow: '0 10px 30px rgba(0,0,0,0.18)',
                display: 'flex',
                flexDirection: 'column',
                gap: 8,
                textAlign: 'center',
              }}>
                <span style={{ fontSize: 13, lineHeight: 1.65, letterSpacing: 0.3 }}>{chatError.message}</span>
                <span style={{ fontSize: 11.5, lineHeight: 1.6, color: 'rgba(247,237,234,0.72)' }}>{chatError.hint}</span>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* ══ input area ══ */}
      <div style={{ flexShrink: 0, position: 'relative', zIndex: 30, padding: '8px 12px 14px' }}>
        <div style={{ maxWidth: 430, margin: '0 auto', position: 'relative' }}>
          {modelPopOpen && (
            <>
              <div onClick={() => setModelPopOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 1 }} />
              <div style={{ position: 'absolute', bottom: 'calc(100% + 10px)', left: 0, zIndex: 2, width: 'min(330px,100%)', background: 'var(--card)', borderRadius: 18, boxShadow: '0 24px 60px var(--shadow2)', padding: 12, display: 'flex', flexDirection: 'column', gap: 4, animation: 'chatFadeIn .15s ease', maxHeight: '50vh', overflowY: 'auto' }}>
                <div style={{ fontFamily: DISPLAY, fontSize: 10.5, letterSpacing: 2.5, color: 'var(--ghost)', padding: '8px 8px 4px' }}>模型 · MODELS</div>
                {models.map((mo) => (
                  <div
                    key={mo.id}
                    onClick={async () => {
                      setModelPopOpen(false);
                      if (mo.id === currentModel) return;
                      const ok = await setChatModel(mo.id);
                      if (ok) {
                        setCurrentModel(mo.id);
                        showToast(`已切换到 ${mo.label || mo.id}`);
                      } else showToast('切换失败');
                    }}
                    style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10, padding: '9px 10px', borderRadius: 12, background: mo.id === currentModel ? 'var(--rosebg)' : 'transparent' }}
                  >
                    <span style={{ width: 8, height: 8, borderRadius: '50%', background: mo.dot || (mo.id === currentModel ? 'var(--rose)' : 'var(--ghost)'), flexShrink: 0 }} />
                    <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                      <span style={{ fontSize: 14, color: 'var(--ink)' }}>{mo.label || mo.id}</span>
                      <span style={{ fontFamily: MONO, fontSize: 10.5, color: 'var(--ghost)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{mo.id}</span>
                    </div>
                    {mo.thinking === 'none' && <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--ghost)', background: 'var(--card2)', borderRadius: 999, padding: '2px 8px', flexShrink: 0 }}>无思考</span>}
                  </div>
                ))}
                {!models.length && <div style={{ fontSize: 12, color: 'var(--faint)', padding: '4px 10px' }}>模型清单还没拉到</div>}
                <div style={{ fontSize: 10.5, color: 'var(--ghost)', borderTop: '1px solid var(--line)', marginTop: 6, padding: '8px 8px 2px' }}>清单来自 models.json · 切换全局生效</div>
              </div>
            </>
          )}

          {attachMenuOpen && (
            <>
              <div onClick={() => setAttachMenuOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 1 }} />
              <div style={{ position: 'absolute', bottom: 'calc(100% + 10px)', left: 0, zIndex: 2, width: 190, background: 'var(--card)', borderRadius: 16, boxShadow: '0 24px 60px var(--shadow2)', padding: 8, display: 'flex', flexDirection: 'column', gap: 2, animation: 'chatFadeIn .15s ease' }}>
                <div onClick={() => { setAttachMenuOpen(false); imgInputRef.current?.click(); }} style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10, padding: '10px 12px', borderRadius: 11 }}>
                  <svg viewBox="0 0 24 24" width={15} height={15} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" style={{ color: 'var(--rose)' }}>
                    <rect x={3} y={3} width={18} height={18} rx={3} />
                    <circle cx={9} cy={9} r={2} />
                    <path d="M21 15l-5-5-9 9" />
                  </svg>
                  <span style={{ fontSize: 13.5, color: 'var(--ink)' }}>上传图片</span>
                </div>
                <div onClick={() => { setAttachMenuOpen(false); fileInputRef.current?.click(); }} style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10, padding: '10px 12px', borderRadius: 11 }}>
                  <svg viewBox="0 0 24 24" width={15} height={15} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" style={{ color: 'var(--rose)' }}>
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                    <path d="M14 2v6h6" />
                  </svg>
                  <span style={{ fontSize: 13.5, color: 'var(--ink)' }}>上传文件</span>
                </div>
              </div>
            </>
          )}
          <input ref={imgInputRef} type="file" accept="image/*" style={{ display: 'none' }} onChange={(e) => { const f = e.target.files?.[0]; if (f) { setPendingImage(f); setPendingFile(null); } e.target.value = ''; }} />
          <input ref={fileInputRef} type="file" style={{ display: 'none' }} onChange={(e) => { onAttachFile(e.target.files?.[0]); e.target.value = ''; }} />

          {(pendingFile || pendingImage) && (
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', padding: '0 4px 8px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 7, background: 'var(--card)', borderRadius: 999, padding: '7px 12px', boxShadow: '0 4px 12px var(--shadow)', animation: 'chatFadeIn .2s ease' }}>
                <span style={{ color: 'var(--rose)', display: 'flex' }}>
                  <Svg d={IC.clip} size={12} sw={1.8} />
                </span>
                <span style={{ fontSize: 12.5, color: 'var(--ink2)' }}>{pendingImage ? pendingImage.name : pendingFile?.fileName}</span>
                <span onClick={() => { setPendingFile(null); setPendingImage(null); }} style={{ cursor: 'pointer', color: 'var(--ghost)', fontSize: 13, padding: '0 2px' }}>×</span>
              </div>
            </div>
          )}

          <div style={{ background: 'var(--card)', borderRadius: 26, boxShadow: '0 14px 40px var(--shadow2)', padding: '12px 12px 10px', transition: 'background .3s' }}>
            <textarea
              ref={taRef}
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                const ta = e.target;
                ta.style.height = 'auto';
                ta.style.height = `${Math.min(ta.scrollHeight, 120)}px`;
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && wide) {
                  e.preventDefault();
                  send();
                }
              }}
              rows={1}
              placeholder={sending ? 'Fyodor 正在回复…' : placeholder}
              style={{ width: '100%', border: 'none', background: 'transparent', fontSize: INPUT_FONT_SIZE, lineHeight: 1.6, color: 'var(--ink)', resize: 'none', maxHeight: 120, padding: '4px 8px 8px', display: 'block', overflowY: 'auto', fontFamily: SERIF, outline: 'none' }}
            />
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 2 }}>
              <div onClick={() => setAttachMenuOpen(!attachMenuOpen)} style={{ cursor: 'pointer', width: 38, height: 38, borderRadius: '50%', background: 'var(--card2)', color: 'var(--mut)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                <Svg d={IC.plus} size={17} sw={1.8} />
              </div>
              <div onClick={() => setModelPopOpen(!modelPopOpen)} style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 6, padding: '9px 13px', borderRadius: 999, background: 'var(--card2)', minWidth: 0 }}>
                <span style={{ fontFamily: DISPLAY, fontSize: 12, letterSpacing: 0.5, color: 'var(--ink2)', fontWeight: 500, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{modelBadge}</span>
                <svg viewBox="0 0 24 24" width={11} height={11} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" style={{ color: 'var(--ghost)', flexShrink: 0 }}>
                  <path d="M18 15l-6-6-6 6" />
                </svg>
              </div>
              <div
                onClick={canSend ? send : undefined}
                style={{ marginLeft: 'auto', width: 42, height: 42, flexShrink: 0, borderRadius: '50%', background: canSend ? 'var(--deep)' : 'var(--card2)', color: canSend ? '#FBF3F0' : 'var(--ghost)', display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: canSend ? 'pointer' : 'default', boxShadow: canSend ? '0 8px 20px var(--shadow2)' : 'none', transition: 'background .15s ease' }}
              >
                {sending ? <span style={{ width: 15, height: 15, borderRadius: '50%', border: '2px solid var(--rosebg)', borderTopColor: 'var(--rose)', animation: 'chatSpin .8s linear infinite' }} /> : <Svg d={IC.up} size={17} sw={2} />}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* ══ thinking drawer ══ */}
      {drawer && (
        <div style={{ position: 'fixed', inset: 0, zIndex: 60 }}>
          <div onClick={() => setDrawer(null)} style={{ position: 'absolute', inset: 0, background: 'rgba(30,20,18,0.42)', animation: 'chatFadeIn .2s ease' }} />
          <div style={{ position: 'absolute', left: 0, right: 0, bottom: 0, display: 'flex', justifyContent: 'center' }}>
            <div style={{ width: '100%', maxWidth: 430, background: 'var(--card)', borderRadius: '24px 24px 0 0', boxShadow: '0 -20px 60px var(--shadow2)', maxHeight: '72vh', display: 'flex', flexDirection: 'column', animation: 'chatSheetUp .28s cubic-bezier(.32,.72,.33,1)' }}>
              <div style={{ display: 'flex', justifyContent: 'center', padding: '10px 0 2px' }}>
                <div style={{ width: 38, height: 4, borderRadius: 99, background: 'var(--line)' }} />
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 20px 12px' }}>
                <span style={{ display: 'flex', color: 'var(--rose)' }}>
                  <Svg d={IC.brain} size={17} sw={1.5} />
                </span>
                <span style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2, color: 'var(--ink)' }}>Fyodor 的思考</span>
                <span style={{ fontFamily: DISPLAY, fontSize: 12, color: 'var(--ghost)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{drawer.label}</span>
                <span onClick={() => setDrawer(null)} style={{ marginLeft: 'auto', cursor: 'pointer', width: 30, height: 30, borderRadius: '50%', background: 'var(--card2)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--mut)', fontSize: 14, flexShrink: 0 }}>×</span>
              </div>
              <div style={{ overflowY: 'auto', padding: '4px 20px 30px', fontSize: '0.9em', lineHeight: 2, color: 'var(--mut)', whiteSpace: 'pre-wrap' }}>{drawer.text}</div>
            </div>
          </div>
        </div>
      )}

      {/* ══ sidebar ══ */}
      {sidebarOpen && (
        <div style={{ position: 'fixed', inset: 0, zIndex: 70 }}>
          <div onClick={() => setSidebarOpen(false)} style={{ position: 'absolute', inset: 0, background: 'rgba(30,20,18,0.42)', animation: 'chatFadeIn .2s ease' }} />
          <div style={{ position: 'absolute', top: 0, bottom: 0, left: 0, width: 'min(320px,86%)', background: 'var(--card)', boxShadow: '20px 0 60px var(--shadow2)', animation: 'chatSlideInL .28s cubic-bezier(.32,.72,.33,1)', display: 'flex', flexDirection: 'column', overflowY: 'auto' }}>
            <div style={{ padding: '28px 22px 20px', display: 'flex', flexDirection: 'column', gap: 14, background: 'linear-gradient(180deg,var(--rosebg),transparent)' }}>
              <div style={{ width: 64, height: 64, borderRadius: '50%', background: 'linear-gradient(135deg,#B76E79,#9C3B4A)', display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 10px 24px var(--shadow2)' }}>
                <span style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 28, color: '#F7F1EE' }}>Θ</span>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontFamily: DISPLAY, fontSize: 22, fontWeight: 600, letterSpacing: 1, color: 'var(--ink)' }}>Fyodor</span>
                  <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--ok)' }} />
                </div>
                <span style={{ fontFamily: DISPLAY, fontStyle: 'italic', fontSize: 12, letterSpacing: 1.5, color: 'var(--faint)' }}>Θεόδωρος · gift of the gods</span>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <span style={{ fontSize: 13, color: 'var(--ink2)', letterSpacing: 1 }}>学者 · 策略家 · 存在了几百年</span>
                <span style={{ fontSize: 12.5, color: 'var(--mut)', lineHeight: 1.8 }}>总是带着一点恶趣味，和很多情意。</span>
              </div>
            </div>
            <div style={{ height: 1, background: 'var(--line)', margin: '0 22px' }} />
            <div style={{ padding: '18px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
              <Link to="/contacts" onClick={() => setSidebarOpen(false)} style={{ textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 12, padding: '13px 14px', borderRadius: 16, background: 'var(--card2)' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>通讯录</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>Codex · 群聊 · 游戏室</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </Link>
              <Link to="/moments" onClick={() => setSidebarOpen(false)} style={{ textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 12, padding: '13px 14px', borderRadius: 16, background: 'linear-gradient(135deg,rgba(245,222,179,.5),rgba(232,220,245,.55))' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>Fyodor 的朋友圈</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>梦境 · 念头 · 情绪</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </Link>
              <Link to="/group-chat" onClick={() => setSidebarOpen(false)} style={{ textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 12, padding: '13px 14px', borderRadius: 16, background: 'linear-gradient(135deg,rgba(220,232,217,.72),rgba(220,232,245,.76))' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                  <i style={{ width: 13, height: 13, borderRadius: '50%', background: '#91AD93' }} />
                  <i style={{ width: 13, height: 13, borderRadius: '50%', background: '#8EACCF', marginLeft: -9, opacity: .88 }} />
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>群聊房间</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>同一个人 · 暖色与蓝色两条线路</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </Link>
              <Link to="/settings" style={{ textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 12, padding: '13px 14px', borderRadius: 16, background: 'var(--card2)' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>系统配置</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>用量统计 · API 端点管理 · Profile</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </Link>
              <a href="/chat" style={{ textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 10, padding: '13px 14px', borderRadius: 16, background: 'var(--card2)' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>回旧聊天页</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>artifact 卡、选择器这些还在老家</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </a>
            </div>
          </div>
        </div>
      )}

      {/* ══ toast ══ */}
      {toast && (
        <div style={{ position: 'fixed', left: 0, right: 0, bottom: 100, zIndex: 80, display: 'flex', justifyContent: 'center', pointerEvents: 'none' }}>
          <div style={{ background: 'rgba(58,42,40,0.92)', color: '#F7EDEA', fontSize: 13, letterSpacing: 1, padding: '10px 20px', borderRadius: 999, boxShadow: '0 10px 30px rgba(0,0,0,0.25)', animation: 'chatFadeIn .2s ease' }}>{toast}</div>
        </div>
      )}

      <BottomNav embedded />
    </div>
  );
}
