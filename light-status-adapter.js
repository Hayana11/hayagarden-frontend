'use strict';

const DEFAULT_LIGHT_DAEMON_URL = 'http://127.0.0.1:5052';

async function readLightStatus({
  fetchImpl = globalThis.fetch,
  daemonUrl = DEFAULT_LIGHT_DAEMON_URL,
} = {}) {
  try {
    const response = await fetchImpl(daemonUrl + '/light/status', { method: 'GET' });
    const text = await response.text();
    return text || '(no output)';
  } catch (error) {
    return 'Error: ' + error.message;
  }
}

module.exports = {
  DEFAULT_LIGHT_DAEMON_URL,
  readLightStatus,
};
