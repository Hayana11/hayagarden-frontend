/**
 * Dash React SPA route + BottomNav registry.
 *
 * BrowserRouter basename `/dash` is applied by React Router — SPA `to` values
 * must be logical paths (e.g. `/contacts`), never hand-prefixed `/dash/...`.
 *
 * Scope: ROUTES / NAV_ITEMS are the single source for the Dash React SPA
 * BottomNav only. Legacy static pages (`static/read.html`, `static/board.html`)
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
 * Add or replace SPA items here (e.g. group-chat) — BottomNav and
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

/** Paths that use the fullscreen shell (no AppShell chrome / bottom nav). */
export const FULLSCREEN_PATHS: readonly string[] = [
  ROUTES.chat,
  ROUTES.settings,
  ROUTES.groupChat,
  ROUTES.moments,
  ROUTES.contacts,
  ROUTES.codexChat,
  ROUTES.profile,
  ROUTES.dailySoftWindow,
];

export function isFullscreenPath(pathname: string): boolean {
  return FULLSCREEN_PATHS.includes(pathname) || pathname.startsWith('/monopoly/');
}

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
