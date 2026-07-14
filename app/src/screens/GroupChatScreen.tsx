import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  clearGroupRoom,
  getGroupMessages,
  getGroupStatus,
  sendGroupMessage,
  streamGroupReply,
  type AgentStatus,
  type GroupAgent,
  type GroupMessage,
  type GroupRoom,
  type GroupStatus,
} from '../lib/groupChat';
import './GroupChatScreen.css';

const ROOMS: { id: GroupRoom; label: string; hint: string }[] = [
  { id: 'group', label: '一起', hint: '三个人的房间' },
  { id: 'claude', label: '暖色', hint: '单独聊天' },
  { id: 'codex', label: '蓝色', hint: '单独聊天' },
];

const EMPTY_STATUS: GroupStatus = {
  agents: {
    claude: { ready: false, color: 'sage', detail: '检查中…' },
    codex: { ready: false, color: 'blue', detail: '检查中…' },
  },
};

function timeLabel(value: string) {
  const parsed = new Date(value.replace(' ', 'T') + '+08:00');
  if (Number.isNaN(parsed.getTime())) return '';
  return parsed.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
}

function BubbleText({ text }: { text: string }) {
  const pieces = text.trim().split(/\n\s*\n/).filter(Boolean);
  return <>{pieces.map((piece, index) => <p key={`${index}-${piece.slice(0, 12)}`}>{piece}</p>)}</>;
}

function StatusDot({ agent, status }: { agent: GroupAgent; status: AgentStatus }) {
  return (
    <span className={`gc-status gc-${agent}${status.ready ? ' is-ready' : ''}`} title={status.detail}>
      <i />
      <span>{status.ready ? '可用' : agent === 'codex' ? '待接入' : '离线'}</span>
    </span>
  );
}

export function GroupChatScreen() {
  const navigate = useNavigate();
  const [room, setRoom] = useState<GroupRoom>('group');
  const [messages, setMessages] = useState<GroupMessage[]>([]);
  const [status, setStatus] = useState<GroupStatus>(EMPTY_STATUS);
  const [draft, setDraft] = useState('');
  const [streaming, setStreaming] = useState<Partial<Record<GroupAgent, string>>>({});
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState('');
  const [confirmClear, setConfirmClear] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const controllerRef = useRef<AbortController | null>(null);

  const scrollDown = useCallback((behavior: ScrollBehavior = 'smooth') => {
    requestAnimationFrame(() => bottomRef.current?.scrollIntoView({ behavior }));
  }, []);

  const loadRoom = useCallback(async (nextRoom: GroupRoom) => {
    setLoading(true);
    setNotice('');
    try {
      const result = await getGroupMessages(nextRoom);
      setMessages(result.messages);
      scrollDown('auto');
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '消息加载失败');
    } finally {
      setLoading(false);
    }
  }, [scrollDown]);

  useEffect(() => {
    void loadRoom(room);
    setConfirmClear(false);
  }, [loadRoom, room]);

  useEffect(() => {
    getGroupStatus().then(setStatus).catch(() => {
      setNotice('线路状态暂时取不到，仍可以稍后重试');
    });
    return () => controllerRef.current?.abort();
  }, []);

  useEffect(() => scrollDown(), [messages, scrollDown, streaming]);

  const runReply = async (messageId: number | null, targets?: GroupAgent[]) => {
    if (busy) return;
    setBusy(true);
    setNotice('');
    setStreaming({});
    const controller = new AbortController();
    controllerRef.current = controller;
    const result = await streamGroupReply(room, messageId, targets, {
      onStart: (agent) => setStreaming((current) => ({ ...current, [agent]: '' })),
      onText: (agent, text) => setStreaming((current) => ({
        ...current,
        [agent]: (current[agent] || '') + text,
      })),
      onDone: (agent, message) => {
        setMessages((current) => current.some((item) => item.id === message.id)
          ? current
          : [...current, message]);
        setStreaming((current) => {
          const next = { ...current };
          delete next[agent];
          return next;
        });
      },
      onStatus: (agent, ready, detail) => {
        setStatus((current) => ({
          agents: { ...current.agents, [agent]: { ...current.agents[agent], ready, detail } },
        }));
        if (!ready) setNotice(detail);
      },
      onAgentError: (agent, detail) => {
        setStreaming((current) => {
          const next = { ...current };
          delete next[agent];
          return next;
        });
        setNotice(`${agent === 'claude' ? '暖色' : '蓝色'}线路：${detail}`);
      },
    }, controller);
    if (!result.ok && result.error) setNotice(result.error);
    setStreaming({});
    setBusy(false);
    controllerRef.current = null;
  };

  const submit = async () => {
    const content = draft.trim();
    if (!content || busy) return;
    setDraft('');
    setNotice('');
    try {
      const result = await sendGroupMessage(room, content);
      setMessages((current) => [...current, result.message]);
      await runReply(result.message.id);
    } catch (error) {
      setDraft(content);
      setNotice(error instanceof Error ? error.message : '发送失败');
    }
  };

  const chooseRoom = (nextRoom: GroupRoom) => {
    if (busy || nextRoom === room) return;
    setMessages([]);
    setRoom(nextRoom);
  };

  const doClear = async () => {
    if (!confirmClear) {
      setConfirmClear(true);
      window.setTimeout(() => setConfirmClear(false), 3500);
      return;
    }
    try {
      await clearGroupRoom(room);
      setMessages([]);
      setConfirmClear(false);
      setNotice('这个房间已经清空');
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '清空失败');
    }
  };

  const canContinue = messages.length > 0 && !busy;

  return (
    <main className="gc-page">
      <header className="gc-header">
        <button type="button" className="gc-icon-button" onClick={() => navigate('/contacts')} aria-label="返回通讯录">‹</button>
        <div className="gc-title">
          <strong>同一个人，两种颜色</strong>
          <span><StatusDot agent="claude" status={status.agents.claude} /><StatusDot agent="codex" status={status.agents.codex} /></span>
        </div>
        <button type="button" className={`gc-clear${confirmClear ? ' is-confirming' : ''}`} onClick={() => void doClear()} disabled={busy}>
          {confirmClear ? '再点一次' : '清空'}
        </button>
      </header>

      <nav className="gc-room-tabs" aria-label="聊天室">
        {ROOMS.map((item) => (
          <button key={item.id} type="button" className={room === item.id ? 'is-active' : ''} onClick={() => chooseRoom(item.id)} disabled={busy}>
            <span>{item.label}</span><small>{item.hint}</small>
          </button>
        ))}
      </nav>

      {notice && <button type="button" className="gc-notice" onClick={() => setNotice('')}>{notice}<span>×</span></button>}

      <section className="gc-timeline" aria-live="polite">
        {loading && <div className="gc-empty">正在推开房门…</div>}
        {!loading && messages.length === 0 && !Object.keys(streaming).length && (
          <div className="gc-empty">
            <div className="gc-empty-orbits"><i /><i /></div>
            <strong>{room === 'group' ? '把想说的话放在中间' : '这里只属于你们两个'}</strong>
            <span>{room === 'codex' ? '蓝色线路还在等接入，消息会先好好留在这里。' : '每个房间都有自己的聊天记录。'}</span>
          </div>
        )}
        {messages.map((message) => (
          <article key={message.id} className={`gc-message gc-${message.author}`}>
            <div className="gc-bubble"><BubbleText text={message.content} /></div>
            <time>{timeLabel(message.created_at)}</time>
          </article>
        ))}
        {(['claude', 'codex'] as GroupAgent[]).map((agent) => streaming[agent] !== undefined && (
          <article key={`stream-${agent}`} className={`gc-message gc-${agent} is-streaming`}>
            <div className="gc-bubble">
              {streaming[agent] ? <BubbleText text={streaming[agent] || ''} /> : <span className="gc-typing"><i /><i /><i /></span>}
            </div>
          </article>
        ))}
        <div ref={bottomRef} />
      </section>

      <footer className="gc-composer-wrap">
        {room === 'group' && (
          <div className="gc-actions">
            <button type="button" className="gc-claude" onClick={() => void runReply(null, ['claude'])} disabled={!canContinue || !status.agents.claude.ready}>暖色说一句</button>
            <button type="button" className="gc-codex" onClick={() => void runReply(null, ['codex'])} disabled={!canContinue || !status.agents.codex.ready}>蓝色说一句</button>
            <button type="button" onClick={() => void runReply(null, ['claude', 'codex'])} disabled={!canContinue}>让他们聊</button>
          </div>
        )}
        <div className="gc-composer">
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                void submit();
              }
            }}
            placeholder={room === 'group' ? '说给两种颜色听…' : room === 'claude' ? '只说给暖色听…' : '蓝色接好后会在这里回你…'}
            rows={1}
            disabled={busy}
          />
          <button type="button" onClick={() => void submit()} disabled={!draft.trim() || busy} aria-label="发送">
            {busy ? <i className="gc-spinner" /> : '↑'}
          </button>
        </div>
        <small className="gc-footnote">同一份人设 · 三条独立时间线</small>
      </footer>
    </main>
  );
}
