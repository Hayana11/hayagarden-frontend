import { FONT_DISPLAY } from '../lib/typography';
import { useTaskTimerClock, type TaskTimerSnapshot } from '../lib/taskTimer';

interface TaskTimerCardProps {
  snapshot: TaskTimerSnapshot;
  placement: 'floating-desktop' | 'inline-mobile';
  /** Only render the completion pill when a real completion capability is wired up. */
  onComplete?: () => void;
  completing?: boolean;
}

export function TaskTimerCard({ snapshot, placement, onComplete, completing }: TaskTimerCardProps) {
  const display = useTaskTimerClock(snapshot);
  if (!display) return null;

  const { phase, primaryLabel, statusText, progressRatio } = display;
  const accentColor = phase === 'overtime' ? 'var(--deep)' : 'var(--rose)';

  return (
    <div className="task-timer-card" data-placement={placement} data-phase={phase}>
      <div className="task-timer-card-status">
        <span className="task-timer-card-dot" style={{ background: accentColor }} />
        <span>{statusText}</span>
      </div>

      <div key={phase} className="task-timer-card-primary" style={{ fontFamily: FONT_DISPLAY }}>
        {primaryLabel}
      </div>

      {snapshot.title && <div className="task-timer-card-title">{snapshot.title}</div>}

      {progressRatio != null && (
        <div className="task-timer-card-progress-track">
          <div className="task-timer-card-progress-fill" style={{ width: `${progressRatio * 100}%` }} />
        </div>
      )}

      {onComplete && (
        <button
          type="button"
          className="task-timer-card-complete-btn"
          disabled={completing}
          onClick={onComplete}
        >
          {completing ? '处理中…' : '完成'}
        </button>
      )}
    </div>
  );
}
