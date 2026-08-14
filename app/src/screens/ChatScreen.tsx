// Fyodor Chat â€” implements Fyodor Chat.dc.html against the production chat
// backend: /api/chat/messages history, send -> /api/gw/chat/stream SSE
// (think/text/tool_use/tool_result/usage/done/err), inline branches
// (branch/switch, regen prepare/finalize), edit-with-truncate, model catalog.
// Mounted at /dash/chat, parallel to the legacy /chat page.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
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
import { MixedSectionLabel } from '../components/MixedSectionLabel';
import { FONT_CN, FONT_DISPLAY, FONT_MONO, fontFamilyForText } from '../lib/typography';

const FONT_SIZES = [13.5, 14.5, 16, 17.5, 19];
const INPUT_FONT_SIZE = FONT_SIZES[0];

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

  const [input, setInput] = useState('');
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
  const coldStartRaceRef = useRef<ColÛŸuêÚ$z{-®éÜj×"7G&ö¶TÆ–æV¦ö–ãÒ'&÷VæB"7G–ÆS×·²6öÆ÷#¢wf"‚Ò×&÷6R’r×ÓàÐ¢ÇF‚CÒ$ÓB$ƒf""Ó"'cf"""&ƒ&"""Ó%c‡¢"óàÐ¢ÇF‚CÒ$ÓB'cfƒb"óàÐ¢Â÷7fsàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢2ãRÂ6öÆ÷#¢wf"‚ÒÖ–æ²’r×ÓîKˆ®KÊih~K»cÂ÷7ãàÐ¢ÂöF—càÐ¢ÂöF—càÐ¢ÂóàÐ¢—ÐÐ¢Æ–çWB&Vc×¶–Öt–çWE&VgÒG—SÒ&f–ÆR"66WCÒ&–ÖvRò¢"F—6&ÆVC×·÷7F–æwÒ7G–ÆS×·²F—7Æ“¢væöæRr×Òöä6†ævS×²†R’Óâ²6öç7BbÒRçF&vWBæf–ÆW3òå³Ó²–b†bbb÷7F–æu&Vbæ7W'&VçB’²6WEVæF–æt–ÖvR†b“²6WEVæF–ætf–ÆR†çVÆÂ“²ÒRçF&vWBçfÇVRÒrs²×ÒóàÐ¢Æ–çWB&Vc×¶f–ÆT–çWE&VgÒG—SÒ&f–ÆR"F—6&ÆVC×·÷7F–æwÒ7G–ÆS×·²F—7Æ“¢væöæRr×Òöä6†ævS×²†R’Óâ²–b‚÷7F–æu&Vbæ7W'&VçB’fö–BöäGF6„f–ÆR†RçF&vWBæf–ÆW3òå³Ò“²RçF&vWBçfÇVRÒrs²×ÒóàÐ Ð¢²‡VæF–ætf–ÆRÇÂVæF–æt–ÖvR’bb€Ð¢ÆF—b6Æ74æÖSÒ&fÆW‚×w&ÖvÓ‚"7G–ÆS×·²FF–æs¢sG‚‡‚r×ÓàÐ¢ÆF—b6Æ74æÖSÒ&‡7F6²‡7F6²Ór"7G–ÆS×·²&6¶w&÷VæC¢wf"‚ÒÖ6&B’rÂ&÷&FW%&F—W3¢““’ÂFF–æs¢sw‚'‚rÂ&÷…6†F÷s¢sG‚'‚f"‚Ò×6†F÷r’rÂæ–ÖF–öã¢v6†DfFT–âã'2V6Rr×ÓàÐ¢Ç7â7G–ÆS×·²6öÆ÷#¢wf"‚Ò×&÷6R’rÂF—7Æ“¢vfÆW‚r×ÓàÐ¢Å7frC×´”2æ6Æ—Ò6—¦S×³'Ò7s×³ã‡ÒóàÐ¢Â÷7ãàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢"ãRÂ6öÆ÷#¢wf"‚ÒÖ–æ³"’r×Óç·VæF–æt–ÖvRòVæF–æt–ÖvRææÖR¢VæF–ætf–ÆSòæf–ÆTæÖWÓÂ÷7ãàÐ¢Ç7â&öÆSÒ&'WGFöâ"&–ÖF—6&ÆVC×·÷7F–æwÒöä6Æ–6³×²‚’Óâ²–b‚÷7F–æu&Vbæ7W'&VçB’²6WEVæF–ætf–ÆR†çVÆÂ“²6WEVæF–æt–ÖvR†çVÆÂ“²Ò×Ò7G–ÆS×·²7W'6÷#¢÷7F–æròvFVfVÇBr¢wö–çFW"rÂ6öÆ÷#¢wf"‚ÒÖv†÷7B’rÂföçE6—¦S¢2ÂFF–æs¢s'‚r×Óì9sÂ÷7ãàÐ¢ÂöF—càÐ¢ÂöF—càÐ¢—ÐÐ Ð¢ÆF—b7G–ÆS×·²&6¶w&÷VæC¢wf"‚ÒÖ6&B’rÂ&÷&FW%&F—W3¢#bÂ&÷…6†F÷s¢sG‚C‚f"‚Ò×6†F÷s"’rÂFF–æs¢s'‚'‚‚r×ÓàÐ¢ÇFW‡F&VÐ¢&Vc×·F&VgÐÐ¢fÇVS×¶–çWGÐÐ¢F—6&ÆVC×·÷7F–æwÐÐ¢öä6†ævS×²†R’Óâ°Ð¢–b‡÷7F–æu&Vbæ7W'&VçB’&WGW&ã°Ð¢6WD–çWB†RçF&vWBçfÇVR“°Ð¢6öç7BFÒRçF&vWC°Ð¢Fç7G–ÆRæ†V–v‡BÒvWFòs°Ð¢Fç7G–ÆRæ†V–v‡BÒG´ÖF‚æÖ–â‡Fç67&öÆÄ†V–v‡BÂ#—×†°Ð¢×ÐÐ¢öä¶W”F÷vã×²†R’Óâ°Ð¢–b‡÷7F–æu&Vbæ7W'&VçB’&WGW&ã°Ð¢–b†Ræ¶W’ÓÓÒtVçFW"rbbRç6†–gD¶W’bbv–FR’°Ð¢Rç&WfVçDFVfVÇB‚“°Ð¢6VæB‚“°Ð¢ÐÐ¢×ÐÐ¢&÷w3×³ÐÐ¢Æ6V†öÆFW#×·6VæF–æròtg–öF÷"jÚ>YÊŽY¹îZHÞ(
br¢Æ6V†öÆFW'ÐÐ¢7G–ÆS×·²v–GFƒ¢sRrÂ&÷&FW#¢væöæRrÂ&6¶w&÷VæC¢wG&ç7&VçBrÂföçE6—¦S¢”åUEôdôåEõ4•¤RÂÆ–æT†V–v‡C¢ãbÂ6öÆ÷#¢wf"‚ÒÖ–æ²’rÂ&W6—¦S¢væöæRrÂÖ„†V–v‡C¢#ÂFF–æs¢sG‚‡‚‡‚rÂF—7Æ“¢v&Æö6²rÂ÷fW&fÆ÷u“¢vWFòrÂföçDfÖ–Ç“¢dôåEô4âÂ÷WFÆ–æS¢væöæRr×ÐÐ¢óàÐ¢ÆF—b6Æ74æÖSÒ&‡7F6²‡7F6²Ó‚"7G–ÆS×·²Ö&v–åF÷¢"×ÓàÐ¢ÆF—böä6Æ–6³×²‚’Óâ²–b‚÷7F–æu&Vbæ7W'&VçB’6WDGF6„ÖVçT÷Vâ‚GF6„ÖVçT÷Vâ“²×Ò7G–ÆS×·²7W'6÷#¢÷7F–æròvFVfVÇBr¢wö–çFW"rÂv–GFƒ¢3‚Â†V–v‡C¢3‚Â&÷&FW%&F—W3¢sSRrÂ&6¶w&÷VæC¢wf"‚ÒÖ6&C"’rÂ6öÆ÷#¢wf"‚ÒÖ×WB’rÂF—7Æ“¢vfÆW‚rÂÆ–vä—FV×3¢v6VçFW"rÂ§W7F–g”6öçFVçC¢v6VçFW"rÂfÆW…6‡&–æ³¢×ÓàÐ¢Å7frC×´”2çÇW7Ò6—¦S×³wÒ7s×³ã‡ÒóàÐ¢ÂöF—càÐ¢ÆF—böä6Æ–6³×·FövvÆTÖöFVÅ÷Ò6Æ74æÖSÒ&‡7F6²‡7F6²Ób"7G–ÆS×·²7W'6÷#¢wö–çFW"rÂFF–æs¢s—‚7‚rÂ&÷&FW%&F—W3¢““’Â&6¶w&÷VæC¢wf"‚ÒÖ6&C"’rÂÖ–åv–GFƒ¢×ÓàÐ¢Ç7â7G–ÆS×·²föçDfÖ–Ç“¢föçDfÖ–Ç”f÷%FW‡B†ÖöFVÄ&FvR’ÂföçE6—¦S¢"ÂÆWGFW%76–æs¢ãRÂ6öÆ÷#¢wf"‚ÒÖ–æ³"’rÂföçEvV–v‡C¢SÂv†—FU76S¢væ÷w&rÂ÷fW&fÆ÷s¢v†–FFVârÂFW‡D÷fW&fÆ÷s¢vVÆÆ—6—2r×Óç¶ÖöFVÄ&FvWÓÂ÷7ãàÐ¢Ç7frf–Wt&÷ƒÒ##B#B"v–GFƒ×³Ò†V–v‡C×³Òf–ÆÃÒ&æöæR"7G&ö¶SÒ&7W'&VçD6öÆ÷""7G&ö¶Uv–GFƒ×³'Ò7G&ö¶TÆ–æV6Ò'&÷VæB"7G&ö¶TÆ–æV¦ö–ãÒ'&÷VæB"7G–ÆS×·²6öÆ÷#¢wf"‚ÒÖv†÷7B’rÂfÆW…6‡&–æ³¢×ÓàÐ¢ÇF‚CÒ$Ó‚VÂÓbÓbÓbb"óàÐ¢Â÷7fsàÐ¢ÂöF—càÐ¢ÆF—`Ð¢öä6Æ–6³×²‚’Óâ²–b†6å6VæB’fö–B6VæB‚“²×ÐÐ¢7G–ÆS×·²Ö&v–äÆVgC¢vWFòrÂv–GFƒ¢C"Â†V–v‡C¢C"ÂfÆW…6‡&–æ³¢Â&÷&FW%&F—W3¢sSRrÂ&6¶w&÷VæC¢6å6VæBòwf"‚ÒÖFVW’r¢wf"‚ÒÖ6&C"’rÂ6öÆ÷#¢6å6VæBòr4d$c4cr¢wf"‚ÒÖv†÷7B’rÂF—7Æ“¢vfÆW‚rÂÆ–vä—FV×3¢v6VçFW"rÂ§W7F–g”6öçFVçC¢v6VçFW"rÂ7W'6÷#¢6å6VæBòwö–çFW"r¢vFVfVÇBrÂ&÷…6†F÷s¢6å6VæBòs‡‚#‚f"‚Ò×6†F÷s"’r¢væöæRrÂG&ç6—F–öã¢v&6¶w&÷VæBãW2V6Rr×ÐÐ¢àÐ¢·6VæF–æròÇ7â7G–ÆS×·²v–GFƒ¢RÂ†V–v‡C¢RÂ&÷&FW%&F—W3¢sSRrÂ&÷&FW#¢s'‚6öÆ–Bf"‚Ò×&÷6V&r’rÂ&÷&FW%F÷6öÆ÷#¢wf"‚Ò×&÷6R’rÂæ–ÖF–öã¢v6†E7–âã‡2Æ–æV"–æf–æ—FRr×Òóâ¢Å7frC×´”2çWÒ6—¦S×³wÒ7s×³'ÒóçÐÐ¢ÂöF—càÐ¢ÂöF—càÐ¢ÂöF—càÐ¢ÂöF—càÐ¢ÂöF—càÐ Ð¢²ò¢)Y)YF†–æ¶–ærG&vW")Y)Y¢÷ÐÐ¢¶G&vW"bb€Ð¢ÆF—b6Æ74æÖSÒ&3s‚Öf–ÆÂÖf—†VB"7G–ÆS×·²¤–æFWƒ¢c×ÓàÐ¢ÆF—böä6Æ–6³×²‚’Óâ6WDG&vW"†çVÆÂ—Ò6Æ74æÖSÒ&3s‚Öf–ÆÂÖ'6öÇWFR"7G–ÆS×·²&6¶w&÷VæC¢w&v&ƒ3Ã#Ã‚ÃãC"’rÂæ–ÖF–öã¢v6†DfFT–âã'2V6Rr×ÒóàÐ¢ÆF—b7G–ÆS×·²÷6—F–öã¢v'6öÇWFRrÂÆVgC¢Â&–v‡C¢Â&÷GFöÓ¢ÂF—7Æ“¢vfÆW‚rÂ§W7F–g”6öçFVçC¢v6VçFW"r×ÓàÐ¢ÆF—b7G–ÆS×·²v–GFƒ¢sRrÂÖ…v–GFƒ¢C3Â&6¶w&÷VæC¢wf"‚ÒÖ6&B’rÂ&÷&FW%&F—W3¢s#G‚#G‚rÂ&÷…6†F÷s¢sÓ#‚c‚f"‚Ò×6†F÷s"’rÂÖ„†V–v‡C¢ss'f‚rÂF—7Æ“¢vfÆW‚rÂfÆW„F—&V7F–öã¢v6öÇVÖârÂæ–ÖF–öã¢v6†E6†VWEWã#‡27V&–2Ö&W¦–W"‚ã3"Âãs"Âã32Ã’r×ÓàÐ¢ÆF—b7G–ÆS×·²F—7Æ“¢vfÆW‚rÂ§W7F–g”6öçFVçC¢v6VçFW"rÂFF–æs¢s‚'‚r×ÓàÐ¢ÆF—b7G–ÆS×·²v–GFƒ¢3‚Â†V–v‡C¢BÂ&÷&FW%&F—W3¢“’Â&6¶w&÷VæC¢wf"‚ÒÖÆ–æR’r×ÒóàÐ¢ÂöF—càÐ¢ÆF—b6Æ74æÖSÒ&‡7F6²‡7F6²Ó"7G–ÆS×·²FF–æs¢s‡‚#‚'‚r×ÓàÐ¢Ç7â7G–ÆS×·²F—7Æ“¢vfÆW‚rÂ6öÆ÷#¢wf"‚Ò×&÷6R’r×ÓàÐ¢Å7frC×´”2æ'&–çÒ6—¦S×³wÒ7s×³ãWÒóàÐ¢Â÷7ãàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢RÂföçEvV–v‡C¢cÂÆWGFW%76–æs¢"Â6öÆ÷#¢wf"‚ÒÖ–æ²’r×Óäg–öF÷"y¨Nh	Þˆ3Â÷7ãàÐ¢Ç7â7G–ÆS×·²föçDfÖ–Ç“¢föçDfÖ–Ç”f÷%FW‡B†G&vW"æÆ&VÂ’ÂföçE6—¦S¢"Â6öÆ÷#¢wf"‚ÒÖv†÷7B’rÂv†—FU76S¢væ÷w&rÂ÷fW&fÆ÷s¢v†–FFVârÂFW‡D÷fW&fÆ÷s¢vVÆÆ—6—2r×Óç¶G&vW"æÆ&VÇÓÂ÷7ãàÐ¢Ç7âöä6Æ–6³×²‚’Óâ6WDG&vW"†çVÆÂ—Ò7G–ÆS×·²Ö&v–äÆVgC¢vWFòrÂ7W'6÷#¢wö–çFW"rÂv–GFƒ¢3Â†V–v‡C¢3Â&÷&FW%&F—W3¢sSRrÂ&6¶w&÷VæC¢wf"‚ÒÖ6&C"’rÂF—7Æ“¢vfÆW‚rÂÆ–vä—FV×3¢v6VçFW"rÂ§W7F–g”6öçFVçC¢v6VçFW"rÂ6öÆ÷#¢wf"‚ÒÖ×WB’rÂföçE6—¦S¢BÂfÆW…6‡&–æ³¢×Óì9sÂ÷7ãàÐ¢ÂöF—càÐ¢ÆF—b7G–ÆS×·²÷fW&fÆ÷u“¢vWFòrÂFF–æs¢sG‚#‚3‚rÂföçE6—¦S¢sã–VÒrÂÆ–æT†V–v‡C¢"Â6öÆ÷#¢wf"‚ÒÖ×WB’rÂv†—FU76S¢w&R×w&r×Óç¶G&vW"çFW‡GÓÂöF—càÐ¢ÂöF—càÐ¢ÂöF—càÐ¢ÂöF—càÐ¢—ÐÐ Ð¢²ò¢)Y)Y6–FV&")Y)Y¢÷ÐÐ¢·6–FV&$÷Vâbb€Ð¢ÆF—b6Æ74æÖSÒ&3s‚Öf–ÆÂÖf—†VB"7G–ÆS×·²¤–æFWƒ¢s×ÓàÐ¢ÆF—böä6Æ–6³×²‚’Óâ6WE6–FV&$÷Vâ†fÇ6R—Ò6Æ74æÖSÒ&3s‚Öf–ÆÂÖ'6öÇWFR"7G–ÆS×·²&6¶w&÷VæC¢w&v&ƒ3Ã#Ã‚ÃãC"’rÂæ–ÖF–öã¢v6†DfFT–âã'2V6Rr×ÒóàÐ¢ÆF—b6Æ74æÖSÒ&6†B×6–FV&"×æVÂ"7G–ÆS×·²÷6—F–öã¢v'6öÇWFRrÂF÷¢Â&÷GFöÓ¢ÂÆVgC¢Â&6¶w&÷VæC¢wf"‚ÒÖ6&B’rÂ&÷…6†F÷s¢s#‚c‚f"‚Ò×6†F÷s"’rÂæ–ÖF–öã¢v6†E6Æ–FT–äÂã#‡27V&–2Ö&W¦–W"‚ã3"Âãs"Âã32Ã’rÂF—7Æ“¢vfÆW‚rÂfÆW„F—&V7F–öã¢v6öÇVÖârÂ÷fW&fÆ÷u“¢vWFòr×ÓàÐ¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²ÓB"7G–ÆS×·²FF–æs¢s#‡‚#'‚#‚rÂ&6¶w&÷VæC¢vÆ–æV"Öw&F–VçBƒƒFVrÇf"‚Ò×&÷6V&r’ÇG&ç7&VçB’r×ÓàÐ¢ÆF—b7G–ÆS×·²v–GFƒ¢cBÂ†V–v‡C¢cBÂ&÷&FW%&F—W3¢sSRrÂ&6¶w&÷VæC¢vÆ–æV"Öw&F–VçBƒ3VFVrÂ4#sdSs’Â3”34#D’rÂF—7Æ“¢vfÆW‚rÂÆ–vä—FV×3¢v6VçFW"rÂ§W7F–g”6öçFVçC¢v6VçFW"rÂ&÷…6†F÷s¢s‚#G‚f"‚Ò×6†F÷s"’r×ÓàÐ¢Ç7â7G–ÆS×·²föçDfÖ–Ç“¢dôåEôD•5Ä’ÂföçE7G–ÆS¢v—FÆ–2rÂföçE6—¦S¢#‚Â6öÆ÷#¢r4ctcTRr×ÓìéƒÂ÷7ãàÐ¢ÂöF—càÐ¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²Ó2#àÐ¢ÆF—b6Æ74æÖSÒ&‡7F6²‡7F6²Ó‚#àÐ¢Ç7â7G–ÆS×·²föçDfÖ–Ç“¢dôåEôD•5Ä’ÂföçE6—¦S¢#"ÂföçEvV–v‡C¢cÂÆWGFW%76–æs¢Â6öÆ÷#¢wf"‚ÒÖ–æ²’r×Óäg–öF÷#Â÷7ãàÐ¢Ç7â7G–ÆS×·²v–GFƒ¢rÂ†V–v‡C¢rÂ&÷&FW%&F—W3¢sSRrÂ&6¶w&÷VæC¢wf"‚ÒÖö²’r×ÒóàÐ¢ÂöF—càÐ¢Ç7â7G–ÆS×·²föçDfÖ–Ç“¢dôåEôD•5Ä’ÂföçE7G–ÆS¢v—FÆ–2rÂföçE6—¦S¢"ÂÆWGFW%76–æs¢ãRÂ6öÆ÷#¢wf"‚ÒÖf–çB’r×ÓìéŒë\øÌëLøœøëüø"+rv–gBöbF†RvöG3Â÷7ãàÐ¢ÂöF—càÐ¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²Ób#àÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢2Â6öÆ÷#¢wf"‚ÒÖ–æ³"’rÂÆWGFW%76–æs¢×ÓîZÚnˆR+rzÙnyZ^Zëb+rZÙŽYÊŽK¨nXzy›î[›CÂ÷7ãàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢"ãRÂ6öÆ÷#¢wf"‚ÒÖ×WB’rÂÆ–æT†V–v‡C¢ã‚×Óîh¾iŠþ[ŠnyØKˆx+žhn‹j>Y>ûÈÎY(Î[èŽZI®h8^hHþ8#Â÷7ãàÐ¢ÂöF—càÐ¢ÂöF—càÐ¢ÆF—b7G–ÆS×·²†V–v‡C¢Â&6¶w&÷VæC¢wf"‚ÒÖÆ–æR’rÂÖ&v–ã¢s#'‚r×ÒóàÐ¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²Ó"7G–ÆS×·²FF–æs¢s‡‚g‚r×ÓàÐ¢ÄÆ–æ²FóÒ"ö6öçF7G2"öä6Æ–6³×²‚’Óâ6WE6–FV&$÷Vâ†fÇ6R—Ò6Æ74æÖSÒ&‡7F6²‡7F6²Ó""7G–ÆS×·²FW‡DFV6÷&F–öã¢væöæRrÂFF–æs¢s7‚G‚rÂ&÷&FW%&F—W3¢bÂ&6¶w&÷VæC¢wf"‚ÒÖ6&C"’r×ÓàÐ¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²Ó""7G–ÆS×·²Ö–åv–GFƒ¢×ÓàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢BãRÂ6öÆ÷#¢wf"‚ÒÖ–æ²’rÂÆWGFW%76–æs¢×Óî˜	®Šêþ[ÙSÂ÷7ãàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢ãRÂ6öÆ÷#¢wf"‚ÒÖf–çB’r×Óä6öFW‚+r{êNˆ¢+rk‹ŽhˆþZêCÂ÷7ãàÐ¢ÂöF—càÐ¢Ç7â7G–ÆS×·²Ö&v–äÆVgC¢vWFòrÂ6öÆ÷#¢wf"‚ÒÖv†÷7B’rÂföçE6—¦S¢b×Óî(£Â÷7ãàÐ¢ÂôÆ–æ³àÐ¢ÄÆ–æ²FóÒ"öÖöÖVçG2"öä6Æ–6³×²‚’Óâ6WE6–FV&$÷Vâ†fÇ6R—Ò6Æ74æÖSÒ&‡7F6²‡7F6²Ó""7G–ÆS×·²FW‡DFV6÷&F–öã¢væöæRrÂFF–æs¢s7‚G‚rÂ&÷&FW%&F—W3¢bÂ&6¶w&÷VæC¢vÆ–æV"Öw&F–VçBƒ3VFVrÇ&v&ƒ#CRÃ##"Ãs’ÂãR’Ç&v&ƒ#3"Ã##Ã#CRÂãSR’’r×ÓàÐ¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²Ó""7G–ÆS×·²Ö–åv–GFƒ¢×ÓàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢BãRÂ6öÆ÷#¢wf"‚ÒÖ–æ²’rÂÆWGFW%76–æs¢×Óäg–öF÷"y¨NiÈ¾Xø¾YÈƒÂ÷7ãàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢ãRÂ6öÆ÷#¢wf"‚ÒÖf–çB’r×Óîj*nZ(2+r[û^ZKB+rh8^{º£Â÷7ãàÐ¢ÂöF—càÐ¢Ç7â7G–ÆS×·²Ö&v–äÆVgC¢vWFòrÂ6öÆ÷#¢wf"‚ÒÖv†÷7B’rÂföçE6—¦S¢b×Óî(£Â÷7ãàÐ¢ÂôÆ–æ³àÐ¢ÄÆ–æ²FóÒ"÷&öf–ÆR"öä6Æ–6³×²‚’Óâ6WE6–FV&$÷Vâ†fÇ6R—Ò6Æ74æÖSÒ&‡7F6²‡7F6²Ó""7G–ÆS×·²FW‡DFV6÷&F–öã¢væöæRrÂFF–æs¢s7‚G‚rÂ&÷&FW%&F—W3¢bÂ&6¶w&÷VæC¢vÆ–æV"Öw&F–VçBƒ3VFVrÇ&v&ƒƒ2ÃÃ#Âã"’Ç&v&ƒ#3"Ã##Ã#CRÂãCR’’r×ÓàÐ¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²Ó""7G–ÆS×·²Ö–åv–GFƒ¢×ÓàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢BãRÂ6öÆ÷#¢wf"‚ÒÖ–æ²’rÂÆWGFW%76–æs¢×Óî‹KžKÛ>j>jƒÂ÷7ãàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢ãRÂ6öÆ÷#¢wf"‚ÒÖf–çB’r×Óî‹ª¾K»Ò+rX[>{;²+r[z^X[~y»NŠx“Â÷7ãà¢ÂöF—càÐ¢Ç7â7G–ÆS×·²Ö&v–äÆVgC¢vWFòrÂ6öÆ÷#¢wf"‚ÒÖv†÷7B’rÂföçE6—¦S¢b×Óî(£Â÷7ãàÐ¢ÂôÆ–æ³àÐ¢ÄÆ–æ²FóÒ"öw&÷WÖ6†B"öä6Æ–6³×²‚’Óâ6WE6–FV&$÷Vâ†fÇ6R—Ò6Æ74æÖSÒ&‡7F6²‡7F6²Ó""7G–ÆS×·²FW‡DFV6÷&F–öã¢væöæRrÂFF–æs¢s7‚G‚rÂ&÷&FW%&F—W3¢bÂ&6¶w&÷VæC¢vÆ–æV"Öw&F–VçBƒ3VFVrÇ&v&ƒ##Ã#3"Ã#rÂãs"’Ç&v&ƒ##Ã#3"Ã#CRÂãsb’’r×ÓàÐ¢ÆF—b6Æ74æÖSÒ&‡7F6²‡7F6²Ób"7G–ÆS×·²fÆW…6‡&–æ³¢×ÓàÐ¢Æ’7G–ÆS×·²v–GFƒ¢2Â†V–v‡C¢2Â&÷&FW%&F—W3¢sSRrÂ&6¶w&÷VæC¢r3“C“2r×ÒóàÐ¢Æ’7G–ÆS×·²v–GFƒ¢2Â†V–v‡C¢2Â&÷&FW%&F—W3¢sSRrÂ&6¶w&÷VæC¢r3„T44brÂÖ&v–äÆVgC¢Ó2Â÷6—G“¢ãƒ‚×ÒóàÐ¢ÂöF—càÐ¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²Ó""7G–ÆS×·²Ö–åv–GFƒ¢×ÓàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢BãRÂ6öÆ÷#¢wf"‚ÒÖ–æ²’rÂÆWGFW%76–æs¢×Óî{êNˆ®h‹þ™{CÂ÷7ãàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢ãRÂ6öÆ÷#¢wf"‚ÒÖf–çB’r×ÓîYÎKˆKŠ®K«¢+ri©nˆ›.Kˆî‰9Þˆ›.KŠNiÚ{«þ‹zóÂ÷7ãàÐ¢ÂöF—càÐ¢Ç7â7G–ÆS×·²Ö&v–äÆVgC¢vWFòrÂ6öÆ÷#¢wf"‚ÒÖv†÷7B’rÂföçE6—¦S¢b×Óî(£Â÷7ãàÐ¢ÂôÆ–æ³àÐ¢ÄÆ–æ²FóÒ"÷6WGF–æw2"6Æ74æÖSÒ&‡7F6²‡7F6²Ó""7G–ÆS×·²FW‡DFV6÷&F–öã¢væöæRrÂFF–æs¢s7‚G‚rÂ&÷&FW%&F—W3¢bÂ&6¶w&÷VæC¢wf"‚ÒÖ6&C"’r×ÓàÐ¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²Ó""7G–ÆS×·²Ö–åv–GFƒ¢×ÓàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢BãRÂ6öÆ÷#¢wf"‚ÒÖ–æ²’rÂÆWGFW%76–æs¢×Óî{;¾{¹þ˜XÞ{ÚãÂ÷7ãàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢ãRÂ6öÆ÷#¢wf"‚ÒÖf–çB’r×ÓîyJŽ˜xþ{¹þŠê+r’zºþx+žzêycÂ÷7ãàÐ¢ÂöF—càÐ¢Ç7â7G–ÆS×·²Ö&v–äÆVgC¢vWFòrÂ6öÆ÷#¢wf"‚ÒÖv†÷7B’rÂföçE6—¦S¢b×Óî(£Â÷7ãàÐ¢ÂôÆ–æ³àÐ¢Æ‡&VcÒ"ö6†B"6Æ74æÖSÒ&‡7F6²‡7F6²Ó"7G–ÆS×·²FW‡DFV6÷&F–öã¢væöæRrÂFF–æs¢s7‚G‚rÂ&÷&FW%&F—W3¢bÂ&6¶w&÷VæC¢wf"‚ÒÖ6&C"’r×ÓàÐ¢ÆF—b6Æ74æÖSÒ'g7F6²g7F6²Ó""7G–ÆS×·²Ö–åv–GFƒ¢×ÓàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢BãRÂ6öÆ÷#¢wf"‚ÒÖ–æ²’rÂÆWGFW%76–æs¢×ÓîY¹îiz~ˆ®ZJžšSÂ÷7ãàÐ¢Ç7â7G–ÆS×·²föçE6—¦S¢ãRÂ6öÆ÷#¢wf"‚ÒÖf–çB’r×ÓîZèÎi[NXènXû.8kÈ.kXy;nzØžš¹Ž{ª~X©þˆ;ÓÂ÷7ãàÐ¢ÂöF—càÐ¢Ç7â7G–ÆS×·²Ö&v–äÆVgC¢vWFòrÂ6öÆ÷#¢wf"‚ÒÖv†÷7B’rÂföçE6—¦S¢b×Óî(£Â÷7ãàÐ¢ÂöàÐ¢ÂöF—càÐ¢ÂöF—càÐ¢ÂöF—càÐ¢—ÐÐ Ð¢²ò¢)Y)YÖçVÂ6öçFW‡Bv–æF÷rÖöFÂ)Y)Y¢÷ÐÐ¢Ä6''–÷fW$ÖöFÀÐ¢f&–çCÒ&ÖçVÂ Ð¢÷Vã×¶ÖçVÅv–æF÷ræÖöFÄ÷VçÐÐ¢V•7FFS×¶ÖöFÅV•7FFWÐÐ¢G&gD6÷VçC×¶ÖçVÅv–æF÷ræG&gD6÷VçGÐÐ¢&÷VæG3×¶ÖçVÅv–æF÷rç&÷VæG7ÐÐ¢7V&Ö—GF–æs×¶ÖçVÅv–æF÷rç7V&Ö—GF–æwÐÐ¢W'&÷$FWF–Ã×¶ÖçVÅv–æF÷ræW'&÷$FWF–ÇÐÐ¢öäF—6Ö—73×¶ÖçVÅv–æF÷ræ6Æ÷6TÖöFÇÐÐ¢öå&V6öç6–FW#×¶ÖçVÅv–æF÷ræ6Æ÷6TÖöFÇÐÐ¢öäG&gD6†ævS×¶ÖçVÅv–æF÷rç6WDG&gD6÷VçGÐÐ¢öä6öæf—&Ó×²‚’Óâ°Ð¢fö–BÖçVÅv–æF÷ræ6öæf—&Õ7v—F6‚‚’çF†Vâ‚†ö²’Óâ°Ð¢–b†ö²’6†÷uFö7B‚~[{.{¸þhÚ.K¨nKˆh˜~ikz©rr“°Ð¢Ò“°Ð¢×ÐÐ¢óàÐ Ð¢²ò¢)Y)YFö7B)Y)Y¢÷ÐÐ¢·Fö7Bbb€Ð¢ÆF—b7G–ÆS×·²÷6—F–öã¢vf—†VBrÂÆVgC¢Â&–v‡C¢Â&÷GFöÓ¢Â¤–æFWƒ¢ƒÂF—7Æ“¢vfÆW‚rÂ§W7F–g”6öçFVçC¢v6VçFW"rÂö–çFW$WfVçG3¢væöæRr×ÓàÐ¢ÆF—b7G–ÆS×·²&6¶w&÷VæC¢w&v&ƒS‚ÃC"ÃCÃã“"’rÂ6öÆ÷#¢r4ctTDTrÂföçE6—¦S¢2ÂÆWGFW%76–æs¢ÂFF–æs¢s‚#‚rÂ&÷&FW%&F—W3¢““’Â&÷…6†F÷s¢s‚3‚&v&ƒÃÃÃã#R’rÂæ–ÖF–öã¢v6†DfFT–âã'2V6Rr×Óç·Fö7GÓÂöF—càÐ¢ÂöF—càÐ¢—ÐÐ Ð¢ÂöF—càÐ¢“°Ð§ÐÐ