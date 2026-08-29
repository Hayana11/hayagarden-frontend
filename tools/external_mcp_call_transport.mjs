import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

import {
  DEFAULT_LIMITS,
  classifyAddress,
  DiscoveryInputError,
  DiscoveryLimitError,
  DiscoveryTimeoutError,
  createSafeDispatcher,
  validateEndpoint,
  SUPPORTED_TRANSPORT,
} from './external_mcp_discovery_transport.mjs';

export const CALL_OUTCOME = Object.freeze({
  SUCCESS: 'SUCCESS',
  TOOL_ERROR: 'TOOL_ERROR',
  NOT_INVOKED: 'NOT_INVOKED',
  OUTCOME_UNKNOWN: 'OUTCOME_UNKNOWN',
});

export const CALL_DEFAULT_LIMITS = Object.freeze({
  connectTimeoutMs: DEFAULT_LIMITS.connectTimeoutMs,
  requestTimeoutMs: DEFAULT_LIMITS.requestTimeoutMs,
  overallTimeoutMs: DEFAULT_LIMITS.overallTimeoutMs,
  maxResponseBytes: DEFAULT_LIMITS.maxResponseBytes,
  maxResultBytes: DEFAULT_LIMITS.maxResponseBytes,
  maxInputBytes: 256 * 1024,
  maxErrorSummaryBytes: DEFAULT_LIMITS.maxErrorSummaryBytes,
});

const REDIRECT_STATUS = new Set([301, 302, 303, 307, 308]);
const textEncoder = new TextEncoder();

class CallInputError extends Error {
  constructor(message) {
    super(message);
    this.name = 'CallInputError';
  }
}

class CallAuthError extends CallInputError {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

class CallReflectionError extends Error {
  constructor() {
    super('provider material contained the transient credential');
    this.code = 'SECRET_REFLECTION_BLOCKED';
  }
}

class CallLimitError extends Error {
  constructor(message) {
    super(message);
    this.name = 'CallLimitError';
  }
}

class CallTimeoutError extends Error {
  constructor(message) {
    super(message);
    this.name = 'CallTimeoutError';
  }
}

function mergedLimits(limits = {}) {
  const merged = { ...CALL_DEFAULT_LIMITS, ...limits };
  for (const name of Object.keys(CALL_DEFAULT_LIMITS)) {
    if (!Number.isInteger(merged[name]) || merged[name] <= 0) {
      throw new CallInputError(`limit ${name} must be a positive integer`);
    }
  }
  return merged;
}

function truncateUtf8(value, maxBytes) {
  const bytes = textEncoder.encode(String(value));
  if (bytes.byteLength <= maxBytes) return String(value);
  let end = maxBytes;
  while (end > 0 && (bytes[end] & 0xc0) === 0x80) end -= 1;
  return new TextDecoder().decode(bytes.slice(0, end));
}

function errorSummary(value, maxBytes) {
  const clean = String(value)
    .replace(/[\u0000-\u001f\u007f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return truncateUtf8(clean, maxBytes);
}

function validateAuth(auth) {
  if (auth === null || typeof auth === 'undefined') return null;
  if (!auth || typeof auth !== 'object' || Array.isArray(auth) || Object.getPrototypeOf(auth) !== Object.prototype) {
    throw new CallAuthError('AUTH_SCHEME_UNSUPPORTED', 'MCP auth scheme is unsupported');
  }
  if (Object.keys(auth).sort().join(',') !== 'credential,scheme' || auth.scheme !== 'bearer' ||
      typeof auth.credential !== 'string' || auth.credential.length === 0) {
    throw new CallAuthError('AUTH_SCHEME_UNSUPPORTED', 'MCP auth scheme is unsupported');
  }
  if (textEncoder.encode(auth.credential).byteLength > 16 * 1024 || /[\u0000-\u001f\u007f]/.test(auth.credential)) {
    throw new CallAuthError('AUTH_SCHEME_UNSUPPORTED', 'MCP auth scheme is unsupported');
  }
  return { scheme: 'bearer', credential: auth.credential };
}

function requestHeaders(initHeaders, auth) {
  const headers = new Headers(initHeaders || {});
  if (!auth) return headers;
  if (headers.has('authorization')) {
    throw new CallAuthError('AUTHORIZATION_HEADER_CONFLICT', 'authorization header is already present');
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

function assertJsonSafe(value, seen = new Set(), path = 'value') {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new CallInputError(`${path} must contain finite numbers`);
    return;
  }
  if (typeof value === 'undefined' || typeof value === 'function' || typeof value === 'symbol' || typeof value === 'bigint') {
    throw new CallInputError(`${path} is not JSON-safe`);
  }
  if (typeof value !== 'object') throw new CallInputError(`${path} is not JSON-safe`);
  if (seen.has(value)) throw new CallInputError(`${path} is circular`);
  seen.add(value);
  if (Array.isArray(value)) {
    value.forEach((entry, index) => assertJsonSafe(entry, seen, `${path}[${index}]`));
  } else {
    for (const [key, entry] of Object.entries(value)) {
      if (key === 'toJSON' && typeof entry === 'function') continue;
      assertJsonSafe(entry, seen, `${path}.${key}`);
    }
  }
  seen.delete(value);
}

function validateToolInput(toolInput, maxBytes) {
  if (toolInput === null || typeof toolInput !== 'object' || Array.isArray(toolInput)) {
    throw new CallInputError('tool_input must be an object');
  }
  const prototype = Object.getPrototypeOf(toolInput);
  if (prototype !== Object.prototype && prototype !== null) {
    throw new CallInputError('tool_input must be a plain object');
  }
  assertJsonSafe(toolInput, new Set(), 'tool_input');
  let serialized;
  try {
    serialized = JSON.stringify(toolInput);
  } catch {
    throw new CallInputError('tool_input is not JSON-serializable');
  }
  if (textEncoder.encode(serialized).byteLength > maxBytes) {
    throw new CallLimitError('tool_input exceeded the byte limit');
  }
  let snapshot;
  try {
    snapshot = JSON.parse(serialized);
  } catch {
    throw new CallInputError('tool_input is not JSON-serializable');
  }
  if (snapshot === null || typeof snapshot !== 'object' || Array.isArray(snapshot)) {
    throw new CallInputError('tool_input must serialize to an object');
  }
  assertJsonSafe(snapshot, new Set(), 'tool_input_snapshot');
  return snapshot;
}

function validateToolName(toolName) {
  if (typeof toolName !== 'string' || toolName.length === 0) throw new CallInputError('tool_name must be non-empty');
  if (toolName.trim() !== toolName) throw new CallInputError('tool_name must not have surrounding whitespace');
  if (/\s/.test(toolName) || /[\u0000-\u001f\u007f]/.test(toolName)) throw new CallInputError('tool_name contains forbidden whitespace or controls');
}

function validateCallEndpoint(endpoint, transport) {
  const url = validateEndpoint(endpoint, transport);
  const host = url.hostname.replace(/^\[|\]$/g, '');
  if (host !== url.hostname && classifyAddress(host) !== 'PUBLIC') {
    throw new DiscoveryInputError('endpoint address is not public');
  }
  return url;
}

function boundedResponse(response, maxBytes) {
  const declared = Number(response.headers?.get?.('content-length') ?? 0);
  if (Number.isFinite(declared) && declared > maxBytes) throw new CallLimitError('MCP response exceeded the byte limit');
  if (!response.body || typeof response.body.pipeThrough !== 'function') return response;
  let total = 0;
  const boundedBody = response.body.pipeThrough(new TransformStream({
    transform(chunk, controller) {
      const bytes = typeof chunk === 'string' ? textEncoder.encode(chunk) : chunk;
      total += bytes.byteLength;
      if (total > maxBytes) {
        controller.error(new CallLimitError('MCP response exceeded the byte limit'));
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

function combineSignals(signals) {
  const active = signals.filter(Boolean);
  if (active.length === 0) return undefined;
  if (typeof AbortSignal.any === 'function') return AbortSignal.any(active);
  const controller = new AbortController();
  const abort = () => controller.abort();
  active.forEach((signal) => (signal.aborted ? abort() : signal.addEventListener('abort', abort, { once: true })));
  return controller.signal;
}

function completeCallToolResult(value) {
  if (!value || typeof value !== 'object' || !Array.isArray(value.content)) return false;
  if (Object.hasOwn(value, 'isError') && typeof value.isError !== 'boolean') return false;
  try {
    assertJsonSafe(value, new Set(), 'call_result');
    const serialized = JSON.stringify(value);
    return typeof serialized === 'string';
  } catch {
    return false;
  }
}

function serializeCallToolResult(value, maxBytes) {
  if (!completeCallToolResult(value)) throw new CallLimitError('MCP call result was malformed or not JSON-safe');
  const serialized = JSON.stringify(value);
  if (textEncoder.encode(serialized).byteLength > maxBytes) throw new CallLimitError('MCP call result exceeded the byte limit');
  return JSON.parse(serialized);
}

function localError(code, summary, limits) {
  return { code, summary: errorSummary(summary, limits.maxErrorSummaryBytes) };
}

function classifyPreCallError(error, limits) {
  if (error instanceof CallAuthError) return localError(error.code, error.message, limits);
  if (error instanceof CallLimitError || error instanceof DiscoveryLimitError) return localError('LIMIT_EXCEEDED', 'MCP transport limit exceeded', limits);
  if (error instanceof CallTimeoutError || error instanceof DiscoveryTimeoutError || error?.name === 'AbortError') return localError('TIMEOUT', 'MCP transport timed out before the tool call', limits);
  if (error instanceof CallInputError || error instanceof DiscoveryInputError) return localError('INPUT_INVALID', error.message, limits);
  const status = Number(error?.code ?? error?.status ?? 0);
  if (status === 401 || status === 403) return localError('AUTH_REQUIRED', 'MCP server requires authentication', limits);
  if (REDIRECT_STATUS.has(status)) return localError('REDIRECT_DENIED', 'MCP redirects are not permitted', limits);
  return localError('NOT_INVOKED', 'MCP connection failed before the tool call', limits);
}

function classifyCallError(error, limits) {
  if (error instanceof CallTimeoutError || error instanceof DiscoveryTimeoutError || error?.name === 'AbortError') return localError('TIMEOUT', 'MCP tool call outcome is unknown after timeout', limits);
  if (error instanceof CallLimitError || error instanceof DiscoveryLimitError) return localError('LIMIT_EXCEEDED', 'MCP tool call outcome is unknown after a transport limit', limits);
  return localError('OUTCOME_UNKNOWN', 'MCP tool call outcome is unknown after the call began', limits);
}

function skeleton(diagnostics) {
  return { status: CALL_OUTCOME.NOT_INVOKED, result: null, error: null, diagnostics };
}

export async function invokeExternalMcp({
  endpoint,
  transport = SUPPORTED_TRANSPORT,
  tool_name: toolName,
  tool_input: toolInput,
  auth = null,
  limits: requestedLimits,
  fetchImpl,
  resolver,
} = {}) {
  let limits;
  const diagnostics = {
    phase: 'PRE_CALL',
    request_count: 0,
    call_started: false,
    call_result_received: false,
    call_tool_count: 0,
  };
  try {
    limits = mergedLimits(requestedLimits);
    validateToolName(toolName);
    const url = validateCallEndpoint(endpoint, transport);
    const toolInputSnapshot = validateToolInput(toolInput, limits.maxInputBytes);
    const authBinding = validateAuth(auth);
    const result = skeleton(diagnostics);
    const dispatcher = createSafeDispatcher({ resolver });
    let requestIndex = 0;
    const fetcher = async (input, init = {}) => {
      requestIndex += 1;
      diagnostics.request_count = requestIndex;
      const timeoutMs = requestIndex === 1 ? limits.connectTimeoutMs : limits.requestTimeoutMs;
      const requestController = new AbortController();
      const signal = combineSignals([init.signal, requestController.signal, overallController.signal]);
      let requestTimer;
      try {
        const request = Promise.resolve((fetchImpl ?? globalThis.fetch)(input, {
          ...init,
          headers: requestHeaders(init.headers, authBinding),
          redirect: 'error',
          signal,
          ...(dispatcher ? { dispatcher } : {}),
        }));
        const timeout = new Promise((_, reject) => {
          requestTimer = setTimeout(() => {
            requestController.abort();
            reject(new CallTimeoutError('MCP request timed out'));
          }, timeoutMs);
        });
        const response = await Promise.race([request, timeout]);
        if (!response || typeof response.body === 'undefined') throw new CallInputError('fetch returned an invalid response');
        return boundedResponse(response, limits.maxResponseBytes);
      } finally {
        clearTimeout(requestTimer);
      }
    };
    const client = new Client({ name: 'hayagarden-external-call', version: '1.0.0' });
    const sdkTransport = new StreamableHTTPClientTransport(url, {
      fetch: fetcher,
      requestInit: { redirect: 'error' },
      reconnectionOptions: { maxRetries: 0, initialReconnectionDelay: 0, maxReconnectionDelay: 0 },
    });
    const overallController = new AbortController();
    let overallTimer;
    try {
      const overallTimeout = new Promise((_, reject) => {
        overallTimer = setTimeout(() => {
          overallController.abort();
          reject(new CallTimeoutError('MCP overall timeout'));
        }, limits.overallTimeoutMs);
      });
      await Promise.race([client.connect(sdkTransport), overallTimeout]);
      diagnostics.phase = 'CALL';
      diagnostics.call_started = true;
      diagnostics.call_tool_count += 1;
      const callResult = await Promise.race([client.callTool({ name: toolName, arguments: toolInputSnapshot }), overallTimeout]);
      if (authBinding?.credential && containsCredential(callResult, authBinding.credential)) throw new CallReflectionError();
      const safeResult = serializeCallToolResult(callResult, limits.maxResultBytes);
      diagnostics.call_result_received = true;
      result.status = safeResult.isError === true ? CALL_OUTCOME.TOOL_ERROR : CALL_OUTCOME.SUCCESS;
      result.result = safeResult;
      return result;
    } catch (error) {
      if (diagnostics.call_started) {
        result.status = CALL_OUTCOME.OUTCOME_UNKNOWN;
        result.error = authBinding?.credential && (error instanceof CallReflectionError || containsCredential(error, authBinding.credential))
          ? localError('SECRET_REFLECTION_BLOCKED', 'provider material was suppressed', limits)
          : classifyCallError(error, limits);
      } else {
        result.status = CALL_OUTCOME.NOT_INVOKED;
        result.error = classifyPreCallError(error, limits);
      }
      return result;
    } finally {
      clearTimeout(overallTimer);
      overallController.abort();
      await client.close().catch(() => undefined);
      await dispatcher?.close().catch(() => undefined);
    }
  } catch (error) {
    limits ??= { ...CALL_DEFAULT_LIMITS, ...(requestedLimits ?? {}) };
    const result = skeleton(diagnostics);
    result.error = classifyPreCallError(error, limits);
    return result;
  }
}

export { SUPPORTED_TRANSPORT };
