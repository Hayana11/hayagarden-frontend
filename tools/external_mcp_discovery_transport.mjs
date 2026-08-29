import dns from 'node:dns/promises';
import net from 'node:net';

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { Agent } from 'undici';

export const DISCOVERY_STATUS = Object.freeze({
  SUCCESS: 'SUCCESS',
  UNAVAILABLE: 'UNAVAILABLE',
  AUTH_REQUIRED: 'AUTH_REQUIRED',
  PROTOCOL_ERROR: 'PROTOCOL_ERROR',
  LIMIT_EXCEEDED: 'LIMIT_EXCEEDED',
});

export const DEFAULT_LIMITS = Object.freeze({
  connectTimeoutMs: 5_000,
  requestTimeoutMs: 5_000,
  overallTimeoutMs: 15_000,
  maxPages: 32,
  maxTools: 1_000,
  maxCatalogBytes: 2 * 1024 * 1024,
  maxResponseBytes: 2 * 1024 * 1024,
  maxErrorSummaryBytes: 512,
});

const SUPPORTED_TRANSPORT = 'streamable_http';
const REDIRECT_STATUS = new Set([301, 302, 303, 307, 308]);
const textEncoder = new TextEncoder();

export class DiscoveryInputError extends Error {
  constructor(message) {
    super(message);
    this.name = 'DiscoveryInputError';
  }
}

export class DiscoveryLimitError extends Error {
  constructor(message) {
    super(message);
    this.name = 'DiscoveryLimitError';
  }
}

export class DiscoveryTimeoutError extends Error {
  constructor(message) {
    super(message);
    this.name = 'DiscoveryTimeoutError';
  }
}

class DiscoveryAuthError extends DiscoveryInputError {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

class DiscoveryReflectionError extends Error {
  constructor() {
    super('provider material contained the transient credential');
    this.code = 'SECRET_REFLECTION_BLOCKED';
  }
}

function byteLength(value) {
  return textEncoder.encode(JSON.stringify(value)).byteLength;
}

function ipv4ToNumber(address) {
  const parts = address.split('.');
  if (parts.length !== 4 || parts.some((part) => !/^\d{1,3}$/.test(part))) return null;
  const octets = parts.map(Number);
  if (octets.some((part) => part > 255)) return null;
  return (((octets[0] << 24) >>> 0) + (octets[1] << 16) + (octets[2] << 8) + octets[3]) >>> 0;
}

function ipv4IsUnsafe(address) {
  const value = ipv4ToNumber(address);
  if (value === null) return true;
  const inRange = (start, end) => value >= start && value <= end;
  return (
    inRange(0x00000000, 0x00ffffff) || // unspecified/current network
    inRange(0x0a000000, 0x0affffff) || // private
    inRange(0x64400000, 0x647fffff) || // shared address space
    inRange(0x7f000000, 0x7fffffff) || // loopback
    inRange(0xa9fe0000, 0xa9feffff) || // link-local
    inRange(0xac100000, 0xac1fffff) || // private
    inRange(0xc0000000, 0xc00000ff) || // IETF protocol assignments
    inRange(0xc0000200, 0xc00002ff) || // documentation
    inRange(0xc0a80000, 0xc0a8ffff) || // private
    inRange(0xc6120000, 0xc613ffff) || // benchmarking
    inRange(0xc6336400, 0xc63364ff) || // documentation
    inRange(0xcb007100, 0xcb0071ff) || // documentation
    inRange(0xe0000000, 0xffffffff) // multicast/reserved
  );
}

function ipv6ToBigInt(address) {
  let value = address.toLowerCase();
  if (value.includes('.')) {
    const separator = value.lastIndexOf(':');
    const embedded = value.slice(separator + 1);
    const ipv4 = ipv4ToNumber(embedded);
    if (ipv4 === null) return null;
    value = `${value.slice(0, separator)}:${(ipv4 >>> 16).toString(16)}:${(ipv4 & 0xffff).toString(16)}`;
  }
  const halves = value.split('::');
  if (halves.length > 2) return null;
  const left = halves[0] ? halves[0].split(':') : [];
  const right = halves.length === 2 && halves[1] ? halves[1].split(':') : [];
  if (left.some((part) => !/^[0-9a-f]{1,4}$/.test(part)) || right.some((part) => !/^[0-9a-f]{1,4}$/.test(part))) return null;
  const missing = 8 - left.length - right.length;
  if (halves.length === 1 && missing !== 0) return null;
  if (halves.length === 2 && missing < 1) return null;
  const parts = [...left, ...Array.from({ length: missing }, () => '0'), ...right];
  return parts.reduce((result, part) => (result << 16n) | BigInt(parseInt(part, 16)), 0n);
}

function ipv6Prefix(value, prefixLength, expected) {
  const shift = 128n - BigInt(prefixLength);
  return (value >> shift) === expected;
}

function ipv6IsUnsafe(address) {
  const value = ipv6ToBigInt(address);
  if (value === null) return true;
  const low32 = value & 0xffffffffn;
  const upper96 = value >> 32n;
  const mappedIpv4 = upper96 === 0xffffn;
  if (mappedIpv4) return ipv4IsUnsafe([0, 1, 2, 3].map((_, index) => Number((low32 >> BigInt(24 - index * 8)) & 0xffn)).join('.'));

  return (
    value === 0n ||
    value === 1n ||
    ipv6Prefix(value, 7, 0xfc00n >> 57n) || // unique local
    ipv6Prefix(value, 10, 0xfe80n >> 118n) || // link-local
    ipv6Prefix(value, 8, 0xffn) || // multicast
    ipv6Prefix(value, 32, 0x20010db8n) || // documentation
    ipv6Prefix(value, 32, 0x20010000n) || // protocol assignments
    ipv6Prefix(value, 28, 0x20010n) || // ORCHID
    ipv6Prefix(value, 96, 0n) || // IPv4-compatible / unspecified space
    (value >> 125n) !== 1n // only global unicast 2000::/3 is admitted
  );
}

export function classifyAddress(address) {
  const family = net.isIP(address);
  if (family === 4) return ipv4IsUnsafe(address) ? 'UNSAFE' : 'PUBLIC';
  if (family === 6) return ipv6IsUnsafe(address) ? 'UNSAFE' : 'PUBLIC';
  return 'UNSAFE';
}

export function validateResolvedAddresses(records) {
  if (!Array.isArray(records) || records.length === 0) {
    throw new DiscoveryInputError('hostname resolution returned no addresses');
  }
  const normalized = records.map((record) => (typeof record === 'string' ? { address: record, family: net.isIP(record) } : record));
  if (normalized.some((record) => !record || classifyAddress(record.address) !== 'PUBLIC')) {
    throw new DiscoveryInputError('hostname resolution included a non-public address');
  }
  return normalized;
}

export function validateEndpoint(endpoint, transport = SUPPORTED_TRANSPORT) {
  if (transport !== SUPPORTED_TRANSPORT) throw new DiscoveryInputError('only streamable_http transport is supported');
  if (typeof endpoint !== 'string' || endpoint.length === 0) throw new DiscoveryInputError('endpoint must be a URL string');
  let url;
  try {
    url = new URL(endpoint);
  } catch {
    throw new DiscoveryInputError('endpoint is not a valid URL');
  }
  if (url.protocol !== 'https:') throw new DiscoveryInputError('endpoint must use HTTPS');
  if (url.username || url.password || url.search || url.hash) throw new DiscoveryInputError('endpoint contains forbidden credential or query material');
  if (!url.hostname) throw new DiscoveryInputError('endpoint hostname is required');
  if (net.isIP(url.hostname) && classifyAddress(url.hostname) !== 'PUBLIC') throw new DiscoveryInputError('endpoint address is not public');
  return url;
}

function defaultResolve(hostname) {
  return dns.lookup(hostname, { all: true, verbatim: true });
}

export function createSafeDispatcher({ resolver = defaultResolve } = {}) {
  const lookup = (hostname, _options, callback) => {
    Promise.resolve(resolver(hostname))
      .then((records) => {
        const validated = validateResolvedAddresses(records);
        const selected = validated[0];
        callback(null, selected.address, selected.family || net.isIP(selected.address));
      })
      .catch((error) => callback(error));
  };
  return new Agent({ connect: { lookup } });
}

function combineSignals(signals) {
  const active = signals.filter(Boolean);
  if (active.length === 0) return undefined;
  if (typeof AbortSignal.any === 'function') return AbortSignal.any(active);
  const controller = new AbortController();
  const abort = () => controller.abort();
  active.forEach((signal) => (signal.aborted ? abort() : signal.addEventListener('abort', abort, { once: true })));
  return controller.signal;
}

function boundedSummary(value, limit) {
  const clean = String(value).replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim();
  return clean.slice(0, limit);
}

function validateAuth(auth) {
  if (auth === null || typeof auth === 'undefined') return null;
  if (!auth || typeof auth !== 'object' || Array.isArray(auth) || Object.getPrototypeOf(auth) !== Object.prototype) {
    throw new DiscoveryAuthError('AUTH_SCHEME_UNSUPPORTED', 'MCP auth scheme is unsupported');
  }
  if (Object.keys(auth).sort().join(',') !== 'credential,scheme' || auth.scheme !== 'bearer' ||
      typeof auth.credential !== 'string' || auth.credential.length === 0) {
    throw new DiscoveryAuthError('AUTH_SCHEME_UNSUPPORTED', 'MCP auth scheme is unsupported');
  }
  if (new TextEncoder().encode(auth.credential).byteLength > 16 * 1024 || /[\u0000-\u001f\u007f]/.test(auth.credential)) {
    throw new DiscoveryAuthError('AUTH_SCHEME_UNSUPPORTED', 'MCP auth scheme is unsupported');
  }
  return { scheme: 'bearer', credential: auth.credential };
}

function requestHeaders(initHeaders, auth) {
  const headers = new Headers(initHeaders || {});
  if (!auth) return headers;
  if (headers.has('authorization')) {
    throw new DiscoveryAuthError('AUTHORIZATION_HEADER_CONFLICT', 'authorization header is already present');
  }
  headers.set('authorization', `Bearer ${auth.credential}`);
  return headers;
}

function containsCredential(value, credential, seen = new Set()) {
  if (!credential || value === null || typeof value === 'undefined') return false;
  if (typeof value === 'string') return value.includes(credential);
  if (typeof value !== 'object' && !(value instanceof Error)) return false;
  if (seen.has(value)) return false;
  seen.add(value);
  if (value instanceof Error && (String(value.message).includes(credential) || String(value.stack || '').includes(credential))) return true;
  for (const key of Object.keys(value)) {
    if (containsCredential(value[key], credential, seen)) return true;
  }
  return false;
}

function boundedResponse(response, maxBytes) {
  const declared = Number(response.headers?.get?.('content-length') ?? 0);
  if (declared > maxBytes) throw new DiscoveryLimitError('MCP response exceeded the byte limit');
  if (!response.body || typeof response.body.pipeThrough !== 'function') return response;
  let total = 0;
  const boundedBody = response.body.pipeThrough(new TransformStream({
    transform(chunk, controller) {
      const bytes = typeof chunk === 'string' ? textEncoder.encode(chunk) : chunk;
      total += bytes.byteLength;
      if (total > maxBytes) {
        controller.error(new DiscoveryLimitError('MCP response exceeded the byte limit'));
        return;
      }
      controller.enqueue(chunk);
    },
  }));
  return new Response(boundedBody, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
}

async function responseWithReflectionGuard(response, credential, maxBytes) {
  const bounded = boundedResponse(response, maxBytes);
  if (!credential || typeof bounded.clone !== 'function') return bounded;
  const reflectedBody = await bounded.clone().text();
  if (reflectedBody.includes(credential)) throw new DiscoveryReflectionError();
  return bounded;
}

function resultSkeleton(diagnostics) {
  return {
    status: DISCOVERY_STATUS.PROTOCOL_ERROR,
    catalog_complete: false,
    zero_tools: false,
    server_metadata: null,
    tools: [],
    tool_record_boundary: 'SDK_VISIBLE_RAW',
    error: null,
    diagnostics,
  };
}

function classifyFailure(error, limits) {
  if (error instanceof DiscoveryAuthError) return { code: error.code, summary: error.message };
  if (error instanceof DiscoveryReflectionError) return { code: 'SECRET_REFLECTION_BLOCKED', summary: 'provider material was suppressed' };
  if (error instanceof DiscoveryLimitError) return { code: DISCOVERY_STATUS.LIMIT_EXCEEDED, summary: error.message };
  if (error instanceof DiscoveryInputError) return { code: DISCOVERY_STATUS.PROTOCOL_ERROR, summary: error.message };
  if (error instanceof DiscoveryTimeoutError || error?.name === 'AbortError') return { code: DISCOVERY_STATUS.UNAVAILABLE, summary: 'discovery timed out' };
  const status = Number(error?.code ?? error?.status ?? 0);
  if (status === 401 || status === 403) return { code: DISCOVERY_STATUS.AUTH_REQUIRED, summary: `remote server returned HTTP ${status}` };
  if (status >= 500 && status <= 599) return { code: DISCOVERY_STATUS.UNAVAILABLE, summary: `remote server returned HTTP ${status}` };
  if (REDIRECT_STATUS.has(status)) return { code: DISCOVERY_STATUS.PROTOCOL_ERROR, summary: 'redirects are not permitted' };
  if (error?.name === 'SyntaxError' || error?.name === 'McpError' || error?.name === 'ZodError') return { code: DISCOVERY_STATUS.PROTOCOL_ERROR, summary: 'remote MCP response was malformed' };
  if (error?.name === 'TypeError' || error?.name === 'ConnectTimeoutError') return { code: DISCOVERY_STATUS.UNAVAILABLE, summary: 'remote MCP network request failed' };
  return { code: DISCOVERY_STATUS.PROTOCOL_ERROR, summary: boundedSummary('remote MCP discovery failed', limits.maxErrorSummaryBytes) };
}

async function discoverWithClient({ url, limits, fetchImpl, resolver, diagnostics, result, auth }) {
  const dispatcher = createSafeDispatcher({ resolver });
  let requestIndex = 0;
  const fetcher = async (input, init = {}) => {
    requestIndex += 1;
    diagnostics.request_count = requestIndex;
    const timeoutMs = requestIndex === 1 ? limits.connectTimeoutMs : limits.requestTimeoutMs;
    const requestController = new AbortController();
    const signal = combineSignals([init.signal, requestController.signal]);
    const timer = setTimeout(() => requestController.abort(), timeoutMs);
    let requestTimeoutHandle;
    try {
      const requestInit = {
        ...init,
        headers: requestHeaders(init.headers, auth),
        redirect: 'error',
        signal,
        ...(dispatcher ? { dispatcher } : {}),
      };
      const response = await Promise.race([
        Promise.resolve((fetchImpl ?? globalThis.fetch)(input, requestInit)),
        new Promise((_, reject) => {
          requestTimeoutHandle = setTimeout(() => reject(new DiscoveryTimeoutError('MCP request timed out')), timeoutMs);
        }),
      ]);
      if (!response || typeof response.body === 'undefined') throw new DiscoveryInputError('fetch returned an invalid response');
      return responseWithReflectionGuard(response, auth?.credential, limits.maxResponseBytes);
    } finally {
      clearTimeout(timer);
      clearTimeout(requestTimeoutHandle);
    }
  };

  const client = new Client({ name: 'hayagarden-external-discovery', version: '1.0.0' });
  const transport = new StreamableHTTPClientTransport(url, {
    fetch: fetcher,
    requestInit: { redirect: 'error' },
    reconnectionOptions: { maxRetries: 0, initialReconnectionDelay: 0, maxReconnectionDelay: 0 },
  });
  const overallController = new AbortController();
  const overallTimer = setTimeout(() => overallController.abort(), limits.overallTimeoutMs);
  try {
    await Promise.race([
      client.connect(transport),
      new Promise((_, reject) => overallController.signal.addEventListener('abort', () => reject(new DiscoveryTimeoutError('discovery timed out')), { once: true })),
    ]);
    result.server_metadata = {
      protocol_version: transport.protocolVersion ?? null,
      server_info: client.getServerVersion?.() ?? null,
      server_capabilities: client.getServerCapabilities?.() ?? null,
    };
    if (auth?.credential && containsCredential(result.server_metadata, auth.credential)) throw new DiscoveryReflectionError();
    if (byteLength(result.server_metadata) > limits.maxCatalogBytes) throw new DiscoveryLimitError('MCP metadata exceeded the byte limit');

    const cursors = new Set();
    let cursor;
    do {
      if (diagnostics.page_count >= limits.maxPages) throw new DiscoveryLimitError('MCP tool catalog exceeded the page limit');
      if (cursor !== undefined) {
        if (typeof cursor !== 'string' || cursors.has(cursor)) throw new DiscoveryInputError('MCP tool catalog cursor loop detected');
        cursors.add(cursor);
      }
      const page = await Promise.race([
        client.listTools(cursor === undefined ? undefined : { cursor }),
        new Promise((_, reject) => overallController.signal.addEventListener('abort', () => reject(new DiscoveryTimeoutError('discovery timed out')), { once: true })),
      ]);
      if (auth?.credential && containsCredential(page, auth.credential)) throw new DiscoveryReflectionError();
      diagnostics.page_count += 1;
      if (!page || !Array.isArray(page.tools)) throw new DiscoveryInputError('MCP tools/list returned no tool array');
      for (const tool of page.tools) {
        if (diagnostics.tool_count >= limits.maxTools) throw new DiscoveryLimitError('MCP tool catalog exceeded the tool limit');
        let size;
        try { size = byteLength(tool); } catch { throw new DiscoveryInputError('MCP tool record was not serializable'); }
        if (diagnostics.catalog_bytes + size > limits.maxCatalogBytes) throw new DiscoveryLimitError('MCP tool catalog exceeded the byte limit');
        result.tools.push(tool);
        diagnostics.tool_count += 1;
        diagnostics.catalog_bytes += size;
      }
      cursor = page.nextCursor;
      if (cursor !== undefined && cursor !== null && cursor !== '' && typeof cursor !== 'string') throw new DiscoveryInputError('MCP tool catalog cursor was invalid');
      if (cursor === '') cursor = undefined;
    } while (cursor !== undefined);
    result.catalog_complete = true;
    result.zero_tools = result.tools.length === 0;
    result.status = DISCOVERY_STATUS.SUCCESS;
  } finally {
    clearTimeout(overallTimer);
    await client.close().catch(() => undefined);
    await dispatcher?.close().catch(() => undefined);
  }
}

export async function discoverExternalMcp({
  endpoint,
  transport = SUPPORTED_TRANSPORT,
  limits: suppliedLimits = {},
  resolver = defaultResolve,
  fetchImpl,
  auth = null,
} = {}) {
  const limits = { ...DEFAULT_LIMITS, ...suppliedLimits };
  const diagnostics = { request_count: 0, page_count: 0, tool_count: 0, catalog_bytes: 0, egress_policy: 'validated_lookup_at_actual_dial' };
  const result = resultSkeleton(diagnostics);
  let url;
  let authBinding = null;
  try {
    url = validateEndpoint(endpoint, transport);
    authBinding = validateAuth(auth);
    await discoverWithClient({ url, limits, fetchImpl, resolver, diagnostics, result, auth: authBinding });
  } catch (error) {
    const failure = authBinding?.credential && containsCredential(error, authBinding.credential)
      ? { code: 'SECRET_REFLECTION_BLOCKED', summary: 'provider material was suppressed' }
      : classifyFailure(error, limits);
    result.status = failure.code;
    result.error = { code: failure.code, summary: boundedSummary(failure.summary, limits.maxErrorSummaryBytes) };
    result.catalog_complete = false;
    result.zero_tools = false;
    result.server_metadata = null;
    result.tools = [];
  }
  return result;
}

export { SUPPORTED_TRANSPORT };
