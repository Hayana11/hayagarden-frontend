1	export type ChatScrollIntent = 'follow-latest' | 'preserve-position' | 'explicit-target';
2	export type ChatScrollSource =
3	  | 'initial-history'
4	  | 'warm-restore'
5	  | 'stream-follow'
6	  | 'background-poll'
7	  | 'search-jump'
8	  | 'history-window'
9	  | 'textarea-resize'
10	  | 'explicit-scroll-bottom';
11	
12	export type ChatScrollProbeRecord = {
13	  timestamp: number;
14	  source: ChatScrollSource;
15	  intent: ChatScrollIntent;
16	  beforeScrollTop: number;
17	  afterScrollTop: number;
18	  targetScrollTop: number;
19	  clientHeight: number;
20	  scrollHeight: number;
21	  followLatest: boolean;
22	  textareaHeight?: number;
23	};
24	
25	type ChatScrollContainer = Pick<HTMLElement, 'scrollHeight' | 'clientHeight'>;
26	type ChatScrollWriter = ChatScrollContainer & {
27	  scrollTop: number;
28	  scrollTo: (options: { top: number; behavior?: ScrollBehavior }) => void;
29	};
30	
31	export function clampChatScrollTop(container: ChatScrollContainer, value: number): number {
32	  const maxScrollTop = Math.max(0, container.scrollHeight - container.clientHeight);
33	  const safeValue = Number.isFinite(value) ? value : 0;
34	  return Math.min(maxScrollTop, Math.max(0, safeValue));
35	}
36	
37	export function writeChatScroll(
38	  container: ChatScrollWriter,
39	  request: {
40	    source: ChatScrollSource;
41	    intent: ChatScrollIntent;
42	    followLatest: boolean;
43	    targetScrollTop?: number;
44	    behavior?: ScrollBehavior;
45	    textareaHeight?: number;
46	  },
47	  probe?: (record: ChatScrollProbeRecord) => void,
48	): number {
49	  const beforeScrollTop = container.scrollTop;
50	  const targetScrollTop = request.intent === 'follow-latest' && request.followLatest
51	    ? clampChatScrollTop(container, container.scrollHeight)
52	    : clampChatScrollTop(container, request.targetScrollTop ?? beforeScrollTop);
53	
54	  if (request.behavior === 'smooth') {
55	    container.scrollTo({ top: targetScrollTop, behavior: 'smooth' });
56	  } else {
57	    container.scrollTop = targetScrollTop;
58	  }
59	
60	  probe?.({
61	    timestamp: Date.now(),
62	    source: request.source,
63	    intent: request.intent,
64	    beforeScrollTop,
65	    afterScrollTop: container.scrollTop,
66	    targetScrollTop,
67	    clientHeight: container.clientHeight,
68	    scrollHeight: container.scrollHeight,
69	    followLatest: request.followLatest,
70	    textareaHeight: request.textareaHeight,
71	  });
72	  return targetScrollTop;
73	}
74	
75	export function resizeChatTextarea(
76	  textarea: Pick<HTMLTextAreaElement, 'style' | 'scrollHeight'>,
77	  scrollContainer: (ChatScrollWriter & HTMLElement) | null,
78	  followLatest: boolean,
79	  probe?: (record: ChatScrollProbeRecord) => void,
80	): number {
81	  const snapshot = scrollContainer
82	    ? {
83	      followLatest,
84	      scrollTop: clampChatScrollTop(scrollContainer, scrollContainer.scrollTop),
85	    }
86	    : null;
87	
88	  textarea.style.height = 'auto';
89	  const nextHeight = Math.min(textarea.scrollHeight, 120);
90	  textarea.style.height = `${nextHeight}px`;
91	
92	  if (scrollContainer && snapshot) {
93	    writeChatScroll(
94	      scrollContainer,
95	      {
96	        source: 'textarea-resize',
97	        intent: snapshot.followLatest ? 'follow-latest' : 'preserve-position',
98	        followLatest: snapshot.followLatest,
99	        targetScrollTop: snapshot.scrollTop,
100	        textareaHeight: nextHeight,
101	      },
102	      probe,
103	    );
104	  }
105	  return nextHeight;
106	}