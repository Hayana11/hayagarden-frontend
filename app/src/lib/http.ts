// Same-origin by default (relative paths) so this works unmodified when the
// web app is served from the same host as the API (production: love-style.xyz).
// Set VITE_API_BASE_URL only for local dev against a different host.
const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '';

type Params = Record<string, string | number | undefined>;

export class HttpError extends Error {
  readonly status: number;
  readonly detail: string;
  /** Machine code from JSON body when present (e.g. `rollover_deferred` on 423). */
  readonly code?: string;
  /** Raw JSON error body when parseable. */
  readonly payload?: unknown;

  constructor(
    status: number,
    detail: string,
    message: string,
    opts?: { code?: string; payload?: unknown },
  ) {
    super(message);
    this.name = 'HttpError';
    this.status = status;
    this.detail = detail;
    if (opts?.code !== undefined) this.code = opts.code;
    if (opts?.payload !== undefined) this.payload = opts.payload;
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

function codeFromPayload(payload: unknown): string | undefined {
  if (!payload || typeof payload !== 'object') return undefined;
  const code = (payload as Record<string, unknown>).code;
  return typeof code === 'string' && code.trim() ? code.trim() : undefined;
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
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'application/json',
  };
  if (init?.headers) {
    const extra = init.headers;
    if (extra instanceof Headers) {
      extra.forEach((v, k) => { headers[k] = v; });
    } else if (Array.isArray(extra)) {
      for (const [k, v] of extra) headers[k] = v;
    } else {
      Object.assign(headers, extra);
    }
  }
  const res = await fetch(buildUrl(path, params), {
    credentials: 'include',
    ...init,
    headers,
  });
  if (!res.ok) {
    let detail = '';
    let payload: unknown;
    let code: string | undefined;
    try {
      payload = await res.json();
      detail = describeErrorPayload(payload);
      code = codeFromPayload(payload);
    } catch { /* non-JSON error body */ }
    const summary = `${init?.method ?? 'GET'} ${path} failed: ${res.status}`;
    throw new HttpError(
      res.status,
      detail,
      detail ? `${summary} · ${detail}` : summary,
      { code, payload },
    );
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const http = {
  get: <T>(path: string, params?: Params, init?: RequestInit) =>
    request<T>(path, init, params),
  patch: <T>(path: string, body: unknown, init?: RequestInit) =>
    request<T>(path, { ...init, method: 'PATCH', body: JSON.stringify(body) }),
  post: <T>(path: string, body?: unknown, init?: RequestInit) =>
    request<T>(path, {
      ...init,
      method: 'POST',
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }),
  put: <T>(path: string, body: unknown, init?: RequestInit) =>
    request<T>(path, { ...init, method: 'PUT', body: JSON.stringify(body) }),
  del: <T>(path: string, init?: RequestInit) =>
    request<T>(path, { ...init, method: 'DELETE' }),
};

export function sseUrl(path: string): string {
  return `${BASE_URL}${path}`;
}
