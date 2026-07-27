import assert from "node:assert/strict";
import {
  factAllowsNewRisk,
  factBoundTone,
  factIsKnown,
  mergeFactRecord,
  readFact,
  readFactComponent,
  readFactNestedComponent,
  decodeLiveAutonomyEvaluation,
  decodeLiveAutonomyStatus,
} from "../api/fact.ts";
import { authStateAfterMeFailure } from "../contexts/authState.ts";
import { pick, pickArray } from "../lib/compat.ts";
import { decodeCanonicalRiskSnapshot, knownMetric } from "../api/riskSnapshot.ts";

const now = Date.now() / 1000;
const envelope = (contract, source = "ctrader", observedAt = now, staleAfter = 30, components = {}) => ({
  envelope: "fact.v1",
  contract,
  state: "known",
  source,
  observed_at: observedAt,
  generated_at: now,
  stale_after_sec: staleAfter,
  reason_code: null,
  components,
});

const genericState = { _fact: envelope("live.state.v2") };
const missingRisk = readFact(undefined, "risk.summary.v2");
assert.equal(readFact(genericState, "live.state.v2").state, "known");
assert.equal(missingRisk.state, "unknown", "generic live state must not turn a failed risk endpoint green");
assert.equal(factAllowsNewRisk(missingRisk), false);
assert.equal(factIsKnown(readFact({ _fact: envelope("risk.summary.v2") }, "risk.summary.v2"), true), false, "request errors must suppress a cached green risk fact");

const canonicalRisk = decodeCanonicalRiskSnapshot({
  snapshot: {
    schema_version: "risk_metrics_snapshot.v2",
    status: "known",
    sample_count: 500,
    components: {
      var: {
        status: "known",
        alpha: 0.95,
        horizon: "one_closed_bar",
        timeframe: "M5",
        sample_count: 500,
        current_equity: 371.5,
        current_net_notional_usd: 0,
        var_usd: 0,
        cvar_usd: 0,
        var_pct: 0,
        cvar_pct: 0,
      },
      var_shadow_99: { status: "known", alpha: 0.99, var_pct: 0, cvar_pct: 0 },
      kelly: { status: "known", kelly_fraction: 0, closed_trades: 181 },
      stress: { status: "known", stress_loss_pct: 0, distinct_position_count: 0 },
      concentration: { status: "known", concentration_pct: 0, applicable: false, is_safe: true },
    },
  },
  var: { status: "known", var_pct: 99 },
});
assert.equal(canonicalRisk.contractKnown, true);
assert.equal(knownMetric(canonicalRisk.var95.status), true);
assert.equal(canonicalRisk.var95.varPct, 0, "known zero exposure must remain a displayable fact");
assert.equal(canonicalRisk.var99.alpha, 0.99);
assert.equal(canonicalRisk.kelly.closedTrades, 181);
assert.equal(
  decodeCanonicalRiskSnapshot({ var: { status: "known", var_pct: 99 } }).contractKnown,
  false,
  "legacy top-level risk fields must not masquerade as the canonical snapshot",
);

const retainedHealthy = {
  status: "healthy",
  _fact: envelope("system.health.v2", "backend", now - 31, 30),
};
const retainedHealthyFact = readFact(retainedHealthy, "system.health.v2");
assert.equal(retainedHealthyFact.state, "stale");
assert.equal(retainedHealthy.status, "healthy", "stale endpoint data remains available for display");
assert.equal(factBoundTone(retainedHealthyFact, "ok"), "pending", "stale retained success must be labeled as pending instead of a business warning");

const cachedKnownFact = readFact({ _fact: envelope("system.health.v2", "backend") }, "system.health.v2");
assert.equal(factBoundTone(cachedKnownFact, "ok", true), "bad", "refetch failure must suppress cached green success");
assert.equal(factBoundTone(cachedKnownFact, "bad", true), "bad", "request failure must preserve a dangerous business tone");

const backendError = {
  _fact: { ...envelope("system.health.v2", "backend"), state: "error" },
};
assert.equal(factBoundTone(readFact(backendError, "system.health.v2"), "ok"), "bad", "endpoint error state must never render green");

const retainedPnl = {
  summary: { realized_pnl: 12.5, trades: 3 },
  _fact: envelope("live.realized-pnl.v2", "ctrader_deals", now - 31, 30),
};
const retainedPnlFact = readFact(retainedPnl, "live.realized-pnl.v2");
assert.equal(retainedPnl.summary.realized_pnl, 12.5, "stale PnL remains available as retained display data");
assert.equal(retainedPnlFact.state, "stale");
assert.equal(factBoundTone(retainedPnlFact, "ok"), "pending", "retained positive PnL must expose pending freshness");
assert.equal(factIsKnown(readFact({ ...retainedPnl, _fact: envelope("live.realized-pnl.v2") }, "live.realized-pnl.v2"), true), false, "PnL refetch failure must suppress a cached known envelope");

for (const contract of ["ops.backend-readiness.v2", "risk.summary.v2"]) {
  const retainedV15Payload = {
    status: "healthy",
    _fact: envelope(contract, "postgresql", now - 181, 180),
  };
  const retainedV15Fact = readFact(retainedV15Payload, contract);
  assert.equal(retainedV15Payload.status, "healthy", `${contract} retained value remains displayable`);
  assert.equal(retainedV15Fact.state, "stale", `${contract} retained fact must expire`);
  assert.equal(factBoundTone(retainedV15Fact, "ok"), "pending", `${contract} retained success must expose pending freshness`);
  assert.equal(factBoundTone(readFact({ _fact: envelope(contract, "postgresql") }, contract), "ok", true), "bad", `${contract} refetch failure must suppress V15 cached green`);
}

const staleSpotState = {
  _fact: envelope("live.state.v2", "ctrader", now, 30, {
    spot: envelope("live.spot-quote.v1", "ctrader_spot", now - 10, 5),
  }),
};
assert.equal(readFact(staleSpotState, "live.state.v2").state, "known");
assert.equal(
  readFactComponent(staleSpotState, "spot", "live.spot-quote.v1").state,
  "stale",
  "spot freshness must be evaluated independently of its parent state",
);

const positionsWithComponents = {
  _fact: envelope("live.positions.v2", "ctrader", now, 15, {
    broker_reconcile: {
      identity: envelope("live.positions.identity.v1", "ctrader_reconcile"),
      pnl: { ...envelope("live.positions.pnl.v1", "ctrader_pnl"), state: "error", reason_code: "pnl_rpc_failed" },
    },
  }),
};
assert.equal(
  readFactNestedComponent(positionsWithComponents, ["broker_reconcile", "identity"], "live.positions.identity.v1").state,
  "known",
  "position identity remains independently displayable",
);
assert.equal(
  readFactNestedComponent(positionsWithComponents, ["broker_reconcile", "pnl"], "live.positions.pnl.v1").state,
  "error",
  "position PnL failure must not normalize to a known zero",
);
const stateWithPositionComponents = {
  _fact: envelope("live.state.v2", "ctrader", now, 5, {
    positions: envelope("live.positions.v2", "ctrader", now, 15, {
      price: envelope("live.positions.price.v1", "ctrader_spot"),
    }),
  }),
};
assert.equal(
  readFactNestedComponent(stateWithPositionComponents, ["positions", "price"], "live.positions.price.v1").state,
  "known",
  "state snapshot position price must be read through its declared component path",
);

for (const source of ["", "none", "unknown", "unavailable", "not_registered", "degraded_cache"]) {
  const payload = { _fact: envelope("live.account.v2", source) };
  assert.equal(readFact(payload, "live.account.v2").state, "unknown", `${source || "empty"} must be unavailable`);
}

const stateWithoutAccount = {
  _fact: envelope("live.state.v2", "ctrader", now, 30, {
    account: envelope("live.account.v2", "none"),
  }),
};
const accountComponent = readFactComponent(stateWithoutAccount, "account", "live.account.v2");
const endpointAccount = readFact({ _fact: envelope("live.account.v2") }, "live.account.v2");
const mergedAccount = mergeFactRecord({ equity: 1250 }, { equity: 0 }, endpointAccount, accountComponent);
assert.equal(mergedAccount.equity, 1250, "unavailable state zero must not overwrite endpoint account fact");

const authenticated = { token: "kept-token", user: "operator", loading: true, authenticated: false };
assert.deepEqual(authStateAfterMeFailure(authenticated, 503), {
  token: "kept-token",
  user: "operator",
  loading: false,
  authenticated: true,
});
assert.equal(authStateAfterMeFailure(authenticated, 401).token, null);

const liveAutonomyStatus = {
  _fact: envelope("ops.live-autonomy-status.v2", "live_autonomy_service"),
  live_autonomy: {
    autonomy_mode: "live_candidate",
    evaluation: { ok: true, status: "unlock_ready", blockers: [] },
  },
  decoy: { evaluation: { ok: false, status: "blocked" } },
};
assert.equal(decodeLiveAutonomyStatus(liveAutonomyStatus).unlockAllowed, true);
assert.equal(
  decodeLiveAutonomyStatus({
    _fact: envelope("ops.live-autonomy-status.v2", "live_autonomy_service"),
    live_autonomy: { evaluation: { ok: false, status: "blocked" } },
    decoy: { evaluation: { ok: true, status: "unlock_ready" } },
  }).unlockAllowed,
  false,
  "decoder must not recursively discover an unrelated evaluation.ok",
);
assert.equal(
  decodeLiveAutonomyStatus({
    live_autonomy: { evaluation: { ok: true, status: "unlock_ready" } },
  }).unlockAllowed,
  false,
  "missing endpoint fact must fail closed",
);
assert.equal(
  decodeLiveAutonomyEvaluation({
    _fact: envelope("ops.live-autonomy-unlock-evaluation.v2", "live_autonomy_service"),
    evaluation: { ok: false, status: "blocked" },
  }).fact.state,
  "known",
  "a blocked business evaluation remains a known endpoint fact",
);

const endpointWithDecoys = {
  status: "blocked",
  items: [{ id: "authoritative" }],
  nested: {
    status: "healthy",
    ok: true,
    items: [{ id: "decoy" }],
  },
};
assert.equal(pick(endpointWithDecoys, ["status"]), "blocked");
assert.equal(
  pick({ nested: { status: "healthy" } }, ["status"]),
  undefined,
  "compat lookup must not recursively discover an unrelated status",
);
assert.deepEqual(
  pickArray(endpointWithDecoys, ["items"]),
  [{ id: "authoritative" }],
  "endpoint items must come from the declared top-level field",
);
assert.deepEqual(
  pickArray({ nested: { items: [{ id: "decoy" }] } }, ["items"]),
  [],
  "compat lookup must not recursively discover unrelated items",
);

console.log("web fact behavior: ok");
