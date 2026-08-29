import { pathToFileURL } from 'node:url';

import { discoverExternalMcp } from './external_mcp_discovery_transport.mjs';

export const BRIDGE_VERSION = 1;
export const MAX_BRIDGE_INPUT_BYTES = 16 * 1024;
export const MAX_BRIDGE_STDERR_BYTES = 4 * 1024;

class BridgeEnvelopeError extends Error {}

function boundedDiagnostic(value) {
  return String(value).replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, MAX_BRIDGE_STDERR_BYTES);
}

function parseEnvelope(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new BridgeEnvelopeError('bridge input must be an object');
  }
  const keys = Object.keys(value).sort();
  if (value.bridge_version !== BRIDGE_VERSION) {
    throw new BridgeEnvelopeError('bridge input version is unsupported');
  }
  if (keys.some((key) => !['bridge_version', 'endpoint', 'transport', 'auth'].includes(key))) {
    throw new BridgeEnvelopeError('bridge input contains unsupported fields');
  }
  if (typeof value.endpoint !== 'string' || value.endpoint.length === 0) {
    throw new BridgeEnvelopeError('bridge endpoint is required');
  }
  if (value.transport !== 'streamable_http') {
    throw new BridgeEnvelopeError('bridge transport is unsupported');
  }
  if (!Object.hasOwn(value, 'auth')) return { endpoint: value.endpoint, transport: value.transport, auth: null };
  if (value.auth !== null && (
    !value.auth || typeof value.auth !== 'object' || Array.isArray(value.auth) ||
    Object.getPrototypeOf(value.auth) !== Object.prototype ||
    Object.keys(value.auth).sort().join(',') !== 'credential,scheme' ||
    value.auth.scheme !== 'bearer' || typeof value.auth.credential !== 'string' ||
    value.auth.credential.length === 0 ||
    new TextEncoder().encode(value.auth.credential).byteLength > 16 * 1024
  )) throw new BridgeEnvelopeError('bridge auth is unsupported', 'AUTH_SCHEME_UNSUPPORTED');
  return { endpoint: value.endpoint, transport: value.transport, auth: value.auth };
}

export async function executeBridgeEnvelope(input, { discover = discoverExternalMcp } = {}) {
  const envelope = parseEnvelope(input);
  const result = await discover(envelope);
  if (!result || typeof result !== 'object' || Array.isArray(result)) {
    throw new BridgeEnvelopeError('discovery returned an invalid result');
  }
  for (const field of ['status', 'catalog_complete', 'zero_tools', 'tools', 'diagnostics', 'error']) {
    if (!(field in result)) throw new BridgeEnvelopeError('discovery result schema is incomplete');
  }
  if (!Array.isArray(result.tools) || !result.diagnostics || typeof result.diagnostics !== 'object') {
    throw new BridgeEnvelopeError('discovery result schema is invalid');
  }
  return { ...result, bridge_version: BRIDGE_VERSION };
}

async function readBoundedInput(stream) {
  const chunks = [];
  let total = 0;
  for await (const chunk of stream) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    total += bytes.byteLength;
    if (total > MAX_BRIDGE_INPUT_BYTES) throw new BridgeEnvelopeError('bridge input exceeded the byte limit');
    chunks.push(bytes);
  }
  return Buffer.concat(chunks).toString('utf8');
}

async function main() {
  const inputText = await readBoundedInput(process.stdin);
  let input;
  try {
    input = JSON.parse(inputText);
  } catch {
    throw new BridgeEnvelopeError('bridge input was malformed JSON');
  }
  const output = await executeBridgeEnvelope(input);
  process.stdout.write(JSON.stringify(output));
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  main().catch((error) => {
    process.stderr.write(`${boundedDiagnostic(error?.message || 'local discovery bridge failed')}\n`);
    process.exitCode = 1;
  });
}

