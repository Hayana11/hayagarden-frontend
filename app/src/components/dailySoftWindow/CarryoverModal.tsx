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
  /** Counts for the excluded-category chips. Defaults match the packing mock. */
  excludedCounts?: Partial<Record<'提醒' | '工具' | 'thinking', number>>;
  onClose: () => void;
  onDraftChange: (count: CarryoverCount) => void;
  onConfirm: () => void;
};

const DEFAULT_EXCLUDED = { 提醒: 7, 工具: 88, thinking: 20 } as const;

function whoLabel(role: CarryoverCandidate['role']): string {
  return role === 'user' ? '小猫' : '爸爸';
}

function tokenHint(count: CarryoverCount): string {
  if (count === 0) return '0';
  return (count * 0.13).toFixed(1);
}

export function CarryoverModal({
  open,
  uiState,
  draftCount,
  candidates,
  submitting,
  errorDetail,
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
  const excluded = { ...DEFAULT_EXCLUDED, ...excludedCounts };

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
          <h3 className="daily-window-section-title">带走轮次</h3>
          <div className="daily-window-options" role="radiogroup" aria-label="带走轮次">
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
                      <span className="daily-window-option-unit">轮</span>
                    </>
                  )}
                </button>
              );
            })}
          </div>
          <p className="daily-window-helper">
            {uiState === 'empty'
              ? '昨天没有可带走的正式对话 · 与各档相同 · 0 tokens'
              : `全部 ${candidates.length} 轮 · 与各档相同 · 约${tokenHint(draftCount)}k tokens`}
          </p>
        </section>

        <div className="daily-window-divider" aria-hidden="true">
          <span className="daily-window-divider-mark" />
        </div>

        <section className="daily-window-section">
          <h3 className="daily-window-section-title">对话首尾片段</h3>
          {draftCount === 0 ? (
            <div className="daily-window-empty">不带走昨天的话。直接开始新的一天也可以。</div>
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
          <h3 className="daily-window-section-title">不会装进行李箱</h3>
          <div className="daily-window-tags">
            {(['提醒', '工具', 'thinking'] as const).map((label) => (
              <span key={label} className="daily-window-tag">
                {label}
                {' · '}
                <span className="daily-window-tag-count">{excluded[label]}</span>
              </span>
            ))}
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
        aria-label="新的一天"
        onClick={(e) => e.stopPropagation()}
      >
        <button type="button" className="daily-window-close" onClick={onClose} aria-label="关闭">
          ×
        </button>

        <div className="daily-window-scroll">
          <header className="daily-window-hero">
            <div className="daily-window-hero-copy">
              <h2 className="daily-window-title">新的一天</h2>
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
            {submitting ? '换窗中…' : isLocked ? '已锁定' : '确认换窗'}
          </button>
        </footer>
      </section>
    </div>
  );
}

/** @deprecated alias — older imports */
export const CarryoverDrawer = CarryoverModal;
