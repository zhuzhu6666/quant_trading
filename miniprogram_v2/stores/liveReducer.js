const VALID_FACT_STATES = new Set(['known', 'unknown', 'stale', 'error']);
const UNAVAILABLE_FACT_SOURCES = new Set([
  '',
  'none',
  'unknown',
  'unavailable',
  'not_registered',
  'degraded_cache',
]);

function hasOwn(record, key) {
  return !!record && Object.prototype.hasOwnProperty.call(record, key);
}

function epochSeconds(value) {
  const numeric = Number(value);
  if (Number.isFinite(numeric) && numeric > 0) {
    return numeric > 1e12 ? numeric / 1000 : numeric;
  }
  const parsed = typeof value === 'string' ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed / 1000 : 0;
}

function localEpochSeconds(now) {
  const parsed = Number(now);
  if (!Number.isFinite(parsed) || parsed <= 0) return Date.now() / 1000;
  return parsed > 1e12 ? parsed / 1000 : parsed;
}

function stateAtLocalClock(state, observedAt, staleAfterSec, now) {
  if (state !== 'known') return state;
  const observed = epochSeconds(observedAt);
  const staleAfter = Number(staleAfterSec || 0);
  if (observed <= 0) return 'unknown';
  if (staleAfter > 0 && localEpochSeconds(now) - observed > staleAfter) return 'stale';
  return state;
}

export function factState(payload, now = Date.now(), expectedContract = '') {
  const fact = payload && payload._fact && typeof payload._fact === 'object' ? payload._fact : {};
  if (fact.envelope !== 'fact.v1') return 'unknown';
  if (expectedContract && String(fact.contract || '') !== String(expectedContract)) return 'unknown';
  const declared = VALID_FACT_STATES.has(fact.state) ? fact.state : 'unknown';
  const source = String(fact.source || 'none').trim().toLowerCase();
  if (declared !== 'error' && UNAVAILABLE_FACT_SOURCES.has(source)) return 'unknown';
  if ((declared === 'known' || declared === 'stale') && !fact.observed_at) return 'unknown';
  return stateAtLocalClock(declared, fact.observed_at, fact.stale_after_sec, now);
}

export function factUsable(payload, now = Date.now(), expectedContract = '') {
  const state = factState(payload, now, expectedContract);
  return state === 'known' || state === 'stale';
}

export function factSource(payload, now = Date.now(), expectedContract = '') {
  const fact = payload && payload._fact && typeof payload._fact === 'object' ? payload._fact : {};
  const state = factState(payload, now, expectedContract);
  const envelopeMissing = fact.envelope !== 'fact.v1';
  const contractMismatch = !!expectedContract
    && String(fact.contract || '') !== String(expectedContract);
  return {
    state,
    reason: envelopeMissing
      ? 'missing_fact_envelope'
      : contractMismatch
        ? 'fact_contract_mismatch'
        : fact.reason_code || (state === 'stale' ? 'fact_freshness_expired' : ''),
    observedAt: fact.observed_at || 0,
    staleAfterSec: Number(fact.stale_after_sec || 0),
  };
}

function ageSource(source, now) {
  const current = source && typeof source === 'object' ? source : {};
  const state = stateAtLocalClock(
    VALID_FACT_STATES.has(current.state) ? current.state : 'unknown',
    current.observedAt,
    current.staleAfterSec,
    now,
  );
  return {
    ...current,
    state,
    reason: current.reason || (state === 'stale' ? 'fact_freshness_expired' : ''),
    observedAt: current.observedAt || 0,
    staleAfterSec: Number(current.staleAfterSec || 0),
  };
}

export function ageLiveSources(sources = {}, now = Date.now()) {
  const aged = {};
  Object.entries(sources || {}).forEach(([key, source]) => {
    aged[key] = ageSource(source, now);
  });
  return aged;
}

export function mergeLiveStateTrading(previous = {}, payload = {}) {
  const next = { ...previous };
  for (const key of ['source', 'equity', 'balance', 'pnl_today', 'n_positions', 'current_price', 'margin', 'margin_free', 'leverage', 'currency']) {
    if (hasOwn(payload, key)) next[key] = payload[key];
  }
  if (hasOwn(payload, 'pnl_today')) next.realized_pnl = payload.pnl_today;
  if (hasOwn(payload, 'positions_list')) {
    next.positions_list = Array.isArray(payload.positions_list) ? payload.positions_list : [];
  }
  for (const key of ['position', 'daily', 'risk']) {
    if (!hasOwn(payload, key)) continue;
    const value = payload[key];
    next[key] = value && typeof value === 'object' && !Array.isArray(value)
      ? { ...(previous[key] || {}), ...value }
      : value;
  }
  return next;
}

export function reduceLiveWsOutcome(previous = {}, payload = {}, attemptedAt = Date.now()) {
  const source = factSource(payload, attemptedAt, 'live.state.v2');
  const base = {
    wsConnected: true,
    lastAttemptAt: Number(attemptedAt),
    sources: { ...ageLiveSources(previous.sources || {}, attemptedAt), state: source },
  };
  if (!factUsable(payload, attemptedAt, 'live.state.v2')) return base;
  return {
    ...base,
    trading: mergeLiveStateTrading(previous.trading || {}, payload),
    lastSuccessAt: Number(attemptedAt),
    lastUpdate: Number(attemptedAt),
  };
}

export function reduceLiveWsDisconnected(
  previous = {},
  attemptedAt = Date.now(),
  reason = 'ws_disconnected',
) {
  const sources = ageLiveSources(previous.sources || {}, attemptedAt);
  const previousWs = sources.state || {};
  return {
    wsConnected: false,
    lastAttemptAt: Number(attemptedAt),
    sources: {
      ...sources,
      state: {
        ...previousWs,
        state: 'stale',
        reason,
        observedAt: previousWs.observedAt || 0,
        staleAfterSec: Number(previousWs.staleAfterSec || 5),
      },
    },
  };
}

export function reduceLivePollOutcome(previous = {}, outcome = {}) {
  const attemptedAt = Number(outcome.attemptedAt || Date.now());
  const base = {
    lastAttemptAt: attemptedAt,
    sources: ageLiveSources(
      { ...(previous.sources || {}), ...(outcome.sources || {}) },
      attemptedAt,
    ),
  };
  if (Number(outcome.usableCount || 0) <= 0) {
    return base;
  }
  return {
    ...(outcome.dataPatch || {}),
    ...base,
    lastSuccessAt: attemptedAt,
    lastUpdate: attemptedAt,
  };
}
