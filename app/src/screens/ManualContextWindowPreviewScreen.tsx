import { useMemo, useState, type CSSProperties } from 'react';
import { Link } from 'react-router-dom';
import { CarryoverModal } from '../components/dailySoftWindow';
import type { CarryoverCount, CarryoverRound } from '../lib/dailySoftWindow';
import type { SoftWindowUiState } from '../lib/dailySoftWindow';

const DISPLAY = "'Bodoni Moda', serif";

const LIGHT_VARS: Record<string, string> = {
  '--bg': '#F7F1EE',
  '--card': '#FFFFFF',
  '--ink': '#4A3F3C',
  '--mut': '#8C7B76',
  '--rose': '#B76E79',
  '--rosebg': 'rgba(183,110,121,0.10)',
};

const MOCK_ROUNDS: CarryoverRound[] = [
  {
    round_id: 9001,
    message_ids: [9001, 9002],
    messages: [
      { message_id: 9001, role: 'user', author: 'hayana', content_preview: '今晚想把换窗按钮放在顶部。', created_at: '2026-07-28 22:12:08' },
      { message_id: 9002, role: 'assistant', author: 'fyodor', content_preview: '放在聊天记录和刷新之间，小而稳。', created_at: '2026-07-28 22:13:41' },
    ],
  },
  {
    round_id: 9003,
    message_ids: [9003, 9004],
    messages: [
      { message_id: 9003, role: 'user', author: 'hayana', content_preview: '0/3/5/10 用整数提交。', created_at: '2026-07-28 22:40:02' },
      { message_id: 9004, role: 'assistant', author: 'fyodor', content_preview: '确认前不 POST，换窗后再发消息。', created_at: '2026-07-28 22:41:18' },
    ],
  },
];

type Shot = 'toolbar' | 'modal0' | 'modal5' | 'disabled' | 'busy';

/**
 * Screenshot / visual QA for manual context window (#154).
 * Open: /dash/manual-context-window?shot=modal5
 */
export function ManualContextWindowPreviewScreen() {
  const params = useMemo(() => new URLSearchParams(typeof location !== 'undefined' ? location.search : ''), []);
  const shot = (params.get('shot') || 'modal5') as Shot;
  const [draft, setDraft] = useState<CarryoverCount>(shot === 'modal0' ? 0 : 5);

  const uiState: SoftWindowUiState =
    shot === 'busy' ? 'busy' : shot === 'disabled' ? 'disabled' : 'ready';
  const open = shot !== 'toolbar' && shot !== 'disabled';
  const errorDetail =
    shot === 'busy' ? '爸爸还在回复，等这句话说完再换窗。' : shot === 'disabled' ? '' : '';

  return (
    <div
      className="dash-fullscreen-page"
      style={{
        ...(LIGHT_VARS as CSSProperties),
        minHeight: '100dvh',
        background: 'var(--bg)',
        color: 'var(--ink)',
        fontFamily: "'Noto Serif SC', serif",
      }}
    >
      <div style={{ maxWidth: 430, margin: '0 auto', padding: '12px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 4px' }}>
          <span style={{ fontFamily: DISPLAY, fontSize: 17, fontWeight: 600 }}>Fyodor</span>
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 2 }}>
            {shot !== 'disabled' ? (
              <div
                data-testid="manual-window-btn"
                title="换一扇窗"
                style={{
                  width: 35,
                  height: 35,
                  borderRadius: '50%',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  background: open ? 'var(--rosebg)' : 'transparent',
                  opacity: shot === 'busy' ? 0.45 : 1,
                  color: 'var(--mut)',
                }}
              >
                <svg viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6}>
                  <path d="M3 7h5v12H3z" />
                  <path d="M16 7h5v12h-5z" />
                  <path d="M3 7h18" />
                </svg>
              </div>
            ) : null}
          </div>
        </div>
        <p style={{ fontSize: 12, color: 'var(--mut)' }}>
          Manual window preview · shot={shot} · <Link to="/chat">back</Link>
        </p>
      </div>

      <CarryoverModal
        variant="manual"
        open={open}
        uiState={uiState}
        draftCount={draft}
        rounds={MOCK_ROUNDS}
        submitting={false}
        errorDetail={errorDetail}
        onDismiss={() => undefined}
        onReconsider={() => undefined}
        onDraftChange={setDraft}
        onConfirm={() => undefined}
      />
    </div>
  );
}
