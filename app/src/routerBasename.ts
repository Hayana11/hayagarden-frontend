export type RouterBasename = '/dash' | '/preview' | undefined;

function matchesPathSegment(pathname: string, segment: '/dash' | '/preview'): boolean {
  return pathname === segment || pathname.startsWith(`${segment}/`);
}

export function resolveRouterBasename(pathname: string): RouterBasename {
  if (matchesPathSegment(pathname, '/dash')) return '/dash';
  if (matchesPathSegment(pathname, '/preview')) return '/preview';
  return undefined;
}
