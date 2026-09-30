'use strict';

const SOURCE = 'xiaomi_fitness_cloud';
const HEALTH_SOURCES = new Set(['health_connect', 'gadgetbridge', 'xiaomi_fitness_cloud', 'mixed', 'none']);
function safeSource(value, fallback = SOURCE) {
  return HEALTH_SOURCES.has(value) ? value : fallback;
}
function safeDetails(metric, value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const safe = {};
  const allowed = new Set(METRICS[metric] || []);
  if (metric === 'sleep') {
    allowed.add('startAt');
    allowed.add('endAt');
    allowed.add('stages');
  }
  for (const key of allowed) {
    const candidate = value[key];
    if (validNumber(candidate) !== null) {
      safe[key] = candidate;
    } else if ((key === 'startAt' || key === 'endAt') && validSample(candidate)) {
      safe[key] = candidate;
    } else if (key === 'stages' && Array.isArray(candidate) && candidate.length <= 64) {
      const stages = candidate.flatMap((stage) => {
        if (!stage || typeof stage !== 'object' || Array.isArray(stage)) return [];
        const stageValue = Number.isInteger(stage.stage) ? stage.stage : null;
        const startAt = validSample(stage.startAt);
        const endAt = validSample(stage.endAt);
        return stageValue !== null && startAt && endAt ? [{ stage: stageValue, startAt, endAt }] : [];
      });
      if (stages.length) safe.stages = stages;
    }
  }
  return Object.keys(safe).length ? safe : null;
}
function validNumber(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function sanitizeRecord(metric, row, fallbackSource = SOURCE) {
  if (!row || typeof row !== 'object' || Array.isArray(row)) return null;
  const output = {
    sampledAt: validSample(row.sampledAt),
    dataDate: validDate(row.dataDate),
    value: validNumber(row.value),
    unit: UNITS[metric],
    source: safeSource(row.source, fallbackSource),
    sourceRecordId: typeof row.sourceRecordId === 'string' && row.sourceRecordId.length <= 200
      ? row.sourceRecordId : null,
    collectedAt: validSample(row.collectedAt),
  };
  const details = safeDetails(metric, row.details);
  if (details) output.details = details;
  if (metric === 'sleep') {
    const sleepWindow = safeSleepWindow(row.sleepWindow);
    if (sleepWindow) output.sleepWindow = sleepWindow;
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
  const provider = safeSource(result?.provider || result?.source, SOURCE);
  return {
    connected: result?.connected === true && authState === 'valid',
    provider,
    source: provider,
    auth_state: authState,
    last_success_at: validSample(result?.last_success_at),
    last_error: SAFE_ERRORS.has(error) ? error : null,
    mobile: result?.mobile && typeof result.mobile === 'object' ? result.mobile : null,
    xiaomi_fitness_cloud: result?.xiaomi_fitness_cloud && typeof result.xiaomi_fitness_cloud === 'object'
      ? result.xiaomi_fitness_cloud : null,
  };
}

function safeSeries(metric, days, result, cache = {}) {
  const fallbackSource = safeSource(result?.provider || result?.source, SOURCE);
  const rows = Array.isArray(result?.records)
    ? result.records.map((row) => sanitizeRecord(metric, row, fallbackSource)).filter(Boolean) : [];
  const successful = result?.status === 'PASS' || result?.status === 'EMPTY';
  return {
    status: successful ? (rows.length ? 'PASS' : 'EMPTY') : 'FAIL',
    provider: fallbackSource,
    source: fallbackSource,
    metric,
    days,
    records: rows,
    cached: cache.cached === true,
    stale: cache.stale === true || result?.stale === true,
    ...(successful ? {} : { error_code: errorCode(result) }),
  };
}

function componentStatus(entry, fallback = 'EMPTY', fallbackSource = SOURCE) {
  const allowed = new Set(['PASS', 'EMPTY', 'FAIL', 'PERMISSION_DENIED', 'UNAVAILABLE']);
  const source = safeSource(entry?.source, fallbackSource);
  const status = entry && typeof entry === 'object' && allowed.has(entry.status) ? entry.status : fallback;
  const output = { status, source, stale: entry?.stale === true };
  if (status === 'FAIL') output.error_code = SAFE_ERRORS.has(entry?.error_code) ? entry.error_code : 'unavailable';
  return output;
}

function cycleHasData(cycle) {
  return Boolean(cycle?.events?.length || cycle?.periods?.length || cycle?.symptoms?.length);
}

function sanitizeMetricStatus(source, metrics, cycle) {
  const raw = source?.metric_status && typeof source.metric_status === 'object' && !Array.isArray(source.metric_status)
    ? source.metric_status
    : {};
  const output = {};
  for (const metric of Object.keys(METRICS)) {
    const fallback = metrics[metric] ? 'PASS' : 'EMPTY';
    output[metric] = componentStatus(raw[metric], fallback, safeSource(metrics[metric]?.source || source?.source, SOURCE));
    if (metrics[metric]) {
      output[metric] = { ...output[metric], status: 'PASS', source: safeSource(metrics[metric].source, SOURCE) };
    }
  }
  output.cycle = componentStatus(raw.cycle || { status: cycle.status, error_code: cycle.error_code, source: cycle.source }, cycle.status, safeSource(cycle.source, SOURCE));
  return output;
}

function safeLatest(result, cache = {}, days = 7) {
  const source = result && typeof result === 'object' ? result : {};
  const provider = safeSource(source.provider || source.source, SOURCE);
  const metrics = {};
  for (const metric of Object.keys(METRICS)) {
    metrics[metric] = sanitizeRecord(metric, source[metric], provider);
  }
  const cycleDays = Number.isInteger(source.cycle?.days) ? source.cycle.days : 180;
  const cycle = safeCycle(source.cycle, cache, cycleDays);
  const metricStatus = sanitizeMetricStatus(source, metrics, cycle);
  const hasData = Object.values(metrics).some(Boolean) || cycleHasData(cycle);
  const componentFailed = Object.values(metricStatus).some((item) => item.status === 'FAIL');
  const topFailed = source.status === 'FAIL' || Boolean(source.error_code);
  const status = hasData ? 'PASS' : ((componentFailed || topFailed) ? 'FAIL' : 'EMPTY');
  const metricSources = Object.values(metrics).filter(Boolean).map((row) => row.source);
  const resolvedSource = metricSources.length && new Set(metricSources).size > 1 ? 'mixed'
    : (metricSources[0] || provider);
  return {
    status,
    provider: resolvedSource,
    source: resolvedSource,
    days,
    sampledAt: validSample(source.sampledAt),
    dataDate: validDate(source.dataDate),
    ...metrics,
    cycle,
    partial: Boolean(hasData && componentFailed),
    metric_status: metricStatus,
    cached: cache.cached === true,
    stale: cache.stale === true || source.stale === true,
    ...(status === 'FAIL' ? { error_code: errorCode(source) } : {}),
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
