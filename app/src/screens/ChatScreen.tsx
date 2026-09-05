1	// Fyodor Chat — implements Fyodor Chat.dc.html against the production chat
2	// backend: /api/chat/messages history, send -> /api/gw/chat/stream SSE
3	// (think/text/tool_use/tool_result/usage/done/err), inline branches
4	// (branch/switch, regen prepare/finalize), edit-with-truncate, model catalog.
5	// Mounted at /dash/chat, parallel to the legacy /chat page.
6	import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type UIEvent } from 'react';
7	import { Link } from 'react-router-dom';
8	import { ROUTES } from '../navigation';
9	import { CarryoverModal } from '../components/dailySoftWindow';
10	import { useManualContextWindow } from '../hooks/useManualContextWindow';
11	import {
12	  editChatMessage,
13	  editFinalize,
14	  fetchChatMessages,
15	  fetchChatMessagesOrNull,
16	  ensureModelCatalog,
17	  getChatEffort,
18	  regenFinalize,
19	  regenPrepare,
20	  sendChatMessage,
21	  setChatEffort,
22	  setChatModel,
23	  switchChatBranch,
24	  uploadChatFile,
25	  type ChatModelCatalog,
26	  type ChatEffortMode,
27	  type ModelCatalogEntry,
28	} from '../lib/api';
29	import {
30	  artifactTypeIcon,
31	  artifactTypeLabel,
32	  cacheLabel,
33	  chatFilePreviewUrl,
34	  chatPlaceholder,
35	  findLatestRoundContext,
36	  formatCapacityLabel,
37	  fmtArtifactSize,
38	  fmtCostUsd,
39	  fmtTokens,
40	  fetchChatGatewayOnline,
41	  forceUnlockChatGenLock,
42	  guessChatErrorHint,
43	  isChoicesAnswered,
44	  streamChatReply,
45	  type ChatMsg,
46	  type ChatToolCall,
47	} from '../lib/chat';
48	import { imageUrlsFromToolValue, type ChatMediaItem } from '../lib/chatMedia';
49	import type { SoftWindowUiState } from '../lib/dailySoftWindow';
50	import {
51	  appendTextDelta,
52	  appendThinkingDelta,
53	  applyToolResult,
54	  createLiveState,
55	  isTextCaretActive,
56	  upsertToolUse,
57	  type LiveSegment,
58	  type LiveState,
59	} from '../lib/chatLiveTimeline';
60	import { realityPromptProjection } from '../lib/reality/realityPromptProjection';
61	import {
62	  availableComposerAttachmentSlots,
63	  canStartComposerAttachmentSelection,
64	  ComposerUploadCoordinator,
65	  releasePendingImageCompression,
66	  reservePendingImageCompression,
67	} from '../lib/composerUpload';
68	import {
69	  compressChatImage,
70	  createPendingChatImage,
71	  mergePendingChatImages,
72	  revokePendingChatImagePreview,
73	  type PendingChatImage,
74	} from '../lib/chatImageCompression';
75	import { ChatThemeQuickToggle, ChatThemeSegmented } from '../components/ChatThemeControl';
76	import { ThemePerfRows } from '../components/ThemePerfRows';
77	import { attachChatTheme, loadChatSettings, patchChatSettings, resolveEffectiveTheme, setChatTheme, type EffectiveTheme, type ThemeMode } from '../lib/chatTheme';
78	import { getLegacyNativeCompatDetails } from '../lib/legacyNativeCompat';
79	import {
80	  readChatWarmReturn,
81	  reconcileChatWarmReturn,
82	  writeChatWarmReturn,
83	} from '../lib/chatWarmReturn';
84	import {
85	  clearChatComposerDraft,
86	  followLatestFromGeometry,
87	  readChatComposerDraft,
88	  writeChatComposerDraft,
89	} from '../lib/chatNavigationState';
90	import {
91	  bumpHistoryGenState,
92	  cancelInFlightWarmUpState,
93	  CHAT_AUTHORITATIVE_LIMIT,
94	  CHAT_LEGACY_INITIAL_LIMIT,
95	  CHAT_LEGACY_WARMUP_LIMIT,
96	  createColdStartRaceState,
97	  hasAuthoritativeCoverage,
98	  markChatColdStart,
99	  markWarmUpSatisfiedState,
100	  mergeOlderChatMessages,
101	  needsLegacyWarmUp,
102	  onAuthoritativeHistorySuccess,
103	  planWarmUpCommit,
104	  scheduleAfterFirstPaint,
105	  shouldMarkWarmUpSatisfiedAfterPage,
106	  tryConsumeDeferredInit,
107	  type ColdStartRaceState,
108	} from '../lib/chatColdStart';
109	import {
110	  clampTranscriptWindow,
111	  followLatestAfterSearchJump,
112	  isTranscriptWindowAtLatest,
113	  latestTranscriptWindow,
114	  shiftTranscriptWindowNewer,
115	  shiftTranscriptWindowOlder,
116	  transcriptWindowAfterPrepend,
117	  transcriptWindowAroundIndex,
118	  windowSize,
119	  type TranscriptWindow,
120	} from '../lib/legacyTranscriptWindow';
121	import {
122	  countDescendants,
123	  setSkipThemePerf,
124	  setThemeProbeMode,
125	  subscribeThemePerf,
126	} from '../lib/themePerfProbe';
127	import type { ReactElement } from 'react';
128	import { installObjectHasOwnCompat } from '../lib/objectHasOwnCompat';
129	import ReactMarkdown, { type Components } from 'react-markdown';
130	import remarkGfm from 'remark-gfm';
131	import remarkBreaks from 'remark-breaks';
132	import './ChatMarkdown.css';
133	import './ChatMessage.css';
134	import { MixedSectionLabel } from '../components/MixedSectionLabel';
135	import { FONT_CN, FONT_DISPLAY, FONT_MONO, fontFamilyForText } from '../lib/typography';
136	import { TaskTimerCard } from '../components/TaskTimerCard';
137	import { isTaskTimerFixtureEnabled, useTaskTimerController } from '../lib/taskTimer';
138	import { ChatMediaGallery, ChatMediaGroup } from '../components/ChatMediaGroup';
139	import {
140	  resizeChatTextarea,
141	  type ChatScrollProbeRecord,
142	  type ChatScrollSource,
143	  writeChatScroll,
144	} from '../lib/chatScrollCoordinator';
145	
146	installObjectHasOwnCompat();
147	
148	const FONT_SIZES = [13.5, 14.5, 16, 17.5, 19];
149	const INPUT_FONT_SIZE = FONT_SIZES[0];
150	
151	function isChatScrollProbeEnabled(): boolean {
152	  if (typeof window === 'undefined') return false;
153	  const explicit = (window as Window & { __chatScrollProbe?: boolean }).__chatScrollProbe === true;
154	  const queryEnabled = new URLSearchParams(window.location.search).get('chatScrollProbe') === '1';
155	  return explicit || (import.meta.env.DEV && queryEnabled);
156	}
157	
158	function isSafeMarkdownHref(href: string): boolean {
159	  const value = href.trim();
160	  if (!value) return false;
161	  try {
162	    const url = new URL(value, window.location.href);
163	    return ['http:', 'https:', 'mailto:', 'tel:'].includes(url.protocol);
164	  } catch {
165	    return false;
166	  }
167	}
168	
169	function isExternalMarkdownHref(href: string): boolean {
170	  try {
171	    const url = new URL(href, window.location.href);
172	    return url.protocol === 'mailto:' || url.protocol === 'tel:' || url.origin !== window.location.origin;
173	  } catch {
174	    return false;
175	  }
176	}
177	
178	const markdownComponents: Components = {
179	  a({ href, children, node: _node, ...props }) {
180	    const safeHref = typeof href === 'string' && isSafeMarkdownHref(href) ? href : null;
181	    if (!safeHref) return <span className="chat-markdown-link-blocked">{children}</span>;
182	    const external = isExternalMarkdownHref(safeHref);
183	    return (
184	      <a
185	        {...props}
186	        href={safeHref}
187	        {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
188	      >
189	        {children}
190	      </a>
191	    );
192	  },
193	};
194	
195	
196	interface ChatPrefs {
197	  fontStep: number;
198	  thinkMode: 'auto' | 'drawer' | 'inline';
199	}
200	
201	function loadChatPrefs(): ChatPrefs {
202	  const s = loadChatSettings();
203	  return { fontStep: s.fontStep, thinkMode: s.thinkMode };
204	}
205	
206	type LayoutDiagRow = { label: string; value: string };
207	
208	function fmtPx(n: number) {
209	  return `${n.toFixed(2)}px`;
210	}
211	
212	function readZoom(st: CSSStyleDeclaration): string {
213	  const raw = (st as CSSStyleDeclaration & { zoom?: string }).zoom;
214	  if (!raw || raw === 'normal' || raw === '1') return 'n/a';
215	  return raw;
216	}
217	
218	function readTextSizeAdjust(st: CSSStyleDeclaration): string {
219	  const v = st.getPropertyValue('-webkit-text-size-adjust');
220	  return v || 'n/a';
221	}
222	
223	function pushElDiag(rows: LayoutDiagRow[], label: string, el: HTMLElement | null) {
224	  if (!el) {
225	    rows.push({ label: `${label} (missing)`, value: 'n/a' });
226	    return;
227	  }
228	  const rect = el.getBoundingClientRect();
229	  const st = getComputedStyle(el);
230	  rows.push({ label: `${label} offsetWidth`, value: String(el.offsetWidth) });
231	  rows.push({ label: `${label} clientWidth`, value: String(el.clientWidth) });
232	  rows.push({ label: `${label} rect.width`, value: fmtPx(rect.width) });
233	  rows.push({ label: `${label} computed zoom`, value: readZoom(st) });
234	  rows.push({ label: `${label} computed transform`, value: st.transform === 'none' ? 'none' : st.transform });
235	  rows.push({ label: `${label} -webkit-text-size-adjust`, value: readTextSizeAdjust(st) });
236	}
237	
238	function collectLayoutDiagnostics(
239	  root: HTMLElement | null,
240	  transcriptEl: HTMLElement | null,
241	  counts: {
242	    loadedCount: number;
243	    mountedCount: number;
244	    window: TranscriptWindow | null;
245	  },
246	): LayoutDiagRow[] {
247	  const vv = window.visualViewport;
248	  const testEl = document.getElementById('c78-layout-test-100') as HTMLElement | null;
249	  const appRoot = document.getElementById('root');
250	  const { body } = document;
251	  const html = document.documentElement;
252	
253	  let noto = 'n/a';
254	  let bodoni = 'n/a';
255	  try {
256	    noto = document.fonts.check('12px "Noto Serif SC"') ? 'true' : 'false';
257	    bodoni = document.fonts.check('12px "Bodoni Moda"') ? 'true' : 'false';
258	  } catch {
259	    /* FontFaceSet unavailable */
260	  }
261	
262	  const compat = getLegacyNativeCompatDetails();
263	
264	  const rows: LayoutDiagRow[] = [
265	    { label: 'legacyNativeCompat', value: String(compat.legacyNativeCompat) },
266	    { label: 'isNativeCapacitor', value: String(compat.isNativeCapacitor) },
267	    { label: 'flexGapUnsupported', value: String(compat.flexGapUnsupported) },
268	    { label: 'body data-legacy-native-compat', value: document.body.getAttribute('data-legacy-native-compat') ?? 'n/a' },
269	    { label: 'window.innerWidth', value: String(window.innerWidth) },
270	    { label: 'window.innerHeight', value: String(window.innerHeight) },
271	    { label: 'document.documentElement.clientWidth', value: String(html.clientWidth) },
272	    { label: 'screen.width', value: String(window.screen.width) },
273	    { label: 'screen.height', value: String(window.screen.height) },
274	    { label: 'window.devicePixelRatio', value: String(window.devicePixelRatio) },
275	    { label: 'visualViewport?.width', value: vv ? String(vv.width) : 'n/a' },
276	    { label: 'visualViewport?.height', value: vv ? String(vv.height) : 'n/a' },
277	    { label: 'visualViewport?.scale', value: vv ? String(vv.scale) : 'n/a' },
278	    { label: 'fonts.check Noto Serif SC', value: noto },
279	    { label: 'fonts.check Bodoni Moda', value: bodoni },
280	  ];
281	
282	  if (testEl) {
283	    const rect = testEl.getBoundingClientRect();
284	    rows.push({ label: 'test offsetWidth', value: String(testEl.offsetWidth) });
285	    rows.push({ label: 'test clientWidth', value: String(testEl.clientWidth) });
286	    rows.push({ label: 'test rect.width', value: fmtPx(rect.width) });
287	    rows.push({
288	      label: 'test rect/offset ratio',
289	      value: testEl.offsetWidth ? (rect.width / testEl.offsetWidth).toFixed(4) : 'n/a',
290	    });
291	  } else {
292	    rows.push({ label: 'test element', value: 'missing' });
293	  }
294	
295	  pushElDiag(rows, 'chat-root', root);
296	  pushElDiag(rows, '#root', appRoot);
297	  pushElDiag(rows, 'body', body);
298	  pushElDiag(rows, 'html', html);
299	
300	  rows.push({ label: 'chat-root descendant count', value: String(countDescendants(root)) });
301	  rows.push({ label: 'mounted transcript descendant count', value: String(countDescendants(transcriptEl)) });
302	  rows.push({ label: 'loaded message count', value: String(counts.loadedCount) });
303	  rows.push({ label: 'mounted message count', value: String(counts.mountedCount) });
304	  rows.push({ label: 'loaded transcript logical count', value: String(counts.loadedCount) });
305	  if (counts.window) {
306	    rows.push({ label: 'legacy window start index', value: String(counts.window.start) });
307	    rows.push({ label: 'legacy window end index', value: String(counts.window.end) });
308	    rows.push({ label: 'legacy window size', value: String(windowSize(counts.window)) });
309	  } else {
310	    rows.push({ label: 'legacy window', value: 'n/a (modern full render)' });
311	  }
312	
313	  if (root) {
314	    rows.push({ label: 'Chat root computed font-size', value: getComputedStyle(root).fontSize });
315	  }
316	
317	  return rows;
318	}
319	
320	
321	
322	interface DrawerState {
323	  text: string;
324	  label: string;
325	}
326	
327	const iconBtn: CSSProperties = {
328	  cursor: 'pointer', width: 35, height: 35, borderRadius: '50%',
329	  display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--mut)', transition: 'background .2s',
330	};
331	
332	function Svg({ d, size = 16, sw = 1.6 }: { d: string; size?: number; sw?: number }) {
333	  return (
334	    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round">
335	      {d.split('|').map((p, i) => (
336	        <path key={i} d={p} />
337	      ))}
338	    </svg>
339	  );
340	}
341	
342	const IC = {
343	  wrench: 'M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z',
344	  moon: 'M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z',
345	  clock: 'M12 7v5l3 2',
346	  edit: 'M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z',
347	  redo: 'M3 12a9 9 0 1 0 3-6.7|M3 4v5h5',
348	  refresh: 'M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8|M21 3v5h-5',
349	  window: 'M3 7h5v12H3z|M16 7h5v12h-5z|M3 7h18',
350	  up: 'M12 19V5|M5 12l7-7 7 7',
351	  down: 'M12 5v14|M19 12l-7 7-7-7',
352	  plus: 'M12 5v14|M5 12h14',
353	  chev: 'M6 9l6 6 6-6',
354	  tool: 'M4 17l6-5-6-5|M12 19h8',
355	  clip: 'M21 12.5l-8.2 8.2a5 5 0 0 1-7-7l8.7-8.7a3.3 3.3 0 0 1 4.7 4.7l-8.7 8.7a1.66 1.66 0 0 1-2.3-2.3l8-8',
356	  brain: 'M12 5a3 3 0 0 0-5.9.6A3.5 3.5 0 0 0 4 9a3.5 3.5 0 0 0 .6 5.4A3.2 3.2 0 0 0 8 19c.6 0 1.2-.2 1.7-.5.6.9 1.4 1.5 2.3 1.5|M12 5a3 3 0 0 1 5.9.6A3.5 3.5 0 0 1 20 9a3.5 3.5 0 0 1-.6 5.4A3.2 3.2 0 0 1 16 19c-.6 0-1.2-.2-1.7-.5-.6.9-1.4 1.5-2.3 1.5|M12 5v15',
357	  thumb: 'M7 10v12H4a1 1 0 0 1-1-1V11a1 1 0 0 1 1-1h3zm0 0l4.5-7a2.4 2.4 0 0 1 2.4 2.4V9h5a2 2 0 0 1 2 2.3l-1.2 8A2 2 0 0 1 17.7 21H7',
358	};
359	
360	function CopyIcon({ size = 15 }: { size?: number }) {
361	  return (
362	    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
363	      <rect x="9" y="9" width="11" height="11" rx="2" />
364	      <path d="M5 15V5a2 2 0 0 1 2-2h10" />
365	    </svg>
366	  );
367	}
368	
369	const MAX_COMPOSER_ATTACHMENTS = 4;
370	
371	export function ChatScreen() {
372	  const [settings, setSettings] = useState<ChatPrefs>(loadChatPrefs);
373	  const legacyCompat = useMemo(() => getLegacyNativeCompatDetails().legacyNativeCompat, []);
374	  const [warmSnapshot] = useState(() => readChatWarmReturn(legacyCompat));
375	  const [wide, setWide] = useState(() => window.innerWidth >= 900);
376	  const [compactToolbar, setCompactToolbar] = useState(() => window.innerWidth <= 360);
377	  const [genLockBusy, setGenLockBusy] = useState(false);
378	
379	  const timerFixtureEnabled = useMemo(() => isTaskTimerFixtureEnabled(), []);
380	  const {
381	    snapshot: taskTimerSnapshot,
382	    onComplete: handleTaskTimerComplete,
383	    completing: taskTimerCompleting,
384	  } = useTaskTimerController(timerFixtureEnabled);
385	
386	  const [msgs, setMsgs] = useState<ChatMsg[]>(() => warmSnapshot ? warmSnapshot.messages : []);
387	  const [hasMoreBefore, setHasMoreBefore] = useState(() => warmSnapshot ? warmSnapshot.hasMoreBefore : false);
388	  const [loadingMore, setLoadingMore] = useState(false);
389	
390	  const [input, setInput] = useState(readChatComposerDraft);
391	  const [sending, setSending] = useState(false);
392	  const [posting, setPosting] = useState(false);
393	  const [live, setLive] = useState<LiveState | null>(null);
394	  const [pendingConfirmation, setPendingConfirmation] = useState<ChatToolCall | null>(null);
395	  const [pendingFiles, setPendingFiles] = useState<Array<{ fileUrl: string; fileName: string }>>([]);
396	  const [pendingImages, setPendingImages] = useState<PendingChatImage[]>([]);
397	  const [uploadingFileCount, setUploadingFileCount] = useState(0);
398	  const [compressingImageCount, setCompressingImageCount] = useState(0);
399	
400	  const [navOpen, setNavOpen] = useState<null | 'wrench' | 'font' | 'search' | 'profile'>(null);
401	  const [searchQ, setSearchQ] = useState('');
402	  const [modelPopOpen, setModelPopOpen] = useState(false);
403	  const [attachMenuOpen, setAttachMenuOpen] = useState(false);
404	  const [models, setModels] = useState<ModelCatalogEntry[]>([]);
405	  const [currentModel, setCurrentModel] = useState('');
406	  const [chatProvider, setChatProvider] = useState<'api_relay' | 'claude_code' | ''>('');
407	  const [modelMode, setModelMode] = useState<'default' | 'explicit' | 'unknown' | ''>('');
408	  const [currentEffort, setCurrentEffort] = useState('');
409	  const [effortMode, setEffortMode] = useState<ChatEffortMode | ''>('');
410	  const [allowedEfforts, setAllowedEfforts] = useState<string[]>([]);
411	
412	  const [openThink, setOpenThink] = useState<Record<string, boolean>>({});
413	  const [openTools, setOpenTools] = useState<Record<string, boolean>>({});
414	  const [editingId, setEditingId] = useState<number | null>(null);
415	  const [editText, setEditText] = useState('');
416	  const [drawer, setDrawer] = useState<DrawerState | null>(null);
417	  const [sidebarOpen, setSidebarOpen] = useState(false);
418	  const [toast, setToast] = useState<string | null>(null);
419	  const [flashId, setFlashId] = useState<number | null>(null);
420	  const [liked, setLiked] = useState<Record<number, 1 | -1>>({});
421	  const [endpointOnline, setEndpointOnline] = useState<boolean | null>(null);
422	  const [initialHistoryReady, setInitialHistoryReady] = useState(() => warmSnapshot !== null);
423	  const [refreshing, setRefreshing] = useState(false);
424	  const [chatError, setChatError] = useState<{ message: string; hint: string } | null>(null);
425	  const [gallery, setGallery] = useState<{ items: ChatMediaItem[]; currentIndex: number } | null>(null);
426	  const [pickedChoices, setPickedChoices] = useState<Record<number, string>>({});
427	  const [layoutDiag, setLayoutDiag] = useState<LayoutDiagRow[] | null>(null);
428	  const [txWin, setTxWin] = useState<TranscriptWindow>(() => warmSnapshot ? { ...warmSnapshot.txWin } : { start: 0, end: 0 });
429	
430	  const manualWindow = useManualContextWindow();
431	  const switchBlocked =
432	    sending || live !== null || genLockBusy || manualWindow.submitting;
433	
434	  const followLatestRef = useRef(warmSnapshot ? warmSnapshot.followLatest : true);
435	  const msgsRef = useRef<ChatMsg[]>([]);
436	  const hasMoreBeforeRef = useRef(false);
437	  const txWinRef = useRef<TranscriptWindow>({ start: 0, end: 0 });
438	  const warmRestoreRef = useRef(warmSnapshot);
439	  const pendingAnchorIdRef = useRef<number | null>(null);
440	  const pendingJumpIdRef = useRef<number | null>(null);
441	
442	  const scrollRef = useRef<HTMLDivElement>(null);
443	  const chatRootRef = useRef<HTMLDivElement>(null);
444	  const taRef = useRef<HTMLTextAreaElement>(null);
445	  const imgInputRef = useRef<HTMLInputElement>(null);
446	  const fileInputRef = useRef<HTMLInputElement>(null);
447	  const abortRef = useRef<AbortController | null>(null);
448	  const toastTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
449	  const mountedRef = useRef(true);
450	  const liveRef = useRef<LiveState | null>(null);
451	  const postingRef = useRef(false);
452	  const warmUpInflightRef = useRef<Promise<void> | null>(null);
453	  const coldStartRaceRef = useRef<ColdStartRaceState>(createColdStartRaceState());
454	  const composerMutationRevisionRef = useRef(0);
455	  const composerDraftRevisionRef = useRef(0);
456	  const uploadingFileSlotsRef = useRef(0);
457	  const compressingImageIdsRef = useRef<Set<string>>(new Set());
458	  const pendingFilesRef = useRef<Array<{ fileUrl: string; fileName: string }>>([]);
459	  const pendingImagesRef = useRef<PendingChatImage[]>([]);
460	  const uploadCoordinatorRef = useRef(new ComposerUploadCoordinator(
461	    () => composerMutationRevisionRef.current,
462	    (files) => {
463	      setPendingFiles((current) => {
464	        const next = [...current, ...files].slice(0, MAX_COMPOSER_ATTACHMENTS);
465	        pendingFilesRef.current = next;
466	        return next;
467	      });
468	    },
469	    (images) => {
470	      setPendingImages((current) => {
471	        const next = mergePendingChatImages(current, images).slice(0, MAX_COMPOSER_ATTACHMENTS);
472	        pendingImagesRef.current = next;
473	        return next;
474	      });
475	    },
476	  ));
477	
478	  const effThinkMode = settings.thinkMode === 'auto' ? (wide ? 'inline' : 'drawer') : settings.thinkMode;
479	
480	  msgsRef.current = msgs;
481	  hasMoreBeforeRef.current = hasMoreBefore;
482	  txWinRef.current = txWin;
483	  pendingFilesRef.current = pendingFiles;
484	  pendingImagesRef.current = pendingImages;
485	
486	  const availableComposerSlots = () => availableComposerAttachmentSlots({
487	    maxAttachments: MAX_COMPOSER_ATTACHMENTS,
488	    pendingFiles: pendingFilesRef.current.length,
489	    pendingImages: pendingImagesRef.current.length,
490	    uploadingFileReservations: uploadingFileSlotsRef.current,
491	  });
492	
493	  const removePendingImage = useCallback((id: string) => {
494	    if (postingRef.current) return;
495	    const current = pendingImagesRef.current;
496	    const removed = current.find((image) => image.id === id);
497	    if (!removed) return;
498	    revokePendingChatImagePreview(removed);
499	    const released = releasePendingImageCompression(compressingImageIdsRef.current, id);
500	    if (released) setCompressingImageCount(compressingImageIdsRef.current.size);
501	    const next = current.filter((image) => image.id !== id);
502	    pendingImagesRef.current = next;
503	    setPendingImages(next);
504	  }, []);
505	
506	  const handleTranscriptScroll = useCallback((event: UIEvent<HTMLDivElement>) => {
507	    const target = event.currentTarget;
508	    followLatestRef.current = followLatestFromGeometry(
509	      {
510	        scrollHeight: target.scrollHeight,
511	        scrollTop: target.scrollTop,
512	        clientHeight: target.clientHeight,
513	      },
514	      !legacyCompat || isTranscriptWindowAtLatest(txWinRef.current, msgsRef.current.length),
515	    );
516	  }, [legacyCompat]);
517	
518	  const placeholder = useMemo(() => chatPlaceholder(new Date()), []);
519	  const capacityLabel = useMemo(
520	    () => formatCapacityLabel(findLatestRoundContext(msgs)),
521	    [msgs],
522	  );
523	  const capacityTitle = '上下文容量；90k 为 soft Swap 门槛，另有 30 轮触发';
524	  const dateLabel = useMemo(() => {
525	    const now = new Date();
526	    const dows = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
527	    return `今天 · ${now.getMonth() + 1}月${now.getDate()}日 ${dows[now.getDay()]}`;
528	  }, []);
529	
530	  const patchSettings = (p: Partial<ChatPrefs>) => {
531	    const merged = patchChatSettings(p);
532	    setSettings({ fontStep: merged.fontStep, thinkMode: merged.thinkMode });
533	  };
534	
535	  const refreshLayoutDiag = useCallback(() => {
536	    setLayoutDiag(collectLayoutDiagnostics(chatRootRef.current, scrollRef.current, {
537	      loadedCount: msgs.length,
538	      mountedCount: legacyCompat ? windowSize(txWin) : msgs.length,
539	      window: legacyCompat ? txWin : null,
540	    }));
541	  }, [msgs.length, legacyCompat, txWin]);
542	
543	  const pinTranscriptToLatest = useCallback(() => {
544	    followLatestRef.current = true;
545	    if (legacyCompat) setTxWin(latestTranscriptWindow(msgs.length));
546	  }, [legacyCompat, msgs.length]);
547	
548	  const runHiddenTranscriptThemeProbe = useCallback(() => {
549	    const root = chatRootRef.current;
550	    const scroll = scrollRef.current;
551	    if (!root || !scroll) return;
552	
553	    const originalMode = loadChatSettings().theme;
554	    const effective = resolveEffectiveTheme(originalMode);
555	    const probeTarget: EffectiveTheme = effective === 'dark' ? 'light' : 'dark';
556	    const prevDisplay = scroll.style.display;
557	
558	    let unsub: (() => void) | null = null;
559	    let restored = false;
560	
561	    const restore = () => {
562	      if (restored) return;
563	      restored = true;
564	      unsub?.();
565	      unsub = null;
566	      try {
567	        setSkipThemePerf(true);
568	        setChatTheme(root, originalMode);
569	      } catch {
570	        /* fail-safe: still restore DOM/mode below */
571	      } finally {
572	        setSkipThemePerf(false);
573	        scroll.style.display = prevDisplay;
574	        setThemeProbeMode('normal');
575	      }
576	    };
577	
578	    try {
579	      scroll.style.display = 'none';
580	      setThemeProbeMode('transcript-hidden');
581	
582	      unsub = subscribeThemePerf((record) => {
583	        if (record.mode !== 'transcript-hidden') return;
584	        restore();
585	      });
586	
587	      setChatTheme(root, probeTarget as ThemeMode);
588	    } catch {
589	      restore();
590	    }
591	  }, []);
592	
593	  const showToast = useCallback((t: string) => {
594	    setToast(t);
595	    clearTimeout(toastTimer.current);
596	    toastTimer.current = setTimeout(() => setToast(null), 2200);
597	  }, []);
598	
599	  const reportChatScroll = useCallback((record: ChatScrollProbeRecord) => {
600	    if (!isChatScrollProbeEnabled()) return;
601	    console.debug('[CHAT_SCROLL_PROBE]', record);
602	  }, []);
603	
604	  const scrollBottom = useCallback((options: boolean | { smooth?: boolean; source?: ChatScrollSource } = false) => {
605	    const smooth = typeof options === 'boolean' ? options : Boolean(options.smooth);
606	    const source = typeof options === 'boolean'
607	      ? 'explicit-scroll-bottom'
608	      : (options.source ?? 'explicit-scroll-bottom');
609	    requestAnimationFrame(() => {
610	      const c = scrollRef.current;
611	      if (!c) return;
612	      writeChatScroll(
613	        c,
614	        {
615	          source,
616	          intent: 'follow-latest',
617	          followLatest: followLatestRef.current,
618	          behavior: smooth ? 'smooth' : 'auto',
619	        },
620	        reportChatScroll,
621	      );
622	    });
623	  }, [reportChatScroll]);
624	
625	  const bumpHistoryGen = useCallback(() => bumpHistoryGenState(coldStartRaceRef.current), []);
626	
627	  const cancelInFlightWarmUp = useCallback(() => {
628	    cancelInFlightWarmUpState(coldStartRaceRef.current);
629	    warmUpInflightRef.current = null;
630	  }, []);
631	
632	  const applyCatalog = useCallback((r: ChatModelCatalog) => {
633	    setModels(r.models);
634	    setChatProvider(r.provider);
635	    setModelMode(r.modelMode);
636	    setCurrentModel(r.configuredModel || r.current || '');
637	  }, []);
638	
639	  const applyEffort = useCallback((r: Awaited<ReturnType<typeof getChatEffort>>) => {
640	    if (r.provider !== 'claude_code') {
641	      setCurrentEffort('');
642	      setEffortMode(r.effortMode);
643	      setAllowedEfforts([]);
644	      return;
645	    }
646	    setCurrentEffort(r.configuredEffort || '');
647	    setEffortMode(r.effortMode);
648	    setAllowedEfforts(r.allowedEfforts);
649	  }, []);
650	
651	  const startModelCatalog = useCallback(() => {
652	    markChatColdStart('catalog_start');
653	    void Promise.all([ensureModelCatalog(), getChatEffort()]).then(([catalog, effort]) => {
654	      markChatColdStart('catalog_ready');
655	      if (!mountedRef.current) return;
656	      applyCatalog(catalog);
657	      applyEffort(effort);
658	    });
659	  }, [applyCatalog, applyEffort]);
660	
661	  const runLegacyWarmUp = useCallback(async (anchorGen: number, earliestId: number) => {
662	    const race = coldStartRaceRef.current;
663	    if (!legacyCompat || race.warmUpSatisfied) return;
664	    if (warmUpInflightRef.current) return warmUpInflightRef.current;
665	
666	    const warmGen = cancelInFlightWarmUpState(race);
667	    markChatColdStart('background_warm_start');
668	
669	    const task = (async () => {
670	      const warmPage = await fetchChatMessagesOrNull({
671	        before: earliestId,
672	        limit: CHAT_LEGACY_WARMUP_LIMIT,
673	      });
674	      if (!warmPage) return;
675	      if (warmGen !== coldStartRaceRef.current.warmUpGen) return;
676	      if (anchorGen !== coldStartRaceRef.current.historyGen) return;
677	      if (!followLatestRef.current) return;
678	      if (!mountedRef.current) return;
679	
680	      const { mergedCount } = planWarmUpCommit(msgsRef.current, warmPage.messages);
681	
682	      setMsgs((latest) => {
683	        if (!mountedRef.current) return latest;
684	        if (anchorGen !== coldStartRaceRef.current.historyGen) return latest;
685	        if (!followLatestRef.current) return latest;
686	        return mergeOlderChatMessages(latest, warmPage.messages);
687	      });
688	
689	      if (!mountedRef.current) return;
690	      if (anchorGen !== coldStartRaceRef.current.historyGen) return;
691	      if (!followLatestRef.current) return;
692	
693	      setHasMoreBefore(warmPage.hasMoreBefore);
694	      if (shouldMarkWarmUpSatisfiedAfterPage(mergedCount, warmPage.hasMoreBefore)) {
695	        markWarmUpSatisfiedState(coldStartRaceRef.current);
696	      }
697	      markChatColdStart('background_warm_ready');
698	    })().finally(() => {
699	      if (warmUpInflightRef.current === task) warmUpInflightRef.current = null;
700	    });
701	
702	    warmUpInflightRef.current = task;
703	    return task;
704	  }, [legacyCompat]);
705	
706	  const ensureDeferredColdStartInit = useCallback((opts: {
707	    anchorGen?: number;
708	    earliestId?: number;
709	    loadedCount: number;
710	  }) => {
711	    if (!mountedRef.current) return;
712	    if (!tryConsumeDeferredInit(coldStartRaceRef.current)) return;
713	    markChatColdStart('first_history_paint_scheduled');
714	    setInitialHistoryReady(true);
715	    startModelCatalog();
716	    if (
717	      opts.earliestId
718	      && needsLegacyWarmUp(legacyCompat, coldStartRaceRef.current, opts.loadedCount)
719	    ) {
720	      void runLegacyWarmUp(opts.anchorGen ?? coldStartRaceRef.current.historyGen, opts.earliestId);
721	    }
722	  }, [legacyCompat, startModelCatalog, runLegacyWarmUp]);
723	
724	  const refetchLatest = useCallback(async (toBottom = true) => {
725	    const gen = bumpHistoryGen();
726	    cancelInFlightWarmUp();
727	    const page = await fetchChatMessages({ limit: CHAT_AUTHORITATIVE_LIMIT });
728	    if (gen !== coldStartRaceRef.current.historyGen) return;
729	    if (!mountedRef.current) return;
730	    onAuthoritativeHistorySuccess(coldStartRaceRef.current, page.messages.length);
731	    setMsgs(page.messages);
732	    setHasMoreBefore(page.hasMoreBefore);
733	    scheduleAfterFirstPaint(() => {
734	      if (!mountedRef.current) return;
735	      ensureDeferredColdStartInit({ loadedCount: page.messages.length });
736	    });
737	    if (toBottom) scrollBottom({ source: 'initial-history' });
738	  }, [scrollBottom, bumpHistoryGen, cancelInFlightWarmUp, ensureDeferredColdStartInit]);
739	
740	  const revalidateWarmReturn = useCallback(async () => {
741	    const gen = bumpHistoryGen();
742	    cancelInFlightWarmUp();
743	    try {
744	      const page = await fetchChatMessages({ limit: CHAT_AUTHORITATIVE_LIMIT });
745	      if (gen !== coldStartRaceRef.current.historyGen) return;
746	      if (!mountedRef.current) return;
747	      onAuthoritativeHistorySuccess(coldStartRaceRef.current, page.messages.length);
748	      const reconciled = reconcileChatWarmReturn(
749	        { messages: msgsRef.current, hasMoreBefore: hasMoreBeforeRef.current },
750	        page,
751	      );
752	      setMsgs(reconciled.messages);
753	      setHasMoreBefore(reconciled.hasMoreBefore);
754	      if (followLatestRef.current) scrollBottom({ source: 'warm-restore' });
755	      scheduleAfterFirstPaint(() => {
756	        if (!mountedRef.current) return;
757	        ensureDeferredColdStartInit({ loadedCount: page.messages.length });
758	      });
759	    } catch {
760	      // Keep the already-painted snapshot when silent revalidation fails.
761	    }
762	  }, [scrollBottom, bumpHistoryGen, cancelInFlightWarmUp, ensureDeferredColdStartInit]);
763	
764	  const flushLegacyWarmUp = useCallback(async () => {
765	    const race = coldStartRaceRef.current;
766	    if (!legacyCompat || race.warmUpSatisfied || hasAuthoritativeCoverage(msgs.length)) return;
767	    const earliestId = msgs[0]?.id;
768	    if (!earliestId) return;
769	    await runLegacyWarmUp(race.historyGen, earliestId);
770	  }, [legacyCompat, msgs, runLegacyWarmUp]);
771	
772	  const toggleModelPop = useCallback(() => {
773	    setModelPopOpen((open) => {
774	      const next = !open;
775	      if (next) startModelCatalog();
776	      return next;
777	    });
778	  }, [startModelCatalog]);
779	
780	  const toggleSearchNav = useCallback(() => {
781	    setNavOpen((open) => {
782	      const next = open === 'search' ? null : 'search';
783	      if (next === 'search' && legacyCompat) void flushLegacyWarmUp();
784	      return next;
785	    });
786	  }, [legacyCompat, flushLegacyWarmUp]);
787	
788	  const refreshChat = useCallback(async () => {
789	    if (refreshing) return;
790	    setRefreshing(true);
791	    setNavOpen(null);
792	    setChatError(null);
793	    abortRef.current?.abort();
794	    abortRef.current = null;
795	    liveRef.current = null;
796	    setLive(null);
797	    setSending(false);
798	    try {
799	      const unlock = await forceUnlockChatGenLock();
800	      pinTranscriptToLatest();
801	      await refetchLatest();
802	      const online = await fetchChatGatewayOnline();
803	      setEndpointOnline(online);
804	      if (unlock.ok && !unlock.busy) showToast('已刷新');
805	      else if (unlock.busy) showToast('锁仍占用，消息已刷新');
806	      else showToast('已刷新（解锁请求失败）');
807	    } finally {
808	      setRefreshing(false);
809	    }
810	  }, [refreshing, refetchLatest, showToast, pinTranscriptToLatest]);
811	
812	  // Cold start: history first; a valid warm snapshot skips only the visible cold path.
813	  useEffect(() => {
814	    let cancelled = false;
815	    if (warmSnapshot) {
816	      scheduleAfterFirstPaint(() => {
817	        if (cancelled || !mountedRef.current) return;
818	        ensureDeferredColdStartInit({ loadedCount: msgsRef.current.length });
819	        void revalidateWarmReturn();
820	      });
821	      return () => { cancelled = true; };
822	    }
823	
824	    const gen = bumpHistoryGen();
825	    cancelInFlightWarmUp();
826	    markChatColdStart('chat_mount');
827	    markChatColdStart('initial_history_start');
828	
829	    const loadInitial = async () => {
830	      const limit = legacyCompat ? CHAT_LEGACY_INITIAL_LIMIT : CHAT_AUTHORITATIVE_LIMIT;
831	      const page = await fetchChatMessages({ limit });
832	      if (cancelled || !mountedRef.current) return;
833	      const superseded = gen !== coldStartRaceRef.current.historyGen;
834	      if (!superseded) {
835	        if (hasAuthoritativeCoverage(page.messages.length)) {
836	          onAuthoritativeHistorySuccess(coldStartRaceRef.current, page.messages.length);
837	        }
838	        if (!mountedRef.current) return;
839	        setMsgs(page.messages);
840	        setHasMoreBefore(page.hasMoreBefore);
841	        scrollBottom({ source: 'initial-history' });
842	        markChatColdStart('initial_history_ready');
843	        scheduleAfterFirstPaint(() => {
844	          if (cancelled || !mountedRef.current) return;
845	          ensureDeferredColdStartInit({
846	            anchorGen: gen,
847	            earliestId: page.messages[0]?.id,
848	            loadedCount: page.messages.length,
849	          });
850	        });
851	      }
852	    };
853	    void loadInitial();
854	    return () => { cancelled = true; };
855	  }, [
856	    warmSnapshot, legacyCompat, scrollBottom, bumpHistoryGen, cancelInFlightWarmUp,
857	    ensureDeferredColdStartInit, revalidateWarmReturn,
858	  ]);
859	
860	  // media listeners (layout only — theme uses DOM data-chat-theme, not parent state)
861	  useEffect(() => {
862	    const onRs = () => {
863	      setWide(window.innerWidth >= 900);
864	      setCompactToolbar(window.innerWidth <= 360);
865	    };
866	    window.addEventListener('resize', onRs);
867	    return () => window.removeEventListener('resize', onRs);
868	  }, []);
869	
870	  useLayoutEffect(() => {
871	    const root = chatRootRef.current;
872	    if (!root) return;
873	    return attachChatTheme(root);
874	  }, []);
875	
876	  useLayoutEffect(() => {
877	    const root = chatRootRef.current;
878	    if (!root) return;
879	    if (legacyCompat) {
880	      root.setAttribute('data-chat-legacy-renderer', 'true');
881	    } else {
882	      root.removeAttribute('data-chat-legacy-renderer');
883	    }
884	  }, [legacyCompat]);
885	
886	  // Legacy DOM window: follow latest or clamp when loaded msgs length changes.
887	  useLayoutEffect(() => {
888	    if (!legacyCompat) return;
889	    setTxWin((w) => (
890	      followLatestRef.current
891	        ? latestTranscriptWindow(msgs.length)
892	        : clampTranscriptWindow(w.start, w.end, msgs.length)
893	    ));
894	  }, [msgs.length, legacyCompat]);
895	
896	  // Deterministic scroll after window shift / search jump (post-commit).
897	  useLayoutEffect(() => {
898	    if (!legacyCompat) return;
899	    const jumpId = pendingJumpIdRef.current;
900	    if (jumpId != null) {
901	      pendingJumpIdRef.current = null;
902	      pendingAnchorIdRef.current = null;
903	      const el = document.getElementById(`msg-${jumpId}`);
904	      const c = scrollRef.current;
905	      if (el && c) {
906	        writeChatScroll(
907	          c,
908	          {
909	            source: 'search-jump',
910	            intent: 'explicit-target',
911	            followLatest: followLatestRef.current,
912	            targetScrollTop: el.offsetTop - 80,
913	            behavior: 'smooth',
914	          },
915	          reportChatScroll,
916	        );
917	      }
918	      return;
919	    }
920	    const anchorId = pendingAnchorIdRef.current;
921	    if (anchorId == null) return;
922	    pendingAnchorIdRef.current = null;
923	    const el = document.getElementById(`msg-${anchorId}`);
924	    const c = scrollRef.current;
925	    if (el && c) {
926	      writeChatScroll(
927	        c,
928	        {
929	          source: 'history-window',
930	          intent: 'explicit-target',
931	          followLatest: followLatestRef.current,
932	          targetScrollTop: el.offsetTop - 80,
933	        },
934	        reportChatScroll,
935	      );
936	    }
937	  }, [txWin, msgs, legacyCompat, reportChatScroll]);
938	
939	  useLayoutEffect(() => {
940	    const snapshot = warmRestoreRef.current;
941	    if (!snapshot) return;
942	    warmRestoreRef.current = null;
943	    const container = scrollRef.current;
944	    if (!container) return;
945	    writeChatScroll(
946	      container,
947	      {
948	        source: 'warm-restore',
949	        intent: snapshot.followLatest ? 'follow-latest' : 'preserve-position',
950	        followLatest: snapshot.followLatest,
951	        targetScrollTop: snapshot.scrollTop,
952	      },
953	      reportChatScroll,
954	    );
955	  }, [reportChatScroll]);
956	
957	  useLayoutEffect(() => {
958	    const textarea = taRef.current;
959	    if (!textarea) return;
960	    resizeChatTextarea(textarea, scrollRef.current, followLatestRef.current, reportChatScroll);
961	  }, [input, reportChatScroll]);
962	
963	  useEffect(() => {
964	    if (!initialHistoryReady) return;
965	    let cancelled = false;
966	    const pollLock = async () => {
967	      try {
968	        const resp = await fetch('/api/gw/chat/lock', { credentials: 'include' });
969	        if (!resp.ok) return;
970	        const data = (await resp.json()) as { busy?: boolean };
971	        if (!cancelled) setGenLockBusy(Boolean(data.busy));
972	      } catch {
973	        /* ignore */
974	      }
975	    };
976	    void pollLock();
977	    const id = setInterval(pollLock, 2500);
978	    return () => {
979	      cancelled = true;
980	      clearInterval(id);
981	    };
982	  }, [initialHistoryReady, sending, live]);
983	
984	  // gateway reachability — breathing status under the name (first probe after history ready)
985	  useEffect(() => {
986	    if (!initialHistoryReady) return;
987	    let cancelled = false;
988	    const check = async () => {
989	      const online = await fetchChatGatewayOnline();
990	      if (!cancelled) setEndpointOnline(online);
991	    };
992	    void check();
993	    const iv = setInterval(() => { void check(); }, 20000);
994	    return () => { cancelled = true; clearInterval(iv); };
995	  }, [initialHistoryReady]);
996	
997	  // poll for new messages (e.g. wake messages from the api-side) when idle
998	  useEffect(() => {
999	    const iv = setInterval(() => {
1000	      if (document.hidden || liveRef.current || sending) return;
1001	      setMsgs((cur) => {
1002	        const newest = cur.length ? cur[cur.length - 1].id : 0;
1003	        fetchChatMessages({ after: newest, limit: 50 }).then((page) => {
1004	          if (page.messages.length) {
1005	            setMsgs((c2) => {
1006	              const known = new Set(c2.map((m) => m.id));
1007	              const fresh = page.messages.filter((m) => !known.has(m.id));
1008	              return fresh.length ? [...c2, ...fresh] : c2;
1009	            });
1010	            // Legacy browsing older window: do not yank to latest on background poll.
1011	            if (!legacyCompat || followLatestRef.current) {
1012	              scrollBottom({ smooth: true, source: 'background-poll' });
1013	            }
1014	          }
1015	        });
1016	        return cur;
1017	      });
1018	    }, 10000);
1019	    return () => clearInterval(iv);
1020	  }, [sending, scrollBottom, legacyCompat]);
1021	
1022	  // Cold-start lifecycle: setup resets mounted for StrictMode dev replay (setup→cleanup→setup).
1023	  useEffect(() => {
1024	    mountedRef.current = true;
1025	
1026	    return () => {
1027	      if (msgsRef.current.length) {
1028	        writeChatWarmReturn({
1029	          legacyCompat,
1030	          messages: msgsRef.current,
1031	          hasMoreBefore: hasMoreBeforeRef.current,
1032	          txWin: txWinRef.current,
1033	          followLatest: followLatestRef.current,
1034	          scrollTop: scrollRef.current?.scrollTop ?? 0,
1035	        });
1036	      }
1037	      pendingImagesRef.current.forEach(revokePendingChatImagePreview);
1038	      pendingImagesRef.current = [];
1039	      compressingImageIdsRef.current.clear();
1040	      mountedRef.current = false;
1041	      cancelInFlightWarmUpState(coldStartRaceRef.current);
1042	      bumpHistoryGenState(coldStartRaceRef.current);
1043	      warmUpInflightRef.current = null;
1044	      abortRef.current?.abort();
1045	      clearTimeout(toastTimer.current);
1046	    };
1047	  }, []);
1048	
1049	  const updateLive = useCallback((fn: (l: LiveState) => LiveState) => {
1050	    liveRef.current = fn(liveRef.current ?? createLiveState());
1051	    setLive(liveRef.current);
1052	  }, []);
1053	
1054	  const clearLivePresentation = useCallback(() => {
1055	    liveRef.current = null;
1056	    setLive(null);
1057	  }, []);
1058	
1059	  const runStream = useCallback(
1060	    async (
1061	      userMessageId: number | null,
1062	      opts: {
1063	        rewriteId?: string | null;
1064	        realityContext?: string;
1065	        confirmation?: { approvalId: string; pendingActionId?: string | null; decision: 'approve' | 'reject' };
1066	      } = {},
1067	    ): Promise<boolean> => {
1068	      // Invariant: any path entering live streaming pins the DOM window to latest
1069	      // so live replies never render under an old browsing window.
1070	      pinTranscriptToLatest();
1071	      liveRef.current = createLiveState();
1072	      setLive(liveRef.current);
1073	      const ctrl = new AbortController();
1074	      abortRef.current = ctrl;
1075	      const res = await streamChatReply(
1076	        userMessageId,
1077	        {
1078	          onThink: (d) => {
1079	            updateLive((l) => appendThinkingDelta(l, d));
1080	            scrollBottom({ source: 'stream-follow' });
1081	          },
1082	          onText: (d) => {
1083	            updateLive((l) => appendTextDelta(l, d));
1084	            scrollBottom({ source: 'stream-follow' });
1085	          },
1086	          onToolUse: (idx, tc) => {
1087	            updateLive((l) => upsertToolUse(l, idx, tc));
1088	            scrollBottom({ source: 'stream-follow' });
1089	          },
1090	          onToolResult: (idx, tc) => {
1091	            updateLive((l) => applyToolResult(l, idx, tc));
1092	            scrollBottom({ source: 'stream-follow' });
1093	          },
1094	          onNotice: (s) => showToast(s),
1095	        },
1096	        ctrl,
1097	        {
1098	          rewriteId: opts.rewriteId,
1099	          approvalId: opts.confirmation?.approvalId,
1100	          pendingActionId: opts.confirmation?.pendingActionId,
1101	          confirmationDecision: opts.confirmation?.decision,
1102	          realityContext: opts.realityContext,
1103	        },
1104	      );
1105	      if (res.deferredTool) {
1106	        setPendingConfirmation({
1107	          ...res.deferredTool,
1108	          running: false,
1109	          confirmation_state: 'pending',
1110	        });
1111	      }
1112	      if (!res.ok || res.deferredTool) clearLivePresentation();
1113	      if (!res.ok && res.error) {
1114	        if (!ctrl.signal.aborted) {
1115	          setChatError({ message: res.error, hint: guessChatErrorHint(res.error) });
1116	          scrollBottom(true);
1117	        }
1118	      }
1119	      return res.ok;
1120	    },
1121	    [clearLivePresentation, scrollBottom, showToast, updateLive, pinTranscriptToLatest],
1122	  );
1123	
1124	  const confirmDeferred = useCallback(async (decision: 'approve' | 'reject') => {
1125	    const pending = pendingConfirmation;
1126	    const approvalId = pending?.approval_id;
1127	    const pendingActionId = pending?.pending_action_id;
1128	    if (!pending || !approvalId || sending || pending.confirmation_state === 'processing') return;
1129	    setPendingConfirmation({ ...pending, confirmation_state: 'processing', running: false });
1130	    setSending(true);
1131	    setChatError(null);
1132	    const realityContext = decision === 'approve'
1133	      ? realityPromptProjection.getSnapshot().text
1134	      : undefined;
1135	    const ok = await runStream(null, {
1136	      confirmation: { approvalId, pendingActionId, decision },
1137	      realityContext,
1138	    });
1139	    if (decision === 'reject') {
1140	      clearLivePresentation();
1141	      setPendingConfirmation({ ...pending, confirmation_state: 'rejected', running: false });
1142	    } else if (ok) {
1143	      await refetchLatest();
1144	      clearLivePresentation();
1145	      setPendingConfirmation(null);
1146	    } else {
1147	      setPendingConfirmation({ ...pending, confirmation_state: 'pending', running: false });
1148	    }
1149	    setSending(false);
1150	  }, [clearLivePresentation, pendingConfirmation, sending, runStream, refetchLatest]);
1151	
1152	  const send = useCallback(async () => {
1153	    const rawText = input;
1154	    const sendText = rawText.trim();
1155	    const attempt = {
1156	      rawText,
1157	      text: sendText,
1158	      files: pendingFiles,
1159	      images: pendingImages,
1160	    };
1161	    if ((!attempt.text && !attempt.files.length && !attempt.images.length)
1162	      || sending
1163	      || uploadingFileCount > 0
1164	      || compressingImageCount > 0) return;
1165	    const realityContext = realityPromptProjection.getSnapshot().text;
1166	    const draftRevisionAtConsume = composerDraftRevisionRef.current;
1167	    clearChatComposerDraft();
1168	    setSending(true);
1169	    setChatError(null);
1170	    setInput('');
1171	    const extra = { files: attempt.files, imageFiles: attempt.images.map((image) => image.file) };
1172	    postingRef.current = true;
1173	    composerMutationRevisionRef.current += 1;
1174	    setPosting(true);
1175	    let messageId: number | null = null;
1176	    try {
1177	      messageId = await sendChatMessage(attempt.text, extra);
1178	    } finally {
1179	      postingRef.current = false;
1180	      setPosting(false);
1181	    }
1182	    if (messageId === null) {
1183	      showToast('发送失败');
1184	      if (composerDraftRevisionRef.current === draftRevisionAtConsume) {
1185	        setInput(rawText);
1186	        writeChatComposerDraft(rawText);
1187	      }
1188	      setSending(false);
1189	      return;
1190	    }
1191	    setPendingFiles((current) => {
1192	      const next = current === attempt.files ? [] : current;
1193	      pendingFilesRef.current = next;
1194	      return next;
1195	    });
1196	    if (pendingImagesRef.current === attempt.images) {
1197	      attempt.images.forEach(revokePendingChatImagePreview);
1198	      pendingImagesRef.current = [];
1199	      setPendingImages([]);
1200	    }
1201	    pinTranscriptToLatest();
1202	    await refetchLatest();
1203	    await runStream(messageId, { realityContext });
1204	    await refetchLatest();
1205	    clearLivePresentation();
1206	    setSending(false);
1207	    taRef.current?.focus();
1208	  }, [clearLivePresentation, input, pendingFiles, pendingImages, sending, uploadingFileCount, compressingImageCount, refetchLatest, runStream, showToast, pinTranscriptToLatest]);
1209	
1210	  const sendChoice = useCallback(async (text: string): Promise<boolean> => {
1211	    const choice = text.trim();
1212	    if (!choice || sending) return false;
1213	    const realityContext = realityPromptProjection.getSnapshot().text;
1214	    setSending(true);
1215	    setChatError(null);
1216	    uploadCoordinatorRef.current.beginChoicePost();
1217	    postingRef.current = true;
1218	    setPosting(true);
1219	    let messageId: number | null = null;
1220	    try {
1221	      messageId = await sendChatMessage(choice);
1222	    } finally {
1223	      postingRef.current = false;
1224	      setPosting(false);
1225	      uploadCoordinatorRef.current.endChoicePost();
1226	    }
1227	    if (messageId === null) {
1228	      showToast('发送失败');
1229	      setSending(false);
1230	      return false;
1231	    }
1232	    pinTranscriptToLatest();
1233	    await refetchLatest();
1234	    await runStream(messageId, { realityContext });
1235	    await refetchLatest();
1236	    clearLivePresentation();
1237	    setSending(false);
1238	    return true;
1239	  }, [clearLivePresentation, sending, refetchLatest, runStream, showToast, pinTranscriptToLatest]);
1240	
1241	  const chooseOption = useCallback(async (text: string, msgId: number) => {
1242	    if (sending || isChoicesAnswered(msgId, msgs)) return;
1243	    setPickedChoices((prev) => ({ ...prev, [msgId]: text }));
1244	    const sent = await sendChoice(text);
1245	    if (!sent) {
1246	      setPickedChoices((prev) => {
1247	        const next = { ...prev };
1248	        delete next[msgId];
1249	        return next;
1250	      });
1251	    }
1252	  }, [msgs, sendChoice, sending]);
1253	
1254	  const redo = useCallback(
1255	    async (msgId: number) => {
1256	      if (sending) return;
1257	      setSending(true);
1258	      setChatError(null);
1259	      const prep = await regenPrepare(msgId);
1260	      if (prep === null) {
1261	        showToast('重答准备失败');
1262	        setSending(false);
1263	        return;
1264	      }
1265	      // Keep old assistant visible until candidate activates.
1266	      // runStream pins latest before live mounts (mutation invariant).
1267	      const ok = await runStream(prep.userMessageId, { rewriteId: prep.rewriteId });
1268	      let committed = false;
1269	      if (ok) {
1270	        // finalize retries transport-ambiguous / effects_pending internally (same rewrite_id).
1271	        const fin = await regenFinalize(prep.rewriteId);
1272	        committed = Boolean(fin);
1273	        if (!fin) showToast('重答结果未确认，正在刷新…');
1274	        else if (fin.effectsPending) showToast('重答已切换，收尾未完成，可再试一次');
1275	      }
1276	      await refetchLatest();
1277	      if (committed) clearLivePresentation();
1278	      setSending(false);
1279	    },
1280	    [clearLivePresentation, sending, refetchLatest, runStream, showToast],
1281	  );
1282	
1283	  const saveEdit = useCallback(
1284	    async (msgId: number) => {
1285	      const content = editText.trim();
1286	      if (!content || sending) return;
1287	      setSending(true);
1288	      setChatError(null);
1289	      setEditingId(null);
1290	      const edit = await editChatMessage(msgId, content);
1291	      if (!edit.ok || !edit.rewriteId) {
1292	        showToast('修改失败');
1293	        setSending(false);
1294	        return;
1295	      }
1296	      // Active transcript stays intact until finalize succeeds.
1297	      // runStream pins latest before live mounts (mutation invariant).
1298	      const ok = await runStream(null, { rewriteId: edit.rewriteId });
1299	      let committed = false;
1300	      if (ok) {
1301	        // finalize retries transport-ambiguous / effects_pending internally (same rewrite_id).
1302	        const fin = await editFinalize(edit.rewriteId);
1303	        committed = fin.ok;
1304	        if (!fin.ok) {
1305	          showToast(
1306	            fin.effectsPending
1307	              ? '修改已切换，收尾未完成，可再试一次'
1308	              : '修改结果未确认，正在刷新…',
1309	          );
1310	        } else if (fin.effectsPending) {
1311	          showToast('修改已切换，收尾未完成，可再试一次');
1312	        }
1313	      }
1314	      await refetchLatest();
1315	      if (committed) clearLivePresentation();
1316	      setSending(false);
1317	    },
1318	    [clearLivePresentation, editText, sending, refetchLatest, runStream, showToast],
1319	  );
1320	
1321	  const branchSwitch = useCallback(
1322	    async (msgId: number, dir: 1 | -1) => {
1323	      const r = await switchChatBranch(msgId, dir);
1324	      if (!r) return;
1325	      // Authoritative msgs replacement invalidates old index windows on legacy.
1326	      if (legacyCompat) pinTranscriptToLatest();
1327	      await refetchLatest(false);
1328	    },
1329	    [refetchLatest, legacyCompat, pinTranscriptToLatest],
1330	  );
1331	
1332	  const copyText = useCallback(
1333	    (t: string) => {
1334	      navigator.clipboard?.writeText(t).catch(() => undefined);
1335	      showToast('已复制');
1336	    },
1337	    [showToast],
1338	  );
1339	
1340	  const loadEarlier = useCallback(async () => {
1341	    if (loadingMore || !msgs.length) return;
1342	    const gen = bumpHistoryGen();
1343	    cancelInFlightWarmUp();
1344	    setLoadingMore(true);
1345	    try {
1346	      const page = await fetchChatMessages({ before: msgs[0].id, limit: CHAT_AUTHORITATIVE_LIMIT });
1347	      if (gen !== coldStartRaceRef.current.historyGen) return;
1348	      const prepended = page.messages.length;
1349	      const newTotal = msgs.length + prepended;
1350	      if (legacyCompat) {
1351	        followLatestRef.current = false;
1352	        pendingAnchorIdRef.current = msgs[0]?.id ?? null;
1353	        setTxWin(transcriptWindowAfterPrepend(prepended, newTotal));
1354	      }
1355	      setMsgs((cur) => [...page.messages, ...cur]);
1356	      setHasMoreBefore(page.hasMoreBefore);
1357	    } finally {
1358	      setLoadingMore(false);
1359	    }
1360	  }, [loadingMore, msgs, legacyCompat, bumpHistoryGen, cancelInFlightWarmUp]);
1361	
1362	  const showEarlierLoaded = useCallback(() => {
1363	    if (!legacyCompat) {
1364	      void loadEarlier();
1365	      return;
1366	    }
1367	    if (txWin.start > 0) {
1368	      pendingAnchorIdRef.current = msgs[txWin.start]?.id ?? null;
1369	      followLatestRef.current = false;
1370	      setTxWin((w) => shiftTranscriptWindowOlder(w, msgs.length));
1371	      return;
1372	    }
1373	    if (hasMoreBefore) void loadEarlier();
1374	  }, [legacyCompat, txWin.start, msgs, hasMoreBefore, loadEarlier]);
1375	
1376	  const showNewerLoaded = useCallback(() => {
1377	    if (!legacyCompat) return;
1378	    const next = shiftTranscriptWindowNewer(txWin, msgs.length);
1379	    followLatestRef.current = isTranscriptWindowAtLatest(next, msgs.length);
1380	    if (followLatestRef.current) {
1381	      setTxWin(latestTranscriptWindow(msgs.length));
1382	      scrollBottom();
1383	      return;
1384	    }
1385	    pendingAnchorIdRef.current = msgs[Math.max(txWin.start, next.start)]?.id ?? null;
1386	    setTxWin(next);
1387	  }, [legacyCompat, txWin, msgs, scrollBottom]);
1388	
1389	  const goToLatestWindow = useCallback(() => {
1390	    pinTranscriptToLatest();
1391	    scrollBottom();
1392	  }, [pinTranscriptToLatest, scrollBottom]);
1393	
1394	  const jumpTo = useCallback((id: number) => {
1395	    setNavOpen(null);
1396	    setFlashId(id);
1397	    if (legacyCompat) {
1398	      const idx = msgs.findIndex((m) => m.id === id);
1399	      if (idx < 0) {
1400	        setTimeout(() => setFlashId((f) => (f === id ? null : f)), 2200);
1401	        return;
1402	      }
1403	      const next = transcriptWindowAroundIndex(idx, msgs.length);
1404	      // Intent-based: geometric "window touches tail" must not auto-follow.
1405	      followLatestRef.current = followLatestAfterSearchJump(idx, msgs.length);
1406	      pendingJumpIdRef.current = id;
1407	      setTxWin(next);
1408	    } else {
1409	      setTimeout(() => {
1410	        const el = document.getElementById(`msg-${id}`);
1411	        const c = scrollRef.current;
1412	        if (el && c) {
1413	          writeChatScroll(
1414	            c,
1415	            {
1416	              source: 'search-jump',
1417	              intent: 'explicit-target',
1418	              followLatest: followLatestRef.current,
1419	              targetScrollTop: el.offsetTop - 80,
1420	              behavior: 'smooth',
1421	            },
1422	            reportChatScroll,
1423	          );
1424	        }
1425	      }, 250);
1426	    }
1427	    setTimeout(() => setFlashId((f) => (f === id ? null : f)), 2200);
1428	  }, [legacyCompat, msgs, reportChatScroll]);
1429	
1430	  const searchResults = useMemo(() => {
1431	    const q = searchQ.trim().toLowerCase();
1432	    if (!q) return [];
1433	    return msgs
1434	      .filter((m) => m.text.toLowerCase().includes(q))
1435	      .slice(-30)
1436	      .reverse()
1437	      .map((m) => {
1438	        const i = m.text.toLowerCase().indexOf(q);
1439	        const start = Math.max(0, i - 12);
1440	        return { id: m.id, who: m.role === 'user' ? '哈娅' : 'Fyodor', snippet: (start > 0 ? '…' : '') + m.text.slice(start, start + 60), ts: m.ts };
1441	      });
1442	  }, [searchQ, msgs]);
1443	
1444	  const lastAssistantId = useMemo(() => {
1445	    for (let i = msgs.length - 1; i >= 0; i--) if (msgs[i].role === 'assistant') return msgs[i].id;
1446	    return -1;
1447	  }, [msgs]);
1448	
1449	  const onAttachFiles = useCallback(
1450	    async (selectedFiles: FileList | null) => {
1451	      setAttachMenuOpen(false);
1452	      if (!selectedFiles || !canStartComposerAttachmentSelection(postingRef.current, availableComposerSlots())) return;
1453	      const remaining = availableComposerSlots();
1454	      const selected = Array.from(selectedFiles).slice(0, Math.max(0, remaining));
1455	      if (!selected.length) {
1456	        showToast('一次消息最多上传 4 个附件');
1457	        return;
1458	      }
1459	      uploadingFileSlotsRef.current += selected.length;
1460	      setUploadingFileCount(uploadingFileSlotsRef.current);
1461	      try {
1462	        const mutationRevision = composerMutationRevisionRef.current;
1463	        const uploadedFiles = (await Promise.all(selected.map(uploadChatFile)))
1464	          .filter((file): file is { fileUrl: string; fileName: string } => file !== null);
1465	        const settled = await uploadCoordinatorRef.current.settle(
1466	          Promise.resolve(uploadedFiles),
1467	          mutationRevision,
1468	        );
1469	        if (!settled || uploadedFiles.length !== selected.length) {
1470	          showToast('部分上传失败（每个文件限 2MB，支持文本、Word 和 PDF）');
1471	        }
1472	      } finally {
1473	        uploadingFileSlotsRef.current -= selected.length;
1474	        setUploadingFileCount(uploadingFileSlotsRef.current);
1475	      }
1476	      if (selectedFiles.length > selected.length) showToast('一次消息最多上传 4 个附件');
1477	    },
1478	    [showToast],
1479	  );
1480	
1481	  const onAttachImages = useCallback(
1482	    async (selectedImages: FileList | null) => {
1483	      setAttachMenuOpen(false);
1484	      if (!selectedImages || !canStartComposerAttachmentSelection(postingRef.current, availableComposerSlots())) return;
1485	      const remaining = availableComposerSlots();
1486	      const selected = Array.from(selectedImages).slice(0, remaining);
1487	      if (!selected.length) {
1488	        showToast('一次消息最多上传 4 个附件');
1489	        return;
1490	      }
1491	
1492	      const pending = selected.map(createPendingChatImage);
1493	      const current = pendingImagesRef.current;
1494	      const next = [...current, ...pending].slice(0, MAX_COMPOSER_ATTACHMENTS);
1495	      pendingImagesRef.current = next;
1496	      setPendingImages(next);
1497	      reservePendingImageCompression(compressingImageIdsRef.current, pending);
1498	      setCompressingImageCount(compressingImageIdsRef.current.size);
1499	      const mutationRevision = composerMutationRevisionRef.current;
1500	
1501	      try {
1502	        const settled = await Promise.all(pending.map(async (image) => {
1503	          try {
1504	            const result = await compressChatImage(image.file);
1505	            return {
1506	              ...image,
1507	              file: result.file,
1508	              status: 'ready' as const,
1509	              outputBytes: result.outputBytes,
1510	            };
1511	          } catch {
1512	            return {
1513	              ...image,
1514	              status: 'ready' as const,
1515	              outputBytes: image.file.size,
1516	            };
1517	          }
1518	        }));
1519	        await uploadCoordinatorRef.current.settleImages(Promise.resolve(settled), mutationRevision);
1520	      } finally {
1521	        pending.forEach((image) => releasePendingImageCompression(compressingImageIdsRef.current, image.id));
1522	        setCompressingImageCount(compressingImageIdsRef.current.size);
1523	      }
1524	      if (selectedImages.length > selected.length) showToast('一次消息最多上传 4 个附件');
1525	    },
1526	    [showToast],
1527	  );
1528	
1529	  const segStyle = (on: boolean): CSSProperties => ({
1530	    flex: 1, textAlign: 'center', padding: '8px 0', borderRadius: 999, fontSize: 13, cursor: 'pointer', transition: 'all .2s',
1531	    background: on ? 'var(--card)' : 'transparent', color: on ? 'var(--deep)' : 'var(--mut)',
1532	    boxShadow: on ? '0 4px 10px var(--shadow)' : 'none',
1533	  });
1534	
1535	  const modelBadge = useMemo(() => {
1536	    if (chatProvider === 'claude_code') {
1537	      if (modelMode === 'explicit' && currentModel) {
1538	        const hit = models.find((m) => m.id === currentModel);
1539	        return `Claude Code · ${hit?.label || currentModel}`;
1540	      }
1541	      if (modelMode === 'default') return 'Claude Code · 默认';
1542	      return 'Claude Code · 读取中…';
1543	    }
1544	    const hit = models.find((m) => m.id === currentModel);
1545	    return hit?.label || currentModel.replace(/^.*\]\s*/, '').slice(0, 22) || '模型';
1546	  }, [models, currentModel, chatProvider, modelMode]);
1547	
1548	  const canSend = Boolean(input.trim() || pendingFiles.length || pendingImages.length)
1549	    && !sending
1550	    && uploadingFileCount === 0
1551	    && compressingImageCount === 0;
1552	
1553	  // ── message block renderers ──
1554	
1555	  function renderThinkBlock(m: ChatMsg, thinkingText = m.thinking, stateKey = String(m.id)) {
1556	    if (!thinkingText) return null;
1557	    const label = thinkingText === m.thinking && m.thinkingSummary
1558	      ? m.thinkingSummary
1559	      : `思考了 ${thinkingText.length} 字`;
1560	    const open = Boolean(openThink[stateKey]);
1561	    const onClick = () => {
1562	      if (effThinkMode === 'drawer') setDrawer({ text: thinkingText, label });
1563	      else setOpenThink((o) => ({ ...o, [stateKey]: !o[stateKey] }));
1564	    };
1565	    return (
1566	      <div className="vstack vstack-8">
1567	        <div onClick={onClick} className="hstack hstack-8" style={{ cursor: 'pointer', color: 'var(--faint)' }}>
1568	          <span style={{ display: 'flex' }}>
1569	            <Svg d={IC.brain} size={17} sw={1.5} />
1570	          </span>
1571	          <span style={{ fontSize: 13, letterSpacing: 1 }}>{label}</span>
1572	          {effThinkMode === 'inline' && (
1573	            <svg viewBox="0 0 24 24" width={12} height={12} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" style={{ transition: 'transform .2s', transform: `rotate(${open ? 180 : 0}deg)` }}>
1574	              <path d="M6 9l6 6 6-6" />
1575	            </svg>
1576	          )}
1577	        </div>
1578	        {open && effThinkMode === 'inline' && (
1579	          <div style={{ borderRadius: 14, background: 'var(--card2)', padding: '14px 16px', fontSize: '0.88em', lineHeight: 1.95, color: 'var(--mut)', whiteSpace: 'pre-wrap', animation: 'chatFadeIn .2s ease' }}>
1580	            {thinkingText}
1581	          </div>
1582	        )}
1583	      </div>
1584	    );
1585	  }
1586	
1587	  function renderArtifactCard(key: string, tc: ChatToolCall) {
1588	    const art = tc.artifact!;
1589	    const previewable = art.type === 'html' || art.type === 'markdown';
1590	    const href = previewable
1591	      ? `/api/artifacts/${encodeURIComponent(String(art.id))}/preview`
1592	      : `/api/artifacts/${encodeURIComponent(String(art.id))}/download`;
1593	    return (
1594	      <div key={key} className="chat-artifact-card">
1595	        <div className="chat-artifact-icon">{artifactTypeIcon(art.type)}</div>
1596	        <div className="chat-artifact-body">
1597	          <div className="chat-artifact-title">{art.title || '未命名'}</div>
1598	          <div className="chat-artifact-meta">
1599	            {artifactTypeLabel(art.type)}
1600	            {art.size ? ` · ${fmtArtifactSize(art.size)}` : ''}
1601	          </div>
1602	        </div>
1603	        <a className="chat-artifact-action" href={href} target="_blank" rel="noopener noreferrer">
1604	          {previewable ? '打开' : '下载'}
1605	        </a>
1606	      </div>
1607	    );
1608	  }
1609	
1610	  function renderChoices(m: ChatMsg) {
1611	    if (!m.choices?.length) return null;
1612	    const answered = isChoicesAnswered(m.id, msgs);
1613	    const picked = pickedChoices[m.id];
1614	    return (
1615	      <div className="chat-choices">
1616	        {m.choices.map((opt, i) => {
1617	          const isPicked = picked === opt;
1618	          const disabled = answered || (Boolean(picked) && !isPicked);
1619	          return (
1620	            <button
1621	              key={`${m.id}-choice-${i}`}
1622	              type="button"
1623	              className={`chat-choice-btn${isPicked ? ' picked' : ''}${disabled ? ' disabled' : ''}`}
1624	              disabled={disabled || sending}
1625	              onClick={() => chooseOption(opt, m.id)}
1626	            >
1627	              {opt}
1628	            </button>
1629	          );
1630	        })}
1631	      </div>
1632	    );
1633	  }
1634	
1635	  function renderToolItems(keyPrefix: string, tools: ChatToolCall[]) {
1636	    if (!tools.length) return null;
1637	    return (
1638	      <div className="vstack vstack-8">
1639	        {tools.map((tc, i) => (
1640	          <Fragment key={`${keyPrefix}-item-${i}`}>
1641	            {tc.artifact
1642	              ? renderArtifactCard(`${keyPrefix}-artifact-${i}`, tc)
1643	              : renderToolCard(`${keyPrefix}-tool-${i}`, tc)}
1644	            {toolMediaItems(tc).length > 0 && (
1645	              <ChatMediaGroup items={toolMediaItems(tc)} onOpenGallery={openMediaGallery} />
1646	            )}
1647	          </Fragment>
1648	        ))}
1649	      </div>
1650	    );
1651	  }
1652	
1653	  function renderToolCard(key: string, tc: ChatToolCall) {
1654	    const open = Boolean(openTools[key]);
1655	    const waiting = tc.deferred_tool_use === true && tc.status === 'waiting_for_confirmation';
1656	    const processing = tc.confirmation_state === 'processing';
1657	    const rejected = tc.confirmation_state === 'rejected';
1658	    const outStr = typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result ?? '', null, 2);
1659	    const inStr = typeof tc.args === 'string' ? tc.args : JSON.stringify(tc.args ?? {}, null, 2);
1660	    return (
1661	      <div key={key} style={{ background: 'var(--card)', borderRadius: 14, boxShadow: '0 6px 16px var(--shadow)', overflow: 'hidden' }}>
1662	        <div onClick={() => setOpenTools((o) => ({ ...o, [key]: !o[key] }))} className="hstack hstack-10" style={{ cursor: 'pointer', padding: '11px 14px' }}>
1663	          <span style={{ color: 'var(--faint)', flexShrink: 0, display: 'flex' }}>
1664	            <Svg d={IC.tool} size={14} sw={1.8} />
1665	          </span>
1666	          <span style={{ fontFamily: FONT_MONO, fontSize: 12.5, color: 'var(--ink2)' }}>{tc.name || 'tool'}</span>
1667	          {tc.running && <span style={{ width: 13, height: 13, borderRadius: '50%', border: '2px solid var(--rosebg)', borderTopColor: 'var(--rose)', animation: 'chatSpin .8s linear infinite', flexShrink: 0 }} />}
1668	          {!tc.running && tc.success !== false && <span style={{ color: 'var(--ok)', fontSize: 13 }}>✓</span>}
1669	          {!tc.running && tc.success === false && <span style={{ color: 'var(--err)', fontSize: 13 }}>✗</span>}
1670	          {tc.caption && <span style={{ fontSize: 11.5, color: 'var(--ghost)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{tc.caption}</span>}
1671	          <svg viewBox="0 0 24 24" width={12} height={12} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" style={{ marginLeft: 'auto', color: 'var(--ghost)', transition: 'transform .2s', transform: `rotate(${open ? 180 : 0}deg)`, flexShrink: 0 }}>
1672	            <path d="M6 9l6 6 6-6" />
1673	          </svg>
1674	        </div>
1675	        {waiting && (
1676	          <div className="vstack vstack-9" style={{ padding: '0 14px 14px', animation: 'chatFadeIn .2s ease' }}>
1677	            <div style={{ height: 1, background: 'var(--line)' }} />
1678	            <div style={{ fontSize: 13.5, lineHeight: 1.7, color: 'var(--ink2)' }}>{rejected ? '已取消' : tc.approval_prompt}</div>
1679	            {!rejected && (
1680	              <div className="hstack hstack-8">
1681	                <button
1682	                  type="button"
1683	                  disabled={processing || sending}
1684	                  onClick={(e) => { e.stopPropagation(); void confirmDeferred('approve'); }}
1685	                  style={{ border: 'none', borderRadius: 999, padding: '8px 18px', background: 'var(--deep)', color: '#FBF3F0', cursor: processing || sending ? 'default' : 'pointer', opacity: processing || sending ? 0.55 : 1 }}
1686	                >
1687	                  {processing ? '处理中…' : '确认'}
1688	                </button>
1689	                <button
1690	                  type="button"
1691	                  disabled={processing || sending}
1692	                  onClick={(e) => { e.stopPropagation(); void confirmDeferred('reject'); }}
1693	                  style={{ border: '1px solid var(--line)', borderRadius: 999, padding: '8px 18px', background: 'transparent', color: 'var(--mut)', cursor: processing || sending ? 'default' : 'pointer', opacity: processing || sending ? 0.55 : 1 }}
1694	                >
1695	                  不要
1696	                </button>
1697	              </div>
1698	            )}
1699	          </div>
1700	        )}
1701	        {open && (
1702	          <div className="vstack vstack-9" style={{ padding: '0 14px 14px', animation: 'chatFadeIn .2s ease' }}>
1703	            <div style={{ height: 1, background: 'var(--line)' }} />
1704	            <div style={{ fontSize: 11, letterSpacing: 2, color: 'var(--ghost)' }}>入参</div>
1705	            <div style={{ background: 'var(--card2)', borderRadius: 10, padding: '10px 12px', fontFamily: FONT_MONO, fontSize: 11.5, lineHeight: 1.7, color: 'var(--mut)', whiteSpace: 'pre-wrap', overflowX: 'auto', maxHeight: 200, overflowY: 'auto' }}>{inStr}</div>
1706	            <div style={{ fontSize: 11, letterSpacing: 2, color: 'var(--ghost)' }}>出参</div>
1707	            <div style={{ background: 'var(--card2)', borderRadius: 10, padding: '10px 12px', fontFamily: FONT_MONO, fontSize: 11.5, lineHeight: 1.7, color: 'var(--mut)', whiteSpace: 'pre-wrap', overflowX: 'auto', maxHeight: 200, overflowY: 'auto' }}>{String(outStr).slice(0, 4000)}</div>
1708	          </div>
1709	        )}
1710	      </div>
1711	    );
1712	  }
1713	
1714	  function openMediaGallery(items: ChatMediaItem[], currentIndex: number) {
1715	    if (items.length) setGallery({ items, currentIndex });
1716	  }
1717	
1718	  function messageMediaItems(m: ChatMsg): ChatMediaItem[] {
1719	    const attachments = m.attachments?.filter((attachment) => attachment.type === 'image') || [];
1720	    if (attachments.length) return attachments.map((attachment) => ({ url: attachment.url, alt: attachment.name || '图片' }));
1721	    return m.imageUrl ? [{ url: m.imageUrl, alt: '图片' }] : [];
1722	  }
1723	
1724	  function toolMediaItems(tc: ChatToolCall): ChatMediaItem[] {
1725	    return imageUrlsFromToolValue(tc.result).map((url) => ({ url, alt: '工具图片' }));
1726	  }
1727	
1728	    function renderMarkdown(text: string, caret = false) {
1729	      return (
1730	        <div className={`chat-markdown${caret ? ' chat-markdown-streaming' : ''}`}>
1731	          <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} disallowedElements={['img']} components={markdownComponents}>
1732	            {text}
1733	          </ReactMarkdown>
1734	        </div>
1735	      );
1736	    }
1737	  function renderUserMsg(m: ChatMsg) {
1738	    const editing = editingId === m.id;
1739	    const attachments = m.attachments?.length ? m.attachments : [
1740	      ...(m.fileUrl ? [{ type: 'file' as const, url: m.fileUrl, name: m.fileName || '文件' }] : []),
1741	      ...(m.imageUrl ? [{ type: 'image' as const, url: m.imageUrl, name: '图片' }] : []),
1742	    ];
1743	    const mediaItems = attachments
1744	      .filter((attachment) => attachment.type === 'image')
1745	      .map((attachment) => ({ url: attachment.url, alt: attachment.name || '图片附件' }));
1746	    const fileItems = attachments.filter((attachment) => attachment.type !== 'image');
1747	    return (
1748	      <div id={`msg-${m.id}`} className={`chat-msg vstack vstack-7${flashId === m.id ? ' chat-flash' : ''}`} style={{ alignItems: 'flex-end', borderRadius: 16 }}>
1749	        {editing ? (
1750	          <div className="vstack vstack-10" style={{ width: '100%', maxWidth: 520, background: 'var(--card)', borderRadius: 18, boxShadow: '0 10px 30px var(--shadow)', padding: 14 }}>
1751	            <textarea
1752	              value={editText}
1753	              onChange={(e) => setEditText(e.target.value)}
1754	              rows={3}
1755	              style={{ width: '100%', border: 'none', background: 'var(--card2)', borderRadius: 12, padding: 12, fontSize: '1em', lineHeight: 1.7, color: 'var(--ink)', resize: 'none', fontFamily: FONT_CN }}
1756	            />
1757	            <div className="hstack hstack-8" style={{ justifyContent: 'flex-end' }}>
1758	              <span style={{ marginRight: 'auto', fontSize: 11, color: 'var(--ghost)' }}>修改会归档后面的对话，重新生成回复</span>
1759	              <div onClick={() => setEditingId(null)} style={{ cursor: 'pointer', padding: '8px 16px', borderRadius: 999, background: 'var(--card2)', color: 'var(--mut)', fontSize: 13 }}>
1760	                取消
1761	              </div>
1762	              <div onClick={() => saveEdit(m.id)} style={{ cursor: 'pointer', padding: '8px 16px', borderRadius: 999, background: 'var(--deep)', color: '#FBF3F0', fontSize: 13, letterSpacing: 1 }}>
1763	                发送新版本
1764	              </div>
1765	            </div>
1766	          </div>
1767	        ) : (
1768	          <>
1769	            <div className="chat-message-content chat-message-content-user">
1770	              {mediaItems.length > 0 && <ChatMediaGroup items={mediaItems} onOpenGallery={openMediaGallery} />}
1771	              {(fileItems.length > 0 || m.text) && (
1772	                <div className="chat-message-bubble chat-message-bubble-user">
1773	                  {fileItems.length > 0 && (
1774	                    <div className="vstack vstack-6">
1775	                      {fileItems.map((attachment, index) => {
1776	                        const previewUrl = chatFilePreviewUrl(attachment.url);
1777	                        return (
1778	                          <a
1779	                            key={`file-${attachment.url}-${index}`}
1780	                            href={previewUrl || attachment.url}
1781	                            target="_blank"
1782	                            rel="noopener noreferrer"
1783	                            className="hstack hstack-6"
1784	                            style={{ background: 'var(--card)', borderRadius: 999, padding: '5px 11px', fontSize: 11.5, color: 'var(--ink2)', textDecoration: 'none' }}
1785	                            title={attachment.name}
1786	                          >
1787	                            <Svg d={IC.clip} size={11} sw={1.8} />
1788	                            {attachment.name}
1789	                          </a>
1790	                        );
1791	                      })}
1792	                    </div>
1793	                  )}
1794	                  {m.text && <span style={{ fontSize: '1em', lineHeight: 1.75, letterSpacing: 0.3, color: 'var(--ink)', whiteSpace: 'pre-wrap' }}>{m.text}</span>}
1795	                </div>
1796	              )}
1797	            </div>
1798	            <div className="hstack hstack-4">
1799	              <div className="chat-msg-acts hstack hstack-4">
1800	                <div
1801	                  onClick={() => {
1802	                    setEditingId(m.id);
1803	                    setEditText(m.text);
1804	                  }}
1805	                  title="修改"
1806	                  style={{ cursor: 'pointer', width: 26, height: 26, borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--faint)' }}
1807	                >
1808	                  <Svg d={IC.edit} size={14} sw={1.7} />
1809	                </div>
1810	                <div onClick={() => copyText(m.text)} title="复制" style={{ cursor: 'pointer', width: 26, height: 26, borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--faint)' }}>
1811	                  <CopyIcon size={14} />
1812	                </div>
1813	              </div>
1814	              <span style={{ fontFamily: FONT_DISPLAY, fontSize: 11, color: 'var(--ghost)', letterSpacing: 1 }}>{m.ts}</span>
1815	            </div>
1816	          </>
1817	        )}
1818	      </div>
1819	    );
1820	  }
1821	
1822	  function renderOrderedAssistantContent(m: ChatMsg) {
1823	    if (!m.displaySegments?.length) return null;
1824	    if (m.displaySegments.some((segment) => (
1825	      segment.type === 'tool' && segment.toolIndex >= m.toolCalls.length
1826	    ))) return null;
1827	    return (
1828	      <div className="vstack vstack-12">
1829	        {m.displaySegments.map((segment, index) => {
1830	          if (segment.type === 'thinking') {
1831	            return (
1832	              <Fragment key={String(m.id) + '-display-think-' + index}>
1833	                {renderThinkBlock(m, segment.text, String(m.id) + '-display-think-' + index)}
1834	              </Fragment>
1835	            );
1836	          }
1837	          if (segment.type === 'text') return (
1838	            <Fragment key={`${m.id}-display-text-${index}`}>
1839	              {segment.text && renderMarkdown(segment.text)}
1840	            </Fragment>
1841	          );
1842	          const tool = m.toolCalls[segment.toolIndex];
1843	          return tool
1844	            ? renderToolItems(`${m.id}-display-tool-${index}`, [tool])
1845	            : null;
1846	        })}
1847	      </div>
1848	    );
1849	  }
1850	
1851	  function renderLegacyAssistantContent(m: ChatMsg) {
1852	    return (
1853	      <>
1854	        {renderThinkBlock(m)}
1855	        {renderToolItems(String(m.id), m.toolCalls)}
1856	        {m.text && renderMarkdown(m.text)}
1857	      </>
1858	    );
1859	  }
1860	
1861	  function renderAssistantMsg(m: ChatMsg) {
1862	    const usage = m.cacheInfo;
1863	    const cache = cacheLabel(usage);
1864	    const mediaItems = messageMediaItems(m);
1865	    return (
1866	      <div id={`msg-${m.id}`} className={`chat-msg vstack vstack-12${flashId === m.id ? ' chat-flash' : ''}`} style={{ alignItems: 'flex-start', borderRadius: 16 }}>
1867	        <div className="chat-message-content chat-message-content-assistant">
1868	          {mediaItems.length > 0 && <ChatMediaGroup items={mediaItems} onOpenGallery={openMediaGallery} />}
1869	          {m.displaySegments?.length
1870	            ? (renderOrderedAssistantContent(m) ?? renderLegacyAssistantContent(m))
1871	            : renderLegacyAssistantContent(m)}
1872	        </div>
1873	        {renderChoices(m)}
1874	        <div className="vstack vstack-7">
1875	          <span style={{ fontFamily: FONT_DISPLAY, fontSize: 11, color: 'var(--ghost)', letterSpacing: 1, padding: '0 2px' }}>{m.ts}</span>
1876	          <div className="flex-wrap-gap-2">
1877	            <div onClick={() => copyText(m.text)} style={{ cursor: 'pointer', width: 31, height: 31, borderRadius: 10, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--faint)' }}>
1878	              <CopyIcon />
1879	            </div>
1880	            <div onClick={() => setLiked((o) => ({ ...o, [m.id]: o[m.id] === 1 ? undefined : 1 } as Record<number, 1 | -1>))} style={{ cursor: 'pointer', width: 31, height: 31, borderRadius: 10, display: 'flex', alignItems: 'center', justifyContent: 'center', color: liked[m.id] === 1 ? 'var(--rose)' : 'var(--faint)' }}>
1881	              <svg viewBox="0 0 24 24" width={15} height={15} fill={liked[m.id] === 1 ? 'var(--rosebg)' : 'none'} stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
1882	                <path d={IC.thumb} />
1883	              </svg>
1884	            </div>
1885	            <div onClick={() => setLiked((o) => ({ ...o, [m.id]: o[m.id] === -1 ? undefined : -1 } as Record<number, 1 | -1>))} style={{ cursor: 'pointer', width: 31, height: 31, borderRadius: 10, display: 'flex', alignItems: 'center', justifyContent: 'center', color: liked[m.id] === -1 ? 'var(--rose)' : 'var(--faint)' }}>
1886	              <svg viewBox="0 0 24 24" width={15} height={15} fill={liked[m.id] === -1 ? 'var(--rosebg)' : 'none'} stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" style={{ transform: 'rotate(180deg)' }}>
1887	                <path d={IC.thumb} />
1888	              </svg>
1889	            </div>
1890	            {m.id === lastAssistantId && !sending && (
1891	              <div onClick={() => redo(m.id)} title="重新回答" style={{ cursor: 'pointer', width: 31, height: 31, borderRadius: 10, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--faint)' }}>
1892	                <Svg d={IC.redo} size={15} sw={1.7} />
1893	              </div>
1894	            )}
1895	            {usage && (
1896	              <div className="flex-wrap-gap-y3-x7" style={{ color: 'var(--ghost)', fontSize: 11.5, padding: '0 2px' }}>
1897	                <span className="hstack hstack-3">
1898	                  <Svg d={IC.up} size={11} sw={1.8} />
1899	                  <span style={{ fontFamily: FONT_DISPLAY }}>{fmtTokens(usage.inputTokens || 0)}</span>
1900	                </span>
1901	                <span>·</span>
1902	                <span className="hstack hstack-3">
1903	                  <Svg d={IC.down} size={11} sw={1.8} />
1904	                  <span style={{ fontFamily: FONT_DISPLAY }}>{fmtTokens(usage.outputTokens || 0)}</span>
1905	                </span>
1906	                {Boolean(usage.elapsedSec) && (
1907	                  <>
1908	                    <span>·</span>
1909	                    <span className="hstack hstack-3">
1910	                      <svg viewBox="0 0 24 24" width={11} height={11} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
1911	                        <circle cx={12} cy={12} r={9} />
1912	                        <path d={IC.clock} />
1913	                      </svg>
1914	                      <span style={{ fontFamily: FONT_DISPLAY }}>{Math.round(usage.elapsedSec || 0)}s</span>
1915	                    </span>
1916	                  </>
1917	                )}
1918	                {cache && (
1919	                  <>
1920	                    <span>·</span>
1921	                    <span>{cache}</span>
1922	                  </>
1923	                )}
1924	                {Boolean(usage.costUsd) && (
1925	                  <>
1926	                    <span>·</span>
1927	                    <span style={{ color: 'rgba(170,108,88,.92)', fontFamily: FONT_DISPLAY }} title={usage.costEstimated ? '按费率估算' : '按账单换算'}>
1928	                      {fmtCostUsd(usage.costUsd, usage.costEstimated)}
1929	                    </span>
1930	                  </>
1931	                )}
1932	              </div>
1933	            )}
1934	            {m.branchTotal > 1 && (
1935	              <div className="hstack hstack-2" style={{ background: 'var(--card)', borderRadius: 999, padding: '2px 6px', boxShadow: '0 4px 10px var(--shadow)', marginLeft: 4 }}>
1936	                <span onClick={() => branchSwitch(m.id, -1)} style={{ cursor: 'pointer', padding: '1px 6px', color: m.branchIdx > 0 ? 'var(--mut)' : 'var(--ghost)', fontSize: 14 }}>
1937	                  ‹
1938	                </span>
1939	                <span style={{ fontFamily: FONT_DISPLAY, fontSize: 11.5, color: 'var(--mut)', letterSpacing: 1 }}>
1940	                  {m.branchIdx + 1}/{m.branchTotal}
1941	                </span>
1942	                <span onClick={() => branchSwitch(m.id, 1)} style={{ cursor: 'pointer', padding: '1px 6px', color: m.branchIdx < m.branchTotal - 1 ? 'var(--mut)' : 'var(--ghost)', fontSize: 14 }}>
1943	                  ›
1944	                </span>
1945	              </div>
1946	            )}
1947	          </div>
1948	        </div>
1949	      </div>
1950	    );
1951	  }
1952	
1953	  function renderLive(l: LiveState) {
1954	    const lastSegment = l.segments[l.segments.length - 1];
1955	    return (
1956	      <div className="vstack vstack-12">
1957	        {l.segments.map((segment: LiveSegment) => {
1958	          if (segment.type === 'thinking') {
1959	            const active = l.lastEvent === 'thinking' && lastSegment?.id === segment.id;
1960	            const lines = segment.text.split('\n').filter(Boolean).slice(-3);
1961	            return (
1962	              <Fragment key={`live-segment-${segment.id}`}>
1963	                <div
1964	                  onClick={() => setDrawer({ text: segment.text, label: active ? '思考中…' : `思考了 ${segment.text.length} 字` })}
1965	                  className="hstack hstack-8" style={{ cursor: 'pointer', color: 'var(--faint)' }}
1966	                >
1967	                  <span style={{ display: 'flex', animation: active ? 'chatBreathe 1.6s ease-in-out infinite' : 'none' }}>
1968	                    <Svg d={IC.brain} size={17} sw={1.5} />
1969	                  </span>
1970	                  <span style={{ fontSize: 13, letterSpacing: 1 }}>{active ? '思考中…' : `思考了 ${segment.text.length} 字`}</span>
1971	                </div>
1972	                {active && (
1973	                  <div style={{ position: 'relative', height: 76, overflow: 'hidden', borderRadius: 14, background: 'var(--card2)' }}>
1974	                    <div className="vstack vstack-4" style={{ position: 'absolute', bottom: 10, left: 16, right: 16 }}>
1975	                      {lines.map((ln, i) => (
1976	                        <span key={`${segment.id}-${i}-${ln.slice(0, 8)}`} style={{ fontSize: 12.5, color: 'var(--mut)', lineHeight: 1.6, animation: 'chatFadeIn .4s ease', overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}>
1977	                          {ln}
1978	                        </span>
1979	                      ))}
1980	                    </div>
1981	                    <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: 30, background: 'linear-gradient(var(--card2),transparent)' }} />
1982	                  </div>
1983	                )}
1984	              </Fragment>
1985	            );
1986	          }
1987	          if (segment.type === 'text') {
1988	            const caret = isTextCaretActive(l) && lastSegment?.id === segment.id;
1989	            return <Fragment key={`live-segment-${segment.id}`}>{renderMarkdown(segment.text, caret)}</Fragment>;
1990	          }
1991	          return <Fragment key={`live-segment-${segment.id}`}>{renderToolItems(`live-${segment.id}`, [segment.tool])}</Fragment>;
1992	        })}
1993	        {!l.segments.length ? (
1994	          <div className="hstack hstack-8" style={{ color: 'var(--faint)', fontSize: 13 }}>
1995	            <span style={{ width: 13, height: 13, borderRadius: '50%', border: '2px solid var(--rosebg)', borderTopColor: 'var(--rose)', animation: 'chatSpin .8s linear infinite' }} />
1996	            正在连接回复…
1997	          </div>
1998	        ) : null}
1999	      </div>
2000	    );
2001	  }
2002	
2003	  // Modern: all loaded msgs. Legacy: bounded DOM window only (msgs state stays full).
2004	  const visibleMsgs = legacyCompat ? msgs.slice(txWin.start, txWin.end) : msgs;
2005	  const atLatestWindow = !legacyCompat || isTranscriptWindowAtLatest(txWin, msgs.length);
2006	  const canShowEarlierLoaded = legacyCompat && txWin.start > 0;
2007	  const canFetchEarlier = (!legacyCompat && hasMoreBefore) || (legacyCompat && txWin.start === 0 && hasMoreBefore);
2008	  const canShowNewerLoaded = legacyCompat && !atLatestWindow;
2009	
2010	  const rendered: ReactElement[] = [];
2011	  let lastDate = '';
2012	  visibleMsgs.forEach((m) => {
2013	    // First visible message always gets a date separator (even mid-day slice).
2014	    if (m.dateKey && m.dateKey !== lastDate) {
2015	      lastDate = m.dateKey;
2016	      const label = m.dateKey === new Date().toISOString().slice(0, 10) ? dateLabel : m.dateKey.replace(/-/g, '.');
2017	      rendered.push(
2018	        <div key={`d-${m.dateKey}-${m.id}`} style={{ textAlign: 'center', fontFamily: fontFamilyForText(label), fontSize: 12, letterSpacing: 2, color: 'var(--ghost)', padding: '2px 0' }}>
2019	          {label}
2020	        </div>,
2021	      );
2022	    }
2023	    rendered.push(
2024	      <div key={m.id}>
2025	        {m.role === 'user' ? renderUserMsg(m) : renderAssistantMsg(m)}
2026	      </div>,
2027	    );
2028	  });
2029	
2030	  const toolbarIcon = compactToolbar ? 32 : 35;
2031	  const modalUiState: SoftWindowUiState =
2032	    manualWindow.uiState === 'probing' || manualWindow.uiState === 'idle'
2033	      ? 'loading'
2034	      : manualWindow.uiState;
2035	
2036	  return (
2037	    <div
2038	      ref={chatRootRef}
2039	      className="chat-root app-frame__page"
2040	      style={{
2041	        display: 'flex',
2042	        flexDirection: 'column',
2043	        background: 'var(--bg)',
2044	        color: 'var(--ink)',
2045	        fontFamily: FONT_CN,
2046	        fontSize: FONT_SIZES[settings.fontStep],
2047	      }}
2048	    >
2049	          <div id="c78-layout-test-100" aria-hidden style={{ position: 'absolute', width: 100, height: 1, visibility: 'hidden', pointerEvents: 'none' }} />
2050	          {wide && taskTimerSnapshot && (
2051	            <TaskTimerCard
2052	              placement="floating-desktop"
2053	              snapshot={taskTimerSnapshot}
2054	              onComplete={handleTaskTimerComplete}
2055	              completing={taskTimerCompleting}
2056	            />
2057	          )}
2058	      {/* ══ top nav ══ */}
2059	      <div style={{ flexShrink: 0, position: 'relative', zIndex: 40 }}>
2060	        <div className="chat-header-surface" style={{ background: 'rgba(255,255,255,0.97)', boxShadow: '0 6px 18px var(--shadow)', position: 'relative', zIndex: 3 }}>
2061	          <div className="page-header-toolbar">
2062	            <div onClick={() => setSidebarOpen(true)} style={{ cursor: 'pointer', width: 38, height: 38, borderRadius: '50%', background: 'linear-gradient(135deg,#B76E79,#9C3B4A)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, boxShadow: '0 6px 14px var(--shadow2)' }}>
2063	              <span style={{ fontFamily: FONT_DISPLAY, fontStyle: 'italic', fontSize: 17, color: '#F7F1EE' }}>Θ</span>
2064	            </div>
2065	            <div className="vstack vstack-1" style={{ minWidth: 0, flexShrink: 1 }}>
2066	              <span style={{ fontFamily: FONT_DISPLAY, fontSize: 17, fontWeight: 600, letterSpacing: 1, color: 'var(--ink)' }}>Fyodor</span>
2067	              <div
2068	                className="hstack hstack-5"
2069	                style={{ minWidth: 0 }}
2070	                title={capacityTitle}
2071	                aria-label={capacityTitle}
2072	              >
2073	                <span
2074	                  style={{
2075	                    width: 6,
2076	                    height: 6,
2077	                    borderRadius: '50%',
2078	                    flexShrink: 0,
2079	                    background: endpointOnline === false ? 'var(--err)' : endpointOnline ? 'var(--ok)' : 'var(--ghost)',
2080	                    animation: endpointOnline === false
2081	                      ? undefined
2082	                      : endpointOnline
2083	                        ? 'chatBreathe 2.2s ease-in-out infinite'
2084	                        : 'chatBreathe 3s ease-in-out infinite',
2085	                  }}
2086	                />
2087	                <span
2088	                  style={{
2089	                    fontFamily: FONT_DISPLAY,
2090	                    fontStyle: 'italic',
2091	                    fontSize: 10.5,
2092	                    letterSpacing: 0.5,
2093	                    color: 'var(--faint)',
2094	                    whiteSpace: 'nowrap',
2095	                    overflow: 'hidden',
2096	                    textOverflow: 'ellipsis',
2097	                  }}
2098	                >
2099	                  {capacityLabel}
2100	                </span>
2101	              </div>
2102	            </div>
2103	            <div className="hstack hstack-1" style={{ marginLeft: 'auto', flexShrink: 0 }}>
2104	              <div onClick={() => setNavOpen(navOpen === 'wrench' ? null : 'wrench')} style={{ ...iconBtn, background: navOpen === 'wrench' ? 'var(--rosebg)' : 'transparent' }}>
2105	                <Svg d={IC.wrench} />
2106	              </div>
2107	              <ChatThemeQuickToggle rootRef={chatRootRef} style={iconBtn} />
2108	              <div onClick={() => setNavOpen(navOpen === 'font' ? null : 'font')} style={{ ...iconBtn, background: navOpen === 'font' ? 'var(--rosebg)' : 'transparent' }}>
2109	                <span style={{ fontFamily: FONT_DISPLAY, fontSize: 14, letterSpacing: 0.5 }}>Aa</span>
2110	              </div>
2111	              <div onClick={toggleSearchNav} style={{ ...iconBtn, width: toolbarIcon, height: toolbarIcon, background: navOpen === 'search' ? 'var(--rosebg)' : 'transparent' }}>
2112	                <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round">
2113	                  <circle cx={12} cy={12} r={9} />
2114	                  <path d={IC.clock} />
2115	                </svg>
2116	              </div>
2117	              {manualWindow.enabled ? (
2118	                <button
2119	                  type="button"
2120	                  title="换一扇窗"
2121	                  aria-label="换一扇窗"
2122	                  disabled={switchBlocked}
2123	                  onClick={() => {
2124	                    if (!switchBlocked) void manualWindow.openModal();
2125	                  }}
2126	                  style={{
2127	                    ...iconBtn,
2128	                    width: toolbarIcon,
2129	                    height: toolbarIcon,
2130	                    border: 'none',
2131	                    padding: 0,
2132	                    background: manualWindow.modalOpen ? 'var(--rosebg)' : 'transparent',
2133	                    opacity: switchBlocked ? 0.45 : 1,
2134	                    cursor: switchBlocked ? 'default' : 'pointer',
2135	                  }}
2136	                >
2137	                  <Svg d={IC.window} />
2138	                </button>
2139	              ) : null}
2140	              <div
2141	                onClick={() => { void refreshChat(); }}
2142	                title="刷新并解锁"
2143	                style={{ ...iconBtn, width: toolbarIcon, height: toolbarIcon, opacity: refreshing ? 0.55 : 1, cursor: refreshing ? 'default' : 'pointer' }}
2144	              >
2145	                <span style={{ display: 'flex', animation: refreshing ? 'chatSpin .8s linear infinite' : undefined }}>
2146	                  <Svg d={IC.refresh} />
2147	                </span>
2148	              </div>
2149	            </div>
2150	          </div>
2151	        </div>
2152	
2153	        {navOpen && (
2154	          <>
2155	            <div onClick={() => setNavOpen(null)} className="c78-fill-fixed" style={{ zIndex: 1, background: 'rgba(40,28,26,0.30)', animation: 'chatFadeIn .2s ease' }} />
2156	            <div style={{ position: 'absolute', top: '100%', left: 0, right: 0, zIndex: 2, animation: 'chatDropIn .22s ease' }}>
2157	              <div style={{ maxWidth: 430, margin: '0 auto', padding: '0 10px' }}>
2158	                <div className="vstack vstack-18" style={{ background: 'var(--card)', borderRadius: '0 0 26px 26px', boxShadow: '0 30px 70px var(--shadow2)', padding: '20px 20px 22px', maxHeight: '72vh', overflowY: 'auto' }}>
2159	                  {navOpen === 'wrench' && (
2160	                    <>
2161	                      <div className="vstack vstack-12">
2162	                        <MixedSectionLabel cn="外观" en="APPEARANCE" />
2163	                        <div className="vstack vstack-8">
2164	                          <span style={{ fontSize: 13.5, color: 'var(--ink2)', letterSpacing: 1 }}>主题</span>
2165	                          <ChatThemeSegmented rootRef={chatRootRef} segStyle={segStyle} />
2166	                        </div>
2167	                      </div>
2168	                      <div style={{ height: 1, background: 'var(--line)' }} />
2169	                      <div className="vstack vstack-12">
2170	                        <MixedSectionLabel cn="对话" en="CONVERSATION" />
2171	                        <div className="vstack vstack-8">
2172	                          <span style={{ fontSize: 13.5, color: 'var(--ink2)', letterSpacing: 1 }}>思维链展开方式</span>
2173	                          <div className="hstack hstack-2" style={{ background: 'var(--card2)', borderRadius: 999, padding: 3 }}>
2174	                            {(['auto', 'drawer', 'inline'] as const).map((t) => (
2175	                              <div key={t} onClick={() => patchSettings({ thinkMode: t })} style={segStyle(settings.thinkMode === t)}>
2176	                                {t === 'auto' ? '自动' : t === 'drawer' ? '抽屉' : '原地展开'}
2177	                              </div>
2178	                            ))}
2179	                          </div>
2180	                          <span style={{ fontSize: 11.5, color: 'var(--ghost)' }}>移动端默认抽屉；桌面端默认原地展开</span>
2181	                        </div>
2182	                      </div>
2183	                      <div style={{ height: 1, background: 'var(--line)' }} />
2184	                      <div className="vstack vstack-12">
2185	                        <MixedSectionLabel cn="诊断" en="LAYOUT (临时)" />
2186	                        <div
2187	                          onClick={refreshLayoutDiag}
2188	                          style={{ ...segStyle(false), flex: 'none', padding: '10px 14px' }}
2189	                        >
2190	                          采集布局读数
2191	                        </div>
2192	                        <div
2193	                          onClick={runHiddenTranscriptThemeProbe}
2194	                          style={{ ...segStyle(false), flex: 'none', padding: '10px 14px' }}
2195	                        >
2196	                          隐藏消息树测试主题
2197	                        </div>
2198	                        <ThemePerfRows />
2199	                        {layoutDiag && (
2200	                          <div className="vstack vstack-6" style={{ background: 'var(--card2)', borderRadius: 14, padding: '12px 14px', fontFamily: FONT_MONO, fontSize: 11, lineHeight: 1.55, color: 'var(--ink2)' }}>
2201	                            {layoutDiag.map((row) => (
2202	                              <div key={row.label} className="hstack hstack-10" style={{ justifyContent: 'space-between' }}>
2203	                                <span style={{ color: 'var(--ghost)', flexShrink: 0 }}>{row.label}</span>
2204	                                <span style={{ textAlign: 'right', wordBreak: 'break-all' }}>{row.value}</span>
2205	                              </div>
2206	                            ))}
2207	                          </div>
2208	                        )}
2209	                      </div>
2210	                    </>
2211	                  )}
2212	                  {navOpen === 'font' && (
2213	                    <div className="vstack vstack-12">
2214	                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
2215	                        <MixedSectionLabel cn="字号" en="TEXT SIZE" />
2216	                        <span style={{ fontFamily: FONT_DISPLAY, fontSize: 12, color: 'var(--rose)' }}>{FONT_SIZES[settings.fontStep]}px</span>
2217	                      </div>
2218	                      <div className="hstack hstack-12">
2219	                        <span style={{ fontSize: 12, color: 'var(--ghost)' }}>字</span>
2220	                        <input type="range" min={0} max={4} step={1} value={settings.fontStep} onChange={(e) => patchSettings({ fontStep: Number(e.target.value) })} style={{ flex: 1, accentColor: 'var(--rose)' }} />
2221	                        <span style={{ fontSize: 19, color: 'var(--ghost)' }}>字</span>
2222	                      </div>
2223	                      <div style={{ background: 'var(--card2)', borderRadius: 14, padding: '12px 14px', fontSize: '1em', lineHeight: 1.8, color: 'var(--ink2)' }}>灯不关，我看着你读完这一页。</div>
2224	                    </div>
2225	                  )}
2226	                  {navOpen === 'search' && (
2227	                    <div className="vstack vstack-10">
2228	                      <MixedSectionLabel cn="聊天记录" en="HISTORY" />
2229	                      <div className="hstack hstack-10" style={{ background: 'var(--card2)', borderRadius: 999, padding: '11px 16px' }}>
2230	                        <svg viewBox="0 0 24 24" width={15} height={15} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" style={{ color: 'var(--ghost)', flexShrink: 0 }}>
2231	                          <circle cx={11} cy={11} r={7} />
2232	                          <path d="M20 20l-3.5-3.5" />
2233	                        </svg>
2234	                        <input value={searchQ} onChange={(e) => setSearchQ(e.target.value)} placeholder="搜索已加载的对话…" style={{ flex: 1, border: 'none', background: 'transparent', fontSize: 14, color: 'var(--ink)', minWidth: 0, fontFamily: FONT_CN, outline: 'none' }} />
2235	                      </div>
2236	                      {searchResults.map((r) => (
2237	                        <div key={r.id} onClick={() => jumpTo(r.id)} className="hstack hstack-10" style={{ cursor: 'pointer', padding: '10px 12px', borderRadius: 14, background: 'var(--card2)' }}>
2238	                          <span style={{ fontSize: 11, padding: '3px 9px', borderRadius: 999, background: 'var(--rosebg)', color: 'var(--deep)', flexShrink: 0 }}>{r.who}</span>
2239	                          <span style={{ flex: 1, fontSize: 13, color: 'var(--ink2)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.snippet}</span>
2240	                          <span style={{ fontFamily: FONT_DISPLAY, fontSize: 11, color: 'var(--ghost)', flexShrink: 0 }}>{r.ts}</span>
2241	                        </div>
2242	                      ))}
2243	                      {searchQ.trim() && !searchResults.length && <div style={{ fontSize: 13, color: 'var(--faint)', padding: '2px 12px' }}>没有找到相关消息</div>}
2244	                    </div>
2245	                  )}
2246	                </div>
2247	              </div>
2248	            </div>
2249	          </>
2250	        )}
2251	      </div>
2252	
2253	      {/* ══ message stream ══ */}
2254	      <div ref={scrollRef} onScroll={handleTranscriptScroll} className="hide-scrollbar" style={{ flex: 1, minHeight: 0, overflowY: 'auto', position: 'relative' }}>
2255	        <div className="vstack vstack-20" style={{ maxWidth: 430, margin: '0 auto', padding: '20px 16px 26px' }}>
2256	          {(canShowEarlierLoaded || canFetchEarlier) && (
2257	            <div
2258	              onClick={showEarlierLoaded}
2259	              style={{ cursor: 'pointer', textAlign: 'center', fontSize: 12, color: 'var(--faint)', padding: '6px 0', letterSpacing: 2 }}
2260	            >
2261	              {loadingMore
2262	                ? '加载中…'
2263	                : canShowEarlierLoaded
2264	                  ? '‹ 显示更早的已加载对话 ›'
2265	                  : '‹ 加载更早的对话 ›'}
2266	            </div>
2267	          )}
2268	          {rendered}
2269	          {live && renderLive(live)}
2270	          {pendingConfirmation && renderToolCard('pending-confirmation', pendingConfirmation)}
2271	          {canShowNewerLoaded && (
2272	            <div className="vstack vstack-8" style={{ padding: '4px 0 2px' }}>
2273	              <div
2274	                onClick={showNewerLoaded}
2275	                style={{ cursor: 'pointer', textAlign: 'center', fontSize: 12, color: 'var(--faint)', letterSpacing: 2 }}
2276	              >
2277	                显示更新的已加载对话 ›
2278	              </div>
2279	              <div
2280	                onClick={goToLatestWindow}
2281	                style={{ cursor: 'pointer', textAlign: 'center', fontSize: 12, color: 'var(--ghost)', letterSpacing: 2 }}
2282	              >
2283	                回到最近对话
2284	              </div>
2285	            </div>
2286	          )}
2287	          {chatError && (
2288	            <div style={{ display: 'flex', justifyContent: 'center', padding: '6px 4px 2px' }}>
2289	              <div className="vstack vstack-8" style={{
2290	                maxWidth: 360,
2291	                width: '100%',
2292	                background: 'rgba(58,42,40,0.92)',
2293	                color: '#F7EDEA',
2294	                borderRadius: 18,
2295	                padding: '14px 16px',
2296	                boxShadow: '0 10px 30px rgba(0,0,0,0.18)',
2297	                textAlign: 'center',
2298	              }}>
2299	                <span style={{ fontSize: 13, lineHeight: 1.65, letterSpacing: 0.3 }}>{chatError.message}</span>
2300	                <span style={{ fontSize: 11.5, lineHeight: 1.6, color: 'rgba(247,237,234,0.72)' }}>{chatError.hint}</span>
2301	              </div>
2302	            </div>
2303	          )}
2304	        </div>
2305	      </div>
2306	
2307	      {/* ══ input area ══ */}
2308	      <div style={{ flexShrink: 0, position: 'relative', zIndex: 30, padding: '8px 12px 14px' }}>
2309	        <div style={{ maxWidth: 430, margin: '0 auto', position: 'relative' }}>
2310	          {!wide && taskTimerSnapshot && (
2311	            <TaskTimerCard
2312	              placement="inline-mobile"
2313	              snapshot={taskTimerSnapshot}
2314	              onComplete={handleTaskTimerComplete}
2315	              completing={taskTimerCompleting}
2316	            />
2317	          )}
2318	          {modelPopOpen && (
2319	            <>
2320	              <div onClick={() => setModelPopOpen(false)} className="c78-fill-fixed" style={{ zIndex: 1 }} />
2321	              <div className="chat-model-pop vstack vstack-4" style={{ position: 'absolute', bottom: 'calc(100% + 10px)', left: 0, zIndex: 2, background: 'var(--card)', borderRadius: 18, boxShadow: '0 24px 60px var(--shadow2)', padding: 12, animation: 'chatFadeIn .15s ease', maxHeight: '50vh', overflowY: 'auto' }}>
2322	                <MixedSectionLabel cn="模型" en="MODELS" style={{ padding: '8px 8px 4px', letterSpacing: 2.5, fontSize: 10.5 }} />
2323	                {chatProvider === 'claude_code' ? (
2324	                  <>
2325	                    <div
2326	                      onClick={async () => {
2327	                        setModelPopOpen(false);
2328	                        if (modelMode === 'default') return;
2329	                        const result = await setChatModel(null);
2330	                        if (result.ok) {
2331	                          setModelMode('default');
2332	                          setCurrentModel('');
2333	                          showToast('下一条消息起生效');
2334	                        } else showToast('切换失败');
2335	                      }}
2336	                      className="hstack hstack-10" style={{ cursor: 'pointer', padding: '9px 10px', borderRadius: 12, background: modelMode === 'default' ? 'var(--rosebg)' : 'transparent' }}
2337	                    >
2338	                      <span style={{ width: 8, height: 8, borderRadius: '50%', background: modelMode === 'default' ? 'var(--rose)' : 'var(--ghost)', flexShrink: 0 }} />
2339	                      <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
2340	                        <span style={{ fontSize: 14, color: 'var(--ink)' }}>默认（跟随 Claude Code）</span>
2341	                        <span style={{ fontFamily: FONT_MONO, fontSize: 10.5, color: 'var(--ghost)' }}>不传 --model</span>
2342	                      </div>
2343	                    </div>
2344	                    {models.map((mo) => (
2345	                      <div
2346	                        key={mo.id}
2347	                        onClick={async () => {
2348	                          setModelPopOpen(false);
2349	                          if (modelMode === 'explicit' && mo.id === currentModel) return;
2350	                          const result = await setChatModel(mo.id);
2351	                          if (result.ok) {
2352	                            setModelMode(result.modelMode || 'explicit');
2353	                            setCurrentModel(result.configuredModel || mo.id);
2354	                            showToast('下一条消息起生效');
2355	                          } else showToast('切换失败');
2356	                        }}
2357	                        className="hstack hstack-10" style={{ cursor: 'pointer', padding: '9px 10px', borderRadius: 12, background: modelMode === 'explicit' && mo.id === currentModel ? 'var(--rosebg)' : 'transparent' }}
2358	                      >
2359	                        <span style={{ width: 8, height: 8, borderRadius: '50%', background: mo.dot || (modelMode === 'explicit' && mo.id === currentModel ? 'var(--rose)' : 'var(--ghost)'), flexShrink: 0 }} />
2360	                        <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
2361	                          <span style={{ fontSize: 14, color: 'var(--ink)' }}>{mo.label || mo.id}</span>
2362	                          <span style={{ fontFamily: FONT_MONO, fontSize: 10.5, color: 'var(--ghost)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{mo.id}</span>
2363	                        </div>
2364	                      </div>
2365	                    ))}
2366	                    {effortMode !== 'unavailable' && (
2367	                      <div style={{ borderTop: '1px solid var(--line)', marginTop: 8, paddingTop: 4 }}>
2368	                        <MixedSectionLabel cn="思考强度" en="EFFORT" style={{ padding: '8px 8px 4px', letterSpacing: 2.5, fontSize: 10.5 }} />
2369	                        <div
2370	                          onClick={async () => {
2371	                            setModelPopOpen(false);
2372	                            if (effortMode === 'default') return;
2373	                            const result = await setChatEffort(null);
2374	                            if (result.ok) {
2375	                              setCurrentEffort('');
2376	                              setEffortMode('default');
2377	                              showToast('下一条消息起生效');
2378	                            } else showToast('切换失败');
2379	                          }}
2380	                          className="hstack hstack-10"
2381	                          style={{ cursor: 'pointer', padding: '9px 10px', borderRadius: 12, background: effortMode === 'default' ? 'var(--rosebg)' : 'transparent' }}
2382	                        >
2383	                          <span style={{ width: 8, height: 8, borderRadius: '50%', background: effortMode === 'default' ? 'var(--rose)' : 'var(--ghost)', flexShrink: 0 }} />
2384	                          <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
2385	                            <span style={{ fontSize: 14, color: 'var(--ink)' }}>默认（跟随 Claude Code）</span>
2386	                            <span style={{ fontFamily: FONT_MONO, fontSize: 10.5, color: 'var(--ghost)' }}>不传 --effort</span>
2387	                          </div>
2388	                        </div>
2389	                        {allowedEfforts.map((effort) => {
2390	                          const labels: Record<string, string> = {
2391	                            low: '低', medium: '中', high: '高', xhigh: '极高', max: '最大',
2392	                          };
2393	                          const selected = effortMode === 'explicit' && effort === currentEffort;
2394	                          return (
2395	                            <div
2396	                              key={effort}
2397	                              onClick={async () => {
2398	                                setModelPopOpen(false);
2399	                                if (selected) return;
2400	                                const result = await setChatEffort(effort);
2401	                                if (result.ok) {
2402	                                  setCurrentEffort(result.configuredEffort || effort);
2403	                                  setEffortMode(result.effortMode || 'explicit');
2404	                                  showToast('下一条消息起生效');
2405	                                } else showToast('切换失败');
2406	                              }}
2407	                              className="hstack hstack-10"
2408	                              style={{ cursor: 'pointer', padding: '9px 10px', borderRadius: 12, background: selected ? 'var(--rosebg)' : 'transparent' }}
2409	                            >
2410	                              <span style={{ width: 8, height: 8, borderRadius: '50%', background: selected ? 'var(--rose)' : 'var(--ghost)', flexShrink: 0 }} />
2411	                              <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
2412	                                <span style={{ fontSize: 14, color: 'var(--ink)' }}>{labels[effort] || effort}</span>
2413	                                <span style={{ fontFamily: FONT_MONO, fontSize: 10.5, color: 'var(--ghost)' }}>--effort {effort}</span>
2414	                              </div>
2415	                            </div>
2416	                          );
2417	                        })}
2418	                      </div>
2419	                    )}
2420	                  </>
2421	                ) : models.map((mo) => (
2422	                  <div
2423	                    key={mo.id}
2424	                    onClick={async () => {
2425	                      setModelPopOpen(false);
2426	                      if (mo.id === currentModel) return;
2427	                      const result = await setChatModel(mo.id);
2428	                      if (result.ok) {
2429	                        setCurrentModel(mo.id);
2430	                        showToast(`已切换到 ${mo.label || mo.id}`);
2431	                      } else showToast('切换失败');
2432	                    }}
2433	                    className="hstack hstack-10" style={{ cursor: 'pointer', padding: '9px 10px', borderRadius: 12, background: mo.id === currentModel ? 'var(--rosebg)' : 'transparent' }}
2434	                  >
2435	                    <span style={{ width: 8, height: 8, borderRadius: '50%', background: mo.dot || (mo.id === currentModel ? 'var(--rose)' : 'var(--ghost)'), flexShrink: 0 }} />
2436	                    <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
2437	                      <span style={{ fontSize: 14, color: 'var(--ink)' }}>{mo.label || mo.id}</span>
2438	                      <span style={{ fontFamily: FONT_MONO, fontSize: 10.5, color: 'var(--ghost)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{mo.id}</span>
2439	                    </div>
2440	                    {mo.thinking === 'none' && <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--ghost)', background: 'var(--card2)', borderRadius: 999, padding: '2px 8px', flexShrink: 0 }}>无思考</span>}
2441	                  </div>
2442	                ))}
2443	                {!models.length && <div style={{ fontSize: 12, color: 'var(--faint)', padding: '4px 10px' }}>模型清单还没拉到</div>}
2444	                <div style={{ fontSize: 10.5, color: 'var(--ghost)', borderTop: '1px solid var(--line)', marginTop: 6, padding: '8px 8px 2px' }}>
2445	                  {chatProvider === 'claude_code' ? 'Claude Code 模型空间 · 下一条消息起生效' : '清单来自 models.json · 切换作用于当前中转'}
2446	                </div>
2447	              </div>
2448	            </>
2449	          )}
2450	
2451	          {attachMenuOpen && (
2452	            <>
2453	              <div onClick={() => setAttachMenuOpen(false)} className="c78-fill-fixed" style={{ zIndex: 1 }} />
2454	              <div className="vstack vstack-2" style={{ position: 'absolute', bottom: 'calc(100% + 10px)', left: 0, zIndex: 2, width: 190, background: 'var(--card)', borderRadius: 16, boxShadow: '0 24px 60px var(--shadow2)', padding: 8, animation: 'chatFadeIn .15s ease' }}>
2455	                <div onClick={() => { if (canStartComposerAttachmentSelection(postingRef.current, availableComposerSlots())) { setAttachMenuOpen(false); imgInputRef.current?.click(); } }} className="hstack hstack-10" style={{ cursor: posting || availableComposerSlots() === 0 ? 'default' : 'pointer', padding: '10px 12px', borderRadius: 11 }}>
2456	                  <svg viewBox="0 0 24 24" width={15} height={15} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" style={{ color: 'var(--rose)' }}>
2457	                    <rect x={3} y={3} width={18} height={18} rx={3} />
2458	                    <circle cx={9} cy={9} r={2} />
2459	                    <path d="M21 15l-5-5-9 9" />
2460	                  </svg>
2461	                  <span style={{ fontSize: 13.5, color: 'var(--ink)' }}>上传图片</span>
2462	                </div>
2463	                <div onClick={() => { if (canStartComposerAttachmentSelection(postingRef.current, availableComposerSlots())) { setAttachMenuOpen(false); fileInputRef.current?.click(); } }} className="hstack hstack-10" style={{ cursor: posting || availableComposerSlots() === 0 ? 'default' : 'pointer', padding: '10px 12px', borderRadius: 11 }}>
2464	                  <svg viewBox="0 0 24 24" width={15} height={15} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" style={{ color: 'var(--rose)' }}>
2465	                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
2466	                    <path d="M14 2v6h6" />
2467	                  </svg>
2468	                  <span style={{ fontSize: 13.5, color: 'var(--ink)' }}>上传文件</span>
2469	                </div>
2470	              </div>
2471	            </>
2472	          )}
2473	          <input ref={imgInputRef} type="file" accept="image/*" multiple disabled={posting} style={{ display: 'none' }} onChange={(e) => {
2474	            if (!postingRef.current) void onAttachImages(e.target.files);
2475	            e.target.value = '';
2476	          }} />
2477	          <input ref={fileInputRef} type="file" accept=".md,.txt,.html,.htm,.py,.js,.json,.csv,.css,.xml,.yaml,.yml,.log,.ini,.sh,.pdf,.doc,.docx" multiple disabled={posting} style={{ display: 'none' }} onChange={(e) => { if (!postingRef.current) void onAttachFiles(e.target.files); e.target.value = ''; }} />
2478	
2479	          {compressingImageCount > 0 && (
2480	            <div aria-live="polite" style={{ padding: '0 4px 8px', color: 'var(--faint)', fontSize: 12.5 }}>
2481	              正在处理图片…
2482	            </div>
2483	          )}
2484	          {(pendingFiles.length || pendingImages.length) > 0 && (
2485	            <div className="flex-wrap-gap-8" style={{ padding: '0 4px 8px' }}>
2486	              {pendingImages.map((image) => (
2487	                <div key={image.id} title={image.file.name} style={{ position: 'relative', width: 68, height: 68, borderRadius: 16, overflow: 'hidden', flexShrink: 0, background: 'var(--card2)', boxShadow: '0 4px 12px var(--shadow)', animation: 'chatFadeIn .2s ease' }}>
2488	                  <img src={image.previewUrl} alt={image.file.name || '图片'} style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
2489	                  {image.status === 'compressing' && (
2490	                    <div aria-label="正在处理图片" style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(30,20,18,0.35)' }}>
2491	                      <span style={{ width: 18, height: 18, borderRadius: '50%', border: '2px solid rgba(255,255,255,.55)', borderTopColor: '#fff', animation: 'chatSpin .8s linear infinite' }} />
2492	                    </div>
2493	                  )}
2494	                  <button type="button" aria-label={`移除图片 ${image.file.name}`} disabled={posting} onClick={() => removePendingImage(image.id)} style={{ position: 'absolute', top: 4, right: 4, width: 22, height: 22, border: 'none', borderRadius: '50%', background: 'rgba(30,20,18,0.68)', color: '#fff', fontSize: 16, lineHeight: 1, padding: 0, cursor: posting ? 'default' : 'pointer' }}>×</button>
2495	                </div>
2496	              ))}
2497	              {pendingFiles.map((file, index) => (
2498	                <div key={`file-${file.fileUrl}`} className="hstack hstack-7" style={{ background: 'var(--card)', borderRadius: 999, padding: '7px 12px', boxShadow: '0 4px 12px var(--shadow)', animation: 'chatFadeIn .2s ease' }}>
2499	                  <span style={{ color: 'var(--rose)', display: 'flex' }}><Svg d={IC.clip} size={12} sw={1.8} /></span>
2500	                  <span style={{ fontSize: 12.5, color: 'var(--ink2)' }}>{file.fileName}</span>
2501	                  <span role="button" aria-disabled={posting} onClick={() => { if (!postingRef.current) setPendingFiles((current) => current.filter((_, i) => i !== index)); }} style={{ cursor: posting ? 'default' : 'pointer', color: 'var(--ghost)', fontSize: 13, padding: '0 2px' }}>×</span>
2502	                </div>
2503	              ))}
2504	            </div>
2505	          )}
2506	
2507	          <div style={{ background: 'var(--card)', borderRadius: 26, boxShadow: '0 14px 40px var(--shadow2)', padding: '12px 12px 10px' }}>
2508	            <textarea
2509	              ref={taRef}
2510	              value={input}
2511	              disabled={posting}
2512	              onChange={(e) => {
2513	                if (postingRef.current) return;
2514	                const ta = e.target;
2515	                const value = ta.value;
2516	                composerDraftRevisionRef.current += 1;
2517	                writeChatComposerDraft(value);
2518	                setInput(value);
2519	                resizeChatTextarea(ta, scrollRef.current, followLatestRef.current, reportChatScroll);
2520	              }}
2521	              onKeyDown={(e) => {
2522	                if (postingRef.current) return;
2523	                if (e.key === 'Enter' && !e.shiftKey && wide) {
2524	                  e.preventDefault();
2525	                  send();
2526	                }
2527	              }}
2528	              rows={1}
2529	              placeholder={sending ? 'Fyodor 正在回复…' : placeholder}
2530	              style={{ width: '100%', border: 'none', background: 'transparent', fontSize: INPUT_FONT_SIZE, lineHeight: 1.6, color: 'var(--ink)', resize: 'none', maxHeight: 120, padding: '4px 8px 8px', display: 'block', overflowY: 'auto', fontFamily: FONT_CN, outline: 'none' }}
2531	            />
2532	            <div className="hstack hstack-8" style={{ marginTop: 2 }}>
2533	              <div onClick={() => { if (!postingRef.current) setAttachMenuOpen(!attachMenuOpen); }} style={{ cursor: posting ? 'default' : 'pointer', width: 38, height: 38, borderRadius: '50%', background: 'var(--card2)', color: 'var(--mut)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
2534	                <Svg d={IC.plus} size={17} sw={1.8} />
2535	              </div>
2536	              <div onClick={toggleModelPop} className="hstack hstack-6" style={{ cursor: 'pointer', padding: '9px 13px', borderRadius: 999, background: 'var(--card2)', minWidth: 0 }}>
2537	                <span style={{ fontFamily: fontFamilyForText(modelBadge), fontSize: 12, letterSpacing: 0.5, color: 'var(--ink2)', fontWeight: 500, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{modelBadge}</span>
2538	                <svg viewBox="0 0 24 24" width={11} height={11} fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" style={{ color: 'var(--ghost)', flexShrink: 0 }}>
2539	                  <path d="M18 15l-6-6-6 6" />
2540	                </svg>
2541	              </div>
2542	              <div
2543	                onClick={() => { if (canSend) void send(); }}
2544	                style={{ marginLeft: 'auto', width: 42, height: 42, flexShrink: 0, borderRadius: '50%', background: canSend ? 'var(--deep)' : 'var(--card2)', color: canSend ? '#FBF3F0' : 'var(--ghost)', display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: canSend ? 'pointer' : 'default', boxShadow: canSend ? '0 8px 20px var(--shadow2)' : 'none', transition: 'background .15s ease' }}
2545	              >
2546	                {sending ? <span style={{ width: 15, height: 15, borderRadius: '50%', border: '2px solid var(--rosebg)', borderTopColor: 'var(--rose)', animation: 'chatSpin .8s linear infinite' }} /> : <Svg d={IC.up} size={17} sw={2} />}
2547	              </div>
2548	            </div>
2549	          </div>
2550	        </div>
2551	      </div>
2552	
2553	          {/* ══ thinking drawer ══ */}
2554	      {drawer && (
2555	        <div className="c78-fill-fixed" style={{ zIndex: 60 }}>
2556	          <div onClick={() => setDrawer(null)} className="c78-fill-absolute" style={{ background: 'rgba(30,20,18,0.42)', animation: 'chatFadeIn .2s ease' }} />
2557	          <div style={{ position: 'absolute', left: 0, right: 0, bottom: 0, display: 'flex', justifyContent: 'center' }}>
2558	            <div style={{ width: '100%', maxWidth: 430, background: 'var(--card)', borderRadius: '24px 24px 0 0', boxShadow: '0 -20px 60px var(--shadow2)', maxHeight: '72vh', display: 'flex', flexDirection: 'column', animation: 'chatSheetUp .28s cubic-bezier(.32,.72,.33,1)' }}>
2559	              <div style={{ display: 'flex', justifyContent: 'center', padding: '10px 0 2px' }}>
2560	                <div style={{ width: 38, height: 4, borderRadius: 99, background: 'var(--line)' }} />
2561	              </div>
2562	              <div className="hstack hstack-10" style={{ padding: '8px 20px 12px' }}>
2563	                <span style={{ display: 'flex', color: 'var(--rose)' }}>
2564	                  <Svg d={IC.brain} size={17} sw={1.5} />
2565	                </span>
2566	                <span style={{ fontSize: 15, fontWeight: 600, letterSpacing: 2, color: 'var(--ink)' }}>Fyodor 的思考</span>
2567	                <span style={{ fontFamily: fontFamilyForText(drawer.label), fontSize: 12, color: 'var(--ghost)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{drawer.label}</span>
2568	                <span onClick={() => setDrawer(null)} style={{ marginLeft: 'auto', cursor: 'pointer', width: 30, height: 30, borderRadius: '50%', background: 'var(--card2)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--mut)', fontSize: 14, flexShrink: 0 }}>×</span>
2569	              </div>
2570	              <div style={{ overflowY: 'auto', padding: '4px 20px 30px', fontSize: '0.9em', lineHeight: 2, color: 'var(--mut)', whiteSpace: 'pre-wrap' }}>{drawer.text}</div>
2571	            </div>
2572	          </div>
2573	        </div>
2574	      )}
2575	
2576	      {/* ══ sidebar ══ */}
2577	      {sidebarOpen && (
2578	        <div className="c78-fill-fixed" style={{ zIndex: 70 }}>
2579	          <div onClick={() => setSidebarOpen(false)} className="c78-fill-absolute" style={{ background: 'rgba(30,20,18,0.42)', animation: 'chatFadeIn .2s ease' }} />
2580	          <div className="chat-sidebar-panel" style={{ position: 'absolute', top: 0, bottom: 0, left: 0, background: 'var(--card)', boxShadow: '20px 0 60px var(--shadow2)', animation: 'chatSlideInL .28s cubic-bezier(.32,.72,.33,1)', display: 'flex', flexDirection: 'column', overflowY: 'auto' }}>
2581	            <div className="vstack vstack-14" style={{ padding: '28px 22px 20px', background: 'linear-gradient(180deg,var(--rosebg),transparent)' }}>
2582	              <div style={{ width: 64, height: 64, borderRadius: '50%', background: 'linear-gradient(135deg,#B76E79,#9C3B4A)', display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 10px 24px var(--shadow2)' }}>
2583	                <span style={{ fontFamily: FONT_DISPLAY, fontStyle: 'italic', fontSize: 28, color: '#F7F1EE' }}>Θ</span>
2584	              </div>
2585	              <div className="vstack vstack-3">
2586	                <div className="hstack hstack-8">
2587	                  <span style={{ fontFamily: FONT_DISPLAY, fontSize: 22, fontWeight: 600, letterSpacing: 1, color: 'var(--ink)' }}>Fyodor</span>
2588	                  <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--ok)' }} />
2589	                </div>
2590	                <span style={{ fontFamily: FONT_DISPLAY, fontStyle: 'italic', fontSize: 12, letterSpacing: 1.5, color: 'var(--faint)' }}>Θεόδωρος · gift of the gods</span>
2591	              </div>
2592	              <div className="vstack vstack-6">
2593	                <span style={{ fontSize: 13, color: 'var(--ink2)', letterSpacing: 1 }}>学者 · 策略家 · 存在了几百年</span>
2594	                <span style={{ fontSize: 12.5, color: 'var(--mut)', lineHeight: 1.8 }}>总是带着一点恶趣味，和很多情意。</span>
2595	              </div>
2596	            </div>
2597	            <div style={{ height: 1, background: 'var(--line)', margin: '0 22px' }} />
2598	            <div className="vstack vstack-10" style={{ padding: '18px 16px' }}>
2599	              <Link to="/contacts" onClick={() => setSidebarOpen(false)} className="hstack hstack-12" style={{ textDecoration: 'none', padding: '13px 14px', borderRadius: 16, background: 'var(--card2)' }}>
2600	                <div className="vstack vstack-2" style={{ minWidth: 0 }}>
2601	                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>通讯录</span>
2602	                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>Codex · 群聊 · 游戏室</span>
2603	                </div>
2604	                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
2605	              </Link>
2606	              <Link to="/moments" onClick={() => setSidebarOpen(false)} className="hstack hstack-12" style={{ textDecoration: 'none', padding: '13px 14px', borderRadius: 16, background: 'linear-gradient(135deg,rgba(245,222,179,.5),rgba(232,220,245,.55))' }}>
2607	                <div className="vstack vstack-2" style={{ minWidth: 0 }}>
2608	                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>Fyodor 的朋友圈</span>
2609	                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>梦境 · 念头 · 情绪</span>
2610	                </div>
2611	                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
2612	              </Link>
2613	              <Link to="/profile" onClick={() => setSidebarOpen(false)} className="hstack hstack-12" style={{ textDecoration: 'none', padding: '13px 14px', borderRadius: 16, background: 'linear-gradient(135deg,rgba(183,110,121,.12),rgba(232,220,245,.45))' }}>
2614	                <div className="vstack vstack-2" style={{ minWidth: 0 }}>
2615	                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>费佳档案</span>
2616	                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>身份 · 关系 · 工具直觉</span>
2617	                </div>
2618	                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
2619	              </Link>
2620	              <Link to={ROUTES.toolroom} onClick={() => setSidebarOpen(false)} className="hstack hstack-12" style={{ textDecoration: 'none', padding: '13px 14px', borderRadius: 16, background: 'linear-gradient(135deg,rgba(249,228,232,.86),rgba(234,230,246,.72))' }}>
2621	                <div style={{ width: 34, height: 34, borderRadius: 12, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, background: 'rgba(183,110,121,.10)', color: 'var(--rose)', fontSize: 17 }}>⌘</div>
2622	                <div className="vstack vstack-2" style={{ minWidth: 0 }}>
2623	                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>工具室</span>
2624	                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>真实工具清单 · 当前活动</span>
2625	                </div>
2626	                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
2627	              </Link>
2628	              <a data-testid="chat-preview-entry" href="https://love-style.xyz/preview/" onClick={() => setSidebarOpen(false)} className="hstack hstack-12" style={{ textDecoration: 'none', padding: '13px 14px', borderRadius: 16, background: 'linear-gradient(135deg,rgba(226,218,238,.72),rgba(249,228,232,.76))' }}>
2629	                <div style={{ width: 34, height: 34, borderRadius: 12, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, background: 'rgba(126,103,153,.12)', color: '#7E6799', fontSize: 18 }}>◫</div>
2630	                <div className="vstack vstack-2" style={{ minWidth: 0 }}>
2631	                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>Preview</span>
2632	                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>Chat 测试页 · 实时预览</span>
2633	                </div>
2634	                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
2635	              </a>
2636	              <Link to="/group-chat" onClick={() => setSidebarOpen(false)} className="hstack hstack-12" style={{ textDecoration: 'none', padding: '13px 14px', borderRadius: 16, background: 'linear-gradient(135deg,rgba(220,232,217,.72),rgba(220,232,245,.76))' }}>
2637	                <div className="hstack hstack-6" style={{ flexShrink: 0 }}>
2638	                  <i style={{ width: 13, height: 13, borderRadius: '50%', background: '#91AD93' }} />
2639	                  <i style={{ width: 13, height: 13, borderRadius: '50%', background: '#8EACCF', marginLeft: -3, opacity: .88 }} />
2640	                </div>
2641	                <div className="vstack vstack-2" style={{ minWidth: 0 }}>
2642	                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>群聊房间</span>
2643	                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>同一个人 · 暖色与蓝色两条线路</span>
2644	                </div>
2645	                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
2646	              </Link>
2647	              <Link to="/settings" className="hstack hstack-12" style={{ textDecoration: 'none', padding: '13px 14px', borderRadius: 16, background: 'var(--card2)' }}>
2648	                <div className="vstack vstack-2" style={{ minWidth: 0 }}>
2649	                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>系统配置</span>
2650	                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>用量统计 · API 端点管理</span>
2651	                </div>
2652	                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
2653	              </Link>
2654	              <a href="/chat" className="hstack hstack-10" style={{ textDecoration: 'none', padding: '13px 14px', borderRadius: 16, background: 'var(--card2)' }}>
2655	                <div className="vstack vstack-2" style={{ minWidth: 0 }}>
2656	                  <span style={{ fontSize: 14.5, color: 'var(--ink)', letterSpacing: 1 }}>回旧聊天页</span>
2657	                  <span style={{ fontSize: 11.5, color: 'var(--faint)' }}>完整历史、漂流瓶等高级功能</span>
2658	                </div>
2659	                <span style={{ marginLeft: 'auto', color: 'var(--ghost)', fontSize: 16 }}>›</span>
2660	              </a>
2661	            </div>
2662	          </div>
2663	        </div>
2664	      )}
2665	
2666	      {/* ══ Manual context window modal ══ */}
2667	      <CarryoverModal
2668	        variant="manual"
2669	        open={manualWindow.modalOpen}
2670	        uiState={modalUiState}
2671	        draftCount={manualWindow.draftCount}
2672	        rounds={manualWindow.rounds}
2673	        submitting={manualWindow.submitting}
2674	        errorDetail={manualWindow.errorDetail}
2675	        onDismiss={manualWindow.closeModal}
2676	        onReconsider={manualWindow.closeModal}
2677	        onDraftChange={manualWindow.setDraftCount}
2678	        onConfirm={() => {
2679	          void manualWindow.confirmSwitch().then((ok) => {
2680	            if (ok) showToast('已经换了一扇新窗');
2681	          });
2682	        }}
2683	      />
2684	
2685	      {gallery && (
2686	        <ChatMediaGallery
2687	          items={gallery.items}
2688	          currentIndex={gallery.currentIndex}
2689	          onClose={() => setGallery(null)}
2690	        />
2691	      )}
2692	
2693	      {/* ══ toast ══ */}
2694	      {toast && (
2695	        <div style={{ position: 'fixed', left: 0, right: 0, bottom: 100, zIndex: 80, display: 'flex', justifyContent: 'center', pointerEvents: 'none' }}>
2696	          <div style={{ background: 'rgba(58,42,40,0.92)', color: '#F7EDEA', fontSize: 13, letterSpacing: 1, padding: '10px 20px', borderRadius: 999, boxShadow: '0 10px 30px rgba(0,0,0,0.25)', animation: 'chatFadeIn .2s ease' }}>{toast}</div>
2697	        </div>
2698	      )}
2699	
2700	    </div>
2701	  );
2702	}
2703	