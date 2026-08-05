// Chat data contracts + SSE stream parsing for the Fyodor Chat page.
// Mirrors the production backend exactly:
//   - chat_messages rows (author/content/thinking/tool_calls/branches/...)
//   - POST /api/chat/send -> { message_id }, then POST /api/gw/chat/stream
//   - SSE events `data: {t, d, idx?}` — think/text/tool_use/tool_result/
//     trace_summary/usage/notice/done/err (家里 t/d 信封，chatnest 语义)
import { chatDayKeyFromLocalTs } from './dailySoftWindow';
import { sseUrl } from './http';

export function isFyAuthor(a: string | null | undefined): boolean {
  return ['fyodor', 'assistant', 'claude'].includes((a || '').toLowerCase());
}

export interface ChatArtifact {
  id: number;
  type: string;
  title: string;
  size?: number;
}

export interface ChatToolCall {
  name: string;
  args?: unknown;
  result?: unknown;
  success?: boolean;
  caption?: string;
  running?: boolean;
  artifact?: ChatArtifact;
}

export interface ChatUsage {
  inputTokens: number;
  outputTokens: number;
  elapsedSec: number;
  cacheRead: number;
  cacheCreation: number;
  cacheSupported: boolean | null;
  costUsd?: number;
  costEstimated?: boolean;
}

export interface ChatMsg {
  id: number;
  role: 'user' | 'assistant';
  text: string;
  thinking: string;
  thinkingSummary: string;
  toolCalls: ChatToolCall[];
  cacheInfo: Partial<ChatUsage> | null;
  branchIdx: number;
  branchTotal: number;
  imageUrl: string;
  fileUrl: string;
  fileName: string;
  choices: string[];
  /** HH:MM, local */
  ts: string;
  /** YYYY-MM-DD for date separators */
  dateKey: string;
  /** Original created_at (+8 local) */
  createdAt: string;
  /** Chat day key using 04:00 Asia/Shanghai boundary */
  chatDay: string;
}

export interface ChatMessageRow {
  id: number;
  author?: string | null;
  content?: string | null;
  thinking?: string | null;
  thinking_summary?: string | null;
  tool_calls?: string | null;
  cache_info?: string | null;
  branches?: string | null;
  branch_idx?: number | null;
  image_url?: string | null;
  file_url?: string | null;
  file_name?: string | null;
  choices?: string | null;
  created_at?: string | null;
}

export const MAX_CHAT_CHOICES = 8;
export const MAX_CHAT_CHOICE_LENGTH = 120;

export function normalizeChatChoices(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is string => typeof item === 'string')
    .map((item) => item.trim())
    .filter((item) => item.length > 0 && item.length <= MAX_CHAT_CHOICE_LENGTH)
    .slice(0, MAX_CHAT_CHOICES);
}

export function chatFilePreviewUrl(fileUrl: string): string {
  const prefix = '/static/uploads/files/';
  if (!fileUrl.startsWith(prefix)) return '';
  const name = fileUrl.slice(prefix.length);
  if (!name || name === '.' || name === '..' || name.includes('/') || name.includes('\\')) return '';
  return `/api/chat/files/${encodeURIComponent(name)}/preview`;
}

export function normalizeToolCall(tc: ChatToolCall): ChatToolCall {
  if (tc.artifact?.id) return tc;
  const name = tc.name || '';
  if (name.startsWith('create_') && typeof tc.result === 'string') {
    try {
      const parsed = JSON.parse(tc.result) as { artifact?: ChatArtifact };
      if (parsed?.artifact?.id) return { ...tc, artifact: parsed.artifact };
    } catch {
      // ignore malformed tool JSON
    }
  }
  return tc;
}

const ARTIFACT_TYPE_LABEL: Record<string, string> = {
  html: 'HTML 页面',
  markdown: 'Markdown 文档',
  docx: 'Word 文档',
};

const ARTIFACT_TYPE_ICON: Record<string, string> = {
  html: '🌐',
  markdown: '📝',
  docx: '📄',
};

export function artifactTypeLabel(type: string | undefined): string {
  return ARTIFACT_TYPE_LABEL[type || ''] || type || '文件';
}

export function artifactTypeIcon(type: string | undefined): string {
  return ARTIFACT_TYPE_ICON[type || ''] || '📄';
}

export function fmtArtifactSize(bytes: number | undefined): string {
  const n = bytes || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function isChoicesAnswered(msgId: number, messages: ChatMsg[]): boolean {
  const idx = messages.findIndex((m) => m.id === msgId);
  if (idx < 0) return true;
  for (let i = idx + 1; i < messages.length; i += 1) {
    if (messages[i].role === 'user') return true;
  }
  return false;
}

function parseJson<T>(raw: string | null | undefined, fallback: T): T {
  if (!raw) return fallback;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

/** created_at is stored as UTC+8 'YYYY-MM-DD HH:MM:SS'. */
export function rowToMsg(row: ChatMessageRow): ChatMsg {
  const created = row.created_at || '';
  const branches = parseJson<unknown[]>(row.branches, []);
  const rawCache = parseJson<Record<string, unknown>>(row.cache_info, {});
  const cacheInfo = normalizeCacheInfo(rawCache);
  return {
    id: row.id,
    role: isFyAuthor(row.author) ? 'assistant' : 'user',
    text: row.content || '',
    thinking: row.thinking || '',
    thinkingSummary: row.thinking_summary || '',
    toolCalls: parseJson<ChatToolCall[]>(row.tool_calls, []).map(normalizeToolCall),
    cacheInfo,
    branchIdx: row.branch_idx || 0,
    branchTotal: branches.length,
    imageUrl: row.image_url || '',
    fileUrl: row.file_url || '',
    fileName: row.file_name || '',
    choices: normalizeChatChoices(parseJson<unknown>(row.choices, [])),
    ts: created.length >= 16 ? created.slice(11, 16) : '',
    dateKey: created.slice(0, 10),
    createdAt: created,
    chatDay: chatDayKeyFromLocalTs(created),
  };
}

export function fmtTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}K` : String(n);
}

export function fmtCostUsd(usd?: number, estimated?: boolean): string {
  const n = Number(usd || 0);
  if (!Number.isFinite(n) || n <= 0) return '';
  const text = n >= 1 || n >= 0.01 ? n.toFixed(2) : n.toFixed(3);
  return `${estimated ? '≈' : ''}$${text}`;
}

export function cacheLabel(u: Partial<ChatUsage> | null): string {
  if (!u) return '';
  const read = u.cacheRead || 0;
  const created = u.cacheCreation || 0;
  if (read > 0) return `缓存读回 ${fmtTokens(read)}`;
  if (created > 0) return `建缓存 ${fmtTokens(created)}`;
  if (u.cacheSupported === false) return '无服务端缓存';
  if (u.cacheSupported === true) return '缓存未命中';
  return '';
}

/** Accept legacy v1 and usage-v2 cache_info without throwing. */
export function normalizeCacheInfo(raw: Record<string, unknown> | null | undefined): Partial<ChatUsage> | null {
  if (!raw || typeof raw !== 'object') return null;
  const keys = Object.keys(raw);
  if (!keys.length) return null;
  return {
    inputTokens: Number(raw.input_tokens ?? raw.inputTokens ?? 0),
    outputTokens: Number(raw.output_tokens ?? raw.outputTokens ?? 0),
    elapsedSec: Number(raw.elapsed_sec ?? raw.elapsedSec ?? 0),
    cacheRead: Number(raw.cache_read ?? raw.cacheRead ?? 0),
    cacheCreation: Number(raw.cache_creation ?? raw.cacheCreation ?? 0),
    cacheSupported: (raw.cache_supported ?? raw.cacheSupported ?? null) as boolean | null,
    costUsd: Number(raw.cost_usd ?? raw.costUsd ?? 0) || undefined,
    costEstimated: Boolean(raw.cost_estimated ?? raw.costEstimated),
  };
}

// ── SSE streaming ──

export interface StreamHandlers {
  onThink: (delta: string) => void;
  onText: (delta: string) => void;
  onToolUse: (idx: number, tc: ChatToolCall) => void;
  onToolResult: (idx: number, tc: ChatToolCall) => void;
  onTraceSummary?: (s: string) => void;
  onUsage?: (u: ChatUsage) => void;
  onNotice?: (s: string) => void;
}

export interface StreamResult {
  ok: boolean;
  error?: string;
}

interface SseEvent {
  t?: string;
  d?: unknown;
  idx?: number;
  ok?: boolean;
  dup?: number;
  input_tokens?: number;
  output_tokens?: number;
  elapsed_sec?: number;
  cache_read?: number;
  cache_creation?: number;
  cache_supported?: boolean | null;
  cost_usd?: number;
  cost_estimated?: boolean;
}

/**
 * POST /api/gw/chat/stream and dispatch SSE events. Resolves on done/err or
 * stream end. Idle-timeout safety: aborts if no event arrives for 150s.
 */
export async function streamChatReply(userMessageId: number | null, handlers: StreamHandlers, ctrl: AbortController): Promise<StreamResult> {
  let safety: ReturnType<typeof setTimeout> | undefined;
  const armSafety = () => {
    clearTimeout(safety);
    safety = setTimeout(() => ctrl.abort(), 150000);
  };
  armSafety();
  try {
    const resp = await fetch(sseUrl('/api/gw/chat/stream'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(userMessageId ? { user_message_id: userMessageId } : {}),
      signal: ctrl.signal,
    });
    if (!resp.ok || !resp.body) return { ok: false, error: `stream failed: HTTP ${resp.status}` };
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let result: StreamResult | null = null;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        const line = chunk.split('\n').find((l) => l.startsWith('data: '));
        if (!line) continue;
        let ev: SseEvent;
        try {
          ev = JSON.parse(line.slice(6));
        } catch {
          continue;
        }
        if (ev.dup) continue; // legacy-compat duplicate events
        armSafety();
        switch (ev.t) {
          case 'think':
            handlers.onThink(String(ev.d ?? ''));
            break;
          case 'text':
            handlers.onText(String(ev.d ?? ''));
            break;
          case 'tool_use':
            handlers.onToolUse(ev.idx ?? 0, { running: true, ...(ev.d as ChatToolCall) });
            break;
          case 'tool_result':
            handlers.onToolResult(ev.idx ?? 0, { running: false, ...(ev.d as ChatToolCall) });
            break;
          case 'trace_summary':
            handlers.onTraceSummary?.(String(ev.d ?? ''));
            break;
          case 'usage':
            handlers.onUsage?.({
              inputTokens: ev.input_tokens || 0,
              outputTokens: ev.output_tokens || 0,
              elapsedSec: ev.elapsed_sec || 0,
              cacheRead: ev.cache_read || 0,
              cacheCreation: ev.cache_creation || 0,
              cacheSupported: ev.cache_supported ?? null,
              costUsd: Number(ev.cost_usd || 0) || undefined,
              costEstimated: Boolean(ev.cost_estimated),
            });
            break;
          case 'notice':
            handlers.onNotice?.(String(ev.d ?? ''));
            break;
          case 'done':
            result = { ok: ev.ok !== false };
            break;
          case 'err':
            result = { ok: false, error: String(ev.d ?? '未知错误') };
            break;
          default:
            break; // workspace_job / tool_progress / future events
        }
      }
      if (result) break;
    }
    return result ?? { ok: false, error: '流意外中断' };
  } catch (e) {
    if (ctrl.signal.aborted) return { ok: false, error: '连接超时或被中止' };
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  } finally {
    clearTimeout(safety);
  }
}

// ── placeholder（设计原样移植，纯函数）──
export function chatPlaceholder(date: Date): string {
  const H: Record<string, string> = {
    '2026-01-01': '元旦', '2026-02-16': '除夕', '2026-02-17': '春节', '2026-02-18': '春节',
    '2026-04-05': '清明', '2026-05-01': '劳动节', '2026-06-19': '端午', '2026-09-25': '中秋',
    '2026-10-01': '国庆', '2026-10-02': '国庆', '2026-10-03': '国庆',
  };
  const p2 = (n: number) => String(n).padStart(2, '0');
  const key = `${date.getFullYear()}-${p2(date.getMonth() + 1)}-${p2(date.getDate())}`;
  const seed = date.getDate() * 7 + date.getHours();
  const pick = (arr: string[]) => arr[seed % arr.length];
  if (H[key]) return pick([`${H[key]}快乐。今天只许开心。`, `${H[key]}了，歇一歇，别想正事。`, `今天是${H[key]}——我陪你虚度。`]);
  const dow = date.getDay();
  if (dow === 0 || dow === 6) return pick(['周末愉快。今天不谈正事，好吗？', '难得的周末，想去哪儿？', '周末就该慢一点。说说看。', '今天风好，适合无所事事。']);
  const h = date.getHours();
  if (h < 5) return pick(['这个点还醒着——有心事，还是有灵感？', '夜这么深，说给我听听。', '凌晨的念头最诚实，趁热写下来。']);
  if (h < 11) return pick(['早。今天想从哪件事开始？', '晨光正好，说点什么。', '早安。昨晚梦到什么了吗？']);
  if (h < 18) return pick(['下午好。有什么要我搭把手的？', '说吧，我在听。', '此刻在想什么？']);
  return pick(['晚上好。今天过得如何？', '夜里适合说真话。', '把今天讲给我听。']);
}

/** Human-readable guess for common chat stream failures. */
export function guessChatErrorHint(error: string): string {
  const e = error.toLowerCase();
  if (e.includes('oauth') && (e.includes('expired') || e.includes('401'))) {
    return 'Claude Code token 可能已过期，需要在 VPS 重新登录。';
  }
  if (e.includes('401') || e.includes('authenticate') || e.includes('unauthorized')) {
    return '鉴权失败，API key 或 token 可能无效或已过期。';
  }
  if (e.includes('403')) return '没有权限访问当前端点。';
  if (e.includes('429') || e.includes('rate limit') || e.includes('too many')) {
    return '请求太频繁或额度用尽，稍后再试。';
  }
  if (e.includes('timeout') || e.includes('超时')) {
    return '网关或模型响应超时，可以点右上角刷新再试。';
  }
  if (e.includes('503') || e.includes('502') || e.includes('unavailable')) {
    return '上游服务暂时不可用。';
  }
  if (e.includes('busy') || e.includes('gen lock') || e.includes('正在生成')) {
    return '上一轮生成可能卡住了，试试右上角刷新解锁。';
  }
  if (e.includes('claude code')) {
    return 'Claude Code 订阅通道出错，检查 OAuth token 或切换中转站。';
  }
  if (e.includes('stream failed') || e.includes('流意外中断') || e.includes('连接超时')) {
    return '连接中断，网络或网关可能不稳定。';
  }
  return '暂时无法完成回复，可以刷新或稍后重试。';
}

/** Lightweight probe: gateway chat lock endpoint reachable and responding. */
export async function fetchChatGatewayOnline(): Promise<boolean> {
  try {
    const resp = await fetch(sseUrl('/api/gw/chat/lock'), { credentials: 'include' });
    if (!resp.ok) return false;
    const data = (await resp.json()) as { busy?: boolean; error?: string };
    return typeof data.busy === 'boolean' && !data.error;
  } catch {
    return false;
  }
}

/** Force-release stuck generation lock (multi-worker safe via repair API). */
export async function forceUnlockChatGenLock(): Promise<{ ok: boolean; busy: boolean | null }> {
  try {
    const resp = await fetch(sseUrl('/api/repair/unlock-gen'), {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
    });
    if (!resp.ok) return { ok: false, busy: null };
    const data = (await resp.json()) as { ok?: boolean; lock?: { busy?: boolean | null } };
    return { ok: data.ok === true, busy: data.lock?.busy ?? null };
  } catch {
    return { ok: false, busy: null };
  }
}
