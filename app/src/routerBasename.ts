export type RouterBasename = '/dash' | '/preview' | '/preview/dash' | undefined;

function matchesPathSegment(pathname: string, segment: '/dash' | '/preview'): boolean {
  return pathname === segment || pathname.startsWith(`${segment}/`);
}

export function resolveRouterBasename(pathname: string): RouterBasename {
  if (pathname === '/preview/dash' || pathname.startsWith('/preview/dash/')) return '/preview/dash';
  if (matchesPathSegment(pathname, '/dash')) return '/dash';
  if (matchesPathSegment(pathname, '/preview')) return '/preview';
  return undefined;
}
