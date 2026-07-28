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
  onClose: () => void;
  onDraftChange: (count: CarryoverCount) => void;
  onConfirm: () => void;
};

function whoLabel(role: CarryoverCandidate['role']): string {
  return role === 'user' ? '哈娅' : 'Fyodor';
}

/** Soft suitcase glyph — cozy mark without the kitten illustration. */
function SuitcaseMark() {
  return (
    <div className="dsw-suit" aria-hidden="true">
      <svg viewBox="0 0 64 64" fill="none">
        <rect x="12" y="22" width="40" height="30" rx="7" fill="#F7F1EE" stroke="#9C3B4A" strokeWidth="2.2" />
        <path d="M24 22V18a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v4" stroke="#9C3B4A" strokeWidth="2.2" strokeLinecap="round" />
        <path d="M12 34h40" stroke="#C4848D" strokeWidth="2" />
        <circle cx="20" cy="37" r="2" fill="#B76E79" />
        <circle cx="44" cy="37" r="2" fill="#B76E79" />
        <rect x="28" y="30" width="8" height="6" rx="2" fill="#B76E79" opacity="0.85" />
        <path d="M18 48h8M38 48h8" stroke="#C4848D" strokeWidth="2" strokeLinecap="round" />
      </svg>
    </div>
  );
}

export function CarryoverModal({
  open,
  uiState,
  draftCount,
  candidates,
  submitting,
  errorDetail,
  onClose,
  onDraftChange,
  onConfirm,
}: Props) {
  if (!open) return null;

  const preview = pickLastNCandidates(candidates, draftCount);
  const isLocked = uiState === 'locked';
  const canConfirm = !submitting && (uiState === 'ready' || uiState === 'empty');
  // Reference-style “首尾片段”: show first + last when picking many; else exact N.
  const snippetRows =
    draftCount >= 5 && preview.length >= 2
      ? [preview[0], preview[preview.length - 1]]
      : preview;

  let body: ReactNode;
  if (uiState === 'loading') {
    body = (
      <div className="dsw-state">
        <strong>翻找中</strong>
        正在取出昨天可带走的句子…
      </div>
    );
  } else if (uiState === 'disabled' || uiState === 'conflict' || uiState === 'error') {
    body = (
      <div className="dsw-state">
        <strong>{uiState === 'disabled' ? '404' : uiState === 'conflict' ? '409' : '出错了'}</strong>
        {errorDetail || softWindowErrorMessage(uiState)}
      </div>
    );
  } else {
    body = (
      <>
        <section className="dsw-section">
          <div className="dsw-section-label">
            <span className="dsw-spark">✦</span>
            带走轮次
          </div>
          <div className="dsw-options" role="radiogroup" aria-label="带走条数">
            {CARRYOVER_COUNTS.map((n) => (
              <button
                key={n}
                type="button"
                className={`dsw-opt${draftCount === n ? ' selected' : ''}`}
                role="radio"
                aria-checked={draftCount === n}
                disabled={isLocked}
                onClick={() => onDraftChange(n)}
              >
                <span className="dsw-opt-num">{n === 0 ? '0' : String(n)}</span>
                <span className="dsw-opt-cap">{n === 0 ? '不带' : '条'}</span>
              </button>
            ))}
          </div>
          <div className="dsw-meta">
            {uiState === 'empty'
              ? '昨天没有可带走的正式对话 · 与存档相同 · 0 tokens'
              : `全部 ${candidates.length} 条 · 与选中相同 · 约${draftCount === 0 ? '0' : (draftCount * 0.26).toFixed(1)}k tokens`}
          </div>
        </section>

        <section className="dsw-section">
          <div className="dsw-section-label">
            <span className="dsw-spark">✦</span>
            对话首尾片段
          </div>
          {draftCount === 0 ? (
            <div className="dsw-state" style={{ padding: '12px 8px' }}>
              不带走昨天的话。直接开始新的一天也可以。
            </div>
          ) : snippetRows.length === 0 ? (
            <div className="dsw-state" style={{ padding: '12px 8px' }}>
              没有足够的候选消息可预览。
            </div>
          ) : (
            <div className="dsw-preview">
              {snippetRows.map((c, i) => (
                <div
                  key={`${c.message_id}-${i}`}
                  className="dsw-preview-row highlight"
                  style={{ animationDelay: `${i * 40}ms` }}
                >
                  <div className="dsw-preview-who">{whoLabel(c.role)}</div>
                  <div className="dsw-preview-text">{c.content_preview}</div>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="dsw-section">
          <div className="dsw-section-label">
            <span className="dsw-spark">✦</span>
            不会装进行李箱
          </div>
          <div className="dsw-exclude">
            <span className="dsw-exclude-tag">提醒 · <b>7</b></span>
            <span className="dsw-exclude-tag">工具 · <b>88</b></span>
            <span className="dsw-exclude-tag">thinking · <b>20</b></span>
          </div>
        </section>
      </>
    );
  }

  return (
    <div className="dsw-ov" role="dialog" aria-modal="true" aria-label="小猫的行李箱">
      <div className="dsw-backdrop" onClick={onClose} />
      <div className="dsw-modal">
        <button type="button" className="dsw-close" onClick={onClose} aria-label="关闭">
          ×
        </button>
        <div className="dsw-modal-hd">
          <SuitcaseMark />
          <div className="dsw-modal-titles">
            <div className="dsw-modal-title">小猫的行李箱</div>
            <div className="dsw-modal-sub">Packing for the Next Window</div>
          </div>
        </div>
        <div className="dsw-body">{body}</div>
        <div className="dsw-foot">
          <button type="button" className="dsw-btn ghost" onClick={onClose}>
            再想一想
          </button>
          <button
            type="button"
            className="dsw-btn primary"
            disabled={!canConfirm || isLocked}
            onClick={onConfirm}
          >
            {submitting ? '装进行李箱…' : isLocked ? '已锁定' : '确认换窗'}
          </button>
        </div>
      </div>
    </div>
  );
}

/** @deprecated alias — older imports */
export const CarryoverDrawer = CarryoverModal;
