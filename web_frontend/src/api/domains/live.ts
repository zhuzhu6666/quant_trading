import { postJson } from "@/api/client";
import { readFact, readFactComponent } from "@/api/fact";
import type {
  AccountFact,
  LoopFact,
  MutationResult,
  Position,
  PositionsFact,
  SessionRiskFact,
  SpotFact,
} from "@/types/contracts";
import {
  booleanValue,
  decisionBarTimestampValue,
  firstString,
  identifierValue,
  numberValue,
  object,
  stringList,
  stringOrNumberValue,
  stringValue,
  timestampValue,
  type UnknownObject,
} from "@/api/domains/shared";

function readPosition(value: unknown, index: number): Position {
  const source = object(value);
  const numericDirection = source.direction === 1 ? "long" : source.direction === 2 ? "short" : null;
  const rawDirection = (stringValue(source, "direction")?.toLowerCase() ?? numericDirection ?? stringValue(source, "type")?.toLowerCase());
  const direction: Position["direction"] = rawDirection === "long" || rawDirection === "buy"
    ? "long"
    : rawDirection === "short" || rawDirection === "sell"
      ? "short"
      : "unknown";
  return {
    id: identifierValue(source, "position_id") ?? identifierValue(source, "id") ?? `unknown-position-${index}`,
    symbol: stringValue(source, "symbol") ?? "",
    direction,
    volume: numberValue(source, "volume"),
    entryPrice: numberValue(source, "entry_price") ?? numberValue(source, "entryPrice") ?? numberValue(source, "price_open") ?? numberValue(source, "open_price"),
    currentPrice: numberValue(source, "current_price") ?? numberValue(source, "currentPrice"),
    stopLoss: numberValue(source, "stop_loss") ?? numberValue(source, "stopLoss") ?? numberValue(source, "sl"),
    takeProfit: numberValue(source, "take_profit") ?? numberValue(source, "takeProfit") ?? numberValue(source, "tp"),
    unrealizedPnl: numberValue(source, "unrealized_pnl") ?? numberValue(source, "unrealizedPnl") ?? numberValue(source, "pnl") ?? numberValue(source, "profit"),
    observedAt: source.observed_at as string | number | null ?? null,
  };
}

export function decodeAccount(payload: unknown): AccountFact {
  const source = object(payload);
  return {
    fact: readFact(source, "live.account.v2"),
    broker: stringValue(source, "broker"),
    balance: numberValue(source, "balance"),
    equity: numberValue(source, "equity"),
    margin: numberValue(source, "margin"),
    freeMargin: numberValue(source, "margin_free") ?? numberValue(source, "free_margin"),
    currency: stringValue(source, "currency"),
  };
}

export function decodePositions(payload: unknown): PositionsFact {
  const source = object(payload);
  const fact = readFact(source, "live.positions.v2");
  const reconcile = object(fact.components?.broker_reconcile);
  return {
    fact,
    positions: Array.isArray(source.positions) ? source.positions.map(readPosition) : null,
    brokerReconcile: {
      identity: readFactComponent({ _fact: { components: { identity: reconcile.identity } } }, "identity", "live.positions.identity.v1"),
      protection: readFactComponent({ _fact: { components: { protection: reconcile.protection } } }, "protection", "live.positions.protection.v1"),
      price: readFactComponent({ _fact: { components: { price: reconcile.price } } }, "price", "live.positions.price.v1"),
      pnl: readFactComponent({ _fact: { components: { pnl: reconcile.pnl } } }, "pnl", "live.positions.pnl.v1"),
    },
  };
}

export function decodeLoop(payload: unknown): LoopFact {
  const source = object(payload);
  const fact = readFact(source, "live.loop.v2");
  return {
    fact,
    running: booleanValue(source, "running"),
    acceptingNewRisk: booleanValue(source, "accepting_new_risk") ?? booleanValue(source, "acceptingNewRisk"),
    broker: stringValue(source, "broker"),
    reasonCode: fact.reason_code,
  };
}

export function decodeSession(payload: unknown): SessionRiskFact {
  const source = object(payload);
  return {
    fact: readFact(source, "live.session-risk.v2"),
    pnlToday: numberValue(source, "pnl_today") ?? numberValue(source, "pnlToday"),
    tradeCount: numberValue(source, "trade_count") ?? numberValue(source, "trades"),
    drawdownPct: numberValue(source, "drawdown_pct") ?? numberValue(source, "drawdownPct"),
    consecutiveLosses: numberValue(source, "consecutive_loss") ?? numberValue(source, "consecutiveLoss"),
  };
}

export function decodeSpot(payload: unknown): SpotFact {
  const source = object(payload);
  return {
    fact: readFact(source, "live.spot-quote.v1"),
    bid: numberValue(source, "bid"),
    ask: numberValue(source, "ask"),
    mid: numberValue(source, "mid"),
  };
}

function decodeFactorComposite(value: unknown) {
  const source = object(value);
  return {
    gatePassed: booleanValue(source, "gate_passed"),
    gateReason: firstString(source, "gate_reason", "reason", "action_reason"),
    direction: stringOrNumberValue(source, "direction") ?? stringValue(source, "side"),
    score: numberValue(source, "score"),
    factorSetVersion: firstString(source, "factor_set_version", "factor_version"),
    activeFactors: numberValue(source, "n_active_factors"),
    availableFactors: numberValue(source, "n_available_factors"),
    scoringFactors: numberValue(source, "n_scoring_factors"),
    contributingFactors: numberValue(source, "n_contributing_factors"),
    abstainFactors: numberValue(source, "n_abstain_factors"),
    decisionBarAt: decisionBarTimestampValue(source),
    observedAt: timestampValue(source),
  };
}

function decodeSnapshotComponent<T>(payload: UnknownObject, name: string, decode: (value: unknown) => T, value: unknown = payload): T {
  const components = object(readFact(payload, "live.state.v2").components);
  const domain = object(value);
  const componentFact = object(components[name]);
  return decode({ ...domain, _fact: componentFact });
}

export function decodeLiveSnapshot(payload: unknown) {
  const source = object(payload);
  const fact = readFact(source, "live.state.v2");
  const strategyStatus = object(source.strategy_status);
  const strategyV4Status = object(strategyStatus.v4_status);
  const daily = object(source.daily);
  const risk = object(source.risk);
  const account = decodeSnapshotComponent(source, "account", decodeAccount, source.account ?? source);
  const positions = decodeSnapshotComponent(source, "positions", decodePositions, source.positions ?? { ...source, positions: source.positions_list });
  const loop = decodeSnapshotComponent(source, "loop", decodeLoop, source.loop_status ?? {
    ...strategyStatus,
    broker: stringValue(strategyStatus, "broker") ?? stringValue(source, "broker"),
    running: booleanValue(strategyStatus, "running"),
    accepting_new_risk: booleanValue(strategyStatus, "accepting_new_risk") ?? booleanValue(source, "accepting_new_risk"),
  });
  const session = decodeSnapshotComponent(source, "session", decodeSession, source.session_stats ?? {
    ...source,
    trade_count: numberValue(source, "trade_count") ?? numberValue(daily, "trades"),
    drawdown_pct: numberValue(source, "drawdown_pct") ?? numberValue(daily, "drawdown_pct"),
    consecutive_loss: numberValue(source, "consecutive_loss") ?? numberValue(risk, "consecutive_loss"),
  });
  const spot = decodeSnapshotComponent(source, "spot", decodeSpot, source.spot_quote ?? source.spot ?? source);
  const safety = readFactComponent({ _fact: fact }, "safety", "live.safety-freshness.v1");
  const safetyBlockers = stringList(
    strategyStatus.safety_blockers
      ?? strategyStatus.new_risk_reconcile_blockers
      ?? source.safety_blockers
      ?? source.new_risk_reconcile_blockers,
  );
  const gates = object(source.action_gates);
  const pipelineFact = readFactComponent({ _fact: fact }, "strategy", "live.strategy.v2");
  return {
    fact,
    serverTime: stringValue(source, "server_time"),
    broker: stringValue(source, "broker"),
    account,
    positions,
    loop,
    pipeline: {
      fact: pipelineFact,
      strategy: firstString(strategyStatus, "strategy", "name"),
      mode: firstString(strategyStatus, "mode"),
      executionMode: firstString(strategyStatus, "execution_mode"),
      active: booleanValue(strategyV4Status, "pipeline_active") ?? booleanValue(strategyStatus, "running"),
      engineWarm: booleanValue(strategyV4Status, "engine_warm"),
      bufferSize: numberValue(strategyV4Status, "buffer_size"),
      factorVotes: object(source.factor_votes),
      composite: decodeFactorComposite(source.last_composite),
    },
    session,
    spot,
    safety,
    safetyBlockers,
    actionGates: Object.fromEntries(Object.entries(gates).filter(([, value]) => typeof value === "boolean")) as Readonly<Record<string, boolean>>,
  };
}

export function decodeMutationResult(payload: unknown): MutationResult {
  const source = object(payload);
  const status = stringValue(source, "status")?.toLowerCase();
  const commitStatus = stringValue(source, "commit_status")?.toLowerCase();
  return {
    status: status === "committed" || status === "applied" ? "committed" : status === "rejected" || status === "blocked" ? "rejected" : status === "pending" ? "pending" : status === "aborted" ? "aborted" : "unknown",
    mutationId: stringValue(source, "mutation_id"),
    auditId: stringValue(source, "audit_id"),
    commitStatus: commitStatus === "committed" ? "committed" : commitStatus === "pending" ? "pending" : commitStatus === "not_committed" ? "not_committed" : "unknown",
    reasonCode: stringValue(source, "reason_code") ?? stringValue(source, "error"),
    observedAt: source.committed_at as string | number | null ?? source.updated_at as string | number | null ?? null,
  };
}

export async function submitStart(): Promise<MutationResult> {
  return postJson<unknown>("/api/live/start", { broker: "ctrader", strategy_name: "factor_v4" }, { "X-Confirm": "start-live" }).then(decodeMutationResult);
}

export async function submitStop(): Promise<MutationResult> {
  return postJson<unknown>("/api/live/stop", {}).then(decodeMutationResult);
}

export async function submitEmergencyClose(): Promise<MutationResult> {
  return postJson<unknown>("/api/live/emergency-close", { broker: "ctrader", symbol: null }, { "X-Confirm": "emergency" }).then(decodeMutationResult);
}
