export function GradientBrainBar({ gradient }: { gradient: number }) {
  return (
    <div
      style={{
        position: 'relative',
        height: 10,
        borderRadius: 5,
        background: 'linear-gradient(90deg,#8A7AB5,#C08497,#D9A441)',
      }}
    >
      <div
        style={{
          position: 'absolute',
          top: -3,
          left: `calc(${gradient * 100}% - 8px)`,
          width: 16,
          height: 16,
          borderRadius: '50%',
          background: '#FFFFFF',
          boxShadow: '0 2px 6px rgba(0,0,0,0.18)',
        }}
      />
    </div>
  );
}

export function GradientBrainLabels() {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, marginTop: 6 }}>
      <span style={{ color: 'var(--color-violet)' }}>费佳</span>
      <span style={{ color: 'var(--color-amber)' }}>哈娅</span>
    </div>
  );
}
