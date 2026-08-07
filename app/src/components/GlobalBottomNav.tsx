import { NavLink, useLocation } from 'react-router-dom';
import { NAV_ITEMS, resolveActiveNavKey, type NavItem } from '../navigation';

function NavAnchor({
  item,
  active,
}: {
  item: NavItem;
  active: boolean;
}) {
  const className = `ni global-bottom-nav__item${active ? ' act' : ''}`;
  const ariaCurrent = active ? ('page' as const) : undefined;
  const content = (
    <>
      <i className={`ti ${item.icon} global-bottom-nav__icon`} />
      <span className="global-bottom-nav__label">{item.label}</span>
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

export type GlobalBottomNavVariant = 'fixed' | 'embedded';

/**
 * Canonical React global bottom navigation — renders visible items from NAV_ITEMS only.
 */
export function GlobalBottomNav({ variant = 'fixed' }: { variant?: GlobalBottomNavVariant }) {
  const location = useLocation();
  const active = resolveActiveNavKey(location.pathname);

  const links = NAV_ITEMS.map((item) => (
    <NavAnchor key={item.key} item={item} active={active === item.key} />
  ));

  const inner = <div className="bnav-inner global-bottom-nav__inner">{links}</div>;

  if (variant === 'embedded') {
    return (
      <nav className="bnav-embedded global-bottom-nav global-bottom-nav--embedded" aria-label="主导航">
        {inner}
      </nav>
    );
  }

  return (
    <nav className="bnav global-bottom-nav global-bottom-nav--fixed" aria-label="主导航">
      {inner}
    </nav>
  );
}

/** @deprecated Use GlobalBottomNav */
export { GlobalBottomNav as BottomNav };
