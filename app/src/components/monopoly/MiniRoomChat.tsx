import { useEffect, useRef, useState } from 'react';
import { messageTime, providerLabel } from '../../lib/monopolyRoom';
import type { MonopolyMessage } from '../../lib/monopolyTypes';
import { useRoomComposer } from '../../hooks/useRoomComposer';

const AUTHOR = {
  haya: { name: '哈娅', symbol: '♥' }, cc: { name: 'CC', symbol: '✦' }, codex: { name: 'Codex', symbol: '◆' },
} as const;

export function MiniRoomChat({
  messages, streaming, busy, paused, onSend, onSpeak,
}: {
  messages: MonopolyMessage[];
  streaming: Partial<Record<'cc' | 'codex', string>>;
  busy: boolean;
  paused: boolean;
  onSend: (content: string, targets: Array<'cc' | 'codex'>) => Promise<boolean>;
  onSpeak: () => void;
}) {
  const [open, setOpen] = useState(false);
  const composer = useRoomComposer();
  const listRef = useRef<HTMLDivElement>(null);
  const streamKey = `${streaming.cc ?? ''}|${streaming.codex ?? ''}`;

  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [messages.length, streamKey]);

  const send = async () => {
    const content = composer.content.trim();
    if (!content) return;
    if (await onSend(content, composer.targets)) composer.setContent('');
  };

  return (
    <aside className={`mono-chat ${open ? 'open' : ''}`}>
      <div className="mono-chat-panel">
        <header className="mono-chat-header" onClick={() => setOpen((value) => !value)}>
          <i className="mono-chat-handle" />
          <span>ROOM CHAT</span><strong>三人房</strong>
          <button type="button" onClick={(event) => { event.stopPropagation(); onSpeak(); }} disabled={paused}>让他们继续聊</button>
        </header>
        <div className="mono-chat-messages" ref={listRef}>
          {messages.map((message) => {
            if (message.author === 'system') return (
              <div className="mono-system-message" key={message.id}><i /><span>{message.content}</span><i /></div>
            );
            const mine = message.author === 'haya';
            const meta = AUTHOR[message.author];
            return (
              <article key={message.id} className={`mono-chat-message ${message.author} ${mine ? 'mine' : ''}`}>
                {!mine && <span className="mono-chat-avatar">{meta.symbol}</span>}
                <div className="mono-message-body">
                  {!mine && <div className="mono-message-author"><strong>{meta.name}</strong><small>{providerLabel(message.provider_meta)}</small></div>}
                  <p>{message.content}</p>
                </div>
                <time>{messageTime(message.created_at)}</time>
              </article>
            );
          })}
          {(['cc', 'codex'] as const).map((actor) => streaming[actor] !== undefined && (
            <article key={`stream-${actor}`} className={`mono-chat-message ${actor}`}>
              <span className="mono-chat-avatar">{AUTHOR[actor].symbol}</span>
              <div className="mono-message-body">
                <div className="mono-message-author"><strong>{AUTHOR[actor].name}</strong><small>正在输入</small></div>
                <p>{streaming[actor] || <span className="mono-typing"><i /><i /><i /></span>}</p>
              </div>
            </article>
          ))}
        </div>
        <footer className="mono-chat-composer" onClick={(event) => event.stopPropagation()}>
          <button type="button" className="mono-target-button" onClick={composer.cycleTarget} aria-label="切换收件人">{composer.target === 'all' ? '全部' : composer.target.toUpperCase()}</button>
          <input
            value={composer.content}
            onChange={(event) => composer.setContent(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void send(); } }}
            placeholder="说点什么…（单独输入 404 立即停局）"
          />
          <button type="button" className="mono-send-button" disabled={busy || !composer.content.trim()} onClick={() => void send()} aria-label="发送">↑</button>
        </footer>
      </div>
    </aside>
  );
}
