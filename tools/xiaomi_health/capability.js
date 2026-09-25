'use strict';

const SOURCE = 'xiaomi_fitness_cloud';
const METRICS = Object.freeze({
  steps: Object.freeze(['steps', 'step', 'step_count', 'stepcount', 'total_steps', 'count', 'value']),
  sleep: Object.freeze(['asleep_minutes', 'time_asleep_minutes', 'sleep_minutes', 'total_sleep_minutes', 'sleep_duration', 'total_sleep', 'total_sleep_time', 'duration_minutes', 'duration', 'deep_sleep', 'light_sleep', 'rem_sleep', 'awake_minutes', 'awake_duration', 'sleep_awake_duration', 'sleep_score', 'score']),
  heart_rate: Object.freeze(['bpm', 'heart_rate', 'avg_hrm', 'avg_heart_rate', 'average_heart_rate', 'resting_heart_rate', 'min_heart_rate', 'max_heart_rate']),
});
const UNITS = Object.freeze({ steps: 'steps', sleep: 'minutes', heart_rate: 'bpm' });
const SAFE_ERRORS = new Set(['auth_expired', 'timeout', 'api_error', 'malformed_response', 'unavailable']);
const CYCLE_EVENT_TYPES = new Set(['period_start', 'period_end', 'period_start_end']);
const HP_VALUES = new Set(['little', 'normal', 'much']);
const MOOD_VALUES = new Set(['happy', 'normal', 'uncomfortable']);
const PAIN_VALUES = new Set(['light', 'normal', 'heavy']);
const SERIES_TTL_MS = 15 * 60 * 1000;
const LATEST_TTL_MS = 60 * 1000;

function validDate(value) {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : null;
}

function validSample(value) {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value) ? value : null;
}

function validNumber(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function sanitizeRecord(metric, row) {
  if (!row || typeof row !== 'object' || Array.isArray(row)) return null;
  const output = {
    sampledAt: validSample(row.sampledAt),
    dataDate: validDate(row.dataDate),
    value: validNumber(row.value),
    unit: UNITS[metric],
  };
  if (metric !== 'steps' && row.details && typeof row.details === 'object' && !Array.isArray(row.details)) {
    const safe = {};
    for (const key of METRICS[metric]) {
      const value = validNumber(row.details[key]);
      if (value !== null) safe[key] = value;
    }
    if (Object.keys(safe).length) output.details = safe;
  }
  return output;
}

function errorCode(result) {
  return SAFE_ERRORS.has(result?.error_code) ? result.error_code : 'unavailable';
}

function safeStatus(result) {
  const allowed = new Set(['missing', 'valid', 'auth_expired', 'unavailable']);
  const authState = allowed.has(result?.auth_state) ? result.auth_state : 'unavailable';
  const error = result?.last_error;
  return {
    connected: result?.connected === true && authState === 'valid',
    provider: SOURCE,
    source: SOURCE,
    auth_state: authState,
    last_success_at: validSample(result?.last_success_at),
    last_error: SAFE_ERRORS.has(error) ? error : null,
  };
}

function safeSeries(metric, days, result, cache = {}) {
  const rows = Array.isArray(result?.records) ? result.records.map((row) => sanitizeRecord(metric, row)).filter(Boolean) : [];
  const successful = result?.status === 'PASS' || result?.status === 'EMPTY';
  return {
    status: successful ? (rows.length ? 'PASS' : 'EMPTY') : 'FAIL',
    provider: SOURCE,
    source: SOURCE,
    metric,
    days,
    records: rows,
    cached: cache.cached === true,
    stale: cache.stale === true,
    ...(successful ? {} : { error_code: errorCode(result) }),
  };
}

function safeLatest(result, cache = {}, days = 7) {
  const source = result && typeof result === 'object' ? result : {};
  const metrics = {};
  for (const metric of Object.keys(METRICS)) metrics[metric] = sanitizeRecord(metric, source[metric]);
  const hasData = Object.values(metrics).some(Boolean);
  const failed = source.status === 'FAIL' || source.error_code;
  const cycleDays = Number.isInteger(source.cycle?.days) ? source.cycle.days : 180;
  return {
    status: failed ? 'FAIL' : (hasData ? 'PASS' : 'EMPTY'),
    provider: SOURCE,
    source: SOURCE,
    days,
    sampledAt: validSample(source.sampledAt),
    dataDate: validDate(source.dataDate),
    ...metrics,
    cycle: safeCycle(source.cycle, cache, cycleDays),
    cached: cache.cached === true,
    stale: cache.stale === true,
    ...(failed ? { error_code: errorCode(source) } : {}),
  };
}

function safeCycle(result, cache = {}, days = 180) {
  const successful = result?.status === 'PASS' || result?.status === 'EMPTY';
  const events = Array.isArray(result?.events) ? result.events.flatMap((event) => {
    if (!event || !CYCLE_EVENT_TYPES.has(event.type)) return [];
    const timestamp = validSample(event.timestamp);
    const updatedAt = validSample(event.updated_at);
    return timestamp && updatedAt ? [{ type: event.type, timestamp, updated_at: updatedAt }] : [];
  }) : [];
  const periods = Array.isArray(result?.periods) ? result.periods.flatMap((period) => {
    if (!period || !validSample(period.start)) return [];
    const end = period.end === null ? null : validSample(period.end);
    if (period.end !== null && !end) return [];
    const open = end === null;
    if (period.open !== open) return [];
    return [{ start: period.start, end, open, source: 'recorded' }];
  }) : [];
  const enumValue = (value, allowed) => allowed.has(value) ? value : null;
  const symptoms = Array.isArray(result?.symptoms) ? result.symptoms.flatMap((symptom) => {
    if (!symptom || !validSample(symptom.timestamp)) return [];
    return [{
      timestamp: symptom.timestamp,
      hp: enumValue(symptom.hp, HP_VALUES),
      mood: enumValue(symptom.mood, MOOD_VALUES),
      pain: enumValue(symptom.pain, PAIN_VALUES),
    }];
  }) : [];
  const hasData = events.length > 0 || symptoms.length > 0;
  const failed = !successful;
  return {
    status: failed ? 'FAIL' : (hasData ? 'PASS' : 'EMPTY'),
    provider: SOURCE,
    source: SOURCE,
    metric: 'cycle',
    days,
    events,
    periods,
    symptoms,
    predictions: null,
    cached: cache.cached === true,
    stale: cache.stale === true,
    ...(failed ? { error_code: errorCode(result) } : {}),
  };
}

function createHealthCapabilities({ runAdapter, now = Date.now } = {}) {
  if (typeof runAdapter !== 'function') throw new TypeError('runAdapter is required');
  const cache = new Map();

  async function read(key, operation, args, ttl, shape) {
    const at = now();
    const previous = cache.get(key);
    if (previous && previous.expiresAt > at) return shape(previous.value, { cached: true, stale: false });
    try {
      const result = await runAdapter(operation, args);
      if (result?.status === 'FAIL' || result?.error_code) throw Object.assign(new Error('provider unavailable'), { safeCode: errorCode(result) });
      const shaped = shape(result, { cached: false, stale: false });
      cache.set(key, { value: shaped, expiresAt: at + ttl });
      return shaped;
    } catch (error) {
      if (previous) return shape(previous.value, { cached: true, stale: true, errorCode: SAFE_ERRORS.has(error?.safeCode) ? error.safeCode : 'unavailable' });
      const code = SAFE_ERRORS.has(error?.safeCode) ? error.safeCode : 'unavailable';
      return shape({ status: 'FAIL', error_code: code }, { cached: false, stale: false });
    }
  }

  return Object.freeze({
    async get({ metric = 'all', days: requestedDays } = {}) {
      if (!['all', 'status', ...Object.keys(METRICS), 'cycle'].includes(metric)) {
        throw new TypeError('unsupported health metric');
      }
      const days = requestedDays === undefined ? (metric === 'cycle' ? 180 : 7) : requestedDays;
      const maximumDays = metric === 'cycle' ? 365 : 30;
      if (!Number.isInteger(days) || days < 1 || days > maximumDays) {
        throw new RangeError(metric === 'cycle' ? 'cycle days must be between 1 and 365' : 'days must be between 1 and 30');
      }
      if (metric === 'status') {
        try {
          return safeStatus(await runAdapter('get_health', { metric, days }));
        } catch {
          return safeStatus({ auth_state: 'unavailable', last_error: 'unavailable' });
        }
      }
      if (metric === 'all') {
        return read(`all:${days}`, 'get_health', { metric, days }, LATEST_TTL_MS, (result, cacheState) => safeLatest(result, cacheState, days));
      }
      if (metric === 'cycle') {
        return read(`cycle:${days}`, 'get_health', { metric, days }, SERIES_TTL_MS, (result, cacheState) => safeCycle(result, cacheState, days));
      }
      return read(`${metric}:${days}`, 'get_health', { metric, days }, SERIES_TTL_MS, (result, cacheState) => safeSeries(metric, days, result, cacheState));
    },
  });
}

module.exports = {
  LATEST_TTL_MS,
  SERIES_TTL_MS,
  SOURCE,
  createHealthCapabilities,
  safeLatest,
  safeCycle,
  safeSeries,
  safeStatus,
};

