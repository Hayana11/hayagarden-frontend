import type { ReactNode } from 'react';
import {
  CARRYOVER_COUNTS,
  pickLastNCandidates,
  softWindowErrorMessage,
  type CarryoverCandidate,
  type CarryoverCount,
  type SoftWindowUiState,
} from '../../lib/dailySoftWindow';
import './dailySoftWindow.css';

type Props = {
  open: boolean;
  uiState: SoftWindowUiState;
  draftCount: CarryoverCount;
  candidates: CarryoverCandidate[];
  submitting?: boolean;
  errorDetail?: string;
  /** Optional transparent PNG for the hero art slot (may peek outside). */
  artSrc?: string;
  artAlt?: string;
  /** Counts for the excluded-category chips. Omitted keys render without a count. */
  excludedCounts?: Partial<Record<'提醒' | '工具' | 'thinking', number>>;
  onClose: () => void;
  onDraftChange: (count: CarryoverCount) => void;
  onConfirm: () => void;
};

function whoLabel(role: CarryoverCandidate['role']): string {
  return role === 'user' ? '小猫' : '爸爸';
}

/** Line-art suitcase placeholder until a real transparent PNG is dropped in. */
function HeroArtPlaceholder() {
  return (
    <svg viewBox="0 0 72 68" fill="none" aria-hidden="true">
      <g stroke="#d9a8b8" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <rect x="8" y="24" width="52" height="34" rx="5" />
        <path d="M8 34h52" />
        <path d="M27 24v-5a4 4 0 0 1 4-4h6a4 4 0 0 1 4 4v5" />
        <rect x="30" y="30" width="8" height="8" rx="2" />
        <path d="M16 58v4M52 58v4" opacity="0.6" />
      </g>
    </svg>
  );
}

export function CarryoverModal({
  open,
  uiState,
  draftCount,
  candidates,
  submitting,
  errorDetail,
  artSrc,
  artAlt = '',
  excludedCounts,
  onClose,
  onDraftChange,
  onConfirm,
}: Props) {
  if (!open) return null;

  const preview = pickLastNCandidates(candidates, draftCount);
  const isLocked = uiState === 'locked';
  const canConfirm = !submitting && (uiState === 'ready' || uiState === 'empty');
  const snippetRows =
    draftCount >= 5 && preview.length >= 2
      ? [preview[0], preview[preview.length - 1]]
      : preview;

  let mid: ReactNode;
  if (uiState === 'loading') {
    mid = <div className="daily-window-empty">正在翻找昨天可带走的句子…</div>;
  } else if (uiState === 'disabled' || uiState === 'conflict' || uiState === 'error') {
    mid = (
      <div className="daily-window-empty">
        {errorDetail || softWindowErrorMessage(uiState)}
      </div>
    );
  } else {
    mid = (
      <>
        <section className="daily-window-section">
          <h3 className="daily-window-section-title">带过去几句</h3>
          <div className="daily-window-options" role="radiogroup" aria-label="带过去几句">
            {CARRYOVER_COUNTS.map((n) => {
              const selected = draftCount === n;
              return (
                <button
                  key={n}
                  type="button"
                  className={`daily-window-option${selected ? ' is-selected' : ''}`}
                  aria-pressed={selected}
                  disabled={isLocked}
                  onClick={() => onDraftChange(n)}
                >
                  {n === 0 ? (
                    <span className="daily-window-option-word">不带</span>
                  ) : (
                    <>
                      <span className="daily-window-option-number">{n}</span>
                      <span className="daily-window-option-unit">句</span>
                    </>
                  )}
                </button>
              );
            })}
          </div>
          <p className="daily-window-helper">
            {`全部 ${candidates.length} 句 · 只取正式对话 · 各档相同`}
          </p>
        </section>

        <div className="daily-window-divider" aria-hidden="true">
          <span className="daily-window-divider-mark" />
        </div>

        <section className="daily-window-section">
          <h3 className="daily-window-section-title">会带过去的话</h3>
          {draftCount === 0 ? (
            <div className="daily-window-empty">不带走昨天的话。直接翻到新的一页也可以。</div>
          ) : snippetRows.length === 0 || uiState === 'empty' ? (
            <div className="daily-window-empty">昨天没有可带走的正式对话。</div>
          ) : (
            <div className="daily-window-previews">
              {snippetRows.map((c: CarryoverCandidate, i) => (
                <article key={`${c.message_id}-${i}`} className="daily-window-preview">
                  <strong className="daily-window-preview-speaker">{whoLabel(c.role)}</strong>
                  <span className="daily-window-preview-text">{c.content_preview}</span>
                </article>
              ))}
            </div>
          )}
        </section>

        <section className="daily-window-section">
          <h3 className="daily-window-section-title">不会带过去</h3>
          <div className="daily-window-tags">
            {(['提醒', '工具', 'thinking'] as const).map((label) => {
              const n = excludedCounts?.[label];
              return (
                <span key={label} className="daily-window-tag">
                  {label}
                  {typeof n === 'number' && (
                    <>
                      {' · '}
                      <span className="daily-window-tag-count">{n}</span>
                    </>
                  )}
                </span>
              );
            })}
          </div>
        </section>
      </>
    );
  }

  return (
    <div className="daily-window-overlay" onClick={onClose}>
      <section
        className={`daily-window-dialog${uiState === 'loading' || submitting ? ' is-loading' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-label="翻到新的一页"
        onClick={(e) => e.stopPropagation()}
      >
        <button type="button" className="daily-window-close" onClick={onClose} aria-label="关闭">
          ×
        </button>

        {/* 插画独立于滚动区，才能探出卡片顶边 */}
        <div className={`daily-window-hero-art${artSrc ? '' : ' is-placeholder'}`}>
          {artSrc ? <img src={artSrc} alt={artAlt} /> : <HeroArtPlaceholder />}
        </div>

        <div className="daily-window-scroll">
          <header className="daily-window-hero">
            <div className="daily-window-hero-copy">
              <h2 className="daily-window-title">翻到新的一页</h2>
              <p className="daily-window-subtitle">Packing for the Next Window</p>
            </div>
          </header>

          {mid}
        </div>

        <footer className="daily-window-actions">
          <button type="button" className="daily-window-button daily-window-button--secondary" onClick={onClose}>
            再想想
          </button>
          <button
            type="button"
            className="daily-window-button daily-window-button--primary"
            disabled={!canConfirm || isLocked}
            onClick={onConfirm}
          >
            {submitting ? '翻页中…' : isLocked ? '已锁定' : '确认翻页'}
          </button>
        </footer>
      </section>
    </div>
  );
}

/** @deprecated alias — older imports */
export const CarryoverDrawer = CarryoverModal;
