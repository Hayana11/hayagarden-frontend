/**
 * Contract: rewrite finalize retries once on effects_pending OR transport-ambiguous
 * failure (network reject / 5xx / 408), always with the same rewrite_id.
 * Never retries 400 / 404 / 409 (stale).
 */
import assert from 'node:assert/strict';
import {
  editFinalize,
  isRetryableRewriteFinalizeFailure,
  regenFinalize,
  runRewriteFinalizeWithRetry,
} from '../src/lib/api.ts';
import { HttpError } from '../src/lib/http.ts';

assert.equal(isRetryableRewriteFinalizeFailure(new TypeError('Failed to fetch')), true);
assert.equal(isRetryableRewriteFinalizeFailure(new Error('network')), true);
assert.equal(isRetryableRewriteFinalizeFailure(new HttpError(500, '', 'boom')), true);
assert.equal(isRetryableRewriteFinalizeFailure(new HttpError(503, '', 'boom')), true);
assert.equal(isRetryableRewriteFinalizeFailure(new HttpError(408, '', 'timeout')), true);
assert.equal(isRetryableRewriteFinalizeFailure(new HttpError(400, '', 'bad')), false);
assert.equal(isRetryableRewriteFinalizeFailure(new HttpError(404, '', 'missing')), false);
assert.equal(isRetryableRewriteFinalizeFailure(new HttpError(409, '', 'stale')), false);

// Shared helper: transport-ambiguous first attempt → one retry
{
  let calls = 0;
  const out = await runRewriteFinalizeWithRetry(async () => {
    calls += 1;
    if (calls === 1) return { status: 'retryable_error' };
    return { status: 'ok', effectsPending: false, value: { n: calls } };
  });
  assert.equal(calls, 2);
  assert.equal(out.status, 'ok');
  assert.equal(out.value.n, 2);
}

// Shared helper: effects_pending → one retry
{
  let calls = 0;
  const out = await runRewriteFinalizeWithRetry(async () => {
    calls += 1;
    if (calls === 1) return { status: 'ok', effectsPending: true, value: { n: 1 } };
    return { status: 'ok', effectsPending: false, value: { n: 2 } };
  });
  assert.equal(calls, 2);
  assert.equal(out.status, 'ok');
  assert.equal(out.effectsPending, false);
}

// Shared helper: fatal (e.g. stale) → no retry
{
  let calls = 0;
  const out = await runRewriteFinalizeWithRetry(async () => {
    calls += 1;
    return { status: 'fatal_error' };
  });
  assert.equal(calls, 1);
  assert.equal(out.status, 'fatal_error');
}

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

async function withMockFetch(handler, fn) {
  const prev = globalThis.fetch;
  globalThis.fetch = handler;
  try {
    return await fn();
  } finally {
    globalThis.fetch = prev;
  }
}

// editFinalize: first transport reject → second success, same rewrite_id twice
{
  const rewriteId = 'edit-rw-transport-1';
  const seen = [];
  await withMockFetch(async (_url, init) => {
    const body = JSON.parse(String(init.body));
    seen.push(body.rewrite_id);
    if (seen.length === 1) throw new TypeError('Failed to fetch');
    return jsonResponse(200, {
      ok: true,
      message_id: 11,
      assistant_message_id: 12,
    });
  }, async () => {
    const r = await editFinalize(rewriteId);
    assert.deepEqual(seen, [rewriteId, rewriteId]);
    assert.equal(r.ok, true);
    assert.equal(r.messageId, 11);
    assert.equal(r.effectsPending, false);
  });
}

// regenFinalize: first 503 → second success, same rewrite_id twice
{
  const rewriteId = 'regen-rw-5xx-1';
  const seen = [];
  await withMockFetch(async (_url, init) => {
    const body = JSON.parse(String(init.body));
    seen.push(body.rewrite_id);
    if (seen.length === 1) {
      return jsonResponse(503, { ok: false, error: 'worker down' });
    }
    return jsonResponse(200, { ok: true, branch_idx: 1, total: 2 });
  }, async () => {
    const r = await regenFinalize(rewriteId);
    assert.deepEqual(seen, [rewriteId, rewriteId]);
    assert.ok(r);
    assert.equal(r.branchIdx, 1);
    assert.equal(r.effectsPending, false);
  });
}

// editFinalize: 409 stale → no retry
{
  const rewriteId = 'edit-rw-stale-1';
  let calls = 0;
  await withMockFetch(async () => {
    calls += 1;
    return jsonResponse(409, { ok: false, error: 'stale', code: 'stale' });
  }, async () => {
    const r = await editFinalize(rewriteId);
    assert.equal(calls, 1);
    assert.equal(r.ok, false);
  });
}

// legacy surface: finalizeRewriteWithRetry must exist and gate retryable statuses
{
  const fs = await import('node:fs');
  const html = fs.readFileSync(new URL('../../static/chat.html', import.meta.url), 'utf8');
  assert.match(html, /async function finalizeRewriteWithRetry\(url, rewriteId\)/);
  assert.match(html, /status === 408 \|\| \(status >= 500 && status <= 599\)/);
  assert.match(html, /finalizeRewriteWithRetry\('\/api\/chat\/regen\/finalize'/);
  assert.match(html, /finalizeRewriteWithRetry\('\/api\/chat\/edit\/finalize'/);
  assert.doesNotMatch(
    html,
    /if\(fin && \(fin\.effects_pending \|\| fin\.code==='effects_pending'\)\) fin = await _/,
  );
}

console.log('test-rewrite-finalize-retry: ok');
