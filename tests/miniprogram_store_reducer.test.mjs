import assert from "node:assert/strict";
import { createStore } from "../miniprogram_v2/utils/store.js";
import {
  ageLiveSources,
  factSource,
  factState,
  reduceLivePollOutcome,
  reduceLiveWsDisconnected,
  reduceLiveWsOutcome,
} from "../miniprogram_v2/stores/liveReducer.js";
import {
  isRiskFactKnown,
  metricFactPresentation,
} from "../miniprogram_v2/stores/liveViewFacts.js";
import {
  positionComponentStateAt,
  positionComponentsAllKnown,
  readPositionFactBundle,
  reducePositionFactSnapshot,
} from "../miniprogram_v2/stores/livePositionFacts.js";
import { authStateAfterMeFailure } from "../miniprogram_v2/services/authState.js";

const store = createStore({ count: 0, nested: { value: 1 } });
const observed = [];
const unsubscribe = store.subscribe((state) => observed.push(state));

store.setState({ count: 1 });
assert.deepEqual(store.getState(), { count: 1, nested: { value: 1 } });
assert.equal(observed.length, 1);

const detached = store.getState();
detached.nested.value = 99;
assert.equal(store.getState().nested.value, 1, "getState must return a detached snapshot");

unsubscribe();
store.setState({ count: 2 });
assert.equal(observed.length, 1, "unsubscribe must stop reducer notifications");

const liveBefore = {
  lastAttemptAt: 100,
  lastSuccessAt: 90,
  lastUpdate: 90,
  account: { equity: 1234 },
  trading: { balance: 1200 },
  sources: { account: { state: "known" } },
};
const allFailed = reduceLivePollOutcome(liveBefore, {
  attemptedAt: 200,
  usableCount: 0,
  sources: { account: { state: "error" }, positions: { state: "error" } },
});
const allFailedState = { ...liveBefore, ...allFailed };
assert.equal(allFailedState.lastAttemptAt, 200);
assert.equal(allFailedState.lastSuccessAt, 90, "all failures must not advance lastSuccessAt");
assert.deepEqual(allFailedState.account, { equity: 1234 }, "all failures must preserve the last account fact");

const oneKnown = reduceLivePollOutcome(liveBefore, {
  attemptedAt: 210,
  usableCount: 1,
  sources: { account: { state: "known" } },
  dataPatch: { account: { equity: 1300 } },
});
assert.equal(oneKnown.lastSuccessAt, 210);
assert.deepEqual(oneKnown.account, { equity: 1300 });

const wsBefore = {
  lastSuccessAt: 300,
  lastUpdate: 300,
  sources: { risk: { state: "error" } },
  trading: {
    equity: 1200,
    balance: 1100,
    positions_list: [{ position_id: "p-1" }],
    daily: { trades: 4, pnl: 15 },
    risk: { circuit_breaker: true, consecutive_loss: 2 },
  },
};
const wsPartial = reduceLiveWsOutcome(wsBefore, {
  equity: 1250,
  _fact: {
    envelope: "fact.v1",
    contract: "live.state.v2",
    state: "known",
    source: "ctrader",
    observed_at: 400,
    generated_at: 400,
    stale_after_sec: 5,
  },
}, 400);
assert.equal(wsPartial.trading.equity, 1250);
assert.equal(wsPartial.trading.balance, 1100, "partial WS patch must preserve an absent balance");
assert.deepEqual(wsPartial.trading.positions_list, [{ position_id: "p-1" }], "partial WS patch must preserve absent positions");
assert.deepEqual(wsPartial.trading.risk, { circuit_breaker: true, consecutive_loss: 2 });
assert.equal(wsPartial.lastSuccessAt, 400);
assert.equal(wsPartial.sources.state.observedAt, 400);
assert.equal(wsPartial.sources.state.staleAfterSec, 5);

const wsUnavailable = reduceLiveWsOutcome(wsBefore, {
  equity: 0,
  _fact: {
    envelope: "fact.v1",
    contract: "live.state.v2",
    state: "known",
    source: "none",
    observed_at: 500,
    generated_at: 500,
    stale_after_sec: 5,
  },
}, 500);
assert.equal(wsUnavailable.trading, undefined, "source=none WS payload must not overwrite the last fact");
assert.equal(wsUnavailable.lastSuccessAt, undefined, "unavailable WS payload must not advance success time");

const clockBaseMs = 1_700_000_000_000;
const freshSources = {
  state: {
    state: "known",
    reason: "",
    observedAt: clockBaseMs / 1000,
    staleAfterSec: 5,
  },
};
assert.equal(ageLiveSources(freshSources, clockBaseMs + 4_000).state.state, "known");
assert.equal(
  ageLiveSources(freshSources, clockBaseMs + 6_000).state.state,
  "stale",
  "local clock advance must age a known source without another backend response",
);
const clockAdvanced = reduceLivePollOutcome(
  { ...wsBefore, sources: freshSources },
  { attemptedAt: clockBaseMs + 6_000, usableCount: 0, sources: {} },
);
assert.equal(clockAdvanced.sources.state.state, "stale");
assert.equal(clockAdvanced.trading, undefined, "freshness aging must not overwrite the last trading value");

const wsClosed = reduceLiveWsDisconnected(
  { ...wsBefore, sources: freshSources },
  clockBaseMs + 2_000,
  "ws_closed",
);
assert.equal(wsClosed.wsConnected, false);
assert.equal(wsClosed.sources.state.state, "stale");
assert.equal(wsClosed.sources.state.reason, "ws_closed");
assert.equal(wsClosed.trading, undefined, "WS close must preserve the last trading value");

assert.equal(isRiskFactKnown({ sources: { state: { state: "known" }, risk: { state: "error" } } }), false);
assert.equal(isRiskFactKnown({ sources: { strategy: { state: "known" }, risk: { state: "known" } } }), true);

const retainedAuth = authStateAfterMeFailure(
  { token: "mini-token", user: { name: "operator" }, isAuthenticated: false, busy: true },
  "mini-token",
  503,
);
assert.equal(retainedAuth.clearToken, false);
assert.equal(retainedAuth.statePatch.token, "mini-token");
assert.equal(retainedAuth.statePatch.isAuthenticated, true, "non-401 /auth/me failure must retain the local session");
assert.equal(authStateAfterMeFailure({}, "mini-token", 401).clearToken, true);

assert.equal(
  factState({ _fact: { state: 'known', source: 'ctrader', observed_at: clockBaseMs / 1000 } }, clockBaseMs),
  'unknown',
  'missing fact.v1 envelope must fail closed',
);
const mismatchedAccount = {
  _fact: {
    envelope: 'fact.v1',
    contract: 'live.positions.v2',
    state: 'known',
    source: 'ctrader',
    observed_at: clockBaseMs / 1000,
    stale_after_sec: 15,
  },
};
assert.equal(
  factState(mismatchedAccount, clockBaseMs, 'live.account.v2'),
  'unknown',
  'a different endpoint contract must not satisfy account freshness',
);
assert.equal(
  factSource(mismatchedAccount, clockBaseMs, 'live.account.v2').reason,
  'fact_contract_mismatch',
);

function positionComponent(name, state, observedAt, options = {}) {
  return {
    envelope: 'fact.v1',
    contract: `live.positions.${name}.v1`,
    state,
    source: options.source || 'ctrader_reconcile',
    observed_at: observedAt,
    generated_at: observedAt,
    stale_after_sec: 15,
    reason_code: options.reason || null,
    components: {
      known_position_ids: options.knownIds || [],
      unknown_position_ids: options.unknownIds || [],
    },
  };
}

function positionsPayload({ state = 'known', observedAt, positions = [], components = {} }) {
  return {
    positions,
    _fact: {
      envelope: 'fact.v1',
      contract: 'live.positions.v2',
      state,
      source: 'ctrader',
      observed_at: observedAt,
      generated_at: observedAt,
      stale_after_sec: 15,
      reason_code: state === 'error' ? 'source_error' : null,
      components: { broker_reconcile: components },
    },
  };
}

const componentObservedAt = clockBaseMs / 1000 - 1;
const priorTrading = {
  positions_list: [{
    position_id: 'p-42',
    symbol: 'XAUUSD+',
    type: 'buy',
    open_price: 2400,
    volume: 0.1,
    sl: 2390,
    tp: 2420,
    current_price: 2405,
    pnl: 12.5,
    current_price_last_known: { value: 2405, observedAt: componentObservedAt - 1 },
    pnl_last_known: { value: 12.5, observedAt: componentObservedAt - 1 },
    position_facts: {
      price: { state: 'known', observedAt: componentObservedAt - 1 },
      pnl: { state: 'known', observedAt: componentObservedAt - 1 },
    },
  }],
  unrealized_pnl: 12.5,
  unrealized_pnl_state: 'known',
  unrealized_pnl_observed_at: componentObservedAt - 1,
};
const splitTruthPayload = positionsPayload({
  state: 'error',
  observedAt: componentObservedAt,
  positions: [{
    position_id: 'p-42',
    symbol: 'XAUUSD+',
    type: 'buy',
    open_price: 2401,
    volume: 0.2,
    sl: 2392,
    tp: 2425,
    current_price: 0,
    current_price_state: 'unknown',
    pnl: 0,
    pnl_state: 'error',
  }],
  components: {
    identity: positionComponent('identity', 'known', componentObservedAt, { knownIds: ['p-42'] }),
    protection: positionComponent('protection', 'known', componentObservedAt, { knownIds: ['p-42'] }),
    price: positionComponent('price', 'unknown', componentObservedAt, { unknownIds: ['p-42'] }),
    pnl: positionComponent('pnl', 'error', componentObservedAt, {
      source: 'ctrader_pnl_rpc',
      reason: 'pnl_rpc_failed',
      unknownIds: ['p-42'],
    }),
  },
});
const splitTruth = reducePositionFactSnapshot(priorTrading, splitTruthPayload, clockBaseMs);
assert.equal(splitTruth.usable, true, 'known identity must remain consumable when PnL fails');
assert.equal(splitTruth.patch.positions_list[0].open_price, 2401, 'identity fields must update independently');
assert.equal(splitTruth.patch.positions_list[0].sl, 2392, 'known protection fields must update independently');
assert.equal(splitTruth.patch.positions_list[0].tp, 2425);
assert.equal(splitTruth.patch.positions_list[0].pnl, undefined, 'error PnL must not normalize to zero');
assert.equal(splitTruth.patch.positions_list[0].current_price, undefined, 'unknown price must not normalize to zero');
assert.equal(splitTruth.patch.positions_list[0].pnl_last_known.value, 12.5, 'last trusted PnL remains retained separately');
assert.equal(splitTruth.patch.positions_list[0].current_price_last_known.value, 2405);
assert.equal(splitTruth.patch.unrealized_pnl, undefined);
assert.equal(splitTruth.patch.unrealized_pnl_state, 'error');
assert.equal(
  metricFactPresentation(
    splitTruth.patch.positions_list[0].pnl,
    splitTruth.patch.positions_list[0].position_facts.pnl,
    splitTruth.patch.positions_list[0].pnl_last_known,
  ).tone,
  'warning',
  'an error fact must never reuse the positive tone of the last value',
);
assert.equal(
  metricFactPresentation(
    splitTruth.patch.positions_list[0].pnl,
    splitTruth.patch.positions_list[0].position_facts.pnl,
    splitTruth.patch.positions_list[0].pnl_last_known,
  ).value,
  undefined,
  'unknown/error values render as unavailable, not zero or a silently reused fact',
);
const missingPositionFact = reducePositionFactSnapshot(
  priorTrading,
  { positions: [] },
  clockBaseMs,
);
assert.equal(missingPositionFact.changed, true);
assert.equal(missingPositionFact.usable, false);
assert.equal(missingPositionFact.patch.positions_identity_state, 'unknown');
assert.equal(
  missingPositionFact.patch.positions_list,
  undefined,
  'a payload without fact.v1 must not clear the last position list or assert empty truth',
);

const staleObservedAt = clockBaseMs / 1000 - 20;
const staleComponents = Object.fromEntries(
  ['identity', 'protection', 'price', 'pnl'].map((name) => [
    name,
    positionComponent(name, 'stale', staleObservedAt, { knownIds: ['p-42'] }),
  ]),
);
const staleSnapshot = reducePositionFactSnapshot(priorTrading, positionsPayload({
  state: 'stale',
  observedAt: staleObservedAt,
  positions: [{
    position_id: 'p-42',
    symbol: 'XAUUSD+',
    type: 'buy',
    open_price: 2401,
    volume: 0.2,
    sl: 2392,
    tp: 2425,
    current_price: 2406,
    pnl: 14,
  }],
  components: staleComponents,
}), clockBaseMs);
const stalePnlView = metricFactPresentation(
  staleSnapshot.patch.positions_list[0].pnl,
  staleSnapshot.patch.positions_list[0].position_facts.pnl,
  staleSnapshot.patch.positions_list[0].pnl_last_known,
);
assert.equal(stalePnlView.value, 14, 'stale facts display the retained numeric value');
assert.equal(stalePnlView.tone, 'warning', 'stale positive PnL must not render green');
assert.equal(stalePnlView.label, '已过期');
assert.equal(stalePnlView.observedAt, staleObservedAt, 'stale display carries its observation time');
assert.equal(
  positionComponentStateAt(
    { position_components: { identity: { state: 'known', observedAt: componentObservedAt, staleAfterSec: 15 } } },
    'identity',
    clockBaseMs + 20_000,
  ),
  'stale',
  'component facts must age locally when polling no longer returns a fresh snapshot',
);
const locallyAgedPositive = metricFactPresentation(
  10,
  { state: 'known', observedAt: componentObservedAt, staleAfterSec: 15 },
  null,
  clockBaseMs + 20_000,
);
assert.equal(locallyAgedPositive.state, 'stale');
assert.equal(locallyAgedPositive.tone, 'warning', 'locally aged positive values must stop rendering green');

const knownEmpty = reducePositionFactSnapshot(priorTrading, positionsPayload({
  state: 'known',
  observedAt: componentObservedAt,
  positions: [],
  components: {},
}), clockBaseMs);
assert.equal(knownEmpty.usable, true);
assert.deepEqual(knownEmpty.patch.positions_list, [], 'confirmed empty reconcile clears the last position list');
assert.equal(knownEmpty.patch.n_positions, 0);
assert.equal(knownEmpty.patch.unrealized_pnl, 0, 'only a known empty snapshot may emit zero floating PnL');
assert.equal(knownEmpty.patch.unrealized_pnl_state, 'known');
assert.equal(
  positionComponentsAllKnown({ position_components: knownEmpty.patch.position_components }, clockBaseMs),
  true,
);

const wsComponents = Object.fromEntries(
  ['identity', 'protection', 'price', 'pnl'].map((name) => [
    name,
    positionComponent(name, 'known', componentObservedAt, { knownIds: ['ws-1'] }),
  ]),
);
const wsPositionPayload = {
  positions_list: [{ position_id: 'ws-1', type: 'sell', open_price: 2402, volume: 0.1, pnl: -3, current_price: 2404 }],
  _fact: {
    envelope: 'fact.v1',
    contract: 'live.state.v2',
    state: 'known',
    source: 'ctrader',
    observed_at: componentObservedAt,
    stale_after_sec: 5,
    components: {
      positions: {
        envelope: 'fact.v1',
        contract: 'live.positions.v2',
        state: 'known',
        source: 'ctrader',
        observed_at: componentObservedAt,
        stale_after_sec: 15,
        components: wsComponents,
      },
    },
  },
};
assert.equal(readPositionFactBundle(wsPositionPayload, clockBaseMs).source, 'ws');
const wsPosition = reducePositionFactSnapshot({}, wsPositionPayload, clockBaseMs);
assert.equal(wsPosition.patch.positions_list[0].position_id, 'ws-1');
assert.equal(wsPosition.patch.positions_list[0].pnl, -3, 'WS nested component facts use the same reducer contract');

console.log("miniprogram store reducer: ok");
