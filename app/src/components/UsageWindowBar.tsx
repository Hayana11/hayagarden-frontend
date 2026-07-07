export function UsageWindowBar({
  label,
  pct,
  color,
  hint,
  big,
}: {
  label: string;
  pct: number;
  color: string;
  hint?: string;
  big?: boolean;
}) {
  return (
    <div style={{ marginTop: big ? 16 : 10 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <span style={{ fontSize: big ? 14 : 13, color: 'var(--color-text-soft)' }}>{label}</span>
        <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: big ? 18 : 16, fontWeight: 600 }}>{pct}%</span>
      </div>
      <div
        style={{
          height: big ? 10 : 7,
          borderRadius: 5,
          background: 'var(--color-track)',
          marginTop: big ? 8 : 5,
          overflow: 'hidden',
        }}
      >
        <div style={{ height: '100%', borderRadius: 5, background: color, width: `${pct}%` }} />
      </div>
      {hint && <div style={{ fontSize: 11, color: 'var(--color-text-faint)', marginTop: 4 }}>{hint}</div>}
    </div>
  );
}
