import type { ReactNode } from 'react';
import {
  CARRYOVER_COUNTS,
  countLabel,
  pickLastNCandidates,
  softWindowErrorMessage,
  type CarryoverCandidate,
  type CarryoverCount,
  type SoftWindowUiState,
} from '../../lib/dailySoftWindow';
import './dailySoftWindow.css';

type Props = {
  open: boolean;
  wide: boolean;
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

export function CarryoverDrawer({
  open,
  wide,
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
            <span className="dsw-spark">✧</span>
            带走几句
          </div>
          <div className="dsw-options" role="radiogroup" aria-label="带走条数">
            {CARRYOVER_COUNTS.map((n) => (
              <button
                key={n}
                type="button"
                className={`dsw-opt${draftCount === n ? ' selected' : ''}`}
                role="radio"
                aria-checked={draftCount === n}
                disabled={uiState === 'locked'}
                onClick={() => onDraftChange(n)}
              >
                <span className="dsw-opt-num">{n === 0 ? '0' : String(n)}</span>
                <span className="dsw-opt-cap">{countLabel(n)}</span>
              </button>
            ))}
          </div>
          <div className="dsw-meta">
            {uiState === 'empty'
              ? '昨天没有可带走的正式对话 · 可选 0 条继续'
              : `候选 ${candidates.length} 条 · 精确到 message id · 选中将锁进今天`}
          </div>
        </section>

        <section className="dsw-section">
          <div className="dsw-section-label">
            <span className="dsw-spark">✧</span>
            将被带入的原话
          </div>
          {draftCount === 0 ? (
            <div className="dsw-state" style={{ padding: '16px 8px' }}>
              不带走昨天的话。直接开始新的一天也可以。
            </div>
          ) : preview.length === 0 ? (
            <div className="dsw-state" style={{ padding: '16px 8px' }}>
              没有足够的候选消息可预览。
            </div>
          ) : (
            <div className="dsw-preview">
              {preview.map((c, i) => (
                <div
                  key={c.message_id}
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
      </>
    );
  }

  return (
    <div className={`dsw-ov${wide ? ' desktop' : ''}`} role="dialog" aria-modal="true" aria-label="带走昨天的话">
      <div className="dsw-backdrop" onClick={onClose} />
      <div className="dsw-sheet">
        {!wide ? (
          <div className="dsw-handle">
            <i />
          </div>
        ) : null}
        <div className="dsw-sheet-hd">
          <div className="dsw-mark" aria-hidden="true">
            ☾
          </div>
          <div className="dsw-sheet-titles">
            <div className="dsw-sheet-title">新的一天</div>
            <div className="dsw-sheet-sub">Packing for the Next Window</div>
          </div>
          <button type="button" className="dsw-close" onClick={onClose} aria-label="关闭">
            ×
          </button>
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
            {submitting ? '装进行李箱…' : isLocked ? '已锁定' : '确认带走'}
          </button>
        </div>
      </div>
    </div>
  );
}
