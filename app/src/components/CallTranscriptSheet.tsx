// Opened from a call's record card in Chat: everything said in that call.
// The call's turns are folded out of the chat page (they stay in the chat
// history the model reads); this sheet reads the call's own lines instead.
// Chrome 78: no flex `gap`, no `inset` shorthand.
import { useEffect, useState } from 'react';
import { displayLineText, hasGloss, subtitleToggleLabel, voiceCall, type CallLine, type VoiceCall } from '../lib/voiceCall';
import { useSubtitleMode } from '../lib/useSubtitleMode';
import { FONT_CN } from '../lib/typography';

type Props = {
  callId: string;
  title: string;
  onClose: () => void;
};

type Loaded = { call: VoiceCall | null; lines: CallLine[] };

export function CallTranscriptSheet({ callId, title, onClose }: Props) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [failed, setFailed] = useState(false);
  const [subtitleMode, toggleSubtitleMode] = useSubtitleMode();

  useEffect(() => {
    let alive = true;
    setLoaded(null);
    setFailed(false);
    voiceCall.load(callId).then(
      (data) => { if (alive) setLoaded({ call: data.call, lines: data.lines || [] }); },
      () => { if (alive) setFailed(true); },
    );
    return () => { alive = false; };
  }, [callId]);

  const lines = loaded?.lines ?? [];
  return (
    <div
      data-testid="call-transcript-sheet"
      onClick={onClose}
      style={{ position: 'fixed', top: 0, right: 0, bottom: 0, left: 0, zIndex: 60, background: 'rgba(0,0,0,0.32)', display: 'flex', alignItems: 'flex-end', justifyContent: 'center' }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{ width: '100%', maxWidth: 560, maxHeight: '78vh', display: 'flex', flexDirection: 'column', background: 'var(--card)', borderRadius: '22px 22px 0 0', boxShadow: '0 -10px 40px var(--shadow2)', fontFamily: FONT_CN }}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '16px 18px 10px' }}>
          <div className="vstack vstack-2">
            <span style={{ fontSize: 16, color: 'var(--ink)' }}>通话记录</span>
            <span style={{ fontSize: 12, color: 'var(--ghost)' }}>{title}</span>
          </div>
          <button
            type="button"
            aria-label="关闭"
            onClick={onClose}
            style={{ border: 'none', background: 'transparent', color: 'var(--faint)', fontSize: 22, lineHeight: 1, cursor: 'pointer', padding: 4 }}
          >
            ×
          </button>
        </div>
        <div className="vstack vstack-8" style={{ overflowY: 'auto', padding: '6px 16px calc(18px + env(safe-area-inset-bottom))' }}>
          {failed && <span style={{ fontSize: 13, color: 'var(--ghost)', textAlign: 'center' }}>没能读到这通电话的内容。</span>}
          {!failed && !loaded && <span style={{ fontSize: 13, color: 'var(--ghost)', textAlign: 'center' }}>加载中…</span>}
          {loaded && lines.length === 0 && (
            <span style={{ fontSize: 13, color: 'var(--ghost)', textAlign: 'center' }}>这通电话没有说话内容。</span>
          )}
          {lines.map((line) => (
            <div
              key={line.seq}
              data-speaker={line.speaker}
              style={{ display: 'flex', justifyContent: line.speaker === 'her' ? 'flex-end' : 'flex-start' }}
            >
              <span
                style={{
                  maxWidth: '82%',
                  padding: '8px 12px',
                  borderRadius: 14,
                  fontSize: 14,
                  lineHeight: 1.6,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  color: 'var(--ink)',
                  background: line.speaker === 'her' ? 'var(--rosebg)' : 'var(--bg)',
                }}
              >
                {displayLineText(line, subtitleMode)}
                {hasGloss(line) ? (
                  <button
                    type="button"
                    onClick={toggleSubtitleMode}
                    style={{ display: 'block', marginTop: 6, padding: 0, border: 'none', background: 'transparent', fontSize: 12, color: 'var(--ghost)', cursor: 'pointer' }}
                  >
                    {subtitleToggleLabel(subtitleMode)}
                  </button>
                ) : null}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
