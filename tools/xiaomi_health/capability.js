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
  if (metric === 'heart_rate') {
    allowed.add('min');
    allowed.add('max');
    allowed.add('avg');
    allowed.add('samples');
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
  if (typeof value !== 'string' || value.length > 40) return null;
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$/.test(value)) return null;
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return null;
  const canon = new Date(parsed).toISOString();
  const date = value.slice(0, 10);
  const hms = value.slice(11, 19);
  if (date !== canon.slice(0, 10) || hms !== canon.slice(11, 19)) return null;
  const fraction = value.includes('.') ? value.slice(value.indexOf('.') + 1, -1) : '';
  if (fraction.length > 3 && !/^0+$/.test(fraction.slice(3))) return null;
  if (fraction) {
    const normalized = `${fraction}000`.slice(0, 3);
    if (normalized !== canon.slice(20, 23)) return null;
  }
  return value;
}

function safeSleepWindow(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const bedtime = validSample(value.bedtime);
  const wakeUpTime = validSample(value.wakeUpTime);
  if (!bedtime || !wakeUpTime) return null;
  const bedtimeMs = Date.parse(bedtime);
  const wakeUpTimeMs = Date.parse(wakeUpTime);
  if (!Number.isFinite(bedtimeMs) || !Number.isFinite(wakeUpTimeMs) || wakeUpTimeMs <= bedtimeMs) return null;
  const canonical = (milliseconds) => new Date(milliseconds).toISOString().replace(/\.000Z$/, 'Z');
  if (canonical(bedtimeMs) !== bedtime || canonical(wakeUpTimeMs) !== wakeUpTime) return null;
  return { bedtime, wakeUpTime };
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
    const heartRate = sanitizeSleepHeartRate(row.heartRate);
    if (heartRate) output.heartRate = heartRate;
  }
  return output;
}

function sanitizeLastHour(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const output = {};
  for (const key of ['min', 'max', 'avg', 'samples']) {
    if (validNumber(value[key]) !== null) output[key] = value[key];
  }
  return Object.keys(output).length ? output : null;
}

function sanitizeSleepHeartRate(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const output = {};
  for (const key of ['avg', 'min', 'samples']) {
    if (validNumber(value[key]) !== null) output[key] = value[key];
  }
  return Object.keys(output).length ? output : null;
}

function sanitizeHeartRateDailyRow(row) {
  if (!row || typeof row !== 'object' || Array.isArray(row)) return null;
  const dataDate = validDate(row.dataDate);
  if (!dataDate) return null;
  const output = { dataDate };
  for (const key of ['restingHeartRate', 'min', 'max', 'sampleCount']) {
    if (validNumber(row[key]) !== null) output[key] = row[key];
  }
  return output;
}

function sanitizeHeartRateDailyRecords(records, limit = 30) {
  if (!Array.isArray(records) || records.length > limit) return [];
  return records.map(sanitizeHeartRateDailyRow).filter(Boolean).slice(0, limit);
}

function sanitizeHeartRateModel(row, fallbackSource = SOURCE) {
  if (!row || typeof row !== 'object' || Array.isArray(row)) return null;
  if (row.view === 'daily') {
    const records = sanitizeHeartRateDailyRecords(row.records);
    if (!records.length) return null;
    return {
      view: 'daily',
      days: Number.isInteger(row.days) ? row.days : undefined,
      records,
      unit: UNITS.heart_rate,
      source: safeSource(row.source, fallbackSource),
    };
  }
  const output = sanitizeRecord('heart_rate', row, fallbackSource);
  if (!output) return null;
  if (validNumber(row.ageSeconds) !== null) output.ageSeconds = Math.trunc(row.ageSeconds);
  if (typeof row.stale === 'boolean') output.stale = row.stale;
  const lastHour = sanitizeLastHour(row.lastHour);
  if (lastHour) output.lastHour = lastHour;
  if (row.view === 'snapshot') output.view = 'snapshot';
  return output;
}

function safeHeartRateSnapshot(result, cache = {}) {
  const fallbackSource = safeSource(result?.provider || result?.source, SOURCE);
  const snapshot = sanitizeHeartRateModel(
    result?.view === 'snapshot' || result?.value != null || result?.lastHour
      ? result
      : null,
    fallbackSource,
  );
  const successful = result?.status === 'PASS' || result?.status === 'EMPTY';
  const hasValue = snapshot && snapshot.value != null;
  return {
    status: successful ? (hasValue ? 'PASS' : 'EMPTY') : 'FAIL',
    provider: fallbackSource,
    source: fallbackSource,
    metric: 'heart_rate',
    view: 'snapshot',
    value: snapshot?.value ?? null,
    unit: UNITS.heart_rate,
    sampledAt: snapshot?.sampledAt ?? null,
    dataDate: snapshot?.dataDate ?? null,
    ageSeconds: snapshot?.ageSeconds,
    lastHour: snapshot?.lastHour ?? null,
    cached: cache.cached === true,
    stale: cache.stale === true || result?.stale === true || snapshot?.stale === true,
    ...(successful ? {} : { error_code: errorCode(result) }),
  };
}

function safeHeartRateDaily(days, result, cache = {}) {
  const fallbackSource = safeSource(result?.provider || result?.source, SOURCE);
  const records = sanitizeHeartRateDailyRecords(result?.records, days);
  const successful = result?.status === 'PASS' || result?.status === 'EMPTY';
  return {
    status: successful ? (records.length ? 'PASS' : 'EMPTY') : 'FAIL',
    provider: fallbackSource,
    source: fallbackSource,
    metric: 'heart_rate',
    view: 'daily',
    days,
    records,
    cached: cache.cached === true,
    stale: cache.stale === true || result?.stale === true,
    ...(successful ? {} : { error_code: errorCode(result) }),
  };
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
    metrics[metric] = metric === 'heart_rate'
      ? sanitizeHeartRateModel(source[metric], provider)
      : sanitizeRecord(metric, source[metric], provider);
  }
  const cycleDays = Number.isInteger(source.cycle?.days) ? source.cycle.days : 180;
  const cycle = safeCycle(source.cycle, cache, cycleDays);
  const metricStatus = sanitizeMetricStatus(source, metrics, cycle);
  const hasData = Object.values(metrics).some(Boolean) || cycleHasData(cycle);
  const componentFailed = Object.values(metricStatus).some((item) => item.status === 'FAIL');
  const topFailed = source.status === 'FAIL';
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
      if (result?.status === 'FAIL') throw Object.assign(new Error('provider unavailable'), { safeCode: errorCode(result) });
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
      const daysOmitted = requestedDays === undefined;
      const days = daysOmitted ? (metric === 'cycle' ? 180 : 7) : requestedDays;
      const maximumDays = metric === 'cycle' ? 365 : 30;
      if (!Number.isInteger(days) || days < 1 || days > maximumDays) {
        throw new RangeError(metric === 'cycle' ? 'cycle days must be between 1 and 365' : 'days must be between 1 and 30');
      }
      const adapterInput = { metric };
      if (!daysOmitted || (metric !== 'all' && metric !== 'heart_rate')) {
        adapterInput.days = days;
      }
      if (metric === 'status') {
        try {
          return safeStatus(await runAdapter('get_health', adapterInput));
        } catch {
          return safeStatus({ auth_state: 'unavailable', last_error: 'unavailable' });
        }
      }
      if (metric === 'all') {
        const key = daysOmitted ? 'all:snapshot' : `all:${days}`;
        return read(key, 'get_health', adapterInput, LATEST_TTL_MS, (result, cacheState) => safeLatest(result, cacheState, days));
      }
      if (metric === 'cycle') {
        return read(`cycle:${days}`, 'get_health', adapterInput, SERIES_TTL_MS, (result, cacheState) => safeCycle(result, cacheState, days));
      }
      if (metric === 'heart_rate') {
        const key = daysOmitted ? 'heart_rate:snapshot' : `heart_rate:${days}`;
        return read(
          key,
          'get_health',
          adapterInput,
          daysOmitted ? LATEST_TTL_MS : SERIES_TTL_MS,
          (result, cacheState) => (
            daysOmitted
              ? safeHeartRateSnapshot(result, cacheState)
              : safeHeartRateDaily(days, result, cacheState)
          ),
        );
      }
      return read(`${metric}:${days}`, 'get_health', adapterInput, SERIES_TTL_MS, (result, cacheState) => safeSeries(metric, days, result, cacheState));
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
  safeHeartRateSnapshot,
  safeHeartRateDaily,
  safeStatus,
  validSample,
};
