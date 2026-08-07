/**
 * Dash React SPA route + BottomNav registry.
 *
 * BrowserRouter basename `/dash` is applied by React Router — SPA `to` values
 * must be logical paths (e.g. `/contacts`), never hand-prefixed `/dash/...`.
 *
 * Scope: ROUTES / NAV_ITEMS / ROUTE_META are the single source for the Dash React SPA
 * shell chrome and BottomNav. Legacy static pages (`static/read.html`, `static/board.html`)
 * still keep their own HTML bottom bars; those are not driven by this file.
 * Unify them only when those pages migrate into the SPA — do not claim that
 * editing NAV_ITEMS alone updates the whole site navigation.
 */

export const ROUTES = {
  dash: '/',
  memory: '/memory',
  usage: '/usage',
  reading: '/reading',
  ledger: '/ledger',
  period: '/period',
  contacts: '/contacts',
  chat: '/chat',
  settings: '/settings',
  groupChat: '/group-chat',
  moments: '/moments',
  codexChat: '/codex-chat',
  profile: '/profile',
  dailySoftWindow: '/daily-soft-window',
  manualContextWindow: '/manual-context-window',
} as const;

export type SpaRoute = (typeof ROUTES)[keyof typeof ROUTES];

/** Dynamic monopoly room path — kept explicit (param segment). */
export const MONOPOLY_ROOM_PATH = '/monopoly/:roomId';

export type RouteChrome = 'standard' | 'fullscreen';
export type GlobalNavPlacement = 'fixed' | 'embedded';

export type RouteMeta = {
  chrome: RouteChrome;
  globalNav: boolean;
  globalNavPlacement?: GlobalNavPlacement;
};

/**
 * Per-route shell chrome metadata. Keys mirror ROUTES — paths are never duplicated.
 * Dynamic `/monopoly/:roomId` uses MONOPOLY_ROUTE_META below.
 */
export const ROUTE_META = {
  dash: { chrome: 'standard', globalNav: true, globalNavPlacement: 'fixed' },
  memory: { chrome: 'standard', globalNav: false },
  usage: { chrome: 'standard', globalNav: true, globalNavPlacement: 'fixed' },
  reading: { chrome: 'standard', globalNav: true, globalNavPlacement: 'fixed' },
  ledger: { chrome: 'standard', globalNav: true, globalNavPlacement: 'fixed' },
  period: { chrome: 'standard', globalNav: true, globalNavPlacement: 'fixed' },
  manualContextWindow: { chrome: 'standard', globalNav: true, globalNavPlacement: 'fixed' },
  contacts: { chrome: 'fullscreen', globalNav: true, globalNavPlacement: 'embedded' },
  chat: { chrome: 'fullscreen', globalNav: true, globalNavPlacement: 'embedded' },
  codexChat: { chrome: 'fullscreen', globalNav: true, globalNavPlacement: 'embedded' },
  settings: { chrome: 'fullscreen', globalNav: false },
  groupChat: { chrome: 'fullscreen', globalNav: false },
  moments: { chrome: 'fullscreen', globalNav: false },
  profile: { chrome: 'fullscreen', globalNav: false },
  dailySoftWindow: { chrome: 'fullscreen', globalNav: false },
} as const satisfies Record<keyof typeof ROUTES, RouteMeta>;

/** Chrome for dynamic monopoly room routes (`/monopoly/:roomId`). */
export const MONOPOLY_ROUTE_META: RouteMeta = { chrome: 'fullscreen', globalNav: false };

const DEFAULT_ROUTE_META: RouteMeta = {
  chrome: 'standard',
  globalNav: true,
  globalNavPlacement: 'fixed',
};

const ROUTE_PATH_TO_KEY = Object.fromEntries(
  Object.entries(ROUTES).map(([key, path]) => [path, key]),
) as Record<SpaRoute, keyof typeof ROUTES>;

/** Resolve shell chrome for a React Router pathname (basename already stripped). */
export function resolveRouteMeta(pathname: string): RouteMeta {
  if (pathname.startsWith('/monopoly/')) return MONOPOLY_ROUTE_META;
  const key = ROUTE_PATH_TO_KEY[pathname as SpaRoute];
  if (key) return ROUTE_META[key];
  return DEFAULT_ROUTE_META;
}

export function isFullscreenPath(pathname: string): boolean {
  return resolveRouteMeta(pathname).chrome === 'fullscreen';
}

export type NavKind = 'spa' | 'static' | 'external';

type NavItemBase = {
  key: string;
  label: string;
  icon: string;
  /**
   * Paths that highlight this item (React Router pathname, basename stripped).
   * SPA default: `[to]`. static/external default: `[href]`.
   */
  activePaths?: readonly string[];
};

export type NavItem =
  | (NavItemBase & {
      kind: 'spa';
      /** React Router path (no `/dash` prefix). */
      to: SpaRoute;
    })
  | (NavItemBase & {
      kind: 'static';
      /** Absolute site path for independent HTML pages. */
      href: string;
    })
  | (NavItemBase & {
      kind: 'external';
      href: string;
    });

/**
 * Visible Dash SPA bottom-nav surface (dash / chat / read / board).
 * Add or replace SPA items here (e.g. group-chat) — GlobalBottomNav and
 * resolveActiveNavKey stay generic. Does not rewrite legacy static HTML navs.
 */
export const NAV_ITEMS = [
  {
    key: 'dash',
    label: 'dash',
    icon: 'ti-layout-dashboard',
    kind: 'spa',
    to: ROUTES.dash,
  },
  {
    key: 'chat',
    label: 'chat',
    icon: 'ti-message-2',
    kind: 'spa',
    // Product: bottom-nav "chat" opens ContactsScreen
    to: ROUTES.contacts,
    activePaths: [ROUTES.contacts, ROUTES.chat],
  },
  {
    key: 'read',
    label: 'read',
    icon: 'ti-book',
    kind: 'static',
    href: '/read',
    // SPA reading screen still highlights the read tab when mounted under /dash
    activePaths: [ROUTES.reading],
  },
  {
    key: 'board',
    label: 'board',
    icon: 'ti-clipboard-list',
    kind: 'static',
    href: '/board',
  },
] as const satisfies readonly NavItem[];

export type NavKey = (typeof NAV_ITEMS)[number]['key'];

function activePathsFor(item: (typeof NAV_ITEMS)[number]): readonly string[] {
  if ('activePaths' in item && item.activePaths) return item.activePaths;
  if (item.kind === 'spa') return [item.to];
  return [item.href];
}

/**
 * Map React Router location.pathname (basename already stripped) to nav key.
 * Walks NAV_ITEMS; unmatched paths fall back to the first item (dash).
 */
export function resolveActiveNavKey(pathname: string): NavKey {
  for (const item of NAV_ITEMS) {
    if (activePathsFor(item).includes(pathname)) return item.key;
  }
  return NAV_ITEMS[0].key;
}
