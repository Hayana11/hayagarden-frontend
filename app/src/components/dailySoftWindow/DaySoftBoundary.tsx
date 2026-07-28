import './dailySoftWindow.css';

type Props = {
  title?: string;
  subtitle?: string;
};

/** Soft chat-day separator after 04:00 — old bubbles stay; only a light mark appears. */
export function DaySoftBoundary({
  title = '☾ 新的一天',
  subtitle = '昨天的话还留在身后。',
}: Props) {
  return (
    <div className="dsw-boundary" role="separator" aria-label="新的一天">
      <div className="dsw-boundary-line" />
      <div className="dsw-boundary-title">{title}</div>
      <div className="dsw-boundary-sub">{subtitle}</div>
    </div>
  );
}
