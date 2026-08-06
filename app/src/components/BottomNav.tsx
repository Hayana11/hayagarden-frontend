import { NavLink, useLocation } from 'react-router-dom';
import { NAV_ITEMS, resolveActiveNavKey, type NavItem } from '../navigation';

function NavAnchor({
  item,
  active,
}: {
  item: NavItem;
  active: boolean;
}) {
  const className = `ni${active ? ' act' : ''}`;
  const ariaCurrent = active ? ('page' as const) : undefined;
  const content = (
    <>
      <i className={`ti ${item.icon}`} />
      <span>{item.label}</span>
    </>
  );

  if (item.kind === 'spa') {
    return (
      <NavLink to={item.to} className={className} aria-current={ariaCurrent} end={item.to === '/'}>
        {content}
      </NavLink>
    );
  }

  // static | external — full document navigation (or future system browser)
  return (
    <a
      className={className}
      href={item.href}
      aria-current={ariaCurrent}
      {...(item.kind === 'external' ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
    >
      {content}
    </a>
  );
}

export function BottomNav({ embedded = false }: { embedded?: boolean }) {
  const location = useLocation();
  const active = resolveActiveNavKey(location.pathname);

  if (!embedded && location.pathname.startsWith('/memory')) return null;

  const links = NAV_ITEMS.map((item) => (
    <NavAnchor key={item.key} item={item} active={active === item.key} />
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
