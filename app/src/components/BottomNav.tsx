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
        background: 'rgba(255,255,255,0.97)',
        backdropFilter: 'blur(12px)',
        borderTop: '1px solid var(--color-border)',
        padding: '7px 0 calc(7px + env(safe-area-inset-bottom, 0px))',
        display: 'flex',
        justifyContent: 'space-between',
      }}
    >
      <NavItem active={dashActive} label="dash" onClick={() => navigate('/')}>
        <svg viewBox="0 0 24 24" width={21} height={21} fill="none" stroke="currentColor" strokeWidth={1.8}>
          <rect x="3" y="4" width="8" height="7" rx="1.5" />
          <rect x="13" y="4" width="8" height="5" rx="1.5" />
          <rect x="13" y="11" width="8" height="9" rx="1.5" />
          <rect x="3" y="13" width="8" height="7" rx="1.5" />
        </svg>
      </NavItem>
      <NavItem active={false} label="chat" onClick={() => window.location.assign('/chat')}>
        <svg viewBox="0 0 24 24" width={21} height={21} fill="none" stroke="currentColor" strokeWidth={1.8}>
          <path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v7A2.5 2.5 0 0 1 17.5 16H10l-4 4v-4h-.5A2.5 2.5 0 0 1 4 13.5z" />
        </svg>
      </NavItem>
      <NavItem active={readActive} label="read" onClick={() => navigate('/reading')}>
        <svg viewBox="0 0 24 24" width={21} height={21} fill="none" stroke="currentColor" strokeWidth={1.8}>
          <path d="M12 6c-1.5-1.6-3.6-2-6-2v14c2.4 0 4.5.4 6 2 1.5-1.6 3.6-2 6-2V4c-2.4 0-4.5.4-6 2z" />
          <path d="M12 6v14" />
        </svg>
      </NavItem>
      <NavItem active={false} label="board" onClick={() => window.location.assign('/board')}>
        <svg viewBox="0 0 24 24" width={21} height={21} fill="none" stroke="currentColor" strokeWidth={1.8}>
          <rect x="4" y="3" width="16" height="18" rx="2" />
          <path d="M8 7h8M8 11h8M8 15h5" />
        </svg>
      </NavItem>
    </div>
  );
}
