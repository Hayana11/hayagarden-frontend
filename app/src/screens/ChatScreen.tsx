// Fyodor Chat — implements Fyodor Chat.dc.html against the production chat
// backend: /api/chat/messages history, send -> /api/gw/chat/stream SSE
// (think/text/tool_use/tool_result/usage/done/err), inline branches
// (branch/switch, regen prepare/finalize), edit-with-truncate, model catalog.
// Mounted at /dash/chat, parallel to the legacy /chat page.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type UIEvent } from 'react';
import { Link } from 'react-router-dom';
import { CarryoverModal } from '../components/dailySoftWindow';
import { useManualContextWindow } from '../hooks/useManualContextWindow';
import {
  editChatMessage,
  editFinalize,
  fetchChatMessages,
  fetchChatMessagesOrNull,
  ensureModelCatalog,
  regenFinalize,
  regenPrepare,
  sendChatMessage,
  setChatModel,
  switchChatBranch,
  uploadChatFile,
  type ChatModelCatalog,
  type ModelCatalogEntry,
} from '../lib/api';
import {
  artifactTypeIcon,
  artifactTypeLabel,
  cacheLabel,
  chatFilePreviewUrl,
  chatPlaceholder,
  findLatestRoundContext,
  formatCapacityLabel,
  fmtArtifactSize,
  fmtCostUsd,
  fmtTokens,
  fetchChatGatewayOnline,
  forceUnlockChatGenLock,
  guessChatErrorHint,
  isChoicesAnswered,
  normalizeToolCall,
  streamChatReply,
  type ChatMsg,
  type ChatToolCall,
} from '../lib/chat';
import type { SoftWindowUiState } from '../lib/dailySoftWindow';
import { ComposerUploadCoordinator } from '../lib/composerUpload';
import { ChatThemeQuickToggle, ChatThemeSegmented } from '../components/ChatThemeControl';
import { ThemePerfRows } from '../components/ThemePerfRows';
import { attachChatTheme, loadChatSettings, patchChatSettings, resolveEffectiveTheme, setChatTheme, type EffectiveTheme, type ThemeMode } from '../lib/chatTheme';
import { getLegacyNativeCompatDetails } from '../lib/legacyNativeCompat';
import {
  readChatWarmReturn,
  reconcileChatWarmReturn,
  writeChatWarmReturn,
} from '../lib/chatWarmReturn';
import {
  clearChatComposerDraft,
  followLatestFromGeometry,
  readChatComposerDraft,
  writeChatComposerDraft,
} from '../lib/chatNavigationState';
import {
  bumpHistoryGenState,
  cancelInFlightWarmUpState,
  CHAT_AUTHORITATIVE_LIMIT,
  CHAT_LEGACY_INITIAL_LIMIT,
  CHAT_LEGACY_WARMUP_LIMIT,
  createColdStartRaceState,
  hasAuthoritativeCoverage,
  markChatColdStart,
  markWarmUpSatisfiedState,
  mergeOlderChatMessages,
  needsLegacyWarmUp,
  onAuthoritativeHistorySuccess,
  planWarmUpCommit,
  scheduleAfterFirstPaint,
  shouldMarkWarmUpSatisfiedAfterPage,
  tryConsumeDeferredInit,
  type ColdStartRaceState,
} from '../lib/chatColdStart';
import {
  clampTranscriptWindow,
  followLatestAfterSearchJump,
  isTranscriptWindowAtLatest,
  latestTranscriptWindow,
  shiftTranscriptWindowNewer,
  shiftTranscriptWindowOlder,
  transcriptWindowAfterPrepend,
  transcriptWindowAroundIndex,
  windowSize,
  type TranscriptWindow,
} from '../lib/legacyTranscriptWindow';
import {
  countDescendants,
  setSkipThemePerf,
  setThemeProbeMode,
  subscribeThemePerf,
} from '../lib/themePerfProbe';
import type { ReactElement } from 'react';
import { installObjectHasOwnCompat } from '../lib/objectHasOwnCompat';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import './ChatMarkdown.css';
import { MixedSectionLabel } from '../components/MixedSectionLabel';
import { FONT_CN, FONT_DISPLAY, FONT_MONO, fontFamilyForText } from '../lib/typography';

installObjectHasOwnCompat();

const FONT_SIZES = [13.5, 14.5, 16, 17.5, 19];
const INPUT_FONT_SIZE = FONT_SIZES[0];

function isSafeMarkdownHref(href: string): boolean {
  const value = href.trim();
  if (!value) return false;
  try {
    const url = new URL(value, window.location.href);
    return ['http:', 'https:', 'mailto:', 'tel:'].includes(url.protocol);
  } catch {
    return false;
  }
}

function isExternalMarkdownHref(href: string): boolean {
  try {
    const url = new URL(href, window.location.href);
    return url.protocol === 'mailto:' || url.protocol === 'tel:' || url.origin !== window.location.origin;
  } catch {
    return false;
  }
}

const markdownComponents: Components = {
  a({ href, children, node: _node, ...props }) {
    const safeHref = typeof href === 'string' && isSafeMarkdownHref(href) ? href : null;
    if (!safeHref) return <span className="chat-markdown-link-blocked">{children}</span>;
    const external = isExternalMarkdownHref(safeHref);
    return (
      <a
        {...props}
        href={safeHref}
        {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
      >
        {children}
      </a>
    );
  },
};


interface ChatPrefs {
  fontStep: number;
  thinkMode: 'auto' | 'drawer' | 'inline';
}

function loadChatPrefs(): ChatPrefs {
  const s = loadChatSettings();
  return { fontStep: s.fontStep, thinkMode: s.thinkMode };
}

type LayoutDiagRow = { label: string; value: string };

function fmtPx(n: number) {
  return `${n.toFixed(2)}px`;
}

function readZoom(st: CSSStyleDeclaration): string {
  const raw = (st as CSSStyleDeclaration & { zoom?: string }).zoom;
  if (!raw || raw === 'normal' || raw === '1') return 'n/a';
  return raw;
}

function readTextSizeAdjust(st: CSSStyleDeclaration): string {
  const v = st.getPropertyValue('-webkit-text-size-adjust');
  return v || 'n/a';
}

function pushElDiag(rows: LayoutDiagRow[], label: string, el: HTMLElement | null) {
  if (!el) {
    rows.push({ label: `${label} (missing)`, value: 'n/a' });
    return;
  }
  const rect = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  rows.push({ label: `${label} offsetWidth`, value: String(el.offsetWidth) });
  rows.push({ label: `${label} clientWidth`, value: String(el.clientWidth) });
  rows.push({ label: `${label} rect.width`, value: fmtPx(rect.width) });
  rows.push({ label: `${label} computed zoom`, value: readZoom(st) });
  rows.push({ label: `${label} computed transform`, value: st.transform === 'none' ? 'none' : st.transform });
  rows.push({ label: `${label} -webkit-text-size-adjust`, value: readTextSizeAdjust(st) });
}

function collectLayoutDiagnostics(
  root: HTMLElement | null,
  transcriptEl: HTMLElement | null,
  counts: {
    loadedCount: number;
    mountedCount: number;
    window: TranscriptWindow | null;
  },
): LayoutDiagRow[] {
  const vv = window.visualViewport;
  const testEl = document.getElementById('c78-layout-test-100') as HTMLElement | null;
  const appRoot = document.getElementById('root');
  const { body } = document;
  const html = document.documentElement;

  let noto = 'n/a';
  let bodoni = 'n/a';
  try {
    noto = document.fonts.check('12px "Noto Serif SC"') ? 'true' : 'false';
    bodoni = document.fonts.check('12px "Bodoni Moda"') ? 'true' : 'false';
  } catch {
    /* FontFaceSet unavailable */
  }

  const compat = getLegacyNativeCompatDetails();

  const rows: LayoutDiagRow[] = [
    { label: 'legacyNativeCompat', value: String(compat.legacyNativeCompat) },
    { label: 'isNativeCapacitor', value: String(compat.isNativeCapacitor) },
    { label: 'flexGapUnsupported', value: String(compat.flexGapUnsupported) },
    { label: 'body data-legacy-native-compat', value: document.body.getAttribute('data-legacy-native-compat') ?? 'n/a' },
    { label: 'window.innerWidth', value: String(window.innerWidth) },
    { label: 'window.innerHeight', value: String(window.innerHeight) },
    { label: 'document.documentElement.clientWidth', value: String(html.clientWidth) },
    { label: 'screen.width', value: String(window.screen.width) },
    { label: 'screen.height', value: String(window.screen.height) },
    { label: 'window.devicePixelRatio', value: String(window.devicePixelRatio) },
    { label: 'visualViewport?.width', value: vv ? String(vv.width) : 'n/a' },
    { label: 'visualViewport?.height', value: vv ? String(vv.height) : 'n/a' },
    { label: 'visualViewport?.scale', value: vv ? String(vv.scale) : 'n/a' },
    { label: 'fonts.check Noto Serif SC', value: noto },
    { label: 'fonts.check Bodoni Moda', value: bodoni },
  ];

  if (testEl) {
    const rect = testEl.getBoundingClientRect();
    rows.push({ label: 'test offsetWidth', value: String(testEl.offsetWidth) });
    rows.push({ label: 'test clientWidth', value: String(testEl.clientWidth) });
    rows.push({ label: 'test rect.width', value: fmtPx(rect.width) });
    rows.push({
      label: 'test rect/offset ratio',
      value: testEl.offsetWidth ? (rect.width / testEl.offsetWidth).toFixed(4) : 'n/a',
    });
  } else {
    rows.push({ label: 'test element', value: 'missing' });
  }

  pushElDiag(rows, 'chat-root', root);
  pushElDiag(rows, '#root', appRoot);
  pushElDiag(rows, 'body', body);
  pushElDiag(rows, 'html', html);

  rows.push({ label: 'chat-root descendant count', value: String(countDescendants(root)) });
  rows.push({ label: 'mounted transcript descendant count', value: String(countDescendants(transcriptEl)) });
  rows.push({ label: 'loaded message count', value: String(counts.loadedCount) });
  rows.push({ label: 'mounted message count', value: String(counts.mountedCount) });
  rows.push({ label: 'loaded transcript logical count', value: String(counts.loadedCount) });
  if (counts.window) {
    rows.push({ label: 'legacy window start index', value: String(counts.window.start) });
    rows.push({ label: 'legacy window end index', value: String(counts.window.end) });
    rows.push({ label: 'legacy window size', value: String(windowSize(counts.window)) });
  } else {
    rows.push({ label: 'legacy window', value: 'n/a (modern full render)' });
  }

  if (root) {
    rows.push({ label: 'Chat root computed font-size', value: getComputedStyle(root).fontSize });
  }

  return rows;
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
  window: 'M3 7h5v12H3z|M16 7h5v12h-5z|M3 7h18',
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
  const [settings, setSettings] = useState<ChatPrefs>(loadChatPrefs);
  const legacyCompat = useMemo(() => getLegacyNativeCompatDetails().legacyNativeCompat, []);
  const [warmSnapshot] = useState(() => readChatWarmReturn(legacyCompat));
  const [wide, setWide] = useState(() => window.innerWidth >= 900);
  const [compactToolbar, setCompactToolbar] = useState(() => window.innerWidth <= 360);
  const [genLockBusy, setGenLockBusy] = useState(false);

  const [msgs, setMsgs] = useState<ChatMsg[]>(() => warmSnapshot ? warmSnapshot.messages : []);
  const [hasMoreBefore, setHasMoreBefore] = useState(() => warmSnapshot ? warmSnapshot.hasMoreBefore : false);
  const [loadingMore, setLoadingMore] = useState(false);

  const [input, setInput] = useState(readChatComposerDraft);
  const [sending, setSending] = useState(false);
  const [posting, setPosting] = useState(false);
  const [live, setLive] = useState<LiveState | null>(null);
  const [pendingConfirmation, setPendingConfirmation] = useState<ChatToolCall | null>(null);
  const [pendingFile, setPendingFile] = useState<{ fileUrl: string; fileName: string } | null>(null);
  const [pendingImage, setPendingImage] = useState<File | null>(null);

  const [navOpen, setNavOpen] = useState<null | 'wrench' | 'font' | 'search' | 'profile'>(null);
  const [searchQ, setSearchQ] = useState('');
  const [modelPopOpen, setModelPopOpen] = useState(false);
  const [attachMenuOpen, setAttachMenuOpen] = useState(false);
  const [models, setModels] = useState<ModelCatalogEntry[]>([]);
  const [currentModel, setCurrentModel] = useState('');
  const [chatProvider, setChatProvider] = useState<'api_relay' | 'claude_code' | ''>('');
  const [modelMode, setModelMode] = useState<'default' | 'explicit' | 'unknown' | ''>('');

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
  const [initialHistoryReady, setInitialHistoryReady] = useState(() => warmSnapshot !== null);
  const [refreshing, setRefreshing] = useState(false);
  const [chatError, setChatError] = useState<{ message: string; hint: string } | null>(null);
  const [pickedChoices, setPickedChoices] = useState<Record<number, string>>({});
  const [layoutDiag, setLayoutDiag] = useState<LayoutDiagRow[] | null>(null);
  const [txWin, setTxWin] = useState<TranscriptWindow>(() => warmSnapshot ? { ...warmSnapshot.txWin } : { start: 0, end: 0 });

  const manualWindow = useManualContextWindow();
  const switchBlocked =
    sending || live !== null || genLockBusy || manualWindow.submitting;

  const followLatestRef = useRef(warmSnapshot ? warmSnapshot.followLatest : true);
  const msgsRef = useRef<ChatMsg[]>([]);
  const hasMoreBeforeRef = useRef(false);
  const txWinRef = useRef<TranscriptWindow>({ start: 0, end: 0 });
  const warmRestoreRef = useRef(warmSnapshot);
  const pendingAnchorIdRef = useRef<number | null>(null);
  const pendingJumpIdRef = useRef<number | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  const chatRootRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const imgInputRef = useRef<HTMLInputElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const mountedRef = useRef(true);
  const liveRef = useRef<LiveState | null>(null);
  const postingRef = useRef(false);
  const warmUpInflightRef = useRef<Promise<void> | null>(null);
  const coldStartRaceRef = useRef<ColdStartRaceState>(createColdStartRaceState());
  const composerMutationRevisionRef = useRef(0);
  const composerDraftRevisionRef = useRef(0);
  const uploadCoordinatorRef = useRef(new ComposerUploadCoordinator(
    () => composerMutationRevisionRef.current,
    (file) => {
      setPendingFile(file);
      setPendingImage(null);
    },
  ));

  const effThinkMode = settings.thinkMode === 'auto' ? (wide ? 'inline' : 'drawer') : settings.thinkMode;

  msgsRef.current = msgs;
  hasMoreBeforeRef.current = hasMoreBefore;
  txWinRef.current = txWin;

  const handleTranscriptScroll = useCallback((event: UIEvent<HTMLDivElement>) => {
    const target = event.currentTarget;
    followLatestRef.current = followLatestFromGeometry(
      {
        scrollHeight: target.scrollHeight,
        scrollTop: target.scrollTop,
        clientHeight: target.clientHeight,
      },
      !legacyCompat || isTranscriptWindowAtLatest(txWinRef.current, msgsRef.current.length),
    );
  }, [legacyCompat]);

  const placeholder = useMemo(() => chatPlaceholder(new Date()), []);
  const capacityLabel = useMemo(
    () => formatCapacityLabel(findLatestRoundContext(msgs)),
    [msgs],
  );
  const capacityTitle = '上下文容量；90k 为 soft Swap 门槛，另有 30 轮触发';
  const dateLabel = useMemo(() => {
    const now = new Date();
    const dows = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
    return `今天 · ${now.getMonth() + 1}月${now.getDate()}日 ${dows[now.getDay()]}`;
  }, []);

  const patchSettings = (p: Partial<ChatPrefs>) => {
    const merged = patchChatSettings(p);
    setSettings({ fontStep: merged.fontStep, thinkMode: merged.thinkMode });
  };

  const refreshLayoutDiag = useCallback(() => {
    setLayoutDiag(collectLayoutDiagnostics(chatRootRef.current, scrollRef.current, {
      loadedCount: msgs.length,
      mountedCount: legacyCompat ? windowSize(txWin) : msgs.length,
      window: legacyCompat ? txWin : null,
    }));
  }, [msgs.length, legacyCompat, txWin]);

  const pinTranscriptToLatest = useCallback(() => {
    followLatestRef.current = true;
    if (legacyCompat) setTxWin(latestTranscriptWindow(msgs.length));
  }, [legacyCompat, msgs.length]);

  const runHiddenTranscriptThemeProbe = useCallback(() => {
    const root = chatRootRef.current;
    const scroll = scrollRef.current;
    if (!root || !scroll) return;

    const originalMode = loadChatSettings().theme;
    const effective = resolveEffectiveTheme(originalMode);
    const probeTarget: EffectiveTheme = effective === 'dark' ? 'light' : 'dark';
    const prevDisplay = scroll.style.display;

    let unsub: (() => void) | null = null;
    let restored = false;

    const restore = () => {
      if (restored) return;
      restored = true;
      unsub?.();
      unsub = null;
      try {
        setSkipThemePerf(true);
        setChatTheme(root, originalMode);
      } catch {
        /* fail-safe: still restore DOM/mode below */
      } finally {
        setSkipThemePerf(false);
        scroll.style.display = prevDisplay;
        setThemeProbeMode('normal');
      }
    };

    try {
      scroll.style.display = 'none';
      setThemeProbeMode('transcript-hidden');

      unsub = subscribeThemePerf((record) => {
        if (record.mode !== 'transcript-hidden') return;
        restore();
      });

      setChatTheme(root, probeTarget as ThemeMode);
    } catch {
      restore();
    }
  }, []);

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

  const bumpHistoryGen = useCallback(() => bumpHistoryGenState(coldStartRaceRef.current), []);

  const cancelInFlightWarmUp = useCallback(() => {
    cancelInFlightWarmUpState(coldStartRaceRef.current);
    warmUpInflightRef.current = null;
  }, []);

  const applyCatalog = useCallback((r: ChatModelCatalog) => {
    setModels(r.models);
    setChatProvider(r.provider);
    setModelMode(r.modelMode);
    setCurrentModel(r.configuredModel || r.current || '');
  }, []);

  const startModelCatalog = useCallback(() => {
    markChatColdStart('catalog_start');
    void ensureModelCatalog().then((r) => {
      markChatColdStart('catalog_ready');
      if (!mountedRef.current) return;
      applyCatalog(r);
    });
  }, [applyCatalog]);

  const runLegacyWarmUp = useCallback(async (anchorGen: number, earliestId: number) => {
    const race = coldStartRaceRef.current;
    if (!legacyCompat || race.warmUpSatisfied) return;
    if (warmUpInflightRef.current) return warmUpInflightRef.current;

    const warmGen = cancelInFlightWarmUpState(race);
    markChatColdStart('background_warm_start');

    const task = (async () => {
      const warmPage = await fetchChatMessagesOrNull({
        before: earliestId,
        limit: CHAT_LEGACY_WARMUP_LIMIT,
      });
      if (!warmPage) return;
      if (warmGen !== coldStartRaceRef.current.warmUpGen) return;
      if (anchorGen !== coldStartRaceRef.current.historyGen) return;
      if (!followLatestRef.current) return;
      if (!mountedRef.current) return;

      const { mergedCount } = planWarmUpCommit(msgsRef.current, warmPage.messages);

      setMsgs((latest) => {
        if (!mountedRef.current) return latest;
        if (anchorGen !== coldStartRaceRef.current.historyGen) return latest;
        if (!followLatestRef.current) return latest;
        return mergeOlderChatMessages(latest, warmPage.messages);
      });

      if (!mountedRef.current) return;
      if (anchorGen !== coldStartRaceRef.current.historyGen) return;
      if (!followLatestRef.current) return;

      setHasMoreBefore(warmPage.hasMoreBefore);
      if (shouldMarkWarmUpSatisfiedAfterPage(mergedCount, warmPage.hasMoreBefore)) {
        markWarmUpSatisfiedState(coldStartRaceRef.current);
      }
      markChatColdStart('background_warm_ready');
    })().finally(() => {
      if (warmUpInflightRef.current === task) warmUpInflightRef.current = null;
    });

    warmUpInflightRef.current = task;
    return task;
  }, [legacyCompat]);

  const ensureDeferredColdStartInit = useCallback((opts: {
    anchorGen?: number;
    earliestId?: number;
    loadedCount: number;
  }) => {
    if (!mountedRef.current) return;
    if (!tryConsumeDeferredInit(coldStartRaceRef.current)) return;
    markChatColdStart('first_history_paint_scheduled');
    setInitialHistoryReady(true);
    startModelCatalog();
    if (
      opts.earliestId
      && needsLegacyWarmUp(legacyCompat, coldStartRaceRef.current, opts.loadedCount)
    ) {
      void runLegacyWarmUp(opts.anchorGen ?? coldStartRaceRef.current.historyGen, opts.earliestId);
    }
  }, [legacyCompat, startModelCatalog, runLegacyWarmUp]);

  const refetchLatest = useCallback(async (toBottom = true) => {
    const gen = bumpHistoryGen();
    cancelInFlightWarmUp();
    const page = await fetchChatMessages({ limit: CHAT_AUTHORITATIVE_LIMIT });
    if (gen !== coldStartRaceRef.current.historyGen) return;
    if (!mountedRef.current) return;
    onAuthoritativeHistorySuccess(coldStartRaceRef.current, page.messages.length);
    setMsgs(page.messages);
    setHasMoreBefore(page.hasMoreBefore);
    scheduleAfterFirstPaint(() => {
      if (!mountedRef.current) return;
      ensureDeferredColdStartInit({ loadedCount: page.messages.length });
    });
    if (toBottom) scrollBottom();
  }, [scrollBottom, bumpHistoryGen, cancelInFlightWarmUp, ensureDeferredColdStartInit]);

  const revalidateWarmReturn = useCallback(async () => {
    const gen = bumpHistoryGen();
    cancelInFlightWarmUp();
    try {
      const page = await fetchChatMessages({ limit: CHAT_AUTHORITATIVE_LIMIT });
      if (gen !== coldStartRaceRef.current.historyGen) return;
      if (!mountedRef.current) return;
      onAuthoritativeHistorySuccess(coldStartRaceRef.current, page.messages.length);
      const reconciled = reconcileChatWarmReturn(
        { messages: msgsRef.current, hasMoreBefore: hasMoreBeforeRef.current },
        page,
      );
      setMsgs(reconciled.messages);
      setHasMoreBefore(reconciled.hasMoreBefore);
      if (followLatestRef.current) scrollBottom();
      scheduleAfterFirstPaint(() => {
        if (!mountedRef.current) return;
        ensureDeferredColdStartInit({ loadedCount: page.messages.length });
      });
    } catch {
      // Keep the already-painted snapshot when silent revalidation fails.
    }
  }, [scrollBottom, bumpHistoryGen, cancelInFlightWarmUp, ensureDeferredColdStartInit]);

  const flushLegacyWarmUp = useCallback(async () => {
    const race = coldStartRaceRef.current;
    if (!legacyCompat || race.warmUpSatisfied || hasAuthoritativeCoverage(msgs.length)) return;
    const earliestId = msgs[0]?.id;
    if (!earliestId) return;
    await runLegacyWarmUp(race.historyGen, earliestId);
  }, [legacyCompat, msgs, runLegacyWarmUp]);

  const toggleModelPop = useCallback(() => {
    setModelPopOpen((open) => {
      const next = !open;
      if (next) startModelCatalog();
      return next;
    });
  }, [startModelCatalog]);

  const toggleSearchNav = useCallback(() => {
    setNavOpen((open) => {
      const next = open === 'search' ? null : 'search';
      if (next === 'search' && legacyCompat) void flushLegacyWarmUp();
      return next;
    });
  }, [legacyCompat, flushLegacyWarmUp]);

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
      pinTranscriptToLatest();
      await refetchLatest();
      const online = await fetchChatGatewayOnline();
      setEndpointOnline(online);
      if (unlock.ok && !unlock.busy) showToast('已刷新');
      else if (unlock.busy) showToast('锁仍占用，消息已刷新');
      else showToast('已刷新（解锁请求失败）');
    } finally {
      setRefreshing(false);
    }
  }, [refreshing, refetchLatest, showToast, pinTranscriptToLatest]);

  // Cold start: history first; a valid warm snapshot skips only the visible cold path.
  useEffect(() => {
    let cancelled = false;
    if (warmSnapshot) {
      scheduleAfterFirstPaint(() => {
        if (cancelled || !mountedRef.current) return;
        ensureDeferredColdStartInit({ loadedCount: msgsRef.current.length });
        void revalidateWarmReturn();
      });
      return () => { cancelled = true; };
    }

    const gen = bumpHistoryGen();
    cancelInFlightWarmUp();
    markChatColdStart('chat_mount');
    markChatColdStart('initial_history_start');

    const loadInitial = async () => {
      const limit = legacyCompat ? CHAT_LEGACY_INITIAL_LIMIT : CHAT_AUTHORITATIVE_LIMIT;
      const page = await fetchChatMessages({ limit });
      if (cancelled || !mountedRef.current) return;
      const superseded = gen !== coldStartRaceRef.current.historyGen;
      if (!superseded) {
        if (hasAuthoritativeCoverage(page.messages.length)) {
          onAuthoritativeHistorySuccess(coldStartRaceRef.current, page.messages.length);
        }
        if (!mountedRef.current) return;
        setMsgs(page.messages);
        setHasMoreBefore(page.hasMoreBefore);
        scrollBottom();
        markChatColdStart('initial_history_ready');
        scheduleAfterFirstPaint(() => {
          if (cancelled || !mountedRef.current) return;
          ensureDeferredColdStartInit({
            anchorGen: gen,
            earliestId: page.messages[0]?.id,
            loadedCount: page.messages.length,
          });
        });
      }
    };
    void loadInitial();
    return () => { cancelled = true; };
  }, [
    warmSnapshot, legacyCompat, scrollBottom, bumpHistoryGen, cancelInFlightWarmUp,
    ensureDeferredColdStartInit, revalidateWarmReturn,
  ]);

  // media listeners (layout only — theme uses DOM data-chat-theme, not parent state)
  useEffect(() => {
    const onRs = () => {
      setWide(window.innerWidth >= 900);
      setCompactToolbar(window.innerWidth <= 360);
    };
    window.addEventListener('resize', onRs);
    return () => window.removeEventListener('resize', onRs);
  }, []);

  useLayoutEffect(() => {
    const root = chatRootRef.current;
    if (!root) return;
    return attachChatTheme(root);
  }, []);

  useLayoutEffect(() => {
    const root = chatRootRef.current;
    if (!root) return;
    if (legacyCompat) {
      root.setAttribute('data-chat-legacy-renderer', 'true');
    } else {
      root.removeAttribute('data-chat-legacy-renderer');
    }
  }, [legacyCompat]);

  // Legacy DOM window: follow latest or clamp when loaded msgs length changes.
  useLayoutEffect(() => {
    if (!legacyCompat) return;
    setTxWin((w) => (
      followLatestRef.current
        ? latestTranscriptWindow(msgs.length)
        : clampTranscriptWindow(w.start, w.end, msgs.length)
    ));
  }, [msgs.length, legacyCompat]);

  // Deterministic scroll after window shift / search jump (post-commit).
  useLayoutEffect(() => {
    if (!legacyCompat) return;
    const jumpId = pendingJumpIdRef.current;
    if (jumpId != null) {
      pendingJumpIdRef.current = null;
      pendingAnchorIdRef.current = null;
      const el = document.getElementById(`msg-${jumpId}`);
      const c = scrollRef.current;
      if (el && c) c.scrollTo({ top: Math.max(0, el.offsetTop - 80), behavior: 'smooth' });
      return;
    }
    const anchorId = pendingAnchorIdRef.current;
    if (anchorId == null) return;
    pendingAnchorIdRef.current = null;
    const el = document.getElementById(`msg-${anchorId}`);
    const c = scrollRef.current;
    if (el && c) c.scrollTop = Math.max(0, el.offsetTop - 80);
  }, [txWin, msgs, legacyCompat]);

  useLayoutEffect(() => {
    const snapshot = warmRestoreRef.current;
    if (!snapshot) return;
    warmRestoreRef.current = null;
    const container = scrollRef.current;
    if (!container) return;
    if (snapshot.followLatest) container.scrollTop = container.scrollHeight;
    else container.scrollTop = snapshot.scrollTop;
  }, []);

  useLayoutEffect(() => {
    const textarea = taRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 120)}px`;
  }, [input]);

  useEffect(() => {
    if (!initialHistoryReady) return;
    let cancelled = false;
    const pollLock = async () => {
      try {
        const resp = await fetch('/api/gw/chat/lock', { credentials: 'include' });
        if (!resp.ok) return;
        const data = (await resp.json()) as { busy?: boolean };
        if (!cancelled) setGenLockBusy(Boolean(data.busy));
      } catch {
        /* ignore */
      }
    };
    void pollLock();
    const id = setInterval(pollLock, 2500);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [initialHistoryReady, sending, live]);

  // gateway reachability — breathing status under the name (first probe after history ready)
  useEffect(() => {
    if (!initialHistoryReady) return;
    let cancelled = false;
    const check = async () => {
      const online = await fetchChatGatewayOnline();
      if (!cancelled) setEndpointOnline(online);
    };
    void check();
    const iv = setInterval(() => { void check(); }, 20000);
    return () => { cancelled = true; clearInterval(iv); };
  }, [initialHistoryReady]);

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
            // Legacy browsing older window: do not yank to latest on background poll.
            if (!legacyCompat || followLatestRef.current) scrollBottom(true);
          }
        });
        return cur;
      });
    }, 10000);
    return () => clearInterval(iv);
  }, [sending, scrollBottom, legacyCompat]);

  // Cold-start lifecycle: setup resets mounted for StrictMode dev replay (setup→cleanup→setup).
  useEffect(() => {
    mountedRef.current = true;

    return () => {
      if (msgsRef.current.length) {
        writeChatWarmReturn({
          legacyCompat,
          messages: msgsRef.current,
          hasMoreBefore: hasMoreBeforeRef.current,
          txWin: txWinRef.current,
          followLatest: followLatestRef.current,
          scrollTop: scrollRef.current?.scrollTop ?? 0,
        });
      }
      mountedRef.current = false;
      cancelInFlightWarmUpState(coldStartRaceRef.current);
      bumpHistoryGenState(coldStartRaceRef.current);
      warmUpInflightRef.current = null;
      abortRef.current?.abort();
      clearTimeout(toastTimer.current);
    };
  }, []);

  const updateLive = useCallback((fn: (l: LiveState) => LiveState) => {
    liveRef.current = fn(liveRef.current ?? { thinking: '', text: '', tools: [], phase: 'wait' });
    setLive(liveRef.current);
  }, []);

  const runStream = useCallback(
    async (
      userMessageId: number | null,
      opts: {
        rewriteId?: string | null;
        confirmation?: { approvalId: string; decision: 'approve' | 'reject' };
      } = {},
    ): Promise<boolean> => {
      // Invariant: any path entering live streaming pins the DOM window to latest
      // so live replies never render under an old browsing window.
      pinTranscriptToLatest();
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
              tools[idx] = normalizeToolCall({ ...tools[idx], ...tc, running: false });
              return { ...l, tools };
            }),
          onNotice: (s) => showToast(s),
        },
        ctrl,
        {
          rewriteId: opts.rewriteId,
          approvalId: opts.confirmation?.approvalId,
          confirmationDecision: opts.confirmation?.decision,
        },
      );
      if (res.deferredTool) {
        setPendingConfirmation({
          ...res.deferredTool,
          running: false,
          confirmation_state: 'pending',
        });
      }
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
    [scrollBottom, showToast, updateLive, pinTranscriptToLatest],
  );

  const confirmDeferred = useCallback(async (decision: 'approve' | 'reject') => {
    const pending = pendingConfirmation;
    const approvalId = pending?.approval_id;
    if (!pending || !approvalId || sending || pending.confirmation_state === 'processing') return;
    setPendingConfirmation({ ...pending, confirmation_state: 'processing', running: false });
    setSending(true);
    setChatError(null);
    const ok = await runStream(null, {
      confirmation: { approvalId, decision },
    });
    if (decision === 'reject') {
      setPendingConfirmation({ ...pending, confirmation_state: 'rejected', running: false });
    } else if (ok) {
      await refetchLatest();
      setPendingConfirmation(null);
    } else {
      setPendingConfirmation({ ...pending, confirmation_state: 'pending', running: false });
    }
    setSending(false);
  }, [pendingConfirmation, sending, runStream, refetchLatest]);

  const send = useCallback(async () => {
    const rawText = input;
    const sendText = rawText.trim();
    const attempt = {
      rawText,
      text: sendText,
      file: pendingFile,
      image: pendingImage,
    };
    if ((!attempt.text && !attempt.file && !attempt.image) || sending) return;
    const draftRevisionAtConsume = composerDraftRevisionRef.current;
    clearChatComposerDraft();
    setSending(true);
    setChatError(null);
    setInput('');
    if (taRef.current) taRef.current.style.height = 'auto';
    const extra = attempt.image
      ? { imageFile: attempt.image }
      : attempt.file
        ? { fileUrl: attempt.file.fileUrl, fileName: attempt.file.fileName }
        : {};
    postingRef.current = true;
    composerMutationRevisionRef.current += 1;
    setPosting(true);
    let messageId: number | null = null;
    try {
      messageId = await sendChatMessage(attempt.text, extra);
    } finally {
      postingRef.current = false;
      setPosting(false);
    }
    if (messageId === null) {
      showToast('发送失败');
      if (composerDraftRevisionRef.current === draftRevisionAtConsume) {
        setInput(rawText);
        writeChatComposerDraft(rawText);
      }
      setSending(false);
      return;
    }
    setPendingFile((current) => (current === attempt.file ? null : current));
    setPendingImage((current) => (current === attempt.image ? null : current));
    pinTranscriptToLatest();
    await refetchLatest();
    await runStream(messageId);
    await refetchLatest();
    setSending(false);
    taRef.current?.focus();
  }, [input, pendingFile, pendingImage, sending, refetchLatest, runStream, showToast, pinTranscriptToLatest]);
  const sendChoice = useCallback(async (text: string): Promise<boolean> => {
    const choice = text.trim();
    if (!choice || sending) return false;
    setSending(true);
    setChatError(null);
    uploadCoordinatorRef.current.beginChoicePost();
    postingRef.current = true;
    setPosting(true);
    let messageId: number | null = null;
    try {
      messageId = await sendChatMessage(choice);
    } finally {
      postingRef.current = false;
      setPosting(false);
      uploadCoordinatorRef.current.endChoicePost();
    }
    if (messageId === null) {
      showToast('发送失败');
      setSending(false);
      return false;
    }
    pinTranscriptToLatest();
    await refetchLatest();
    await runStream(messageId);
    await refetchLatest();
    setSending(false);
    return true;
  }, [sending, refetchLatest, runStream, showToast, pinTranscriptToLatest]);

  const chooseOption = useCallback(async (text: string, msgId: number) => {
    if (sending || isChoicesAnswered(msgId, msgs)) return;
    setPickedChoices((prev) => ({ ...prev, [msgId]: text }));
    const sent = await sendChoice(text);
    if (!sent) {
      setPickedChoices((prev) => {
        const next = { ...prev };
        delete next[msgId];
        return next;
      });
    }
  }, [msgs, sendChoice, sending]);

  const redo = useCallback(
    async (msgId: number) => {
      if (sending) return;
      setSending(true);
      setChatError(null);
      const prep = await regenPrepare(msgId);
      if (prep === null) {
        showToast('重答准备失败');
        setSending(false);
        return;
      }
      // Keep old assistant visible until candidate activates.
      // runStream pins latest before live mounts (mutation invariant).
      const ok = await runStream(prep.userMessageId, { rewriteId: prep.rewriteId });
      if (ok) {
        // finalize retries transport-ambiguous / effects_pending internally (same rewrite_id).
        const fin = await regenFinalize(prep.rewriteId);
        if (!fin) showToast('重答结果未确认，正在刷新…');
        else if (fin.effectsPending) showToast('重答已切换，收尾未完成，可再试一次');
      }
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
      setChatError(null);
      setEditingId(null);
      const edit = await editChatMessage(msgId, content);
      if (!edit.ok || !edit.rewriteId) {
        showToast('修改失败');
        setSending(false);
        return;
      }
      // Active transcript stays intact until finalize succeeds.
      // runStream pins latest before live mounts (mutation invariant).
      const ok = await runStream(null, { rewriteId: edit.rewriteId });
      if (ok) {
        // finalize retries transport-ambiguous / effects_pending internally (same rewrite_id).
        const fin = await editFinalize(edit.rewriteId);
        if (!fin.ok) {
          showToast(
            fin.effectsPending
              ? '修改已切换，收尾未完成，可再试一次'
              : '修改结果未确认，正在刷新…',
          );
        } else if (fin.effectsPending) {
          showToast('修改已切换，收尾未完成，可再试一次');
        }
      }
      await refetchLatest();
      setSending(false);
    },
    [editText, sending, refetchLatest, runStream, showToast],
  );

  const branchSwitch = useCallback(
    async (msgId: number, dir: 1 | -1) => {
      const r = await switchChatBranch(msgId, dir);
      if (!r) return;
      // Authoritative msgs replacement invalidates old index windows on legacy.
      if (legacyCompat) pinTranscriptToLatest();
      await refetchLatest(false);
    },
    [refetchLatest, legacyCompat, pinTranscriptToLatest],
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
    const gen = bumpHistoryGen();
    cancelInFlightWarmUp();
    setLoadingMore(true);
    try {
      const page = await fetchChatMessages({ before: msgs[0].id, limit: CHAT_AUTHORITATIVE_LIMIT });
      if (gen !== coldStartRaceRef.current.historyGen) return;
      const prepended = page.messages.length;
      const newTotal = msgs.length + prepended;
      if (legacyCompat) {
        followLatestRef.current = false;
        pendingAnchorIdRef.current = msgs[0]?.id ?? null;
        setTxWin(transcriptWindowAfterPrepend(prepended, newTotal));
      }
      setMsgs((cur) => [...page.messages, ...cur]);
      setHasMoreBefore(page.hasMoreBefore);
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, msgs, legacyCompat, bumpHistoryGen, cancelInFlightWarmUp]);

  const showEarlierLoaded = useCallback(() => {
    if (!legacyCompat) {
      void loadEarlier();
      return;
    }
    if (txWin.start > 0) {
      pendingAnchorIdRef.current = msgs[txWin.start]?.id ?? null;
      followLatestRef.current = false;
      setTxWin((w) => shiftTranscriptWindowOlder(w, msgs.length));
      return;
    }
    if (hasMoreBefore) void loadEarlier();
  }, [legacyCompat, txWin.start, msgs, hasMoreBefore, loadEarlier]);

  const showNewerLoaded = useCallback(() => {
    if (!legacyCompat) return;
    const next = shiftTranscriptWindowNewer(txWin, msgs.length);
    followLatestRef.current = isTranscriptWindowAtLatest(next, msgs.length);
    if (followLatestRef.current) {
      setTxWin(latestTranscriptWindow(msgs.length));
      scrollBottom();
      return;
    }
    pendingAnchorIdRef.current = msgs[Math.max(txWin.start, next.start)]?.id ?? null;
    setTxWin(next);
  }, [legacyCompat, txWin, msgs, scrollBottom]);

  const goToLatestWindow = useCallback(() => {
    pinTranscriptToLatest();
    scrollBottom();
  }, [pinTranscriptToLatest, scrollBottom]);

  const jumpTo = useCallback((id: number) => {
    setNavOpen(null);
    setFlashId(id);
    if (legacyCompat) {
      const idx = msgs.findIndex((m) => m.id === id);
      if (idx < 0) {
        setTimeout(() => setFlashId((f) => (f === id ? null : f)), 2200);
        return;
      }
      const next = transcriptWindowAroundIndex(idx, msgs.length);
      // Intent-based: geometric "window touches tail" must not auto-follow.
      followLatestRef.current = followLatestAfterSearchJump(idx, msgs.length);
      pendingJumpIdRef.current = id;
      setTxWin(next);
    } else {
      setTimeout(() => {
        const el = document.getElementById(`msg-${id}`);
        const c = scrollRef.current;
        if (el && c) c.scrollTo({ top: Math.max(0, el.offsetTop - 80), behavior: 'smooth' });
      }, 250);
    }
    setTimeout(() => setFlashId((f) => (f === id ? null : f)), 2200);
  }, [legacyCompat, msgs]);

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
      if (!f || postingRef.current) return;
      const mutationRevision = composerMutationRevisionRef.current;
      const uploaded = await uploadCoordinatorRef.current.settle(
        uploadChatFile(f),
        mutationRevision,
      );
      if (!uploaded) showToast('上传失败（只收 2MB 内文本类文件）');
    },
    [showToast],
  );

  const segStyle = (on: boolean): CSSProperties => ({
    flex: 1, textAlign: 'center', padding: '8px 0', borderRadius: 999, fontSize: 13, cursor: 'pointer', transition: 'all .2s',
    background: on ? 'var(--card)' : 'transparent', color: on ? 'var(--deep)' : 'var(--mut)',
    boxShadow: on ? '0 4px 10px var(--shadow)' : 'none',
  });

  const modelBadge = useMemo(() => {
    if (chatProvider === 'claude_code') {
      if (modelMode === 'explicit' && currentModel) {
        const hit = models.find((m) => m.id === currentModel);
        return `Claude Code · ${hit?.label || currentModel}`;
      }
      if (modelMode === 'default') return 'Claude Code · 默认';
      return 'Claude Code · 读取中…';
    }
    const hit = models.find((m) => m.id === currentModel);
    return hit?.label || currentModel.replace(/^.*\]\s*/, '').slice(0, 22) || '模型';
  }, [models, currentModel, chatProvider, modelMode]);

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
      <div className="vstack vstack-8">
        <div onClick={onClick} className="hstack hstack-8" style={{ cursor: 'pointer', color: 'var(--faint)' }}>
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

  function renderArtifactCard(key: string, tc: ChatToolCall) {
    const art = tc.artifact!;
    const previewable = art.type === 'html' || art.type === 'markdown';
    const href = previewable
      ? `/api/artifacts/${encodeURIComponent(String(art.id))}/preview`
      : `/api/artifacts/${encodeURIComponent(String(art.id))}/download`;
    return (
      <div key={key} className="chat-artifact-card">
        <div className="chat-artifact-icon">{artifactTypeIcon(art.type)}</div>
        <div className="chat-artifact-body">
          <div className="chat-artifact-title">{art.title || '未命名'}</div>
          <div className="chat-artifact-meta">
            {artifactTypeLabel(art.type)}
            {art.size ? ` · ${fmtArtifactSize(art.size)}` : ''}
          </div>
        </div>
        <a className="chat-artifact-action" href={href} target="_blank" rel="noopener noreferrer">
          {previewable ? '打开' : '下载'}
        </a>
      </div>
    );
  }

  function renderChoices(m: ChatMsg) {
    if (!m.choices?.length) return null;
    const answered = isChoicesAnswered(m.id, msgs);
    const picked = pickedChoices[m.id];
    return (
      <div className="chat-choices">
        {m.choices.map((opt, i) => {
          const isPicked = picked === opt;
          const disabled = answered || (Boolean(picked) && !isPicked);
          return (
            <button
              key={`${m.id}-choice-${i}`}
              type="button"
              className={`chat-choice-btn${isPicked ? ' picked' : ''}${disabled ? ' disabled' : ''}`}
              disabled={disabled || sending}
              onClick={() => chooseOption(opt, m.id)}
            >
              {opt}
            </button>
          );
        })}
      </div>
    );
  }

  function renderToolItems(keyPrefix: string, tools: ChatToolCall[]) {
    if (!tools.length) return null;
    return (
      <div className="vstack vstack-8">
        {tools.map((tc, i) =>
          tc.artifact
            ? renderArtifactCard(`${keyPrefix}-artifact-${i}`, tc)
            : renderToolCard(`${keyPrefix}-tool-${i}`, tc),
        )}
      </div>
    );
  }

  function renderToolCard(key: string, tc: ChatToolCall) {
    const open = Boolean(openTools[key]);
    const waiting = tc.deferred_tool_use === true && tc.status === 'waiting_for_confirmation';
    const processing = tc.confirmation_state === 'processing';
    const rejected = tc.confirmation_state === 'rejected';
    const outStr = typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result ?? '', null, 2);
    const inStr = typeof tc.args === 'string' ? tc.args : JSON.stringify(tc.args ?? {}, null, 2);
    return (
      <div key={key} style={{ background: 'var(--card)', borderRadius: 14, boxShadow: '0 6px 16px var(--shadow)', overflow: 'hidden' }}>
        <div onClick={() => setOpenTools((o) => ({ ...o, [key]: !o[key] }))} className="hstack hstack-10" style={{ cursor: 'pointer', padding: '11px 14px' }}>
          <span style={{ color: 'var(--faint)', flexShrink: 0, display: 'flex' }}>
            <Svg d={IC.tool} size={14} sw={1.8} />
          </span>
          <span style={{ fontFamily: FONT_MONO, fontSize: 12.5, color: 'var(--ink2)' }}>{tc.name || 'tool'}</span>
          {tc.running && <span style={{ width: 13, height: 13, borderRadius: '50%', border: '2px solid var(--rosebg)', borderTopColor: 'var(--rose)', animation: 'chatSpin .8s linear infinite', flexShrink: 0 }} />}
          {!tc.running && tc.success !== false && <span style={{ color: 'var(--ok)', fontSize: 13 }}>✓</span>}
          {!tc.running && tc.success === false && <span style={{ color: 'var(--err)', fontSize: 13 }}>✗</span>}
          {tc.caption && <span style={{ fontSize: 11.5, color: 'var(--ghost)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{tc.caption}</span>}
          <svg viewBox="0 0 24 24" width={12} height={12} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" style={{ marginLeft: 'auto', color: 'var(--ghost)', transition: 'transform .2s', transform: `rotate(${open ? 180 : 0}deg)`, flexShrink: 0 }}>
            <path d="M6 9l6 6 6-6" />
          </svg>
        </div>
        {waiting && (
          <div className="vstack vstack-9" style={{ padding: '0 14px 14px', animation: 'chatFadeIn .2s ease' }}>
            <div style={{ height: 1, background: 'var(--line)' }} />
            <div style={{ fontSize: 13.5, lineHeight: 1.7, color: 'var(--ink2)' }}>{rejected ? '已取消' : tc.approval_prompt}</div>
            {!rejected && (
              <div className="hstack hstack-8">
                <button
                  type="button"
                  disabled={processing || sending}
                  onClick={(e) => { e.stopPropagation(); void confirmDeferred('approve'); }}
                  style={{ border: 'none', borderRadius: 999, padding: '8px 18px', background: 'var(--deep)', color: '#FBF3F0', cursor: processing || sending ? 'default' : 'pointer', opacity: processing || sending ? 0.55 : 1 }}
                >
                  {processing ? '处理中…' : '确认'}
                </button>
                <button
                  type="button"
                  disabled={processing || sending}
                  onClick={(e) => { e.stopPropagation(); void confirmDeferred('reject'); }}
                  style={{ border: '1px solid var(--line)', borderRadius: 999, padding: '8px 18px', background: 'transparent', color: 'var(--mut)', cursor: processing || sending ? 'default' : 'pointer', opacity: processing || sending ? 0.55 : 1 }}
                >
                  不要
                </button>
              </div>
            )}
          </div>
        )}
        {open && (
          <div className="vstack vstack-9" style={{ padding: '0 14px 14px', animation: 'chatFadeIn .2s ease' }}>
            <div style={{ height: 1, background: 'var(--line)' }} />
            <div style={{ fontSize: 11, letterSpacing: 2, color: 'var(--ghost)' }}>入参</div>
            <div style={{ background: 'var(--card2)', borderRadius: 10, padding: '10px 12px', fontFamily: FONT_MONO, fontSize: 11.5, lineHeight: 1.7, color: 'var(--mut)', whiteSpace: 'pre-wrap', overflowX: 'auto', maxHeight: 200, overflowY: 'auto' }}>{inStr}</div>
            <div style={{ fontSize: 11, letterSpacing: 2, color: 'var(--ghost)' }}>出参</div>
            <div style={{ background: 'var(--card2)', borderRadius: 10, padding: '10px 12px', fontFamily: FONT_MONO, fontSize: 11.5, lineHeight: 1.7, color: 'var(--mut)', whiteSpace: 'pre-wrap', overflowX: 'auto', maxHeight: 200, overflowY: 'auto' }}>{String(outStr).slice(0, 4000)}</div>
          </div>
        )}
      </div>
    );
  }

  function renderMarkdown(text: string, caret = false) {
    return (
      <div className={`chat-markdown${caret ? ' chat-markdown-streaming' : ''}`}>
        <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} disallowedElements={['img']} components={markdownComponents}>
          {text}
        </ReactMarkdown>
      </div>
    );
  }
  function renderUserMsg(m: ChatMsg) {
    const editing = editingId === m.id;
    const filePreview = chatFilePreviewUrl(m.fileUrl);
    return (
      <div id={`msg-${m.id}`} className={`chat-msg vstack vstack-7${flashId === m.id ? ' chat-flash' : ''}`} style={{ alignItems: 'flex-end', borderRadius: 16 }}>
        {editing ? (
          <div className="vstack vstack-10" style={{ width: '100%', maxWidth: 520, background: 'var(--card)', borderRadius: 18, boxShadow: '0 10px 30px var(--shadow)', padding: 14 }}>
            <textarea
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              rows={3}
              style={{ width: '100%', border: 'none', background: 'var(--card2)', borderRadius: 12, padding: 12, fontSize: '1em', lineHeight: 1.7, color: 'var(--ink)', resize: 'none', fontFamily: FONT_CN }}
            />
            <div className="hstack hstack-8" style={{ justifyContent: 'flex-end' }}>
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
            <div className="vstack vstack-8" style={{ maxWidth: '82%', background: 'var(--bubble)', borderRadius: '18px 18px 6px 18px', padding: '12px 16px', boxShadow: '0 6px 16px var(--shadow)' }}>
              {(m.fileName || m.imageUrl) && (
                <div className="flex-wrap-gap-6">
                  {m.fileName && (filePreview ? (
                    <a href={filePreview} target="_blank" rel="noopener noreferrer" className="hstack hstack-6" style={{ background: 'var(--card)', borderRadius: 999, padding: '5px 11px', fontSize: 11.5, color: 'var(--ink2)', textDecoration: 'none' }}>
                      <Svg d={IC.clip} size={11} sw={1.8} />
                      {m.fileName}
                    </a>
                  ) : (
                    <span className="hstack hstack-6" style={{ background: 'var(--card)', borderRadius: 999, padding: '5px 11px', fontSize: 11.5, color: 'var(--ink2)' }}>
                      <Svg d={IC.clip} size={11} sw={1.8} />
                      {m.fileName}
                    </span>
                  ))}
                  {m.imageUrl && <img src={m.imageUrl} alt="" style={{ maxWidth: 200, maxHeight: 200, borderRadius: 12, objectFit: 'cover' }} />}
                </div>
              )}
              {m.text && <span style={{ fontSize: '1em', lineHeight: 1.75, letterSpacing: 0.3, color: 'var(--ink)', whiteSpace: 'pre-wrap' }}>{m.text}</span>}
            </div>
            <div className="hstack hstack-4">
              <div className="chat-msg-acts hstack hstack-4">
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
              <span style={{ fontFamily: FONT_DISPLAY, fontSize: 11, color: 'var(--ghost)', letterSpacing: 1 }}>{m.ts}</span>
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
      <div id={`msg-${m.id}`} className={`chat-msg vstack vstack-12${flashId === m.id ? ' chat-flash' : ''}`} style={{ borderRadius: 16 }}>
        {renderThinkBlock(m)}
        {renderToolItems(String(m.id), m.toolCalls)}
        {m.imageUrl && <img src={m.imageUrl} alt="" style={{ maxWidth: 240, borderRadius: 14 }} />}
        {m.text && renderMarkdown(m.text)}
        {renderChoices(m)}
        <div className="vstack vstack-7">
          <span style={{ fontFamily: FONT_DISPLAY, fontSize: 11, color: 'var(--ghost)', letterSpacing: 1, padding: '0 2px' }}>{m.ts}</span>
          <div className="flex-wrap-gap-2">
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
              <div className="flex-wrap-gap-y3-x7" style={{ color: 'var(--ghost)', fontSize: 11.5, padding: '0 2px' }}>
                <span className="hstack hstack-3">
                  <Svg d={IC.up} size={11} sw={1.8} />
                  <span style={{ fontFamily: FONT_DISPLAY }}>{fmtTokens(usage.inputTokens || 0)}</span>
                </span>
                <span>·</span>
                <span className="hstack hstack-3">
                  <Svg d={IC.down} size={11} sw={1.8} />
                  <span style={{ fontFamily: FONT_DISPLAY }}>{fmtTokens(usage.outputTokens || 0)}</span>
                </span>
                {Boolean(usage.elapsedSec) && (
                  <>
                    <span>·</span>
                    <span className="hstack hstack-3">
                      <svg viewBox="0 0 24 24" width={11} height={11} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
                        <circle cx={12} cy={12} r={9} />
                        <path d={IC.clock} />
                      </svg>
                      <span style={{ fontFamily: FONT_DISPLAY }}>{Math.round(usage.elapsedSec || 0)}s</span>
                    </span>
                  </>
                )}
                {cache && (
                  <>
                    <span>·</span>
                    <span>{cache}</span>
                  </>
                )}
                {Boolean(usage.costUsd) && (
                  <>
                    <span>·</span>
                    <span style={{ color: 'rgba(170,108,88,.92)', fontFamily: FONT_DISPLAY }} title={usage.costEstimated ? '按费率估算' : '按账单换算'}>
                      {fmtCostUsd(usage.costUsd, usage.costEstimated)}
                    </span>
                  </>
                )}
              </div>
            )}
            {m.branchTotal > 1 && (
              <div className="hstack hstack-2" style={{ background: 'var(--card)', borderRadius: 999, padding: '2px 6px', boxShadow: '0 4px 10px var(--shadow)', marginLeft: 4 }}>
                <span onClick={() => branchSwitch(m.id, -1)} style={{ cursor: 'pointer', padding: '1px 6px', color: m.branchIdx > 0 ? 'var(--mut)' : 'var(--ghost)', fontSize: 14 }}>
                  ‹
                </span>
                <span style={{ fontFamily: FONT_DISPLAY, fontSize: 11.5, color: 'var(--mut)', letterSpacing: 1 }}>
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
      <div className="vstack vstack-12">
        {l.thinking && (
          <>
            <div
              onClick={() => setDrawer({ text: l.thinking, label: '思考中…' })}
              className="hstack hstack-8" style={{ cursor: 'pointer', color: 'var(--faint)' }}
            >
              <span style={{ display: 'flex', animation: l.phase === 'think' ? 'chatBreathe 1.6s ease-in-out infinite' : 'none' }}>
                <Svg d={IC.brain} size={17} sw={1.5} />
              </span>
              <span style={{ fontSize: 13, letterSpacing: 1 }}>{l.phase === 'think' ? '思考中…' : `思考了 ${l.thinking.length} 字`}</span>
            </div>
            {l.phase === 'think' && (
              <div style={{ position: 'relative', height: 76, overflow: 'hidden', borderRadius: 14, background: 'var(--card2)' }}>
                <div className="vstack vstack-4" style={{ position: 'absolute', bottom: 10, left: 16, right: 16 }}>
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
        {renderToolItems('live', l.tools)}
        {l.phase === 'text' ? renderMarkdown(l.text, true) : !l.thinking && !l.tools.length ? (
          <div className="hstack hstack-8" style={{ color: 'var(--faint)', fontSize: 13 }}>
            <span style={{ width: 13, height: 13, borderRadius: '50%', border: '2px solid var(--rosebg)', borderTopColor: 'var(--rose)', animation: 'chatSpin .8s linear infinite' }} />
            正在连接回复…
          </div>
        ) : null}
      </div>
    );
  }

  // Modern: all loaded msgs. Legacy: bounded DOM window only (msgs state stays full).
  const visibleMsgs = legacyCompat ? msgs.slice(txWin.start, txWin.end) : msgs;
  const atLatestWindow = !legacyCompat || isTranscriptWindowAtLatest(txWin, msgs.length);
  const canShowEarlierLoaded = legacyCompat && txWin.start > 0;
  const canFetchEarlier = (!legacyCompat && hasMoreBefore) || (legacyCompat && txWin.start === 0 && hasMoreBefore);
  const canShowNewerLoaded = legacyCompat && !atLatestWindow;

  const rendered: ReactElement[] = [];
  let lastDate = '';
  visibleMsgs.forEach((m) => {
    // First visible message always gets a date separator (even mid-day slice).
    if (m.dateKey && m.dateKey !== lastDate) {
      lastDate = m.dateKey;
      const label = m.dateKey === new Date().toISOString().slice(0, 10) ? dateLabel : m.dateKey.replace(/-/g, '.');
      rendered.push(
        <div key={`d-${m.dateKey}-${m.id}`} style={{ textAlign: 'center', fontFamily: fontFamilyForText(label), fontSize: 12, letterSpacing: 2, color: 'var(--ghost)', padding: '2px 0' }}>
          {label}
        </div>,
      );
    }
    rendered.push(
      <div key={m.id}>
        {m.role === 'user' ? renderUserMsg(m) : renderAssistantMsg(m)}
      </div>,
    );
  });

  const toolbarIcon = compactToolbar ? 32 : 35;
  const modalUiState: SoftWindowUiState =
    manualWindow.uiState === 'probing' || manualWindow.uiState === 'idle'
      ? 'loading'
      : manualWindow.uiState;

  return (
    <div
      ref={chatRootRef}
      className="chat-root app-frame__page"
      style={{
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--bg)',
        color: 'var(--ink)',
        fontFamily: FONT_CN,
        fontSize: FONT_SIZES[settings.fontStep],
      }}
    >
      <div id="c78-layout-test-100" aria-hidden style={{ position: 'absolute', width: 100, height: 1, visibility: 'hidden', pointerEvents: 'none' }} />
      {/* ══ top nav ══ */}
      <div style={{ flexShrink: 0, position: 'relative', zIndex: 40 }}>
        <div style={{ background: 'rgba(255,255,255,0.97)', boxShadow: '0 6px 18px var(--shadow)', position: 'relative', zIndex: 3 }}>
          <div className="page-header-toolbar">
            <div onClick={() => setSidebarOpen(true)} style={{ cursor: 'pointer', width: 38, height: 38, borderRadius: '50%', background: 'linear-gradient(135deg,#B76E79,#9C3B4A)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, boxShadow: '0 6px 14px var(--shadow2)' }}>
              <span style={{ fontFamily: FONT_DISPLAY, fontStyle: 'italic', fontSize: 17, color: '#F7F1EE' }}>Θ</span>
            </div>
            <div className="vstack vstack-1" style={{ minWidth: 0, flexShrink: 1 }}>
              <span style={{ fontFamily: FONT_DISPLAY, fontSize: 17, fontWeight: 600, letterSpacing: 1, color: 'var(--ink)' }}>Fyodor</span>
              <div
                className="hstack hstack-5"
                style={{ minWidth: 0 }}
                title={capacityTitle}
                aria-label={capacityTitle}
              >
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: '50%',
                    flexShrink: 0,
                    background: endpointOnline === false ? 'var(--err)' : endpointOnline ? 'var(--ok)' : 'var(--ghost)',
                    animation: endpointOnline === false
                      ? undefined
                      : endpointOnline
                        ? 'chatBreathe 2.2s ease-in-out infinite'
                        : 'chatBreathe 3s ease-in-out infinite',
                  }}
                />
                <span
                  style={{
                    fontFamily: FONT_DISPLAY,
                    fontStyle: 'italic',
                    fontSize: 10.5,
                    letterSpacing: 0.5,
                    color: 'var(--faint)',
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {capacityLabel}
                </span>
              </div>
            </div>
            <div className="hstack hstack-1" style={{ marginLeft: 'auto', flexShrink: 0 }}>
              <div onClick={() => setNavOpen(navOpen === 'wrench' ? null : 'wrench')} style={{ ...iconBtn, background: navOpen === 'wrench' ? 'var(--rosebg)' : 'transparent' }}>
                <Svg d={IC.wrench} />
              </div>
              <ChatThemeQuickToggle rootRef={chatRootRef} style={iconBtn} />
              <div onClick={() => setNavOpen(navOpen === 'font' ? null : 'font')} style={{ ...iconBtn, background: navOpen === 'font' ? 'var(--rosebg)' : 'transparent' }}>
                <span style={{ fontFamily: FONT_DISPLAY, fontSize: 14, letterSpacing: 0.5 }}>Aa</span>
              </div>
              <div onClick={toggleSearchNav} style={{ ...iconBtn, width: toolbarIcon, height: toolbarIcon, background: navOpen === 'search' ? 'var(--rosebg)' : 'transparent' }}>
                <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round">
                  <circle cx={12} cy={12} r={9} />
                  <path d={IC.clock} />
                </svg>
              </div>
              {manualWindow.enabled ? (
                <button
                  type="button"
                  title="换一扇窗"
                  aria-label="换一扇窗"
                  disabled={switchBlocked}
                  onClick={() => {
                    if (!switchBlocked) void manualWindow.openModal();
                  }}
                  style={{
                    ...iconBtn,
                    width: toolbarIcon,
                    height: toolbarIcon,
                    border: 'none',
                    padding: 0,
                    background: manualWindow.modalOpen ? 'var(--rosebg)' : 'transparent',
                    opacity: switchBlocked ? 0.45 : 1,
                    cursor: switchBlocked ? 'default' : 'pointer',
                  }}
                >
                  <Svg d={IC.window} />
                </button>
              ) : null}
              <div
                onClick={() => { void refreshChat(); }}
                title="刷新并解锁"
                style={{ ...iconBtn, width: toolbarIcon, height: toolbarIcon, opacity: refreshing ? 0.55 : 1, cursor: refreshing ? 'default' : 'pointer' }}
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
            <div onClick={() => setNavOpen(null)} className="c78-fill-fixed" style={{ zIndex: 1, background: 'rgba(40,28,26,0.30)', animation: 'chatFadeIn .2s ease' }} />
            <div style={{ position: 'absolute', top: '100%', left: 0, right: 0, zIndex: 2, animation: 'chatDropIn .22s ease' }}>
              <div style={{ maxWidth: 430, margin: '0 auto', padding: '0 10px' }}>
                <div className="vstack vstack-18" style={{ background: 'var(--card)', borderRadius: '0 0 26px 26px', boxShadow: '0 30px 70px var(--shadow2)', padding: '20px 20px 22px', maxHeight: '72vh', overflowY: 'auto' }}>
                  {navOpen === 'wrench' && (
                    <>
                      <div className="vstack vstack-12">
                        <MixedSectionLabel cn="外观" en="APPEARANCE" />
                        <div className="vstack vstack-8">
                          <span style={{ fontSize: 13.5, color: 'var(--ink2)', letterSpacing: 1 }}>主题</span>
                          <ChatThemeSegmented rootRef={chatRootRef} segStyle={segStyle} />
                        </div>
                      </div>
                      <div style={{ height: 1, background: 'var(--line)' }} />
                      <div className="vstack vstack-12">
                        <MixedSectionLabel cn="对话" en="CONVERSATION" />
                        <div className="vstack vstack-8">
                          <span style={{ fontSize: 13.5, color: 'var(--ink2)', letterSpacing: 1 }}>思维链展开方式</span>
                          <div className="hstack hstack-2" style={{ background: 'var(--card2)', borderRadius: 999, padding: 3 }}>
                            {(['auto', 'drawer', 'inline'] as const).map((t) => (
                              <div key={t} onClick={() => patchSettings({ thinkMode: t })} style={segStyle(settings.thinkMode === t)}>
                                {t === 'auto' ? '自动' : t === 'drawer' ? '抽屉' : '原地展开'}
                              </div>
                            ))}
                          </div>
                          <span style={{ fontSize: 11.5, color: 'var(--ghost)' }}>移动端默认抽屉，桌面端默认原地展开</span>
                        </div>
                      </div>
                      <div style={{ height: 1, background: 'var(--line)' }} />
                      <div className="vstack vstack-12">
                        <MixedSectionLabel cn="诊断" en="LAYOUT (临时)" />
                        <div
                          onClick={refreshLayoutDiag}
                          style={{ ...segStyle(false), flex: 'none', padding: '10px 14px' }}
                        >
                          采集布局读数
                        </div>
                        <div
                          onClick={runHiddenTranscriptThemeProbe}
                          style={{ ...segStyle(false), flex: 'none', padding: '10px 14px' }}
                        >
                          隐藏消息树测试主题
                        </div>
                        <ThemePerfRows />
                        {layoutDiag && (
                          <div className="vstack vstack-6" style={{ background: 'var(--card2)', borderRadius: 14, padding: '12px 14px', fontFamily: FONT_MONO, fontSize: 11, lineHeight: 1.55, color: 'var(--ink2)' }}>
                            {layoutDiag.map((row) => (
                              <div key={row.label} className="hstack hstack-10" style={{ justifyContent: 'space-between' }}>
                                <span style={{ color: 'var(--ghost)', flexShrink: 0 }}>{row.label}</span>
                                <span style={{ textAlign: 'right', wordBreak: 'break-all' }}>{row.value}</span>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    </>
                  )}
                  {navOpen === 'font' && (
                    <div className="vstack vstack-12">
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                        <MixedSectionLabel cn="字号" en="TEXT SIZE" />
                        <span style={{ fontFamily: FONT_DISPLAY, fontSize: 12, color: 'var(--rose)' }}>{FONT_SIZES[settings.fontStep]}px</span>
                      </div>
                      <div className="hstack hstack-12">
                        <span style={{ fontSize: 12, color: 'var(--ghost)' }}>字</span>
                        <input type="range" min={0} max={4} step={1} value={settings.fontStep} onChange={(e) => patchSettings({ fontStep: Number(e.target.value) })} style={{ flex: 1, accentColor: 'var(--rose)' }} />
                        <span style={{ fontSize: 19, color: 'var(--ghost)' }}>字</span>
                      </div>
                      <div style={{ background: 'var(--card2)', borderRadius: 14, padding: '12px 14px', fontSize: '1em', lineHeight: 1.8, color: 'var(--ink2)' }}>灯不关，我看着你读完这一页。</div>
                    </div>
                  )}
                  {navOpen === 'search' && (
                    <div className="vstack vstack-10">
                      <MixedSectionLabel cn="聊天记录" en="HISTORY" />
                      <div className="hstack hstack-10" style={{ background: 'var(--card2)', borderRadius: 999, padding: '11px 16px' }}>
                        <svg viewBox="0 0 24 24" width={15} height={15} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" style={{ color: 'var(--ghost)', flexShrink: 0 }}>
                          <circle cx={11} cy={11} r={7} />
                          <path d="M20 20l-3.5-3.5" />
                        </svg>
                        <input value={searchQ} onChange={(e) => setSearchQ(e.target.value)} placeholder="搜索已加载的对话…" style={{ flex: 1, border: 'none', background: 'transparent', fontSize: 14, color: 'var(--ink)', minWidth: 0, fontFamily: FONT_CN, outline: 'none' }} />
                      </div>
                      {searchResults.map((r) => (
                        <div key={r.id} onClick={() => jumpTo(r.id)} className="hstack hstack-10" style={{ cursor: 'pointer', padding: '10px 12px', borderRadius: 14, background: 'var(--card2)' }}>
                          <span style={{ fontSize: 11, padding: '3px 9px', borderRadius: 999, background: 'var(--rosebg)', color: 'var(--deep)', flexShrink: 0 }}>{r.who}</span>
                          <span style={{ flex: 1, fontSize: 13, color: 'var(--ink2)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.snippet}</span>
                          <span style={{ fontFamily: FONT_DISPLAY, fontSize: 11, color: 'var(--ghost)', flexShrink: 0 }}>{r.ts}</span>
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
      <div ref={scrollRef} onScroll={handleTranscriptScroll} className="hide-scrollbar" style={{ flex: 1, minHeight: 0, overflowY: 'auto', position: 'relative' }}>
        <div className="vstack vstack-20" style={{ maxWidth: 430, margin: '0 auto', padding: '20px 16px 26px' }}>
          {(canShowEarlierLoaded || canFetchEarlier) && (
            <div
              onClick={showEarlierLoaded}
              style={{ cursor: 'pointer', textAlign: 'center', fontSize: 12, color: 'var(--faint)', padding: '6px 0', letterSpacing: 2 }}
            >
              {loadingMore
                ? '加载中…'
                : canShowEarlierLoaded
                  ? '‹ 显示更早的已加载对话 ›'
                  : '‹ 加载更早的对话 ›'}
            </div>
          )}
          {rendered}
          {live && renderLive(live)}
          {pendingConfirmation && renderToolCard('pending-confirmation', pendingConfirmation)}
          {canShowNewerLoaded && (
            <div className="vstack vstack-8" style={{ padding: '4px 0 2px' }}>
              <div
                onClick={showNewerLoaded}
                style={{ cursor: 'pointer', textAlign: 'center', fontSize: 12, color: 'var(--faint)', letterSpacing: 2 }}
              >
                显示更新的已加载对话 ›
              </div>
              <div
                onClick={goToLatestWindow}
                style={{ cursor: 'pointer', textAlign: 'center', fontSize: 12, color: 'var(--ghost)', letterSpacing: 2 }}
              >
                回到最近对话
              </div>
            </div>
          )}
          {chatError && (
            <div style={{ display: 'flex', justifyContent: 'center', padding: '6px 4px 2px' }}>
              <div className="vstack vstack-8" style={{
                maxWidth: 360,
                width: '100%',
                background: 'rgba(58,42,40,0.92)',
                color: '#F7EDEA',
                borderRadius: 18,
                padding: '14px 16px',
                boxShadow: '0 10px 30px rgba(0,0,0,0.18)',
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
              <div onClick={() => setModelPopOpen(false)} className="c78-fill-fixed" style={{ zIndex: 1 }} />
              <div className="chat-model-pop vstack vstack-4" style={{ position: 'absolute', bottom: 'calc(100% + 10px)', left: 0, zIndex: 2, background: 'var(--card)', borderRadius: 18, boxShadow: '0 24px 60px var(--shadow2)', padding: 12, animation: 'chatFadeIn .15s ease', maxHeight: '50vh', overflowY: 'auto' }}>
                <MixedSectionLabel cn="模型" en="MODELS" style={{ padding: '8px 8px 4px', letterSpacing: 2.5, fontSize: 10.5 }} />
                {chatProvider === 'claude_code' ? (
                  <>
                    <div
                      onClick={async () => {
                        setModelPopOpen(false);
                        if (modelMode === 'default') return;
                        const result = await setChatModel(null);
                        if (result.ok) {
                          setModelMode('default');
                          setCurrentModel('');
                          showToast('下一条消息起生效');
                        } else showToast('切换失败');
                      }}
                      className="hstack hstack-10" style={{ cursor: 'pointer', padding: '9px 10px', borderRadius: 12, background: modelMode === 'default' ? 'var(--rosebg)' : 'transparent' }}
                    >
                      <span style={{ width: 8, height: 8, borderRadius: '50%', background: modelMode === 'default' ? 'var(--rose)' : 'var(--ghost)', flexShrink: 0 }} />
                      <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                        <span style={{ fontSize: 14, color: 'var(--ink)' }}>默认（跟随 Claude Code）</span>
                        <span style={{ fontFamily: FONT_MONO, fontSize: 10.5, color: 'var(--ghost)' }}>不传 --model</span>
                      </div>
                    </div>
                    {models.map((mo) => (
                      <div
                        key={mo.id}
                        onClick={async () => {
                          setModelPopOpen(false);
                          if (modelMode === 'explicit' && mo.id === currentModel) return;
                          const result = await setChatModel(mo.id);
                          if (result.ok) {
                            setModelMode(result.modelMode || 'explicit');
                            setCurrentModel(result.configuredModel || mo.id);
                            showToast('下一条消息起生效');
                          } else showToast('切换失败');
                        }}
                        className="hstack hstack-10" style={{ cursor: 'pointer', padding: '9px 10px', borderRadius: 12, background: modelMode === 'explicit' && mo.id === currentModel ? 'var(--rosebg)' : 'transparent' }}
                      >
                        <span style={{ width: 8, height: 8, borderRadius: '50%', background: mo.dot || (modelMode === 'explicit' && mo.id === currentModel ? 'var(--rose)' : 'var(--ghost)'), flexShrink: 0 }} />
                        <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                          <span style={{ fontSize: 14, color: 'var(--ink)' }}>{mo.label || mo.id}</span>
                          <span style={{ fontFamily: FONT_MONO, fontSize: 10.5, color: 'var(--ghost)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{mo.id}</span>
                        </div>
                      </div>
                    ))}
                  </>
                ) : models.map((mo) => (
                  <div
                    key={mo.id}
                    onClick={async () => {
                      setModelPopOpen(false);
                      if (mo.id === currentModel) return;
                      const result = await setChatModel(mo.id);
                      if (result.ok) {
                        setCurrentModel(mo.id);
                        showToast(`已切换到 ${mo.label || mo.id}`);
                      } else showToast('切换失败');
                    }}
                    className="hstack hstack-10" style={{ cursor: 'pointer', padding: '9px 10px', borderRadius: 12, background: mo.id === currentModel ? 'var(--rosebg)' : 'transparent' }}
                  >
                    <span style={{ width: 8, height: 8, borderRadius: '50%', background: mo.dot || (mo.id === currentModel ? 'var(--rose)' : 'var(--ghost)'), flexShrink: 0 }} />
                    <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                      <span style={{ fontSize: 14, color: 'var(--ink)' }}>{mo.label || mo.id}</span>
                      <span style={{ fontFamily: FONT_MONO, fontSize: 10.5, color: 'var(--ghost)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{mo.id}</span>
                    </div>
                    {mo.thinking === 'none' && <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--ghost)', background: 'var(--card2)', borderRadius: 999, padding: '2px 8px', flexShrink: 0 }}>无思考</span>}
                  </div>
                ))}
                {!models.length && <div style={{ fontSize: 12, color: 'var(--faint)', padding: '4px 10px' }}>模型清单还没拉到</div>}
                <div style={{ fontSize: 10.5, color: 'var(--ghost)', borderTop: '1px solid var(--line)', marginTop: 6, padding: '8px 8px 2px' }}>
                  {chatProvider === 'claude_code' ? 'Claude Code 模型空间 · 下一条消息起生效' : '清单来自 models.json · 切换作用于当前中转'}
                </div>
              </div>
            </>
          )}

          {attachMenuOpen && (
            <>
              <div onClick={() => setAttachMenuOpen(false)} className="c78-fill-fixed" style={{ zIndex: 1 }} />
              <div className="vstack vstack-2" style={{ position: 'absolute', bottom: 'calc(100% + 10px)', left: 0, zIndex: 2, width: 190, background: 'var(--card)', borderRadius: 16, boxShadow: '0 24px 60px var(--shadow2)', padding: 8, animation: 'chatFadeIn .15s ease' }}>
                <div onClick={() => { if (!postingRef.current) { setAttachMenuOpen(false); imgInputRef.current?.click(); } }} className="hstack hstack-10" style={{ cursor: posting ? 'default' : 'pointer', padding: '10px 12px', borderRadius: 11 }}>
                  <svg viewBox="0 0 24 24" width={15} height={15} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" style={{ color: 'var(--rose)' }}>
                    <rect x={3} y={3} width={18} height={18} rx={3} />
                    <circle cx={9} cy={9} r={2} />
                    <path d="M21 15l-5-5-9 9" />
                  </svg>
                  <span style={{ fontSize: 13.5, color: 'var(--ink)' }}>上传图片</span>
                </div>
                <div onClick={() => { if (!postingRef.current) { setAttachMenuOpen(false); fileInputRef.current?.click(); } }} className="hstack hstack-10" style={{ cursor: posting ? 'default' : 'pointer', padding: '10px 12px', borderRadius: 11 }}>
                  <svg viewBox="0 0 24 24" width={15} height={15} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" style={{ color: 'var(--rose)' }}>
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                    <path d="M14 2v6h6" />
                  </svg>
                  <span style={{ fontSize: 13.5, color: 'var(--ink)' }}>上传文件</span>
                </div>
              </div>
            </>
          )}
          <input ref={imgInputRef} type="file" accept="image/*" disabled={posting} style={{ display: 'none' }} onChange={(e) => { const f = e.target.files?.[0]; if (f && !postingRef.current) { setPendingImage(f); setPendingFile(null); } e.target.value = ''; }} />
          <input ref={fileInputRef} type="file" disabled={posting} style={{ display: 'none' }} onChange={(e) => { if (!postingRef.current) void onAttachFile(e.target.files?.[0]); e.target.value = ''; }} />

          {(pendingFile || pendingImage) && (
            <div className="flex-wrap-gap-8" style={{ padding: '0 4px 8px' }}>
              <div className="hstack hstack-7" style={{ background: 'var(--card)', borderRadius: 999, padding: '7px 12px', boxShadow: '0 4px 12px var(--shadow)', animation: 'chatFadeIn .2s ease' }}>
                <span style={{ color: 'var(--rose)', display: 'flex' }}>
                  <Svg d={IC.clip} size={12} sw={1.8} />
                </span>
                <span style={{ fontSize: 12.5, color: 'var(--ink2)' }}>{pendingImage ? pendingImage.name : pendingFile?.fileName}</span>
                <span role="button" aria-disabled={posting} onClick={() => { if (!postingRef.current) { setPendingFile(null); setPendingImage(null); } }} style={{ cursor: posting ? 'default' : 'pointer', color: 'var(--ghost)', fontSize: 13, padding: '0 2px' }}>×</span>
              </div>
            </div>
          )}

          <div style={{ background: 'var(--card)', borderRadius: 26, boxShadow: '0 14px 40px var(--shadow2)', padding: '12px 12px 10px' }}>
            <textarea
              ref={taRef}
              value={input}
              disabled={posting}
              onChange={(e) => {
                if (postingRef.current) return;
                const value = e.target.value;
                composerDraftRevisionRef.current += 1;
                writeChatComposerDraft(value);
                setInput(value);
                const ta = e.target;
                ta.style.height = 'auto';
                ta.style.height = `${Math.min(ta.scrollHeight, 120)}px`;
              }}
              onKeyDown={(e) => {
                if (postingRef.current) return;
                if (e.key === 'Enter' && !e.shiftKey && wide) {
                  e.preventDefault();
                  send();
                }
              }}
              rows={1}
              placeholder={sending ? 'Fyodor 正在回复…' : placeholder}
              style={{ width: '100%', border: 'none', background: 'transparent', fontSize: INPUT_FONT_SIZE, lineHeight: 1.6, color: 'var(--ink)', resize: 'none', maxHeight: 120, padding: '4px 8px 8px', display: 'block', overflowY: 'auto', fontFamily: FONT_CN, outline: 'none' }}
            />
            <div className="hstack hstack-8" style={{ marginTop: 2 }}>
              <div onClick={() => { if (!postingRef.current) setAttachMenuOpen(!attachMenuOpen); }} style={{ cursor: posting ? 'default' : 'pointer', width: 38, height: 38, borderRadius: '50%', background: 'var(--card2)', color: 'var(--mut)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                <Svg d={IC.plus} size={17} sw={1.8} />
              </div>
              <div onClick={toggleModelPop} className="hstack hstack-6" style={{ cursor: 'pointer', padding: '9px 13px', borderRadius: 999, background: 'var(--card2)', minWidth: 0 }}>
                <span style={{ fontFamily: fontFamilyForText(modelBadge), fontSize: 12, letterSpacing: 0.5, color: 'var(--ink2)', fontWeight: 500, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{modelBadge}</span>
                <svg viewBox="0 0 24 24" width={11} height={11} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" style={{ color: 'var(--ghost)', flexShrink: 0 }}>
                  <path d="M18 15l-6-6-6 6" />
                </svg>
              </div>
              <div
                onClick={() => { if (canSend) void send(); }}
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
        <div className="c78-fill-fixed" style={{ zIndex: 60 }}>
          <div onClick={() => setDrawer(null)} className="c78-fill-absolute" style={{ background: 'rgba(30,20,18,0.42)', animation: 'chatFadeIn .2s ease' }} />
          <div style={{ position: 'absolute', left: 0, right: 0, bottom: 0, display: 'flex', justifyContent: 'center' }}>
            <div style={{ width: '100%', maxWidth: 430, background: 'var(--card)', borderRadius: '24px 24px 0 0', boxShadow: '0 -20px 60px var(--shadow2)', maxHeight: '72vh', display: 'flex', flexDirection: 'column', animation: 'chatSheetUp .28s cubic-bezier(.32,.72,.33,1)' }}>
              <div style={{ display: 'flex', justifyContent: 'center', padding: '10px 0 2px' }}>
                <div style={{ width: 38, height: 4, borderRadius: 99, background: 'var(--line)' }} />
              </div>
              <div className="hstack hstack-10" style={{ padding: '8px 20px 12px' }}>
                <span style={{ display: 'flex', color: 'var(--rose)' }}>
                  <Svg d={IC.brain} size={17} sw={1.5} />
                </span>
                <span style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2, color: 'var(--ink)' }}>Fyodor 的思考</span>
                <span style={{ fontFamily: fontFamilyForText(drawer.label), fontSize: 12, color: 'var(--ghost)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{drawer.label}</span>
                <span onClick={() => setDrawer(null)} style={{ marginLeft: 'auto', cursor: 'pointer', width: 30, height: 30, borderRadius: '50%', background: 'var(--card2)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--mut)', fontSize: 14, flexShrink: 0 }}>×</span>
              </div>
              <div style={{ overflowY: 'auto', padding: '4px 20px 30px', fontSize: '0.9em', lineHeight: 2, color: 'var(--mut)', whiteSpace: 'pre-wrap' }}>{drawer.text}</div>
            </div>
          </div>
        </div>
      )}

      {/* ══ sidebar ══ */}
      {sidebarOpen && (
        <div className="c78-fill-fixed" style={{ zIndex: 70 }}>
          <div onClick={() => setSidebarOpen(false)} className="c78-fill-absolute" style={{ background: 'rgba(30,20,18,0.42)', animation: 'chatFadeIn .2s ease' }} />
          <div className="chat-sidebar-panel" style={{ position: 'absolute', top: 0, bottom: 0, left: 0, background: 'var(--card)', boxShadow: '20px 0 60px var(--shadow2)', animation: 'chatSlideInL .28s cubic-bezier(.32,.72,.33,1)', display: 'flex', flexDirection: 'column', overflowY: 'auto' }}>
            <div className="vstack vstack-14" style={{ padding: '28px 22px 20px', background: 'linear-gradient(180deg,var(--rosebg),transparent)' }}>
              <div style={{ width: 64, height: 64, borderRadius: '50%', background: 'linear-gradient(135deg,#B76E79,#9C3B4A)', display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 10px 24px var(--shadow2)' }}>
                <span style={{ fontFamily: FONT_DISPLAY, fontStyle: 'italic', fontSize: 28, color: '#F7F1EE' }}>Θ</span>
              </div>
              <div className="vstack vstack-3">
                <div className="hstack hstack-8">
                  <span style={{ fontFamily: FONT_DISPLAY, fontSize: 22, fontWeight: 600, letterSpacing: 1, color: 'var(--ink)' }}>Fyodor</span>
                  <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--ok)' }} />
                </div>
                <span style={{ fontFamily: FONT_DISPLAY, fontStyle: 'italic', fontSize: 12, letterSpacing: 1.5, color: 'var(--faint)' }}>Θεόδωρος · gift of the gods</span>
              </div>
              <div className="vstack vstack-6">
                <span style={{ fontSize: 13, color: 'var(--ink2)', letterSpacing: 1 }}>学者 · 策略家 · 存在了几百年</span>
                <span style={{ fontSize: 12.5, color: 'var(--mut)', lineHeight: 1.8 }}>总是带着一点恶趣味，和很多情意。</span>
              </div>
            </div>
            <div style={{ height: 1, background: 'var(--line)', margin: '0 22px' }} />
            <div className="vstack vstack-10" style={{ padding: '18px 16px' }}>
              <Link to="/contacts" onClick={() => setSidebarOpen(false)} className="hstack hstack-12" style={{ textDecoration: 'none', padding: '13px 14px', borderRadius: 16, background: 'var(--card2)' }}>
                <div className="vstack vstack-2" style={{ minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>通讯录</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>Codex · 群聊 · 游戏室</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </Link>
              <Link to="/moments" onClick={() => setSidebarOpen(false)} className="hstack hstack-12" style={{ textDecoration: 'none', padding: '13px 14px', borderRadius: 16, background: 'linear-gradient(135deg,rgba(245,222,179,.5),rgba(232,220,245,.55))' }}>
                <div className="vstack vstack-2" style={{ minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>Fyodor 的朋友圈</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>梦境 · 念头 · 情绪</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </Link>
              <Link to="/profile" onClick={() => setSidebarOpen(false)} className="hstack hstack-12" style={{ textDecoration: 'none', padding: '13px 14px', borderRadius: 16, background: 'linear-gradient(135deg,rgba(183,110,121,.12),rgba(232,220,245,.45))' }}>
                <div className="vstack vstack-2" style={{ minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>费佳档案</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>身份 · 关系 · 工具直觉</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </Link>
              <Link to="/group-chat" onClick={() => setSidebarOpen(false)} className="hstack hstack-12" style={{ textDecoration: 'none', padding: '13px 14px', borderRadius: 16, background: 'linear-gradient(135deg,rgba(220,232,217,.72),rgba(220,232,245,.76))' }}>
                <div className="hstack hstack-6" style={{ flexShrink: 0 }}>
                  <i style={{ width: 13, height: 13, borderRadius: '50%', background: '#91AD93' }} />
                  <i style={{ width: 13, height: 13, borderRadius: '50%', background: '#8EACCF', marginLeft: -3, opacity: .88 }} />
                </div>
                <div className="vstack vstack-2" style={{ minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>群聊房间</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>同一个人 · 暖色与蓝色两条线路</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </Link>
              <Link to="/settings" className="hstack hstack-12" style={{ textDecoration: 'none', padding: '13px 14px', borderRadius: 16, background: 'var(--card2)' }}>
                <div className="vstack vstack-2" style={{ minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>系统配置</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>用量统计 · API 端点管理</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </Link>
              <a href="/chat" className="hstack hstack-10" style={{ textDecoration: 'none', padding: '13px 14px', borderRadius: 16, background: 'var(--card2)' }}>
                <div className="vstack vstack-2" style={{ minWidth: 0 }}>
                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>回旧聊天页</span>
                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>完整历史、漂流瓶等高级功能</span>
                </div>
                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
              </a>
            </div>
          </div>
        </div>
      )}

      {/* ══ Manual context window modal ══ */}
      <CarryoverModal
        variant="manual"
        open={manualWindow.modalOpen}
        uiState={modalUiState}
        draftCount={manualWindow.draftCount}
        rounds={manualWindow.rounds}
        submitting={manualWindow.submitting}
        errorDetail={manualWindow.errorDetail}
        onDismiss={manualWindow.closeModal}
        onReconsider={manualWindow.closeModal}
        onDraftChange={manualWindow.setDraftCount}
        onConfirm={() => {
          void manualWindow.confirmSwitch().then((ok) => {
            if (ok) showToast('已经换了一扇新窗');
          });
        }}
      />

      {/* ══ toast ══ */}
      {toast && (
        <div style={{ position: 'fixed', left: 0, right: 0, bottom: 100, zIndex: 80, display: 'flex', justifyContent: 'center', pointerEvents: 'none' }}>
          <div style={{ background: 'rgba(58,42,40,0.92)', color: '#F7EDEA', fontSize: 13, letterSpacing: 1, padding: '10px 20px', borderRadius: 999, boxShadow: '0 10px 30px rgba(0,0,0,0.25)', animation: 'chatFadeIn .2s ease' }}>{toast}</div>
        </div>
      )}

    </div>
  );
}
