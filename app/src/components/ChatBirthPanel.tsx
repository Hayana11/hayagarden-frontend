import { useState } from 'react';
import type { BirthTiming } from '../lib/chat';
import { FONT_DISPLAY, FONT_MONO } from '../lib/typography';
import './ChatBirthPanel.css';

const TURN_KIND_LABELS: Record<string, string> = {
  hot: '热回合',
  cold: '冷启动',
  respawn: '重生',
  capacity_swap: '容量切换',
};

function fmtBirthSeconds(ms: number): string {
  const sec = ms / 1000;
  return sec >= 10 ? `${sec.toFixed(1)}s` : `${sec.toFixed(2)}s`;
}

/** "出生 12.4s · 热回合 · 8 段": each step before the reply's first word, in order. */
export function ChatBirthPanel({ birth, defaultOpen = false }: { birth?: BirthTiming; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  if (!birth || !birth.phases.length) return null;
  const slowest = birth.phases.reduce((max, phase) => (phase.ms > max ? phase.ms : max), 0);
  const kind = birth.turnKind ? TURN_KIND_LABELS[birth.turnKind] || birth.turnKind : '';
  return (
    <div className="chat-birth">
      <button
        type="button"
        className="chat-birth-head"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="chat-birth-mark" aria-hidden="true">◌</span>
        <span className="chat-birth-word">出生</span>
        <span className="chat-birth-total" style={{ fontFamily: FONT_DISPLAY }}>{fmtBirthSeconds(birth.totalMs)}</span>
        <span className="chat-birth-meta">
          {[kind, `${birth.phases.length} 段`].filter(Boolean).join(' · ')}
        </span>
        <span className={`chat-birth-caret${open ? ' is-open' : ''}`} aria-hidden="true">›</span>
      </button>
      {open ? (
        <ol className="chat-birth-rows">
          {birth.phases.map((phase, index) => {
            const isSlowest = phase.ms === slowest && slowest >= 500;
            return (
              <li key={`${phase.key}-${index}`} className={`chat-birth-row${isSlowest ? ' is-slowest' : ''}`}>
                <span className="chat-birth-label">
                  {phase.label}
                  {phase.detail ? <span className="chat-birth-detail" style={{ fontFamily: FONT_MONO }}>{phase.detail}</span> : null}
                </span>
                <span className="chat-birth-ms" style={{ fontFamily: FONT_MONO }}>{fmtBirthSeconds(phase.ms)}</span>
              </li>
            );
          })}
        </ol>
      ) : null}
    </div>
  );
}
