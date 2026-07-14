import { useLocation } from 'react-router-dom';

type NavKey = 'dash' | 'chat' | 'read' | 'board';

const ICONS: Record<NavKey, string> = {
  dash: 'ti-layout-dashboard',
  chat: 'ti-message-2',
  read: 'ti-book',
  board: 'ti-clipboard-list',
};

function inDashApp() {
  return typeof window !== 'undefined' && window.location.pathname.startsWith('/dash');
}

function navItems() {
  const dash = inDashApp();
  return (['dash', 'chat', 'read', 'board'] as const).map((key) => ({
    key,
    href: key === 'dash' ? '/dash' : key === 'chat' ? (dash ? '/dash/contacts' : '/contacts') : `/${key === 'read' ? 'read' : 'board'}`,
    icon: ICONS[key],
    label: key,
  }));
}

function activeKey(pathname: string): NavKey {
  if (pathname === '/chat') return 'chat';
  if (pathname === '/reading') return 'read';
  return 'dash';
}

export function BottomNav({ embedded = false }: { embedded?: boolean }) {
  const location = useLocation();
  const active = activeKey(location.pathname);

  if (!embedded && location.pathname.startsWith('/memory')) return null;

  const items = navItems();
  const links = items.map((item) => (
    <a
      key={item.key}
      className={`ni${active === item.key ? ' act' : ''}`}
      href={item.href}
      aria-current={active === item.key ? 'page' : undefined}
    >
      <i className={`ti ${item.icon}`} />
      <span>{item.label}</span>
    </a>
  ));

  if (embedded) {
    return (
      <nav className="bnav-embedded" aria-label="主导航">
        {links}
      </nav>
    );
  }

  return (
    <nav className="bnav" aria-label="主导航">
      <div className="bnav-inner">{links}</div>
    </nav>
  );
}
