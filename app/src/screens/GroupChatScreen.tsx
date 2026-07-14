import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { uploadChatFile } from '../lib/api';
import {
  clearGroupRoom,
  getGroupMessages,
  getGroupStatus,
  sendGroupMessage,
  streamGroupReply,
  type AgentStatus,
  type GroupAgent,
  type GroupMessage,
  type GroupStatus,
} from '../lib/groupChat';
import './GroupChatScreen.css';

const ROOM = 'group' as const;

const EMPTY_STATUS: GroupStatus = {
  agents: {
    claude: { ready: false, color: 'sage', detail: '检查中…' },
    codex: { ready: false, color: 'blue', detail: '检查中…' },
  },
};

const AGENT_LABEL: Record<GroupAgent, string> = {
  claude: 'claude',
  codex: 'gpt',
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
      <span>{status.ready ? AGENT_LABEL[agent] : agent === 'codex' ? '待接入' : '离线'}</span>
    </span>
  );
}

function MessageBubble({ message }: { message: GroupMessage }) {
  const showText = message.content && !message.content.startsWith('[文件:');
  return (
    <div className="gc-bubble">
      {message.file_url && (
        <a className="gc-file" href={message.file_url} target="_blank" rel="noreferrer">
          <span className="gc-file-icon">📎</span>
          <span>{message.file_name || '附件'}</span>
        </a>
      )}
      {showText && <BubbleText text={message.content} />}
    </div>
  );
}

export function GroupChatScreen() {
  const navigate = useNavigate();
  const [messages, setMessages] = useState<GroupMessage[]>([]);
  const [status, setStatus] = useState<GroupStatus>(EMPTY_STATUS);
  const [draft, setDraft] = useState('');
  const [pendingFile, setPendingFile] = useState<{ fileUrl: string; fileName: string } | null>(null);
  const [streaming, setStreaming] = useState<Partial<Record<GroupAgent, string>>>({});
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState('');
  const [confirmClear, setConfirmClear] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const controllerRef = useRef<AbortController | null>(null);

  const scrollDown = useCallback((behavior: ScrollBehavior = 'smooth') => {
    requestAnimationFrame(() => bottomRef.current?.scrollIntoView({ behavior }));
  }, []);

  const loadRoom = useCallback(async () => {
    setLoading(true);
    setNotice('');
    try {
      const result = await getGroupMessages(ROOM);
      setMessages(result.messages);
      scrollDown('auto');
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '消息加载失败');
    } finally {
      setLoading(false);
    }
  }, [scrollDown]);

  useEffect(() => {
    void loadRoom();
    setConfirmClear(false);
  }, [loadRoom]);

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
    const result = await streamGroupReply(ROOM, messageId, targets, {
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
        setNotice(`${AGENT_LABEL[agent]}：${detail}`);
      },
    }, controller);
    if (!result.ok && result.error) setNotice(result.error);
    setStreaming({});
    setBusy(false);
    controllerRef.current = null;
  };

  const onAttachFile = useCallback(async (file: File | undefined) => {
    if (!file) return;
    const up = await uploadChatFile(file);
    if (up) {
      setPendingFile(up);
    } else {
      setNotice('上传失败（只收 2MB 内文本类文件）');
    }
  }, []);

  const submit = async () => {
    const content = draft.trim();
    if ((!content && !pendingFile) || busy) return;
    const file = pendingFile;
    setDraft('');
    setPendingFile(null);
    setNotice('');
    try {
      const result = await sendGroupMessage(ROOM, content, file ? { fileUrl: file.fileUrl, fileName: file.fileName } : undefined);
      setMessages((current) => [...current, result.message]);
      await runReply(result.message.id);
    } catch (error) {
      setDraft(content);
      setPendingFile(file);
      setNotice(error instanceof Error ? error.message : '发送失败');
    }
  };

  const doClear = async () => {
    if (!confirmClear) {
      setConfirmClear(true);
      window.setTimeout(() => setConfirmClear(false), 3500);
      return;
    }
    try {
      await clearGroupRoom(ROOM);
      setMessages([]);
      setConfirmClear(false);
      setNotice('群聊已经清空');
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '清空失败');
    }
  };

  const canContinue = messages.length > 0 && !busy;
  const canSend = Boolean(draft.trim() || pendingFile) && !busy;

  return (
    <main className="gc-page">
      <header className="gc-header">
        <button type="button" className="gc-icon-button" onClick={() => navigate('/contacts')} aria-label="返回通讯录">‹</button>
        <div className="gc-title">
          <strong>群聊</strong>
          <span><StatusDot agent="claude" status={status.agents.claude} /><StatusDot agent="codex" status={status.agents.codex} /></span>
        </div>
        <button type="button" className={`gc-clear${confirmClear ? ' is-confirming' : ''}`} onClick={() => void doClear()} disabled={busy}>
          {confirmClear ? '再点一次' : '清空'}
        </button>
      </header>

      {notice && <button type="button" className="gc-notice" onClick={() => setNotice('')}>{notice}<span>×</span></button>}

      <section className="gc-timeline hide-scrollbar" aria-live="polite">
        {loading && <div className="gc-empty">正在推开房门…</div>}
        {!loading && messages.length === 0 && !Object.keys(streaming).length && (
          <div className="gc-empty">
            <div className="gc-empty-orbits"><i /><i /></div>
            <strong>把想说的话放在中间</strong>
            <span>claude 和 gpt 会在这里一起回你。</span>
          </div>
        )}
        {messages.map((message) => (
          <article key={message.id} className={`gc-message gc-${message.author}`}>
            {(message.author === 'claude' || message.author === 'codex') && (
              <span className="gc-bubble-label">{AGENT_LABEL[message.author]}</span>
            )}
            <MessageBubble message={message} />
            <time>{timeLabel(message.created_at)}</time>
          </article>
        ))}
        {(['claude', 'codex'] as GroupAgent[]).map((agent) => streaming[agent] !== undefined && (
          <article key={`stream-${agent}`} className={`gc-message gc-${agent} is-streaming`}>
            <span className="gc-bubble-label">{AGENT_LABEL[agent]}</span>
            <div className="gc-bubble">
              {streaming[agent] ? <BubbleText text={streaming[agent] || ''} /> : <span className="gc-typing"><i /><i /><i /></span>}
            </div>
          </article>
        ))}
        <div ref={bottomRef} />
      </section>

      <footer className="gc-composer-wrap">
        <div className="gc-actions">
          <button type="button" className="gc-claude" onClick={() => void runReply(null, ['claude'])} disabled={!canContinue || !status.agents.claude.ready}>claude 说一句</button>
          <button type="button" className="gc-codex" onClick={() => void runReply(null, ['codex'])} disabled={!canContinue || !status.agents.codex.ready}>gpt 说一句</button>
          <button type="button" onClick={() => void runReply(null, ['claude', 'codex'])} disabled={!canContinue}>让他们聊</button>
        </div>
        {pendingFile && (
          <div className="gc-pending-file">
            <span>📎 {pendingFile.fileName}</span>
            <button type="button" onClick={() => setPendingFile(null)} aria-label="移除附件">×</button>
          </div>
        )}
        <div className="gc-composer">
          <button type="button" className="gc-attach" onClick={() => fileInputRef.current?.click()} disabled={busy} aria-label="上传文件">＋</button>
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                void submit();
              }
            }}
            placeholder="说给群聊听…"
            rows={1}
            disabled={busy}
          />
          <button type="button" onClick={() => void submit()} disabled={!canSend} aria-label="发送">
            {busy ? <i className="gc-spinner" /> : '↑'}
          </button>
        </div>
        <input ref={fileInputRef} type="file" style={{ display: 'none' }} onChange={(e) => { void onAttachFile(e.target.files?.[0]); e.target.value = ''; }} />
        <small className="gc-footnote">claude · gpt · 同一份人设</small>
      </footer>
    </main>
  );
}
