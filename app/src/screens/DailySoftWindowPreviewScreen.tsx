import { useMemo, useState, type CSSProperties } from 'react';
import { Link } from 'react-router-dom';
import { CarryoverModal, CarryoverPickerCard, DaySoftBoundary } from '../components/dailySoftWindow';
import { useDailySoftWindow } from '../hooks/useDailySoftWindow';
import {
  chatDayKeyFromLocalTs,
  getMockScenario,
  mockPreviewTranscript,
  setMockScenario,
  type SoftWindowMockScenario,
} from '../lib/dailySoftWindow';

const SERIF = "'Noto Serif SC', serif";
const DISPLAY = "'Bodoni Moda', serif";

const LIGHT_VARS: Record<string, string> = {
  '--bg': '#F7F1EE',
  '--card': '#FFFFFF',
  '--card2': '#F6EFEC',
  '--ink': '#4A3F3C',
  '--ink2': '#6B5A55',
  '--mut': '#8C7B76',
  '--faint': '#A99590',
  '--ghost': '#C4B4AF',
  '--line': '#F0E6E2',
  '--rose': '#B76E79',
  '--deep': '#9C3B4A',
  '--rosebg': 'rgba(183,110,121,0.10)',
  '--shadow': 'rgba(183,110,121,0.10)',
  '--shadow2': 'rgba(183,110,121,0.20)',
};

const SCENARIOS: { id: SoftWindowMockScenario; label: string }[] = [
  { id: 'ready', label: 'ready' },
  { id: 'loading', label: 'loading' },
  { id: 'empty', label: 'empty' },
  { id: 'disabled', label: '404' },
  { id: 'conflict', label: '409' },
  { id: 'locked', label: 'locked' },
  { id: 'error', label: 'error' },
];

/**
 * Isolated FE-R0 playground. Does not touch formal chat defaults.
 * Open: /dash/daily-soft-window
 */
export function DailySoftWindowPreviewScreen() {
  const [scenario, setScenario] = useState<SoftWindowMockScenario>(() => getMockScenario());
  const dsw = useDailySoftWindow({ enabled: true, forceMock: true });
  const transcript = useMemo(() => mockPreviewTranscript(), []);

  const applyScenario = (next: SoftWindowMockScenario) => {
    setScenario(next);
    setMockScenario(next);
    // Force mock store rebuild + reload.
    window.location.search = `?mockScenario=${encodeURIComponent(next)}`;
  };

  return (
    <div
      className="dash-fullscreen-page"
      style={{
        ...(LIGHT_VARS as CSSProperties),
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--bg)',
        color: 'var(--ink)',
        fontFamily: SERIF,
      }}
    >
      <header
        style={{
          flexShrink: 0,
          padding: '14px 16px 10px',
          borderBottom: '1px solid var(--line)',
          background: 'rgba(247,241,238,0.96)',
          backdropFilter: 'blur(10px)',
        }}
      >
        <div style={{ maxWidth: 430, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <Link to="/chat" style={{ textDecoration: 'none', color: 'var(--mut)', fontSize: 18 }}>
              ‹
            </Link>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontFamily: DISPLAY, fontSize: 11, letterSpacing: 2, color: 'var(--ghost)' }}>
                FE-R0 · MOCK · 弹窗
              </div>
              <div style={{ fontFamily: SERIF, fontSize: 16, fontWeight: 600, letterSpacing: 1 }}>小猫的行李箱</div>
            </div>
          </div>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {SCENARIOS.map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={() => applyScenario(s.id)}
                style={{
                  border: 'none',
                  borderRadius: 999,
                  padding: '5px 10px',
                  fontSize: 11,
                  fontFamily: DISPLAY,
                  letterSpacing: 0.5,
                  cursor: 'pointer',
                  background: scenario === s.id ? 'var(--rose)' : 'var(--card2)',
                  color: scenario === s.id ? '#fff9f7' : 'var(--mut)',
                }}
              >
                {s.label}
              </button>
            ))}
          </div>
        </div>
      </header>

      <div className="hide-scrollbar" style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
        <div style={{ maxWidth: 430, margin: '0 auto', padding: '18px 16px 24px', display: 'flex', flexDirection: 'column', gap: 14 }}>
          {transcript.map((m, idx) => {
            const prev = transcript[idx - 1];
            const showBoundary = !prev || prev.chatDay !== m.chatDay;
            const highlight = dsw.highlightIds.has(m.id) && dsw.draftCount > 0;
            return (
              <div key={m.id}>
                {showBoundary && idx > 0 ? <DaySoftBoundary /> : null}
                {showBoundary && idx === 0 ? (
                  <div
                    style={{
                      textAlign: 'center',
                      fontFamily: DISPLAY,
                      fontSize: 12,
                      letterSpacing: 2,
                      color: 'var(--ghost)',
                      padding: '2px 0 8px',
                    }}
                  >
                    {chatDayKeyFromLocalTs(m.created_at).replace(/-/g, '.')}
                  </div>
                ) : null}
                <div
                  className={highlight ? 'dsw-msg-highlight' : undefined}
                  style={{
                    display: 'flex',
                    justifyContent: m.role === 'user' ? 'flex-end' : 'flex-start',
                  }}
                >
                  <div
                    style={{
                      maxWidth: '86%',
                      padding: m.role === 'user' ? '10px 14px' : '2px 0',
                      borderRadius: m.role === 'user' ? '16px 16px 4px 16px' : 0,
                      background: m.role === 'user' ? '#E8C9A0' : 'transparent',
                      color: m.role === 'user' ? '#412402' : 'var(--ink)',
                      fontSize: 14.5,
                      lineHeight: 1.7,
                    }}
                  >
                    {m.text}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div style={{ flexShrink: 0, padding: '8px 12px 16px', background: 'rgba(247,241,238,0.96)' }}>
        <div style={{ maxWidth: 430, margin: '0 auto' }}>
          {dsw.showPickerCard ? (
            <CarryoverPickerCard
              locked={dsw.locked}
              carryoverCount={dsw.summary?.carryover_count ?? 0}
              loading={dsw.uiState === 'loading'}
              statusText={dsw.statusText}
              onOpen={dsw.openDrawer}
            />
          ) : null}
          <div
            style={{
              background: 'var(--card)',
              borderRadius: 26,
              boxShadow: '0 14px 40px var(--shadow2)',
              padding: '12px 12px 10px',
            }}
          >
            <div style={{ fontSize: 14, color: 'var(--ghost)', padding: '4px 8px 10px', fontFamily: SERIF }}>
              和岛聊聊…（预览页不发送；点发送语义 = 自动锁 0）
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
              <button
                type="button"
                onClick={() => void dsw.lockZeroIfNeeded()}
                style={{
                  border: 'none',
                  width: 42,
                  height: 42,
                  borderRadius: '50%',
                  background: 'var(--deep)',
                  color: '#FBF3F0',
                  cursor: 'pointer',
                  boxShadow: '0 8px 20px var(--shadow2)',
                }}
                aria-label="模拟发送并锁 0"
              >
                ↑
              </button>
            </div>
          </div>
        </div>
      </div>

      <CarryoverModal
        open={dsw.drawerOpen}
        uiState={dsw.uiState}
        draftCount={dsw.draftCount}
        candidates={dsw.candidates}
        submitting={dsw.submitting}
        errorDetail={dsw.errorDetail}
        onClose={dsw.closeDrawer}
        onDraftChange={dsw.setDraftCount}
        onConfirm={() => {
          void dsw.confirmSelection();
        }}
      />
    </div>
  );
}
