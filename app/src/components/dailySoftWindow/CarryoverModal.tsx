import { useEffect, useRef, type ReactNode } from 'react';
import {
  CARRYOVER_COUNTS,
  countLabel,
  pickLastNRounds,
  roundSnippetMessages,
  softWindowErrorMessage,
  type CarryoverCount,
  type CarryoverMessage,
  type CarryoverRound,
  type SoftWindowUiState,
} from '../../lib/dailySoftWindow';
import './dailySoftWindow.css';

type Props = {
  open: boolean;
  uiState: SoftWindowUiState;
  draftCount: CarryoverCount;
  /** Canonical rounds only — do not group flat candidates for live. */
  rounds: CarryoverRound[];
  submitting?: boolean;
  errorDetail?: string;
  /** Counts for the excluded-category chips. Defaults match the packing mock. */
  excludedCounts?: Partial<Record<'提醒' | '工具' | 'thinking', number>>;
  /** × / overlay dismiss — close only, no POST. */
  onDismiss: () => void;
  /** 再想想 — close only, no POST. */
  onReconsider: () => void;
  onDraftChange: (count: CarryoverCount) => void;
  onConfirm: () => void;
};

const DEFAULT_EXCLUDED = { 提醒: 7, 工具: 88, thinking: 20 } as const;

function whoLabel(role: CarryoverMessage['role']): string {
  return role === 'user' ? '小猫' : '爸爸';
}

/**
 * handoff 的 metaLine 是「与各档相同」——整句不随选中档位变化，
 * 所以估算走全部轮数，不走 draftCount。
 */
function tokenHint(totalRounds: number): string {
  if (totalRounds <= 0) return '0';
  return (totalRounds * 0.13).toFixed(1);
}

export function CarryoverModal({
  open,
  uiState,
  draftCount,
  rounds,
  submitting,
  errorDetail,
  excludedCounts,
  onDismiss,
  onReconsider,
  onDraftChange,
  onConfirm,
}: Props) {
  const dialogRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onDismiss();
        return;
      }
      if (e.key !== 'Tab' || !dialogRef.current) return;
      const focusable = dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    // Focus primary dismiss control lightly
    const closeBtn = dialogRef.current?.querySelector<HTMLElement>('.daily-window-close');
    closeBtn?.focus();
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onDismiss]);

  if (!open) return null;

  const previewRounds = pickLastNRounds(rounds, draftCount);
  const snippetRows = roundSnippetMessages(previewRounds);
  const isLocked = uiState === 'locked';
  const tiersDisabled =
    isLocked || submitting || uiState === 'loading' || uiState === 'probing' || uiState === 'submitting';
  const canConfirm =
    !submitting && (uiState === 'ready' || uiState === 'empty');
  const excluded = { ...DEFAULT_EXCLUDED, ...excludedCounts };

  let mid: ReactNode;
  if (uiState === 'loading' || uiState === 'probing' || uiState === 'submitting') {
    mid = <div className="daily-window-empty">正在翻找昨天可带走的句子…</div>;
  } else if (
    uiState === 'disabled' ||
    uiState === 'conflict' ||
    uiState === 'error' ||
    uiState === 'auth_error' ||
    uiState === 'unavailable' ||
    uiState === 'deferred'
  ) {
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
                  role="radio"
                  className={`daily-window-option${selected ? ' is-selected' : ''}`}
                  aria-checked={selected}
                  disabled={tiersDisabled}
                  onClick={() => onDraftChange(n)}
                >
                  <span className="daily-window-option-label">{countLabel(n)}</span>
                </button>
              );
            })}
          </div>
          <p className="daily-window-helper">
            {uiState === 'empty'
              ? '昨天没有可带走的正式对话 · 与各档相同 · 0 tokens'
              : `全部 ${rounds.length} 轮 · 与各档相同 · 约${tokenHint(rounds.length)}k tokens`}
          </p>
        </section>

        <div className="daily-window-divider" aria-hidden="true">
          <span className="daily-window-divider-mark" />
        </div>

        <section className="daily-window-section daily-window-section--snippets">
          <h3 className="daily-window-section-title">对话首尾片段</h3>
          {draftCount === 0 ? (
            <div className="daily-window-empty">不带走昨天的话。直接开始新的一天也可以。</div>
          ) : snippetRows.length === 0 || uiState === 'empty' ? (
            <div className="daily-window-empty">昨天没有可带走的正式对话。</div>
          ) : (
            <div className="daily-window-previews">
              {snippetRows.map((c: CarryoverMessage, i) => (
                <article key={`${c.message_id}-${i}`} className="daily-window-preview">
                  <strong className="daily-window-preview-speaker">{whoLabel(c.role)}</strong>
                  <span className="daily-window-preview-text">{c.content_preview}</span>
                </article>
              ))}
            </div>
          )}
        </section>

        <section className="daily-window-section daily-window-section--left-behind">
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
    <div className="daily-window-overlay" onClick={onDismiss}>
      <section
        ref={dialogRef}
        className={`daily-window-dialog${uiState === 'loading' || uiState === 'probing' || submitting ? ' is-loading' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-label="新的一天"
        onClick={(e) => e.stopPropagation()}
      >
        <button type="button" className="daily-window-close" onClick={onDismiss} aria-label="关闭">
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
          <button
            type="button"
            className="daily-window-button daily-window-button--secondary"
            onClick={onReconsider}
          >
            再想想
          </button>
          <button
            type="button"
            className="daily-window-button daily-window-button--primary"
            disabled={!canConfirm || isLocked || submitting}
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
