import { useNavigate } from 'react-router-dom';

export function BackHeader({ title }: { title: string }) {
  const navigate = useNavigate();
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
      <div
        onClick={() => navigate('/')}
        style={{
          cursor: 'pointer',
          width: 38,
          height: 38,
          borderRadius: '50%',
          background: '#FFFFFF',
          boxShadow: 'var(--shadow-fab)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: 20,
          color: 'var(--color-text-mute)',
        }}
      >
        ‹
      </div>
      <span style={{ fontSize: 22, fontWeight: 600, letterSpacing: 3 }}>{title}</span>
    </div>
  );
}
