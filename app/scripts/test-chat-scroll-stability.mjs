1	import assert from 'node:assert/strict';
2	import fs from 'node:fs';
3	
4	import {
5	  clampChatScrollTop,
6	  resizeChatTextarea,
7	  writeChatScroll,
8	} from '../src/lib/chatScrollCoordinator.ts';
9	
10	const screen = fs.readFileSync(new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8');
11	const coordinator = fs.readFileSync(new URL('../src/lib/chatScrollCoordinator.ts', import.meta.url), 'utf8');
12	
13	function container({ scrollHeight = 1000, clientHeight = 500, scrollTop = 0 } = {}) {
14	  const writes = [];
15	  const value = {
16	    scrollHeight,
17	    clientHeight,
18	    scrollTop,
19	    writes,
20	    scrollTo({ top, behavior }) {
21	      value.scrollTop = top;
22	      writes.push({ top, behavior });
23	    },
24	  };
25	  return value;
26	}
27	
28	function textarea(scrollHeight) {
29	  return { scrollHeight, style: { height: '96px' } };
30	}
31	
32	function assertLegalScroll(c) {
33	  assert.ok(c.scrollTop >= 0);
34	  assert.ok(c.scrollTop <= Math.max(0, c.scrollHeight - c.clientHeight));
35	}
36	
37	const probeRecords = [];
38	
39	// T1: growth while following latest keeps the transcript at the current bottom.
40	{
41	  const c = container({ scrollTop: 500 });
42	  resizeChatTextarea(textarea(200), c, true, (record) => probeRecords.push(record));
43	  assert.equal(c.scrollTop, 500);
44	  assertLegalScroll(c);
45	}
46	
47	// T2: shrink while following latest does not leave a bottom gap.
48	{
49	  const c = container({ scrollTop: 500 });
50	  resizeChatTextarea(textarea(20), c, true);
51	  assert.equal(c.scrollTop, c.scrollHeight - c.clientHeight);
52	  assertLegalScroll(c);
53	}
54	
55	// T3: resizing while browsing history restores the visual anchor, not the bottom.
56	{
57	  const c = container({ scrollTop: 220 });
58	  resizeChatTextarea(textarea(200), c, false);
59	  assert.equal(c.scrollTop, 220);
60	  assert.notEqual(c.scrollTop, c.scrollHeight - c.clientHeight);
61	  assertLegalScroll(c);
62	}
63	
64	// T4: stream-follow and textarea-resize share the same bounded writer.
65	{
66	  const c = container({ scrollHeight: 1000, clientHeight: 500, scrollTop: 500 });
67	  writeChatScroll(c, {
68	    source: 'stream-follow',
69	    intent: 'follow-latest',
70	    followLatest: true,
71	  });
72	  c.scrollHeight = 1100;
73	  resizeChatTextarea(textarea(120), c, true);
74	  writeChatScroll(c, {
75	    source: 'stream-follow',
76	    intent: 'follow-latest',
77	    followLatest: true,
78	  });
79	  assert.equal(c.scrollTop, 600);
80	  assertLegalScroll(c);
81	}
82	
83	// T5: explicit search/jump remains an explicit target and is not consumed as follow-latest.
84	{
85	  const c = container({ scrollTop: 600 });
86	  writeChatScroll(c, {
87	    source: 'search-jump',
88	    intent: 'explicit-target',
89	    followLatest: false,
90	    targetScrollTop: 280,
91	    behavior: 'smooth',
92	  });
93	  assert.equal(c.scrollTop, 280);
94	  assert.deepEqual(c.writes.at(-1), { top: 280, behavior: 'smooth' });
95	  assertLegalScroll(c);
96	}
97	
98	// T6: warm restore preserves history position, or follows the latest edge when pinned.
99	{
100	  const historical = container({ scrollTop: 180 });
101	  writeChatScroll(historical, {
102	    source: 'warm-restore',
103	    intent: 'preserve-position',
104	    followLatest: false,
105	    targetScrollTop: 180,
106	  });
107	  assert.equal(historical.scrollTop, 180);
108	
109	  const latest = container({ scrollTop: 180 });
110	  writeChatScroll(latest, {
111	    source: 'warm-restore',
112	    intent: 'follow-latest',
113	    followLatest: true,
114	  });
115	  assert.equal(latest.scrollTop, latest.scrollHeight - latest.clientHeight);
116	}
117	
118	// T7: every coordinator write is clamped after geometry changes.
119	{
120	  const c = container({ scrollHeight: 900, clientHeight: 400, scrollTop: 200 });
121	  assert.equal(clampChatScrollTop(c, -50), 0);
122	  assert.equal(clampChatScrollTop(c, 9999), 500);
123	  c.scrollHeight = 250;
124	  c.clientHeight = 400;
125	  writeChatScroll(c, {
126	    source: 'textarea-resize',
127	    intent: 'preserve-position',
128	    followLatest: false,
129	    targetScrollTop: 9999,
130	  });
131	  assert.equal(c.scrollTop, 0);
132	  assertLegalScroll(c);
133	}
134	
135	assert.equal((screen.match(/resizeChatTextarea\(/g) || []).length, 2);
136	assert.match(screen, /from ['"]\.\.\/lib\/chatScrollCoordinator['"]/);
137	assert.match(screen, /source: 'search-jump'/);
138	assert.match(screen, /source: 'history-window'/);
139	assert.match(screen, /source: 'warm-restore'/);
140	assert.match(screen, /source: 'background-poll'/);
141	assert.match(screen, /source: 'stream-follow'/);
142	assert.match(screen, /source: 'initial-history'/);
143	assert.doesNotMatch(screen, /\bscrollTo\s*\(/);
144	assert.doesNotMatch(screen, /\bscrollTop\s*=/);
145	assert.match(screen, /<div ref=\{scrollRef\} onScroll=\{handleTranscriptScroll\}/);
146	assert.match(screen, /renderUserMsg/);
147	const userRenderer = screen.slice(
148	  screen.indexOf('function renderUserMsg'),
149	  screen.indexOf('function renderAssistantMsg'),
150	);
151	assert.match(userRenderer, /chat-message-content chat-message-content-user/);
152	assert.match(userRenderer, /ChatMediaGroup/);
153	assert.match(userRenderer, /chat-message-bubble chat-message-bubble-user/);
154	assert.match(userRenderer, /ChatMediaGroup[\s\S]*chat-message-bubble/);
155	assert.match(screen, /live-segment-/);
156	assert.match(screen, /display-text-/);
157	assert.match(coordinator, /timestamp/);
158	assert.match(coordinator, /beforeScrollTop/);
159	assert.match(coordinator, /afterScrollTop/);
160	assert.match(coordinator, /clientHeight/);
161	assert.match(coordinator, /scrollHeight/);
162	assert.match(coordinator, /followLatest/);
163	assert.match(coordinator, /textareaHeight/);
164	assert.equal(probeRecords.length, 1);
165	assert.equal(probeRecords[0].source, 'textarea-resize');
166	assert.equal(probeRecords[0].followLatest, true);
167	assert.equal(probeRecords[0].textareaHeight, 120);
168	
169	console.log('test:chat-scroll-stability — T1-T7 all checks passed');