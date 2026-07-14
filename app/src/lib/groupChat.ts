import { http, sseUrl } from './http';

export type GroupRoom = 'group' | 'claude' | 'codex';
export type GroupAgent = 'claude' | 'codex';
export type GroupAuthor = 'user' | GroupAgent | 'system';

export interface GroupMessage {
  id: number;
  room: GroupRoom;
  author: GroupAuthor;
  content: string;
  thinking: string;
  meta: string;
  created_at: string;
  file_url?: string;
  file_name?: string;
}

export interface AgentStatus {
  ready: boolean;
  installed?: boolean;
  color: string;
  detail: string;
}

export interface GroupStatus {
  agents: Record<GroupAgent, AgentStatus>;
}

export function enrichGroupMessage(message: GroupMessage): GroupMessage {
  if (!message.meta) return message;
  try {
    const parsed = JSON.parse(message.meta) as { file_url?: string; file_name?: string };
    if (parsed.file_url) {
      return { ...message, file_url: parsed.file_url, file_name: parsed.file_name || '' };
    }
  } catch { /* non-JSON meta */ }
  return message;
}

export const getGroupStatus = () =>
  http.get<GroupStatus>('/api/group-chat/status');

export const getGroupMessages = (room: GroupRoom) =>
  http.get<{ messages: GroupMessage[]; has_more_before: boolean }>(
    '/api/group-chat/messages',
    { room, limit: 120 },
  ).then((result) => ({
    ...result,
    messages: result.messages.map(enrichGroupMessage),
  }));

export const sendGroupMessage = (
  room: GroupRoom,
  content: string,
  extra?: { fileUrl?: string; fileName?: string },
) =>
  http.post<{ ok: boolean; message: GroupMessage }>('/api/group-chat/send', {
    room,
    content,
    file_url: extra?.fileUrl || '',
    file_name: extra?.fileName || '',
  }).then((result) => ({
    ...result,
    message: enrichGroupMessage(result.message),
  }));

export const clearGroupRoom = (room: GroupRoom) =>
  http.post<{ ok: boolean; deleted: number }>('/api/group-chat/clear', {
    room,
    confirm: true,
  });

interface GroupStreamEvent {
  t?: string;
  agent?: GroupAgent;
  d?: unknown;
  ok?: boolean;
  ready?: boolean;
  detail?: string;
  message?: GroupMessage;
}

export interface GroupStreamHandlers {
  onStart: (agent: GroupAgent) => void;
  onText: (agent: GroupAgent, text: string) => void;
  onDone: (agent: GroupAgent, message: GroupMessage) => void;
  onStatus: (agent: GroupAgent, ready: boolean, detail: string) => void;
  onAgentError: (agent: GroupAgent, detail: string) => void;
}

export async function streamGroupReply(
  room: GroupRoom,
  userMessageId: number | null,
  targets: GroupAgent[] | undefined,
  handlers: GroupStreamHandlers,
  controller: AbortController,
): Promise<{ ok: boolean; error?: string }> {
  let safety: ReturnType<typeof setTimeout> | undefined;
  const armSafety = () => {
    clearTimeout(safety);
    safety = setTimeout(() => controller.abort(), 180_000);
  };
  armSafety();
  try {
    const response = await fetch(sseUrl('/api/gw/group-chat/stream'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        room,
        ...(userMessageId ? { user_message_id: userMessageId } : {}),
        ...(targets ? { targets } : {}),
      }),
      signal: controller.signal,
    });
    if (!response.ok || !response.body) {
      let detail = '';
      try {
        const payload = await response.json() as { error?: string };
        detail = payload.error || '';
      } catch { /* non-JSON response */ }
      return { ok: false, error: detail || `HTTP ${response.status}` };
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let result: { ok: boolean; error?: string } | null = null;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
      let separator = buffer.indexOf('\n\n');
      while (separator >= 0) {
        const frame = buffer.slice(0, separator);
        buffer = buffer.slice(separator + 2);
        separator = buffer.indexOf('\n\n');
        const line = frame.split('\n').find((part) => part.startsWith('data: '));
        if (!line) continue;
        let event: GroupStreamEvent;
        try { event = JSON.parse(line.slice(6)) as GroupStreamEvent; } catch { continue; }
        armSafety();
        const agent = event.agent;
        if (event.t === 'agent_start' && agent) handlers.onStart(agent);
        if (event.t === 'text' && agent) handlers.onText(agent, String(event.d ?? ''));
        if (event.t === 'agent_done' && agent && event.message) handlers.onDone(agent, event.message);
        if (event.t === 'agent_status' && agent) {
          handlers.onStatus(agent, Boolean(event.ready), event.detail || '暂不可用');
        }
        if (event.t === 'agent_error' && agent) {
          handlers.onAgentError(agent, String(event.d ?? '回复失败'));
        }
        if (event.t === 'err') result = { ok: false, error: String(event.d ?? '回复失败') };
        if (event.t === 'done') result = { ok: event.ok !== false };
      }
      if (result) break;
    }
    return result ?? { ok: false, error: '回复流意外中断' };
  } catch (error) {
    if (controller.signal.aborted) return { ok: false, error: '连接超时或已中止' };
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  } finally {
    clearTimeout(safety);
  }
}
