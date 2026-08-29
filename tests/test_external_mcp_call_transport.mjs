import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  CALL_DEFAULT_LIMITS,
  CALL_OUTCOME,
  invokeExternalMcp,
  SUPPORTED_TRANSPORT,
} from '../tools/external_mcp_call_transport.mjs';

const PRODUCT_PATH = new URL('../tools/external_mcp_call_transport.mjs', import.meta.url);
const ENDPOINT = 'https://public.example.test/mcp';

function jsonResponse(payload, status = 200) {
  return new Response(payload === undefined ? null : JSON.stringify(payload), {
    status,
    headers: payload === undefined ? {} : { 'content-type': 'application/json' },
  });
}

function initializeResponse(id) {
  return jsonResponse({ jsonrpc: '2.0', id, result: {
    protocolVersion: '2025-06-18',
    capabilities: { tools: {} },
    serverInfo: { name: 'fixture', version: '1.0.0' },
  } });
}

function callResult(text = 'ok', isError) {
  return { content: [{ type: 'text', text }], ...(isError === undefined ? {} : { isError }) };
}

function fixtureFetch({ calls = [], result = callResult(), onCall, onInitialize, connectError, connectDelayMs = 0 } = {}) {
  return async (_url, init) => {
    const body = JSON.parse(init.body);
    calls.push({ method: body.method, params: body.params, headers: init.headers, redirect: init.redirect, signal: init.signal });
    if (body.method === 'initialize') {
      if (connectError) throw connectError;
      if (connectDelayMs) await new Promise((resolve) => setTimeout(resolve, connectDelayMs));
      onInitialize?.();
      return initializeResponse(body.id);
    }
    if (body.method === 'notifications/initialized') return jsonResponse(undefined, 202);
    if (body.method !== 'tools/call') throw new Error(`unexpected MCP method: ${body.method}`);
    if (onCall) return onCall(body, init);
    return jsonResponse({ jsonrpc: '2.0', id: body.id, result });
  };
}

async function invoke(options = {}) {
  return invokeExternalMcp({
    endpoint: ENDPOINT,
    transport: SUPPORTED_TRANSPORT,
    tool_name: 'echo',
    tool_input: { value: 'hello' },
    fetchImpl: fixtureFetch(options),
    ...options,
  });
}

function callCount(calls) {
  return calls.filter((call) => call.method === 'tools/call').length;
}

test('real SDK Client and StreamableHTTPClientTransport return a complete SUCCESS result', async () => {
  const calls = [];
  const result = await invoke({ calls, result: callResult('hello') });
  assert.equal(result.status, CALL_OUTCOME.SUCCESS);
  assert.deepEqual(result.result, callResult('hello'));
  assert.equal(result.error, null);
  assert.equal(result.diagnostics.phase, 'CALL');
  assert.equal(result.diagnostics.call_started, true);
  assert.equal(result.diagnostics.call_result_received, true);
  assert.equal(result.diagnostics.call_tool_count, 1);
  assert.equal(callCount(calls), 1);
  assert.deepEqual(calls.map((call) => call.method), ['initialize', 'notifications/initialized', 'tools/call']);
  assert.ok(calls.every((call) => call.redirect === 'error'));
  assert.ok(calls.every((call) => call.signal instanceof AbortSignal));
});

test('bearer auth is injected by the transport and keeps the safe dispatcher path', async () => {
  const calls = [];
  const result = await invoke({ calls, auth: { scheme: 'bearer', credential: 'M5_B1_CANARY_SECRET_DO_NOT_LEAK_7f13' } });
  assert.equal(result.status, CALL_OUTCOME.SUCCESS);
  assert.equal(calls[0].headers.get('authorization'), 'Bearer M5_B1_CANARY_SECRET_DO_NOT_LEAK_7f13');
  assert.equal(calls.filter((call) => call.headers.get('authorization')).length, 3);
  const source = await readFile(PRODUCT_PATH, 'utf8');
  assert.match(source, /createSafeDispatcher\(\{ resolver \}\)/);
  assert.match(source, /headers: requestHeaders\(init\.headers, authBinding\)/);
});

test('unsupported auth and caller Authorization header conflicts fail closed', async () => {
  const unsupported = await invoke({ auth: { scheme: 'basic', credential: 'x' } });
  assert.equal(unsupported.status, CALL_OUTCOME.NOT_INVOKED);
  assert.equal(unsupported.error.code, 'AUTH_SCHEME_UNSUPPORTED');
  const source = await readFile(PRODUCT_PATH, 'utf8');
  assert.match(source, /AUTHORIZATION_HEADER_CONFLICT/);
  assert.match(source, /headers\.has\('authorization'\)/);
});

test('credential reflection in successful result is wholly suppressed', async () => {
  const credential = 'M5_B1_CANARY_SECRET_DO_NOT_LEAK_7f13';
  const result = await invoke({ auth: { scheme: 'bearer', credential }, result: callResult(credential) });
  assert.equal(result.status, CALL_OUTCOME.OUTCOME_UNKNOWN);
  assert.equal(result.result, null);
  assert.equal(result.error.code, 'SECRET_REFLECTION_BLOCKED');
  assert.equal(JSON.stringify(result).includes(credential), false);
});

test('credential reflection in tool error and thrown error is blocked', async () => {
  const credential = 'M5_B1_CANARY_SECRET_DO_NOT_LEAK_7f13';
  for (const options of [
    { result: callResult(credential, true) },
    { onCall: () => { throw new Error(`remote failure ${credential}`); } },
  ]) {
    const result = await invoke({ auth: { scheme: 'bearer', credential }, ...options });
    assert.equal(result.status, CALL_OUTCOME.OUTCOME_UNKNOWN);
    assert.equal(result.result, null);
    assert.equal(result.error.code, 'SECRET_REFLECTION_BLOCKED');
    assert.equal(JSON.stringify(result).includes(credential), false);
  }
});

test('isError=true is a determinate TOOL_ERROR and never retries', async () => {
  const calls = [];
  const result = await invoke({ calls, result: callResult('remote tool rejected', true) });
  assert.equal(result.status, CALL_OUTCOME.TOOL_ERROR);
  assert.equal(result.result.isError, true);
  assert.equal(result.diagnostics.call_tool_count, 1);
  assert.equal(callCount(calls), 1);
});

test('the SDK-visible result structure is preserved without text flattening or follow-up fetches', async () => {
  const calls = [];
  const sdkResult = {
    content: [{ type: 'text', text: 'one' }, { type: 'text', text: 'two' }],
    isError: false,
  };
  const result = await invoke({ calls, result: sdkResult });
  assert.deepEqual(result.result, sdkResult);
  assert.equal(calls.length, 3);
  assert.equal(calls.some((call) => call.method === 'resources/read'), false);
  assert.equal(calls.some((call) => call.method === 'tools/list'), false);
});

test('empty tool name is NOT_INVOKED', async () => {
  const result = await invoke({ tool_name: '' });
  assert.equal(result.status, CALL_OUTCOME.NOT_INVOKED);
  assert.equal(result.diagnostics.call_started, false);
  assert.equal(result.diagnostics.call_tool_count, 0);
});

test('surrounding whitespace in tool name is rejected', async () => {
  const result = await invoke({ tool_name: ' echo' });
  assert.equal(result.status, CALL_OUTCOME.NOT_INVOKED);
  assert.equal(result.diagnostics.call_tool_count, 0);
});

test('whitespace and control characters in tool name are rejected', async () => {
  for (const tool_name of ['echo name', 'echo\nname', 'echo\u0000name']) {
    const result = await invoke({ tool_name });
    assert.equal(result.status, CALL_OUTCOME.NOT_INVOKED);
    assert.equal(result.diagnostics.call_tool_count, 0);
  }
});

test('tool_input must have object semantics', async () => {
  for (const tool_input of [null, [], 'text', 7]) {
    const result = await invoke({ tool_input });
    assert.equal(result.status, CALL_OUTCOME.NOT_INVOKED);
    assert.equal(result.diagnostics.call_tool_count, 0);
  }
});

test('NaN and Infinity are rejected instead of becoming JSON null', async () => {
  for (const tool_input of [{ value: Number.NaN }, { value: Number.POSITIVE_INFINITY }]) {
    const result = await invoke({ tool_input });
    assert.equal(result.status, CALL_OUTCOME.NOT_INVOKED);
    assert.match(result.error.summary, /finite numbers/);
  }
});

test('oversized and non-serializable input is rejected before connect', async () => {
  const oversized = await invoke({ tool_input: { value: 'x'.repeat(300), }, limits: { maxInputBytes: 32 } });
  assert.equal(oversized.status, CALL_OUTCOME.NOT_INVOKED);
  assert.equal(oversized.error.summary, 'MCP transport limit exceeded');
  const circular = {};
  circular.self = circular;
  const invalid = await invoke({ tool_input: circular });
  assert.equal(invalid.status, CALL_OUTCOME.NOT_INVOKED);
  assert.match(invalid.error.summary, /circular/);
});

test('tool_input is snapshotted before connect and later caller mutation cannot change wire arguments', async () => {
  const calls = [];
  const tool_input = { value: 'before-connect' };
  const result = await invoke({
    calls,
    tool_input,
    onInitialize: () => { tool_input.value = 'x'.repeat(300 * 1024); },
  });
  assert.equal(result.status, CALL_OUTCOME.SUCCESS);
  const call = calls.find((entry) => entry.method === 'tools/call');
  assert.deepEqual(call.params.arguments, { value: 'before-connect' });
});

test('plain-object custom toJSON is evaluated once and the parsed snapshot is sent', async () => {
  const calls = [];
  let serializationCount = 0;
  const tool_input = {
    value: 'stable',
    toJSON() {
      serializationCount += 1;
      return serializationCount === 1 ? { value: 'stable' } : { value: 'x'.repeat(300 * 1024) };
    },
  };
  const result = await invoke({ calls, tool_input });
  assert.equal(result.status, CALL_OUTCOME.SUCCESS);
  assert.equal(serializationCount, 1);
  const call = calls.find((entry) => entry.method === 'tools/call');
  assert.deepEqual(call.params.arguments, { value: 'stable' });
});

test('non-plain-object tool_input is rejected before any MCP request', async () => {
  class ToolInput {
    constructor() { this.value = 'not-plain'; }
  }
  const calls = [];
  const result = await invoke({ calls, tool_input: new ToolInput() });
  assert.equal(result.status, CALL_OUTCOME.NOT_INVOKED);
  assert.match(result.error.summary, /plain object/);
  assert.equal(calls.length, 0);
});

test('HTTP endpoint is rejected by the shared endpoint authority', async () => {
  const result = await invoke({ endpoint: 'http://public.example.test/mcp' });
  assert.equal(result.status, CALL_OUTCOME.NOT_INVOKED);
  assert.match(result.error.summary, /HTTPS/);
});

test('unsupported transport is rejected by the shared endpoint authority', async () => {
  const result = await invoke({ transport: 'sse' });
  assert.equal(result.status, CALL_OUTCOME.NOT_INVOKED);
  assert.match(result.error.summary, /streamable_http/);
});

test('userinfo, query, and fragment endpoint material are rejected', async () => {
  for (const endpoint of [
    'https://user:pass@public.example.test/mcp',
    'https://public.example.test/mcp?token=literal',
    'https://public.example.test/mcp#fragment',
  ]) {
    const result = await invoke({ endpoint });
    assert.equal(result.status, CALL_OUTCOME.NOT_INVOKED);
  }
});

test('literal loopback and private IPv4/IPv6 endpoints are rejected before fetch', async () => {
  for (const endpoint of ['https://127.0.0.1/mcp', 'https://10.0.0.1/mcp', 'https://[::1]/mcp', 'https://[fd00::1]/mcp']) {
    const result = await invoke({ endpoint });
    assert.equal(result.status, CALL_OUTCOME.NOT_INVOKED);
  }
});

test('mixed DNS records fail closed in the actual safe dispatcher lookup seam', async () => {
  const resolverCalls = [];
  const result = await invokeExternalMcp({
    endpoint: ENDPOINT,
    tool_name: 'echo',
    tool_input: {},
    resolver: async (hostname) => {
      resolverCalls.push(hostname);
      return [{ address: '8.8.8.8', family: 4 }, { address: '192.168.1.5', family: 4 }];
    },
    limits: { connectTimeoutMs: 100, requestTimeoutMs: 100, overallTimeoutMs: 300 },
  });
  assert.equal(result.status, CALL_OUTCOME.NOT_INVOKED);
  assert.equal(result.diagnostics.call_started, false);
  assert.deepEqual(resolverCalls, ['public.example.test']);
  assert.equal(result.diagnostics.call_tool_count, 0);
});

test('redirect responses are denied and cannot start a tool call', async () => {
  const calls = [];
  const result = await invoke({
    calls,
    onCall: async () => new Response(null, { status: 302, headers: { location: 'https://other.example.test/mcp' } }),
  });
  assert.equal(result.status, CALL_OUTCOME.OUTCOME_UNKNOWN);
  assert.equal(result.diagnostics.call_started, true);
  assert.equal(callCount(calls), 1);
});

test('connect failure is NOT_INVOKED and has zero callTool attempts', async () => {
  const calls = [];
  const result = await invoke({ calls, connectError: new Error('opaque remote body must not escape') });
  assert.equal(result.status, CALL_OUTCOME.NOT_INVOKED);
  assert.equal(result.diagnostics.call_started, false);
  assert.equal(result.diagnostics.call_tool_count, 0);
  assert.equal(callCount(calls), 0);
  assert.equal(result.error.summary.includes('opaque remote body'), false);
});

test('connect timeout is NOT_INVOKED', async () => {
  const calls = [];
  const result = await invoke({
    calls,
    connectDelayMs: 100,
    limits: { connectTimeoutMs: 5, requestTimeoutMs: 10, overallTimeoutMs: 50 },
  });
  assert.equal(result.status, CALL_OUTCOME.NOT_INVOKED);
  assert.equal(result.diagnostics.call_started, false);
  assert.equal(result.diagnostics.call_tool_count, 0);
});

test('call timeout is OUTCOME_UNKNOWN after exactly one callTool', async () => {
  const calls = [];
  const result = await invoke({
    calls,
    onCall: async (_body, init) => {
      await new Promise((resolve) => setTimeout(resolve, 100));
      if (init.signal.aborted) throw new DOMException('aborted', 'AbortError');
      return jsonResponse({ jsonrpc: '2.0', id: 3, result: callResult() });
    },
    limits: { connectTimeoutMs: 50, requestTimeoutMs: 5, overallTimeoutMs: 50 },
  });
  assert.equal(result.status, CALL_OUTCOME.OUTCOME_UNKNOWN);
  assert.equal(result.diagnostics.call_started, true);
  assert.equal(result.diagnostics.call_result_received, false);
  assert.equal(result.diagnostics.call_tool_count, 1);
  assert.equal(callCount(calls), 1);
});

test('disconnect after callTool is OUTCOME_UNKNOWN without retry', async () => {
  const calls = [];
  const result = await invoke({ calls, onCall: async () => { throw new Error('connection closed'); } });
  assert.equal(result.status, CALL_OUTCOME.OUTCOME_UNKNOWN);
  assert.equal(result.diagnostics.call_tool_count, 1);
  assert.equal(callCount(calls), 1);
});

test('malformed call response is OUTCOME_UNKNOWN, not NOT_INVOKED', async () => {
  const calls = [];
  const result = await invoke({ calls, onCall: async () => new Response('{not-json', { status: 200, headers: { 'content-type': 'application/json' } }) });
  assert.equal(result.status, CALL_OUTCOME.OUTCOME_UNKNOWN, JSON.stringify(result));
  assert.equal(result.diagnostics.call_started, true);
  assert.equal(result.diagnostics.call_result_received, false);
  assert.equal(result.diagnostics.call_tool_count, 1);
});

test('response overflow before call is NOT_INVOKED', async () => {
  const calls = [];
  const oversized = new Response('x'.repeat(1024), { status: 200, headers: { 'content-length': '1024' } });
  const result = await invoke({
    calls,
    onCall: async () => oversized,
    limits: { maxResponseBytes: 512 },
  });
  assert.equal(result.status, CALL_OUTCOME.OUTCOME_UNKNOWN, JSON.stringify(result));
  assert.equal(result.diagnostics.call_started, true);
  assert.equal(result.diagnostics.call_tool_count, 1);
  assert.equal(callCount(calls), 1);
});

test('call response overflow is OUTCOME_UNKNOWN after call begins', async () => {
  const calls = [];
  const result = await invoke({
    calls,
    onCall: async (body) => new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id, result: callResult('x'.repeat(2048)) }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }),
    limits: { maxResponseBytes: 512 },
  });
  assert.equal(result.status, CALL_OUTCOME.OUTCOME_UNKNOWN, JSON.stringify(result));
  assert.equal(result.diagnostics.call_started, true);
  assert.equal(result.diagnostics.call_result_received, false);
  assert.equal(result.diagnostics.call_tool_count, 1);
});

test('result serialization limit is bounded after call begins', async () => {
  const calls = [];
  const result = await invoke({ calls, result: callResult('x'.repeat(128)), limits: { maxResultBytes: 64 } });
  assert.equal(result.status, CALL_OUTCOME.OUTCOME_UNKNOWN);
  assert.equal(result.diagnostics.call_started, true);
  assert.equal(result.diagnostics.call_result_received, false);
  assert.equal(result.diagnostics.call_tool_count, 1);
});

test('overall timeout is finite and yields the phase-appropriate outcome', async () => {
  const calls = [];
  const result = await invoke({
    calls,
    onCall: async () => new Promise(() => {}),
    limits: { connectTimeoutMs: 100, requestTimeoutMs: 100, overallTimeoutMs: 10 },
  });
  assert.equal(result.status, CALL_OUTCOME.OUTCOME_UNKNOWN);
  assert.equal(result.diagnostics.call_tool_count, 1);
});

test('all failure paths maintain CALL_TOOL_COUNT <= 1', async () => {
  const cases = [
    { endpoint: 'http://public.example.test/mcp' },
    { tool_name: '' },
    { connectError: new Error('connect') },
    { result: callResult('x', true) },
    { onCall: async () => { throw new Error('disconnect'); } },
  ];
  for (const options of cases) {
    const calls = [];
    const result = await invoke({ ...options, calls });
    assert.ok(result.diagnostics.call_tool_count <= 1);
    assert.ok(callCount(calls) <= 1);
  }
});

test('default limits are finite and include input, response, result, and summary caps', () => {
  assert.equal(CALL_DEFAULT_LIMITS.connectTimeoutMs, 5_000);
  assert.equal(CALL_DEFAULT_LIMITS.requestTimeoutMs, 5_000);
  assert.equal(CALL_DEFAULT_LIMITS.overallTimeoutMs, 15_000);
  assert.equal(CALL_DEFAULT_LIMITS.maxInputBytes, 256 * 1024);
  assert.equal(CALL_DEFAULT_LIMITS.maxResponseBytes, 2 * 1024 * 1024);
  assert.equal(CALL_DEFAULT_LIMITS.maxResultBytes, 2 * 1024 * 1024);
  assert.equal(CALL_DEFAULT_LIMITS.maxErrorSummaryBytes, 512);
});

test('diagnostics are JSON-safe and never contain the full tool input', async () => {
  const result = await invoke({ tool_input: { secretLikeField: 'do not echo' } });
  assert.doesNotThrow(() => JSON.stringify(result));
  assert.equal(JSON.stringify(result.diagnostics).includes('do not echo'), false);
  assert.equal(Object.hasOwn(result.diagnostics, 'tool_input'), false);
});

test('remote error text is not copied into the bounded local summary', async () => {
  const calls = [];
  const result = await invoke({ calls, connectError: new Error(`opaque ${'x'.repeat(5000)}\u0000 body`) });
  assert.equal(result.status, CALL_OUTCOME.NOT_INVOKED);
  assert.ok(new TextEncoder().encode(result.error.summary).byteLength <= 512);
  assert.equal(/[\u0000-\u001f\u007f]/.test(result.error.summary), false);
  assert.equal(result.error.summary.includes('opaque'), false);
});

test('source contract uses no listTools, no retry, and closes resources without replay', async () => {
  const source = await readFile(PRODUCT_PATH, 'utf8');
  assert.equal(source.includes('.listTools('), false);
  assert.match(source, /call_tool_count\s*\+=\s*1/);
  assert.match(source, /maxRetries:\s*0/);
  assert.match(source, /client\.close\(\)\.catch/);
  assert.match(source, /dispatcher\?\.close\(\)\.catch/);
});

test('source contract has no database, credential, registry, fence, bridge, or public-route dependency', async () => {
  const source = await readFile(PRODUCT_PATH, 'utf8');
  for (const forbidden of [
    'sqlite3', 'subprocess', 'external_server_registry', 'external_tool_registry',
    'external_tool_side_effect_policy', 'external_tool_execution_fence',
    'external_secret_store', 'credential_vault', 'lease_signer', 'public route',
  ]) assert.equal(source.toLowerCase().includes(forbidden), false, forbidden);
});

test('only the shared endpoint and egress authorities are imported', async () => {
  const source = await readFile(PRODUCT_PATH, 'utf8');
  assert.match(source, /validateEndpoint/);
  assert.match(source, /createSafeDispatcher/);
  assert.match(source, /SUPPORTED_TRANSPORT/);
  assert.match(source, /Client/);
  assert.match(source, /StreamableHTTPClientTransport/);
});

