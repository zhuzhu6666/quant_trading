import { factSource } from './liveReducer.js';

const COMPONENT_NAMES = ['identity', 'protection', 'price', 'pnl'];
const USABLE_STATES = new Set(['known', 'stale']);
const VALID_STATES = new Set(['known', 'unknown', 'stale', 'error']);

function hasOwn(record, key) {
  return !!record && Object.prototype.hasOwnProperty.call(record, key);
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === '') return undefined;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : undefined;
}

function firstFinite(record, keys) {
  for (const key of keys) {
    const value = finiteNumber(record && record[key]);
    if (value !== undefined) return value;
  }
  return undefined;
}

function firstPresent(record, keys) {
  for (const key of keys) {
    if (hasOwn(record, key) && record[key] !== null && record[key] !== undefined) {
      return record[key];
    }
  }
  return undefined;
}

function positionKey(item, fallback = '') {
  const value = firstPresent(item, ['position_id', 'positionId', 'ticket', 'id']);
  return value === undefined || value === '' ? String(fallback) : String(value);
}

function emptyFact(reason = 'position_component_missing') {
  return {
    state: 'unknown',
    reason,
    observedAt: 0,
    staleAfterSec: 15,
    source: 'none',
    knownPositionIds: [],
    unknownPositionIds: [],
  };
}

function normalizedFact(rawFact, now, expectedContract) {
  if (!rawFact || typeof rawFact !== 'object') return emptyFact();
  const source = factSource({ _fact: rawFact }, now, expectedContract);
  const details = rawFact.components && typeof rawFact.components === 'object'
    ? rawFact.components
    : {};
  return {
    ...source,
    source: String(rawFact.source || 'none'),
    knownPositionIds: (details.known_position_ids || []).map(String),
    unknownPositionIds: (details.unknown_position_ids || []).map(String),
  };
}

function parentAndComponents(payload) {
  const top = payload && payload._fact && typeof payload._fact === 'object'
    ? payload._fact
    : null;
  if (!top || top.envelope !== 'fact.v1') {
    return { parent: null, rawComponents: null, source: 'none' };
  }
  if (String(top.contract || '') === 'live.positions.v2') {
    return {
      parent: top,
      rawComponents: top.components?.broker_reconcile || null,
      source: 'poll',
    };
  }
  if (String(top.contract || '') === 'live.state.v2') {
    const parent = top.components?.positions;
    return {
      parent: parent && typeof parent === 'object' ? parent : null,
      rawComponents: parent?.components || null,
      source: 'ws',
    };
  }
  return { parent: null, rawComponents: null, source: 'none' };
}

function rawPositionList(payload) {
  if (Array.isArray(payload?.positions)) {
    return { present: true, positions: payload.positions };
  }
  if (Array.isArray(payload?.positions_list)) {
    return { present: true, positions: payload.positions_list };
  }
  return { present: false, positions: [] };
}

/**
 * Read the endpoint/WS position fact tree without inheriting the state of a
 * different endpoint.  A confirmed empty snapshot has no per-position
 * components, so its parent fact is the authoritative fact for all four
 * vacuous components.
 */
export function readPositionFactBundle(payload = {}, now = Date.now()) {
  const { parent, rawComponents, source } = parentAndComponents(payload);
  const raw = rawPositionList(payload);
  const parentFact = normalizedFact(parent, now, 'live.positions.v2');
  const confirmedEmpty = raw.present
    && raw.positions.length === 0
    && USABLE_STATES.has(parentFact.state);
  const components = {};
  COMPONENT_NAMES.forEach((name) => {
    const rawFact = rawComponents && typeof rawComponents[name] === 'object'
      ? rawComponents[name]
      : null;
    components[name] = rawFact
      ? normalizedFact(rawFact, now, `live.positions.${name}.v1`)
      : confirmedEmpty
        ? { ...parentFact, reason: parentFact.reason || '' }
        : emptyFact(`position_${name}_fact_missing`);
  });
  return {
    source,
    present: !!parent,
    snapshotPresent: raw.present,
    rawPositions: raw.positions,
    parent: parentFact,
    components,
    confirmedEmpty,
  };
}

function itemFact(component, item, name, now) {
  const id = positionKey(item);
  const stateKey = name === 'price' ? 'current_price_state' : name === 'pnl' ? 'pnl_state' : '';
  const sourceKey = name === 'price' ? 'current_price_source' : name === 'pnl' ? 'pnl_source' : '';
  const observedKey = name === 'price' ? 'current_price_observed_at' : name === 'pnl' ? 'pnl_observed_at' : '';
  const reasonKey = name === 'price' ? 'current_price_reason_code' : name === 'pnl' ? 'pnl_reason_code' : '';
  const explicitState = stateKey && VALID_STATES.has(String(item?.[stateKey] || '').toLowerCase())
    ? String(item[stateKey]).toLowerCase()
    : '';

  if (explicitState) {
    const explicit = normalizedFact({
      envelope: 'fact.v1',
      contract: `live.positions.${name}.v1`,
      state: explicitState,
      source: item?.[sourceKey] || component.source,
      observed_at: item?.[observedKey] || component.observedAt,
      stale_after_sec: component.staleAfterSec || 15,
      reason_code: item?.[reasonKey] || component.reason,
      components: {},
    }, now, `live.positions.${name}.v1`);
    // A locally aged component cannot be rejuvenated by a nested item that
    // merely repeats the older observation timestamp.
    if (explicit.state === 'known' && component.state === 'stale') {
      return { ...explicit, state: 'stale', reason: component.reason || 'fact_freshness_expired' };
    }
    return explicit;
  }

  if (id && component.unknownPositionIds.includes(id)) {
    return {
      ...component,
      state: component.state === 'error' ? 'error' : 'unknown',
      reason: component.reason || `position_${name}_unknown`,
    };
  }
  if (id && component.knownPositionIds.includes(id)) {
    return {
      ...component,
      state: component.state === 'stale' ? 'stale' : 'known',
    };
  }
  return { ...component };
}

function previousLastKnown(previous, name) {
  const key = name === 'price' ? 'current_price' : 'pnl';
  const lastKey = name === 'price' ? 'current_price_last_known' : 'pnl_last_known';
  const saved = previous && previous[lastKey];
  if (saved && finiteNumber(saved.value) !== undefined) return { ...saved, value: Number(saved.value) };
  const value = finiteNumber(previous && previous[key]);
  const fact = previous?.position_facts?.[name] || {};
  if (value === undefined || !USABLE_STATES.has(String(fact.state || ''))) return null;
  return { value, observedAt: fact.observedAt || 0 };
}

function applyMetricFact(next, previous, item, name, fact) {
  const isPrice = name === 'price';
  const aliases = isPrice
    ? ['current_price', 'price_current']
    : ['netUnrealizedPnL', 'unrealized', 'pnl', 'profit'];
  const value = firstFinite(item, aliases);
  const valueKey = isPrice ? 'current_price' : 'pnl';
  const lastKey = isPrice ? 'current_price_last_known' : 'pnl_last_known';
  let effectiveFact = { ...fact };
  const priorLastKnown = previousLastKnown(previous, name);

  if (USABLE_STATES.has(fact.state) && value !== undefined) {
    next[valueKey] = value;
    next[lastKey] = { value, observedAt: fact.observedAt || 0 };
    if (isPrice) next.price_current = value;
    else next.unrealized = value;
  } else {
    if (USABLE_STATES.has(fact.state) && value === undefined) {
      effectiveFact = {
        ...fact,
        state: 'unknown',
        reason: `${name}_value_missing`,
      };
    }
    aliases.forEach((key) => { delete next[key]; });
    if (priorLastKnown) next[lastKey] = priorLastKnown;
  }
  return effectiveFact;
}

function normalizePosition(item, previous, components, now, index) {
  const next = { ...(previous || {}), ...(item || {}) };
  next.position_id = firstPresent(item, ['position_id', 'positionId', 'ticket', 'id'])
    ?? firstPresent(previous, ['position_id', 'positionId', 'ticket', 'id'])
    ?? String(index);
  const openPrice = firstFinite(item, ['open_price', 'price_open', 'entry_price']);
  const volume = firstFinite(item, ['volume', 'size', 'api_volume']);
  if (openPrice !== undefined) next.open_price = openPrice;
  if (volume !== undefined) next.volume = volume;

  const facts = {};
  COMPONENT_NAMES.forEach((name) => {
    facts[name] = itemFact(components[name], item, name, now);
  });

  if (!USABLE_STATES.has(facts.protection.state)) {
    for (const key of ['sl', 'tp', 'stop_loss', 'take_profit']) {
      if (hasOwn(previous, key)) next[key] = previous[key];
      else delete next[key];
    }
  }

  facts.price = applyMetricFact(next, previous, item, 'price', facts.price);
  facts.pnl = applyMetricFact(next, previous, item, 'pnl', facts.pnl);
  next.position_facts = facts;
  return next;
}

function aggregatePnl(positions, component) {
  if (positions.length === 0) {
    return {
      state: component.state,
      value: USABLE_STATES.has(component.state) ? 0 : undefined,
      observedAt: component.observedAt || 0,
      reason: component.reason || '',
    };
  }
  const facts = positions.map((item) => item.position_facts?.pnl || emptyFact());
  const error = facts.find((fact) => fact.state === 'error');
  const unknown = facts.find((fact) => fact.state === 'unknown');
  const stale = facts.find((fact) => fact.state === 'stale');
  const values = positions.map((item) => finiteNumber(item.pnl));
  const unavailable = values.some((value) => value === undefined);
  const blocker = error || unknown || (unavailable ? emptyFact('pnl_value_missing') : null);
  if (blocker) {
    return {
      state: blocker.state === 'error' ? 'error' : 'unknown',
      value: undefined,
      observedAt: blocker.observedAt || component.observedAt || 0,
      reason: blocker.reason || 'position_pnl_unavailable',
    };
  }
  return {
    state: stale ? 'stale' : 'known',
    value: values.reduce((sum, value) => sum + value, 0),
    observedAt: Math.min(...facts.map((fact) => Number(fact.observedAt || Infinity))),
    reason: stale?.reason || '',
  };
}

/**
 * Produce only the position-related store patch.  Identity controls whether a
 * list may replace the last snapshot; price/PnL never receive a numeric zero
 * merely because their facts are unavailable.
 */
export function reducePositionFactSnapshot(previousTrading = {}, payload = {}, now = Date.now()) {
  const bundle = readPositionFactBundle(payload, now);
  if (!bundle.present) {
    if (!bundle.snapshotPresent) return { changed: false, usable: false, patch: {} };
    return {
      changed: true,
      usable: false,
      patch: {
        position_components: bundle.components,
        positions_identity_state: 'unknown',
        positions_identity_observed_at: 0,
      },
    };
  }

  const components = bundle.components;
  const identityUsable = USABLE_STATES.has(components.identity.state);
  const basePatch = {
    position_components: components,
    positions_identity_state: components.identity.state,
    positions_identity_observed_at: components.identity.observedAt || 0,
  };
  if (!bundle.snapshotPresent || !identityUsable) {
    return { changed: true, usable: false, patch: basePatch };
  }

  const previousById = new Map(
    (previousTrading.positions_list || []).map((item, index) => [positionKey(item, index), item]),
  );
  const positions = bundle.rawPositions.map((item, index) => {
    const key = positionKey(item, index);
    return normalizePosition(item, previousById.get(key), components, now, index);
  });
  const pnl = aggregatePnl(positions, components.pnl);
  const primary = positions[0] || null;
  const priceFact = primary?.position_facts?.price || components.price;
  const currentPrice = primary ? finiteNumber(primary.current_price) : undefined;
  const previousAggregate = previousTrading.unrealized_pnl_last_known
    || (USABLE_STATES.has(String(previousTrading.unrealized_pnl_state || ''))
      && finiteNumber(previousTrading.unrealized_pnl) !== undefined
      ? {
          value: Number(previousTrading.unrealized_pnl),
          observedAt: previousTrading.unrealized_pnl_observed_at || 0,
        }
      : null);
  const aggregateLastKnown = pnl.value !== undefined
    ? { value: pnl.value, observedAt: pnl.observedAt || 0 }
    : previousAggregate;
  const currentPriceLastKnown = primary?.current_price_last_known
    || previousTrading.current_price_last_known
    || null;
  const patch = {
    ...basePatch,
    positions_list: positions,
    n_positions: positions.length,
    position: primary
      ? {
          dir: primary.type === 'buy' || Number(primary.direction) === 1 ? 'LONG'
            : primary.type === 'sell' || Number(primary.direction) === -1 ? 'SHORT'
              : 'FLAT',
          entry: finiteNumber(primary.open_price),
          size: finiteNumber(primary.volume),
          unrealized: finiteNumber(primary.pnl),
          pnl_state: primary.position_facts?.pnl?.state || 'unknown',
        }
      : { dir: 'FLAT', entry: 0, size: 0, unrealized: 0, pnl_state: 'known' },
    unrealized_pnl: pnl.value,
    live_pnl: pnl.value,
    unrealized_pnl_state: pnl.state,
    unrealized_pnl_observed_at: Number.isFinite(pnl.observedAt) ? pnl.observedAt : 0,
    unrealized_pnl_reason: pnl.reason,
    unrealized_pnl_last_known: aggregateLastKnown,
    current_price: currentPrice,
    current_price_state: primary ? priceFact.state : 'unknown',
    current_price_observed_at: primary ? priceFact.observedAt || 0 : 0,
    current_price_reason: primary ? priceFact.reason || '' : 'no_open_position',
    current_price_last_known: currentPriceLastKnown,
  };
  return { changed: true, usable: true, patch };
}

export function positionComponentState(trading = {}, name = '') {
  const fact = trading.position_components?.[name] || {};
  const state = String(fact.state || 'unknown').toLowerCase();
  return VALID_STATES.has(state) ? state : 'unknown';
}

export function positionComponentStateAt(trading = {}, name = '', now = Date.now()) {
  const fact = trading.position_components?.[name] || {};
  const state = positionComponentState(trading, name);
  if (state !== 'known') return state;
  const observedNumeric = Number(fact.observedAt);
  const observedParsed = typeof fact.observedAt === 'string' ? Date.parse(fact.observedAt) / 1000 : 0;
  const observedAt = Number.isFinite(observedNumeric) && observedNumeric > 0
    ? (observedNumeric > 1e12 ? observedNumeric / 1000 : observedNumeric)
    : observedParsed;
  const staleAfterSec = Number(fact.staleAfterSec || 15);
  const nowNumeric = Number(now);
  const nowSec = Number.isFinite(nowNumeric) && nowNumeric > 0
    ? (nowNumeric > 1e12 ? nowNumeric / 1000 : nowNumeric)
    : Date.now() / 1000;
  if (!Number.isFinite(observedAt) || observedAt <= 0) return 'unknown';
  return staleAfterSec > 0 && nowSec - observedAt > staleAfterSec ? 'stale' : 'known';
}

export function positionComponentsAllKnown(trading = {}, now = Date.now()) {
  return COMPONENT_NAMES.every((name) => positionComponentStateAt(trading, name, now) === 'known');
}
