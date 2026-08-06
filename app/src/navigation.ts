/**
 * Single source of truth for Dash SPA routes and bottom-nav targets.
 * BrowserRouter basename `/dash` is applied by React Router — SPA `to` values
 * must be logical paths (e.g. `/contacts`), never hand-prefixed `/dash/...`.
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

export type NavKey = 'dash' | 'chat' | 'read' | 'board';

export type NavItem =
  | {
      key: NavKey;
      label: string;
      icon: string;
      kind: 'spa';
      /** React Router path (no `/dash` prefix). */
      to: SpaRoute;
    }
  | {
      key: NavKey;
      label: string;
      icon: string;
      kind: 'static';
      /** Absolute site path for independent HTML pages. */
      href: string;
    }
  | {
      key: NavKey;
      label: string;
      icon: string;
      kind: 'external';
      href: string;
    };

/**
 * Visible bottom-nav product surface (dash / chat / read / board).
 * Future board→nexus or group-chat additions should edit this list only.
 */
export const NAV_ITEMS: readonly NavItem[] = [
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
  },
  {
    key: 'read',
    label: 'read',
    icon: 'ti-book',
    kind: 'static',
    href: '/read',
  },
  {
    key: 'board',
    label: 'board',
    icon: 'ti-clipboard-list',
    kind: 'static',
    href: '/board',
  },
] as const;

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

/**
 * Map React Router location.pathname (basename already stripped) to nav key.
 */
export function resolveActiveNavKey(pathname: string): NavKey {
  if (pathname === ROUTES.chat || pathname === ROUTES.contacts) return 'chat';
  if (pathname === ROUTES.reading) return 'read';
  return 'dash';
}
