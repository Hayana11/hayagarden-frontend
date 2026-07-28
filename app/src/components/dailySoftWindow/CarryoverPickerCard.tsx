import { CARRYOVER_COUNTS, countLabel, lockedSummaryText, type CarryoverCount } from '../../lib/dailySoftWindow';
import './dailySoftWindow.css';

type Props = {
  locked: boolean;
  /** Locked card uses selected_round_count from server. */
  carryoverCount: number;
  loading?: boolean;
  statusText?: string;
  onOpen: () => void;
};

export function CarryoverPickerCard({
  locked,
  carryoverCount,
  loading,
  statusText,
  onOpen,
}: Props) {
  if (locked) {
    return (
      <div className="dsw-card locked" role="status">
        <div className="dsw-card-kicker">PACKING FOR THE NEXT WINDOW</div>
        <div className="dsw-card-title">新的一天</div>
        <div className="dsw-card-sub">{lockedSummaryText(carryoverCount)}</div>
        {statusText ? <div className="dsw-card-meta">{statusText}</div> : null}
      </div>
    );
  }

  return (
    <div
      className="dsw-card"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onOpen();
        }
      }}
    >
      <div className="dsw-card-kicker">PACKING FOR THE NEXT WINDOW</div>
      <div className="dsw-card-title">新的一天</div>
      <div className="dsw-card-sub">
        {loading
          ? '正在翻看昨天的话…'
          : '要带几句昨天的话，让爸爸接着陪小猫说？'}
      </div>
      <div className="dsw-card-chips" aria-hidden="true">
        {CARRYOVER_COUNTS.map((n: CarryoverCount) => (
          <span key={n} className="dsw-chip">{countLabel(n)}</span>
        ))}
      </div>
      {statusText ? <div className="dsw-card-meta">{statusText}</div> : null}
    </div>
  );
}
