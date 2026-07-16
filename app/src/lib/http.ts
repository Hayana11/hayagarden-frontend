// Same-origin by default (relative paths) so this works unmodified when the
// web app is served from the same host as the API (production: love-style.xyz).
// Set VITE_API_BASE_URL only for local dev against a different host.
const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '';

type Params = Record<string, string | number | undefined>;

export class HttpError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string, message: string) {
    super(message);
    this.name = 'HttpError';
    this.status = status;
    this.detail = detail;
  }
}

function describeErrorPayload(payload: unknown): string {
  if (!payload || typeof payload !== 'object') return '';
  const record = payload as Record<string, unknown>;
  const values = [record.error, record.detail]
    .map((value) => {
      if (typeof value === 'string') return value.trim();
      if (value === undefined || value === null) return '';
      try { return JSON.stringify(value); } catch { return String(value); }
    })
    .filter(Boolean);
  return [...new Set(values)].join(' · ');
}

function buildUrl(path: string, params?: Params): string {
  if (!params) return `${BASE_URL}${path}`;
  const qs = Object.entries(params)
    .filter(([, v]) => v !== undefined)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join('&');
  return qs ? `${BASE_URL}${path}?${qs}` : `${BASE_URL}${path}`;
}

async function request<T>(path: string, init?: RequestInit, params?: Params): Promise<T> {
  const res = await fetch(buildUrl(path, params), {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    ...init,
  });
  if (!res.ok) {
    let detail = '';
    try { detail = describeErrorPayload(await res.json()); } catch { /* non-JSON error body */ }
    const summary = `${init?.method ?? 'GET'} ${path} failed: ${res.status}`;
    throw new HttpError(res.status, detail, detail ? `${summary} · ${detail}` : summary);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const http = {
  get: <T>(path: string, params?: Params) => request<T>(path, undefined, params),
  patch: <T>(path: string, body: unknown) => request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined }),
  put: <T>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
};

export function sseUrl(path: string): string {
  return `${BASE_URL}${path}`;
}
