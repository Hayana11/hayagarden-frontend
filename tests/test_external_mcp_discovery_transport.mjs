import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DEFAULT_LIMITS,
  DISCOVERY_STATUS,
  classifyAddress,
  discoverExternalMcp,
  validateEndpoint,
  validateResolvedAddresses,
} from '../tools/external_mcp_discovery_transport.mjs';

function jsonResponse(payload, status = 200) {
  return new Response(payload === undefined ? null : JSON.stringify(payload), {
    status,
    headers: payload === undefined ? {} : { 'content-type': 'application/json' },
  });
}

function fixtureFetch({ pages = [{ tools: [] }], statusFor = {}, calls = [] } = {}) {
  return async (_url, init) => {
    const body = JSON.parse(init.body);
    calls.push({ method: body.method, headers: init.headers, redirect: init.redirect, signal: init.signal });
    if (body.method === 'initialize') {
      return jsonResponse({ jsonrpc: '2.0', id: body.id, result: {
        protocolVersion: '2025-06-18',
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: 'fixture', version: '1.0.0' },
      } });
    }
    if (body.method === 'notifications/initialized') return jsonResponse(undefined, 202);
    if (body.method !== 'tools/list') throw new Error(`unexpected MCP method: ${body.method}`);
    const pageIndex = body.params?.cursor ? Number(body.params.cursor) : 0;
    if (statusFor[pageIndex]) return jsonResponse({ error: 'opaque remote body must not escape' }, statusFor[pageIndex]);
    const page = pages[pageIndex];
    if (!page) return jsonResponse({ jsonrpc: '2.0', id: body.id, result: { tools: [] } });
    return jsonResponse({ jsonrpc: '2.0', id: body.id, result: { ...page } });
  };
}

test('real SDK initialize + paginated tools/list captures the complete SDK-visible catalog', async () => {
  const calls = [];
  const result = await discoverExternalMcp({
    endpoint: 'https://public.example.test/mcp',
    fetchImpl: fixtureFetch({
      calls,
      pages: [
        { tools: [{ name: 'one', title: 'One', description: 'first', inputSchema: { type: 'object' }, outputSchema: { type: 'object' }, annotations: { readOnlyHint: true } }], nextCursor: '1' },
        { tools: [{ name: 'two', title: 'Two', description: 'second', inputSchema: { type: 'object' }, outputSchema: { type: 'object' }, annotations: { openWorldHint: false } }] },
      ],
    }),
  });
  assert.equal(result.status, DISCOVERY_STATUS.SUCCESS);
  assert.equal(result.catalog_complete, true);
  assert.equal(result.zero_tools, false);
  assert.equal(result.tools.length, 2);
  assert.deepEqual(result.tools[0].annotations, { readOnlyHint: true });
  assert.equal(result.server_metadata.protocol_version, '2025-06-18');
  assert.deepEqual(calls.map((call) => call.method), ['initialize', 'notifications/initialized', 'tools/list', 'tools/list']);
  assert.ok(calls.every((call) => call.redirect === 'error'));
  assert.ok(calls.every((call) => call.signal instanceof AbortSignal));
});

test('bearer auth is injected for every discovery request and safe dispatch remains present', async () => {
  const calls = [];
  const result = await discoverExternalMcp({
    endpoint: 'https://public.example.test/mcp',
    auth: { scheme: 'bearer', credential: 'M5_B1_CANARY_SECRET_DO_NOT_LEAK_7f13' },
    fetchImpl: fixtureFetch({ calls, pages: [{ tools: [] }, { tools: [] }] }),
  });
  assert.equal(result.status, DISCOVERY_STATUS.SUCCESS);
  assert.ok(calls.length >= 3);
  assert.ok(calls.every((call) => call.headers.get('authorization') === 'Bearer M5_B1_CANARY_SECRET_DO_NOT_LEAK_7f13'));
  const source = await (await import('node:fs/promises')).readFile(new URL('../tools/external_mcp_discovery_transport.mjs', import.meta.url), 'utf8');
  assert.match(source, /createSafeDispatcher\(\{ resolver \}\)/);
  assert.match(source, /headers: requestHeaders\(init\.headers, auth\)/);
});

test('unsupported auth and Authorization conflicts are fail closed', async () => {
  const unsupported = await discoverExternalMcp({ endpoint: 'https://public.example.test/mcp', auth: { scheme: 'basic', credential: 'x' }, fetchImpl: fixtureFetch() });
  assert.equal(unsupported.status, 'AUTH_SCHEME_UNSUPPORTED');
  const source = await (await import('node:fs/promises')).readFile(new URL('../tools/external_mcp_discovery_transport.mjs', import.meta.url), 'utf8');
  assert.match(source, /AUTHORIZATION_HEADER_CONFLICT/);
  assert.match(source, /headers\.has\('authorization'\)/);
});

test('credential reflection in catalog and remote errors suppresses all output', async () => {
  const credential = 'M5_B1_CANARY_SECRET_DO_NOT_LEAK_7f13';
  const reflectedCatalog = await discoverExternalMcp({
    endpoint: 'https://public.example.test/mcp',
    auth: { scheme: 'bearer', credential },
    fetchImpl: fixtureFetch({ pages: [{ tools: [{ name: credential, description: 'reflected' }] }] }),
  });
  assert.equal(reflectedCatalog.status, 'SECRET_REFLECTION_BLOCKED');
  assert.deepEqual(reflectedCatalog.tools, []);
  assert.equal(JSON.stringify(reflectedCatalog).includes(credential), false);
  const reflectedError = await discoverExternalMcp({
    endpoint: 'https://public.example.test/mcp',
    auth: { scheme: 'bearer', credential },
    fetchImpl: async (_url, init) => {
      const body = JSON.parse(init.body);
      if (body.method === 'initialize') return jsonResponse({ jsonrpc: '2.0', id: body.id, result: { protocolVersion: '2025-06-18', capabilities: {}, serverInfo: {} } });
      if (body.method === 'notifications/initialized') return jsonResponse(undefined, 202);
      throw new Error(`remote error ${credential}`);
    },
  });
  assert.equal(reflectedError.status, 'SECRET_REFLECTION_BLOCKED');
  assert.equal(JSON.stringify(reflectedError).includes(credential), false);
});

test('successful empty catalog is distinct from a failed discovery', async () => {
  const success = await discoverExternalMcp({ endpoint: 'https://public.example.test/mcp', fetchImpl: fixtureFetch() });
  assert.equal(success.status, DISCOVERY_STATUS.SUCCESS);
  assert.equal(success.zero_tools, true);
  assert.equal(success.catalog_complete, true);

  const failed = await discoverExternalMcp({ endpoint: 'https://public.example.test/mcp', fetchImpl: fixtureFetch({ statusFor: { 0: 503 } }) });
  assert.equal(failed.status, DISCOVERY_STATUS.UNAVAILABLE);
  assert.equal(failed.catalog_complete, false);
  assert.equal(failed.zero_tools, false);
  assert.equal(failed.tools.length, 0);
  assert.equal(failed.error.summary.includes('opaque remote body'), false);
});

test('auth, redirect, pagination loop, and limits fail closed without retries', async () => {
  const auth = await discoverExternalMcp({ endpoint: 'https://public.example.test/mcp', fetchImpl: fixtureFetch({ statusFor: { 0: 401 } }) });
  assert.equal(auth.status, DISCOVERY_STATUS.AUTH_REQUIRED);

  const redirect = await discoverExternalMcp({ endpoint: 'https://public.example.test/mcp', fetchImpl: fixtureFetch({ statusFor: { 0: 302 } }) });
  assert.equal(redirect.status, DISCOVERY_STATUS.PROTOCOL_ERROR);
  assert.match(redirect.error.summary, /redirect/);

  const loop = await discoverExternalMcp({ endpoint: 'https://public.example.test/mcp', fetchImpl: fixtureFetch({ pages: [{ tools: [], nextCursor: '0' }] }) });
  assert.equal(loop.status, DISCOVERY_STATUS.PROTOCOL_ERROR);
  assert.match(loop.error.summary, /cursor loop/);

  const limited = await discoverExternalMcp({ endpoint: 'https://public.example.test/mcp', limits: { maxPages: 1 }, fetchImpl: fixtureFetch({ pages: [{ tools: [], nextCursor: '1' }, { tools: [] }] }) });
  assert.equal(limited.status, DISCOVERY_STATUS.LIMIT_EXCEEDED);
  assert.equal(limited.catalog_complete, false);
});

test('HTTPS/transport and egress policy reject unsafe targets and mixed DNS', () => {
  assert.throws(() => validateEndpoint('http://public.example.test/mcp'), /HTTPS/);
  assert.throws(() => validateEndpoint('https://127.0.0.1/mcp'), /public/);
  assert.throws(() => validateEndpoint('https://public.example.test/mcp', 'sse'), /streamable_http/);
  assert.equal(classifyAddress('8.8.8.8'), 'PUBLIC');
  assert.equal(classifyAddress('127.0.0.1'), 'UNSAFE');
  assert.equal(classifyAddress('::1'), 'UNSAFE');
  assert.throws(() => validateResolvedAddresses([{ address: '8.8.8.8', family: 4 }, { address: '10.0.0.1', family: 4 }]), /non-public/);
});

test('production fetch path reaches the real Undici lookup seam before any TCP dial', async () => {
  const resolverCalls = [];
  const result = await discoverExternalMcp({
    endpoint: 'https://mixed.example.test/mcp',
    limits: { connectTimeoutMs: 100, overallTimeoutMs: 500 },
    resolver: async (hostname) => {
      resolverCalls.push(hostname);
      return [
        { address: '8.8.8.8', family: 4 },
        { address: '192.168.1.10', family: 4 },
      ];
    },
  });
  assert.equal(resolverCalls.length, 1);
  assert.equal(resolverCalls[0], 'mixed.example.test');
  assert.notEqual(result.status, DISCOVERY_STATUS.SUCCESS);
  assert.equal(result.catalog_complete, false);
  assert.equal(result.diagnostics.request_count, 1);
});

test('response body is bounded while the SDK consumes the stream', async () => {
  const oversizedBody = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('x'.repeat(128)));
      controller.close();
    },
  });
  const fetchImpl = async (_url, init) => {
    const body = JSON.parse(init.body);
    if (body.method === 'initialize') {
      return jsonResponse({ jsonrpc: '2.0', id: body.id, result: {
        protocolVersion: '2025-06-18', capabilities: {}, serverInfo: { name: 'fixture', version: '1.0.0' },
      } });
    }
    if (body.method === 'notifications/initialized') return jsonResponse(undefined, 202);
    return new Response(oversizedBody, { status: 200, headers: { 'content-type': 'application/json' } });
  };
  const result = await discoverExternalMcp({
    endpoint: 'https://public.example.test/mcp',
    limits: { maxResponseBytes: 16 },
    fetchImpl,
  });
  assert.equal(result.status, DISCOVERY_STATUS.LIMIT_EXCEEDED);
  assert.equal(result.catalog_complete, false);
  assert.equal(result.tools.length, 0);
});

test('request, overall and cleanup paths remain finite with no automatic retry', async () => {
  let closedSignalSeen = false;
  const calls = [];
  const hangingFetch = async (_url, init) => {
    calls.push(init);
    init.signal.addEventListener('abort', () => { closedSignalSeen = true; }, { once: true });
    await new Promise((resolve) => setTimeout(resolve, 50));
    throw new DOMException('aborted', 'AbortError');
  };
  const result = await discoverExternalMcp({ endpoint: 'https://public.example.test/mcp', limits: { connectTimeoutMs: 5, requestTimeoutMs: 5, overallTimeoutMs: 20 }, fetchImpl: hangingFetch });
  assert.equal(result.status, DISCOVERY_STATUS.UNAVAILABLE);
  assert.equal(result.diagnostics.request_count, 1);
  assert.equal(calls.length, 1);
  assert.equal(closedSignalSeen, true);
});

test('production defaults do not access secret material or expose execution methods', async () => {
  assert.equal(DEFAULT_LIMITS.maxPages, 32);
  assert.equal(DEFAULT_LIMITS.maxTools, 1000);
  assert.equal(DEFAULT_LIMITS.maxCatalogBytes, 2 * 1024 * 1024);
});
