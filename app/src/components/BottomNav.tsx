import type { ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

const ACTIVE = 'var(--color-rose)';
const INACTIVE = 'var(--color-nav-inactive)';

function NavItem({
  active,
  label,
  onClick,
  children,
}: {
  active: boolean;
  label: string;
  onClick?: () => void;
  children: ReactNode;
}) {
  return (
    <div
      onClick={onClick}
      style={{
        cursor: onClick ? 'pointer' : 'default',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 4,
        color: active ? ACTIVE : INACTIVE,
      }}
    >
      {children}
      <span style={{ fontFamily: "'Bodoni Moda',serif", fontSize: 12, letterSpacing: 2 }}>{label}</span>
    </div>
  );
}

export function BottomNav() {
  const location = useLocation();
  const navigate = useNavigate();
  const dashActive = location.pathname === '/';
  const readActive = location.pathname === '/reading';

  return (
    <div
      style={{
        position: 'fixed',
        bottom: 0,
        left: '50%',
        transform: 'translateX(-50%)',
        width: '100%',
        maxWidth: 430,
        background: 'rgba(247,241,238,0.92)',
        backdropFilter: 'blur(12px)',
        padding: '12px 30px 22px',
        display: 'flex',
        justifyContent: 'space-between',
      }}
    >
      <NavItem active={dashActive} label="dash" onClick={() => navigate('/')}>
        <svg viewBox="0 0 24 24" width={24} height={24} fill="none" stroke="currentColor" strokeWidth={1.8}>
          <rect x="4" y="3" width="16" height="18" rx="3" />
          <path d="M9 3v3M15 3v3M8 11h8M8 15h5" />
        </svg>
      </NavItem>
      <NavItem active={false} label="daemon">
        <svg viewBox="0 0 24 24" width={24} height={24} fill="none" stroke="currentColor" strokeWidth={1.8}>
          <path d="M20 13.5A8 8 0 1 1 10.5 4a6.5 6.5 0 0 0 9.5 9.5z" />
          <path d="M17 4l.5 1.5L19 6l-1.5.5L17 8l-.5-1.5L15 6l1.5-.5z" />
        </svg>
      </NavItem>
      <NavItem active={readActive} label="read" onClick={() => navigate('/reading')}>
        <svg viewBox="0 0 24 24" width={24} height={24} fill="none" stroke="currentColor" strokeWidth={1.8}>
          <path d="M12 6c-1.5-1.6-3.6-2-6-2v14c2.4 0 4.5.4 6 2 1.5-1.6 3.6-2 6-2V4c-2.4 0-4.5.4-6 2z" />
          <path d="M12 6v14" />
        </svg>
      </NavItem>
      <NavItem active={false} label="chat">
        <svg viewBox="0 0 24 24" width={24} height={24} fill="none" stroke="currentColor" strokeWidth={1.8}>
          <path d="M6 5h12a3 3 0 0 1 3 3v6a3 3 0 0 1-3 3H10l-4 3v-3H6a3 3 0 0 1-3-3V8a3 3 0 0 1 3-3z" />
        </svg>
      </NavItem>
    </div>
  );
}
