import { pathToFileURL } from 'node:url';

import { CALL_OUTCOME, invokeExternalMcp } from './external_mcp_call_transport.mjs';

export const BRIDGE_VERSION = 1;
export const MAX_BRIDGE_INPUT_BYTES = 16 * 1024;
export const MAX_BRIDGE_OUTPUT_BYTES = 256 * 1024;
export const MAX_BRIDGE_STDERR_BYTES = 4 * 1024;

class BridgeEnvelopeError extends Error {
  constructor(message, code = 'CALL_BRIDGE_INPUT_INVALID') {
    super(message);
    this.code = code;
  }
}

function boundedDiagnostic(value) {
  return String(value).replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, MAX_BRIDGE_STDERR_BYTES);
}

function containsCredential(value, credential, seen = new Set()) {
  if (!credential || value === null || typeof value === 'undefined') return false;
  if (typeof value === 'string') return value.includes(credential);
  if (typeof value !== 'object') return false;
  if (seen.has(value)) return false;
  seen.add(value);
  if (value instanceof Error && (String(value.message).includes(credential) || String(value.stack || '').includes(credential))) return true;
  return Object.keys(value).some((key) => containsCredential(value[key], credential, seen));
}

function parseAuth(auth) {
  if (auth === null) return null;
  if (!auth || typeof auth !== 'object' || Array.isArray(auth) ||
      Object.getPrototypeOf(auth) !== Object.prototype ||
      Object.keys(auth).sort().join(',') !== 'credential,scheme' ||
      auth.scheme !== 'bearer' || typeof auth.credential !== 'string' || auth.credential.length === 0 ||
      new TextEncoder().encode(auth.credential).byteLength > 16 * 1024) {
    throw new BridgeEnvelopeError('call auth is unsupported', 'AUTH_SCHEME_UNSUPPORTED');
  }
  return { scheme: 'bearer', credential: auth.credential };
}

function parseEnvelope(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new BridgeEnvelopeError('bridge input must be an object');
  }
  const keys = Object.keys(value).sort();
  const expected = ['auth', 'bridge_version', 'endpoint', 'tool_input', 'tool_name', 'transport'];
  if (value.bridge_version !== BRIDGE_VERSION) throw new BridgeEnvelopeError('bridge input version is unsupported');
  if (keys.join(',') !== expected.join(',')) throw new BridgeEnvelopeError('bridge input contains unsupported fields');
  if (typeof value.endpoint !== 'string' || value.endpoint.length === 0) throw new BridgeEnvelopeError('bridge endpoint is required');
  if (value.transport !== 'streamable_http') throw new BridgeEnvelopeError('bridge transport is unsupported');
  if (typeof value.tool_name !== 'string' || value.tool_name.length === 0) throw new BridgeEnvelopeError('bridge tool_name is required');
  if (!value.tool_input || typeof value.tool_input !== 'object' || Array.isArray(value.tool_input)) throw new BridgeEnvelopeError('bridge tool_input is invalid');
  return {
    endpoint: value.endpoint,
    transport: value.transport,
    tool_name: value.tool_name,
    tool_input: value.tool_input,
    auth: parseAuth(value.auth),
  };
}

function reflectedResult(auth) {
  return {
    status: CALL_OUTCOME.OUTCOME_UNKNOWN,
    result: null,
    error: { code: 'SECRET_REFLECTION_BLOCKED', summary: 'provider material was suppressed' },
    diagnostics: { phase: 'CALL', request_count: 0, call_started: true, call_result_received: false, call_tool_count: 1 },
    bridge_version: BRIDGE_VERSION,
  };
}

function boundedOutput(value) {
  const serialized = JSON.stringify(value);
  if (new TextEncoder().encode(serialized).byteLength > MAX_BRIDGE_OUTPUT_BYTES) {
    throw new BridgeEnvelopeError('bridge output exceeded the byte limit', 'OUTPUT_LIMIT_EXCEEDED');
  }
  return serialized;
}

export async function executeBridgeEnvelope(input, { invoke = invokeExternalMcp } = {}) {
  const envelope = parseEnvelope(input);
  let result;
  try {
    result = await invoke(envelope);
  } catch (error) {
    if (envelope.auth?.credential && containsCredential(error, envelope.auth.credential)) return reflectedResult(envelope.auth);
    throw new BridgeEnvelopeError('call transport failed', 'CALL_BRIDGE_FAILED');
  }
  if (envelope.auth?.credential && containsCredential(result, envelope.auth.credential)) return reflectedResult(envelope.auth);
  if (!result || typeof result !== 'object' || Array.isArray(result)) throw new BridgeEnvelopeError('call returned an invalid result', 'CALL_BRIDGE_FAILED');
  for (const field of ['status', 'result', 'error', 'diagnostics']) {
    if (!(field in result)) throw new BridgeEnvelopeError('call result schema is incomplete', 'CALL_BRIDGE_FAILED');
  }
  if (!result.diagnostics || typeof result.diagnostics !== 'object') throw new BridgeEnvelopeError('call result schema is invalid', 'CALL_BRIDGE_FAILED');
  const output = { ...result, bridge_version: BRIDGE_VERSION };
  boundedOutput(output);
  return output;
}

async function readBoundedInput(stream) {
  const chunks = [];
  let total = 0;
  for await (const chunk of stream) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    total += bytes.byteLength;
    if (total > MAX_BRIDGE_INPUT_BYTES) throw new BridgeEnvelopeError('bridge input exceeded the byte limit', 'INPUT_LIMIT_EXCEEDED');
    chunks.push(bytes);
  }
  return Buffer.concat(chunks).toString('utf8');
}

async function main() {
  const inputText = await readBoundedInput(process.stdin);
  let input;
  try { input = JSON.parse(inputText); } catch { throw new BridgeEnvelopeError('bridge input was malformed JSON'); }
  process.stdout.write(await executeBridgeEnvelope(input).then(boundedOutput));
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  main().catch((error) => {
    process.stderr.write(`${boundedDiagnostic(error?.code || 'CALL_BRIDGE_FAILED')}\n`);
    process.exitCode = 1;
  });
}

