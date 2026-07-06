import { useLocation } from 'react-router-dom';

type NavKey = 'dash' | 'chat' | 'read' | 'board';

const ITEMS: { key: NavKey; href: string; icon: string; label: string }[] = [
  { key: 'dash', href: '/dash', icon: 'ti-layout-dashboard', label: 'dash' },
  { key: 'chat', href: '/chat', icon: 'ti-message-2', label: 'chat' },
  { key: 'read', href: '/read', icon: 'ti-book', label: 'read' },
  { key: 'board', href: '/board', icon: 'ti-clipboard-list', label: 'board' },
];

export function BottomNav() {
  const location = useLocation();
  const active: NavKey = location.pathname === '/reading' ? 'read' : 'dash';

  return (
    <nav className="bnav" aria-label="主导航">
      <div className="bnav-inner">
        {ITEMS.map((item) => (
          <a
            key={item.key}
            className={`ni${active === item.key ? ' act' : ''}`}
            href={item.href}
            aria-current={active === item.key ? 'page' : undefined}
          >
            <i className={`ti ${item.icon}`} />
            <span>{item.label}</span>
          </a>
        ))}
      </div>
    </nav>
  );
}
