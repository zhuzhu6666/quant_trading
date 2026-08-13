import { apiRequest, postJson } from "@/api/client";
import { factStateFromRaw, readFact, readFactComponent } from "@/api/fact";
import type { FactEnvelope } from "@/api/fact";
import type {
  AccountFact,
  AlertsView,
  Bar,
  DecisionTrace,
  ExecutionTrace,
  GovernanceRecord,
  IncidentControlView,
  LoopFact,
  MarketBars,
  MutationResult,
  Position,
  PositionsFact,
  ReadinessDimension,
  ReadinessView,
  RecoveryView,
  ResearchRow,
  ResearchSnapshot,
  RiskDeskData,
  RiskMetric,
  RiskPolicyVerdict,
  RiskSnapshot,
  RealizedPnlPoint,
  RealizedPnlScope,
  RealizedPnlSeries,
  SessionRiskFact,
  SpotFact,
  OpsHealth,
} from "@/types/contracts";

type UnknownObject = { [key: string]: unknown };

function object(value: unknown): UnknownObject {
  return value && typeof value === "object" && !Array.isArray(value) ? value as UnknownObject : {};
}

function array(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : [];
}

function stringValue(source: UnknownObject, key: string): string | null {
  return typeof source[key] === "string" && source[key].trim() ? source[key] as string : null;
}

function firstString(source: UnknownObject, ...keys: string[]): string | null {
  for (const key of keys) {
    const value = stringValue(source, key);
    if (value) return value;
  }
  return null;
}

function identifierValue(source: UnknownObject, key: string): string | null {
  const value = source[key];
  if (typeof value === "string" && value.trim()) return value;
  return typeof value === "number" && Number.isFinite(value) ? String(value) : null;
}

function numberValue(source: UnknownObject, key: string): number | null {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function booleanValue(source: UnknownObject, key: string): boolean | null {
  return typeof source[key] === "boolean" ? source[key] as boolean : null;
}

function stringList(value: unknown): string[] {
  return array(value).flatMap((entry) => {
    if (typeof entry === "string" && entry.trim()) return [entry];
    const source = object(entry);
    return [firstString(source, "reason_code", "code", "blocker", "message")].filter((item): item is string => Boolean(item));
  });
}

function timestampValue(source: UnknownObject): string | number | null {
  for (const key of ["observed_at", "updated_at", "created_at", "decision_ts", "catalog_ts", "lifecycle_updated_at", "health_updated_at", "last_action_ts"]) {
    const value = source[key];
    if ((typeof value === "string" && value.trim()) || (typeof value === "number" && Number.isFinite(value))) {
      return value as string | number;
    }
  }
  return null;
}

function arrayField(source: UnknownObject, key: string): readonly unknown[] {
  return array(source[key]);
}

function failedFactPayload(contract: string, reasonCode: string): UnknownObject {
  return {
    _fact: {
      envelope: "fact.v1",
      contract,
      state: "error",
      source: "none",
      observed_at: null,
      generated_at: null,
      stale_after_sec: 0,
      reason_code: reasonCode,
      components: {},
    },
  };
}

function factStatus(source: UnknownObject, key = "status") {
  return factStateFromRaw(source[key]);
}

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
  return {
    fact,
    serverTime: stringValue(source, "server_time"),
    broker: stringValue(source, "broker"),
    account,
    positions,
    loop,
    session,
    spot,
    safety,
    safetyBlockers,
    actionGates: Object.fromEntries(Object.entries(gates).filter(([, value]) => typeof value === "boolean")) as Readonly<Record<string, boolean>>,
  };
}

function readinessDimension(name: string, value: unknown, blockers: unknown, fact: FactEnvelope): ReadinessDimension {
  const source = typeof value === "boolean" ? { ready: value } : object(value);
  const dimensionBlockers = stringList(blockers);
  const dimensionFact = readFact(source, "ops.backend-readiness.v2");
  return {
    name,
    ready: booleanValue(source, "ready") ?? booleanValue(source, "ok") ?? (typeof value === "boolean" ? value : null),
    reasonCode: firstString(source, "reason_code", "reason", "status") ?? dimensionBlockers[0] ?? dimensionFact.reason_code ?? fact.reason_code,
    observedAt: source.observed_at as string | number | null ?? dimensionFact.observed_at ?? fact.observed_at ?? fact.generated_at,
  };
}

export function decodeReadiness(payload: unknown): ReadinessView {
  const source = object(payload);
  const readiness = object(source.readiness);
  const dimensionsPayload = object(source.readiness_dimensions);
  const dimensionBlockers = object(dimensionsPayload.blockers);
  const fact = readFact(source, "ops.backend-readiness.v2");
  const dimensionSpecs: [string, string, string][] = [
    ["live execution", "ready_for_live_execution", "live_execution"],
    ["live alpha", "ready_for_live_alpha", "live_alpha"],
    ["autonomous mutation", "ready_for_autonomous_mutation", "autonomous_mutation"],
    ["release", "ready_for_release", "release"],
  ];
  const dimensions = [
    ...dimensionSpecs.map(([name, flag, legacy]) => {
      const value = dimensionsPayload[flag] ?? source[flag] ?? readiness[legacy];
      return [name, value, dimensionBlockers[flag] ?? dimensionBlockers[legacy]] as const;
    }),
  ].filter(([, value]) => value !== undefined).map(([name, value, blockers]) => readinessDimension(name, value, blockers, fact));
  const blockers = [
    ...stringList(source.blockers),
    ...dimensionSpecs.flatMap(([, flag, legacy]) => stringList(dimensionBlockers[flag] ?? dimensionBlockers[legacy])),
  ].filter((value, index, values) => values.indexOf(value) === index);
  return {
    fact,
    dimensions,
    blockers,
    readyForFrontend: booleanValue(source, "ready_for_frontend"),
    readyForLiveExecution: booleanValue(source, "ready_for_live_execution"),
    readyForLiveAlpha: booleanValue(source, "ready_for_live_alpha"),
    readyForAutonomousMutation: booleanValue(source, "ready_for_autonomous_mutation"),
    readyForRelease: booleanValue(source, "ready_for_release"),
  };
}

function metric(value: unknown, key: string, unit: string): RiskMetric {
  const source = object(value);
  const status = factStatus(source);
  return {
    status,
    value: numberValue(source, key),
    unit,
    reasonCode: stringValue(source, "reason_code"),
  };
}

export function decodeRiskSnapshot(payload: unknown): { fact: ReturnType<typeof readFact>; snapshot: RiskSnapshot } {
  const source = object(payload);
  const fact = readFact(source, "risk.summary.v2");
  const snapshot = object(source.snapshot);
  const components = object(snapshot.components);
  const var95 = object(components.var);
  return {
    fact,
    snapshot: {
      schemaVersion: stringValue(snapshot, "schema_version"),
      status: factStatus(snapshot),
      sampleCount: numberValue(snapshot, "sample_count"),
      var95: metric(var95, "var_pct", "%"),
      cvar95: metric(var95, "cvar_pct", "%"),
      var99: metric(components.var_shadow_99, "var_pct", "%"),
      cvar99: metric(components.var_shadow_99, "cvar_pct", "%"),
      stressLossPct: metric(components.stress, "stress_loss_pct", "%"),
      concentrationPct: metric(components.concentration, "concentration_pct", "%"),
      kellyFraction: metric(components.kelly, "kelly_fraction", "fraction"),
    },
  };
}

function decodeVerdict(value: unknown, index: number): RiskPolicyVerdict {
  const source = object(value);
  const rawDecision = firstString(source, "decision", "verdict")?.toLowerCase();
  const allowed = booleanValue(source, "allowed");
  return {
    id: identifierValue(source, "decision_id") ?? identifierValue(source, "verdict_id") ?? identifierValue(source, "id") ?? `verdict-${index}`,
    action: firstString(source, "action", "action_name", "event_type") ?? "unknown",
    decision: allowed === true || rawDecision === "allow" || rawDecision === "allowed"
      ? "allow"
      : allowed === false || rawDecision === "block" || rawDecision === "blocked"
        ? "block"
        : "unknown",
    reasonCode: firstString(source, "reason_code", "reason", "execution_reason", "execution_category"),
    decisionAt: source.decision_ts as string | number | null ?? source.created_at as string | number | null ?? null,
  };
}

function decodeExecutionTrace(value: unknown, index: number): ExecutionTrace {
  const source = object(value);
  return {
    id: identifierValue(source, "review_id") ?? identifierValue(source, "trace_id") ?? identifierValue(source, "id") ?? `trace-${index}`,
    stage: firstString(source, "primary_responsibility", "stage", "execution_stage", "close_reason") ?? "unknown",
    outcome: firstString(source, "outcome_label", "outcome", "execution_outcome", "status") ?? "unknown",
    action: firstString(source, "action", "action_name", "close_reason"),
    reasonCode: firstString(source, "reason_code", "execution_reason", "close_reason", "primary_responsibility"),
    observedAt: timestampValue(source),
    tradeId: identifierValue(source, "trade_id"),
    positionId: identifierValue(source, "position_id"),
    symbol: stringValue(source, "symbol"),
    summary: firstString(source, "summary_text", "summary"),
  };
}

export function decodePolicyVerdicts(payload: unknown): { fact: ReturnType<typeof readFact>; items: RiskPolicyVerdict[] } {
  const source = object(payload);
  return { fact: readFact(source, "risk.policy-verdicts.v2"), items: arrayField(source, "items").map(decodeVerdict) };
}

export function decodeTradeTraces(payload: unknown): { fact: ReturnType<typeof readFact>; items: ExecutionTrace[] } {
  const source = object(payload);
  return { fact: readFact(source, "risk.trade-trace-recent.v2"), items: arrayField(source, "items").map(decodeExecutionTrace) };
}

export function decodeMarketBars(payload: unknown, symbol: string, timeframe: string): MarketBars {
  const source = object(payload);
  const bars: Bar[] = arrayField(source, "bars").flatMap((value) => {
    const bar = object(value);
    const t = numberValue(bar, "t");
    const o = numberValue(bar, "o");
    const h = numberValue(bar, "h");
    const l = numberValue(bar, "l");
    const c = numberValue(bar, "c");
    if ([t, o, h, l, c].some((entry) => entry === null)) return [];
    return [{ t: t as number, o: o as number, h: h as number, l: l as number, c: c as number, v: numberValue(bar, "v") ?? 0, spread: numberValue(bar, "spread") ?? 0 }];
  });
  const fact = readFact(source, "market.bars.v1");
  const range = object(source.range);
  return {
    fact,
    symbol,
    timeframe,
    bars,
    total: numberValue(source, "total") ?? bars.length,
    rangeFrom: numberValue(range, "from"),
    rangeTo: numberValue(range, "to"),
  };
}

function realizedPnlScope(value: unknown): RealizedPnlScope {
  return value === "today" || value === "24h" || value === "7d" || value === "30d" || value === "all" ? value : "today";
}

export function decodeRealizedPnlSeries(payload: unknown): RealizedPnlSeries {
  const source = object(payload);
  const rawPoints: Array<RealizedPnlPoint & { cumulativeFromServer: number | null }> = arrayField(source, "points").flatMap((value) => {
    const point = object(value);
    const ts = numberValue(point, "ts") ?? numberValue(point, "exec_timestamp") ?? numberValue(point, "closed_at");
    const pnl = numberValue(point, "pnl");
    if (ts === null || pnl === null) return [];
    return [{
      ts,
      pnl,
      cumulative: 0,
      cumulativeFromServer: numberValue(point, "cumulative"),
      source: stringValue(point, "source"),
    }];
  });
  rawPoints.sort((left, right) => left.ts - right.ts);
  let running = 0;
  const points = rawPoints.map(({ cumulativeFromServer, ...point }) => {
    running = cumulativeFromServer ?? running + point.pnl;
    return { ...point, cumulative: cumulativeFromServer ?? running };
  });
  const summary = object(source.summary);
  return {
    fact: readFact(source, "live.realized-pnl.v2"),
    scope: realizedPnlScope(source.scope),
    currency: stringValue(source, "currency"),
    fromTs: numberValue(source, "from_ts"),
    toTs: numberValue(source, "to_ts"),
    summary: {
      realizedPnl: numberValue(summary, "realized_pnl"),
      trades: numberValue(summary, "trades"),
      wins: numberValue(summary, "wins"),
      losses: numberValue(summary, "losses"),
      winRate: numberValue(summary, "win_rate"),
    },
    points,
  };
}

function researchRowsFromItems(values: readonly unknown[], indexPrefix: string): ResearchRow[] {
  return values.map((value, index) => {
    const row = object(value);
    return {
      id: identifierValue(row, "id") ?? identifierValue(row, "factor_id") ?? identifierValue(row, "candidate_id") ?? identifierValue(row, "suggestion_id") ?? `${indexPrefix}-${index}`,
      title: firstString(row, "title", "name", "factor_id", "action", "proposal_type", "event_type") ?? "未命名记录",
      state: firstString(row, "status", "state", "review_status", "lifecycle_status", "health_status", "outcome_label") ?? "unknown",
      reasonCode: firstString(row, "reason_code", "reason_excluded", "governance_action", "route_recommendation", "bridge_reason"),
      observedAt: timestampValue(row),
      detail: firstString(row, "summary", "description", "target_scope", "control_surface", "evidence_grade"),
    };
  });
}

function researchRows(payload: unknown, indexPrefix: string): ResearchRow[] {
  return researchRowsFromItems(arrayField(object(payload), "items"), indexPrefix);
}

function replayReportRows(report: UnknownObject, indexPrefix: string): ResearchRow[] {
  if (!Object.keys(report).length) return [];
  const replayId = identifierValue(report, "replay_run_id") ?? identifierValue(report, "run_id") ?? `${indexPrefix}-report`;
  const mismatchCount = numberValue(report, "mismatch_count");
  const decisionCount = numberValue(report, "decision_count");
  const matchedCount = numberValue(report, "matched_live_count");
  const evidenceGrade = stringValue(report, "evidence_grade");
  const detail = [
    decisionCount === null ? null : `决策 ${decisionCount}`,
    matchedCount === null ? null : `匹配 ${matchedCount}`,
    mismatchCount === null ? null : `不一致 ${mismatchCount}`,
    evidenceGrade ? `证据等级 ${evidenceGrade}` : null,
  ].filter((item): item is string => Boolean(item)).join(" · ");
  return [{
    id: replayId,
    title: `回放报告 · ${replayId}`,
    state: firstString(report, "status", "evidence_grade") ?? "unknown",
    reasonCode: firstString(report, "replay_error") ?? (mismatchCount !== null && mismatchCount > 0 ? `mismatch_count:${mismatchCount}` : null),
    observedAt: timestampValue(report),
    detail: detail || null,
  }];
}

function learningRows(payload: UnknownObject): ResearchRow[] {
  const rows: ResearchRow[] = [];
  const latestReview = object(payload.latest_review);
  if (Object.keys(latestReview).length) {
    const reviewId = identifierValue(latestReview, "review_id") ?? identifierValue(latestReview, "trade_id") ?? "latest-review";
    rows.push({
      id: reviewId,
      title: `最近复盘 · ${firstString(latestReview, "outcome_label", "trade_id") ?? reviewId}`,
      state: firstString(latestReview, "outcome_label", "status") ?? "recorded",
      reasonCode: firstString(latestReview, "trace_locator", "reason_code"),
      observedAt: timestampValue(latestReview),
      detail: firstString(latestReview, "summary_text", "summary") ?? (numberValue(latestReview, "pnl") === null ? null : `盈亏 ${numberValue(latestReview, "pnl")}`),
    });
  }
  const latestCandidate = object(payload.latest_parameter_template_candidate);
  if (Object.keys(latestCandidate).length) {
    const candidateId = identifierValue(latestCandidate, "candidate_id") ?? "latest-template-candidate";
    rows.push({
      id: candidateId,
      title: `参数模板候选 · ${firstString(latestCandidate, "factor_id", "template_id") ?? candidateId}`,
      state: firstString(latestCandidate, "status") ?? "recorded",
      reasonCode: firstString(latestCandidate, "template_id", "factor_id"),
      observedAt: timestampValue(latestCandidate),
      detail: firstString(latestCandidate, "updated_at", "created_at"),
    });
  }
  const aggregateLabels: Record<string, string> = {
    suggestions: "学习建议",
    reviews: "学习复核",
    applications: "学习应用",
    parameter_template_candidates: "参数模板候选",
    parameter_template_recommendations: "参数模板推荐",
  };
  for (const [key, label] of Object.entries(aggregateLabels)) {
    const value = payload[key];
    const total: number = typeof value === "number" ? value : Object.values(object(value)).reduce<number>((sum, entry) => sum + (typeof entry === "number" && Number.isFinite(entry) ? entry : 0), 0);
    if (total <= 0 && value === undefined) continue;
    rows.push({
      id: `learning-${key}`,
      title: label,
      state: "aggregate",
      reasonCode: null,
      observedAt: timestampValue(payload),
      detail: `累计 ${total} 条`,
    });
  }
  return rows;
}

export function decodeResearchSnapshot(payload: unknown, contract: ResearchSnapshot["contract"], title: string, indexPrefix: string): ResearchSnapshot {
  const source = object(payload);
  const fact = readFact(source, contract);
  const report = object(source.report);
  const rows = contract === "ops.replay-latest.v2"
    ? replayReportRows(report, indexPrefix)
    : contract === "learning.summary.v2"
      ? learningRows(source)
      : researchRows(source, indexPrefix);
  return {
    fact,
    contract,
    title,
    referenceId: firstString(source, "replay_id", "report_id", "run_id") ?? identifierValue(report, "replay_run_id") ?? identifierValue(report, "run_id"),
    observedAt: fact.observed_at,
    status: fact.state,
    rows,
  };
}

function governanceRecord(value: unknown, kind: GovernanceRecord["kind"], index: number): GovernanceRecord {
  const source = object(value);
  const bridgeReady = booleanValue(source, "bridge_ready");
  const evidenceGaps = stringList(source.evidence_gaps);
  const status = firstString(source, "status", "review_status", "state") ?? (bridgeReady === true ? "bridge_ready" : "unknown");
  const observedAt = timestampValue(source)
    ?? numberValue(source, "updated_at")
    ?? numberValue(source, "created_at")
    ?? numberValue(source, "reviewed_at")
    ?? numberValue(source, "committed_at");
  return {
    id: identifierValue(source, "id") ?? identifierValue(source, "candidate_id") ?? identifierValue(source, "review_id") ?? identifierValue(source, "proposal_id") ?? identifierValue(source, "run_id") ?? `${kind}-${index}`,
    kind,
    status,
    durableId: identifierValue(source, "mutation_id") ?? identifierValue(source, "run_id") ?? identifierValue(source, "proposal_id") ?? identifierValue(source, "candidate_id"),
    auditId: identifierValue(source, "audit_id"),
    commitStatus: firstString(source, "commit_status", "commit_status_label") ?? (bridgeReady === true ? "bridge_ready" : null),
    reasonCode: firstString(source, "reason_code", "bridge_reason", "route_recommendation", "governance_action") ?? (evidenceGaps.length ? `evidence_gaps:${evidenceGaps.join(",")}` : null),
    observedAt,
    source: firstString(source, "source_agent", "source", "control_surface"),
    stage: firstString(source, "review_status", "proposal_type", "release_class"),
    action: firstString(source, "proposal_action", "action", "release_class"),
    target: firstString(source, "target_scope", "target", "control_surface"),
    authorityState: firstString(source, "authority_state", "route_recommendation"),
  };
}

export function decodeGovernanceRecords(payload: unknown, kind: GovernanceRecord["kind"], contract: string): { fact: ReturnType<typeof readFact>; items: GovernanceRecord[] } {
  const source = object(payload);
  const containerKey = kind === "candidate" ? "governance_candidates" : kind === "review" ? "candidate_reviews" : kind === "proposal" ? "proposals" : null;
  const container = containerKey ? object(source[containerKey]) : {};
  const values = kind === "release"
    ? Object.keys(object(source.release)).length ? [source.release] : arrayField(source, "items")
    : arrayField(container, "items").length ? arrayField(container, "items") : arrayField(source, "items");
  return { fact: readFact(source, contract), items: values.map((value, index) => governanceRecord(value, kind, index)) };
}

export function decodeDecisionTrace(payload: unknown, contract = "research.decision-trace.v1"): { fact: ReturnType<typeof readFact>; items: DecisionTrace[] } {
  const source = object(payload);
  return {
    fact: readFact(source, contract),
    items: arrayField(source, "items").map((value, index) => {
      const row = object(value);
      return {
        traceId: stringValue(row, "trace_id") ?? stringValue(row, "id") ?? `decision-trace-${index}`,
        decisionId: identifierValue(row, "decision_id"),
        positionId: identifierValue(row, "position_id"),
        source: firstString(row, "source", "symbol") ?? "server",
        lineage: firstString(row, "lineage", "lineage_id", "trade_id"),
        reasonCode: firstString(row, "reason_code", "action_reason", "outcome_result"),
        observedAt: timestampValue(row),
        eventType: stringValue(row, "event_type"),
        symbol: stringValue(row, "symbol"),
        timeframe: stringValue(row, "timeframe"),
        direction: stringValue(row, "direction"),
        actionReason: stringValue(row, "action_reason"),
        outcomeStatus: stringValue(row, "outcome_status"),
        outcomeResult: stringValue(row, "outcome_result"),
        outcomeLabel: stringValue(row, "outcome_label"),
        pnl: numberValue(row, "pnl"),
        learningStatus: stringValue(row, "learning_status"),
        actionScore: numberValue(row, "action_score"),
      };
    }),
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

export const getReadinessView = () => apiRequest<unknown>("/api/ops/backend-readiness").then(decodeReadiness);
export const getRiskSnapshot = () => apiRequest<unknown>("/api/risk/summary").then(decodeRiskSnapshot);
export const getRiskPolicyVerdicts = (limit = 30) => apiRequest<unknown>(`/api/risk/policy/verdicts?limit=${limit}`).then(decodePolicyVerdicts);
export const getTradeTraces = (limit = 30) => apiRequest<unknown>(`/api/risk/trade-trace/recent?limit=${limit}`).then(decodeTradeTraces);

export async function getRiskDeskData(): Promise<RiskDeskData> {
  const [riskResult, policyResult, tracesResult] = await Promise.allSettled([getRiskSnapshot(), getRiskPolicyVerdicts(), getTradeTraces()]);
  const risk = riskResult.status === "fulfilled" ? riskResult.value : decodeRiskSnapshot(failedFactPayload("risk.summary.v2", "risk_summary_request_failed"));
  const policy = policyResult.status === "fulfilled" ? policyResult.value : decodePolicyVerdicts(failedFactPayload("risk.policy-verdicts.v2", "risk_policy_request_failed"));
  const traces = tracesResult.status === "fulfilled" ? tracesResult.value : decodeTradeTraces(failedFactPayload("risk.trade-trace-recent.v2", "risk_trade_trace_request_failed"));
  return { fact: risk.fact, policyFact: policy.fact, traceFact: traces.fact, snapshot: risk.snapshot, verdicts: policy.items, traceRows: traces.items };
}

export function getMarketBars(symbol = "XAUUSD+", timeframe = "M15", limit = 180): Promise<MarketBars> {
  const params = new URLSearchParams({ symbol, timeframe, limit: String(limit) });
  return apiRequest<unknown>(`/api/market/bars?${params.toString()}`).then((payload) => decodeMarketBars(payload, symbol, timeframe));
}

export function getRealizedPnlSeries(scope: RealizedPnlScope = "all"): Promise<RealizedPnlSeries> {
  const params = new URLSearchParams({ scope, tz: "Asia/Shanghai" });
  return apiRequest<unknown>(`/api/live/realized-pnl-series?${params.toString()}`).then(decodeRealizedPnlSeries);
}

export const getFactorCatalogSnapshot = () => apiRequest<unknown>("/api/v4/catalog?snapshot=latest").then((payload) => decodeResearchSnapshot(payload, "factor.catalog.v4", "因子目录", "factor"));
export const getReplaySnapshot = () => apiRequest<unknown>("/api/ops/replay/latest").then((payload) => decodeResearchSnapshot(payload, "ops.replay-latest.v2", "最近回放", "replay"));
export const getReplayDecisionTrace = () => apiRequest<unknown>("/api/ops/replay/bar-decisions?limit=30").then((payload) => decodeDecisionTrace(payload, "ops.replay-bar-decisions.v2"));
export const getLearningResearchSnapshot = () => apiRequest<unknown>("/api/learning/summary").then((payload) => decodeResearchSnapshot(payload, "learning.summary.v2", "学习证据", "learning"));
export const getGovernanceCandidates = () => apiRequest<unknown>("/api/ops/brain/governance-candidates?limit=40").then((payload) => decodeGovernanceRecords(payload, "candidate", "ops.v16-governance-candidates.v2"));
export const getGovernanceReviews = () => apiRequest<unknown>("/api/ops/brain/governance-candidate-reviews?limit=40").then((payload) => decodeGovernanceRecords(payload, "review", "ops.v16-governance-candidate-reviews.v2"));
export const getGovernanceProposals = () => apiRequest<unknown>("/api/ops/autonomy/proposals?limit=40").then((payload) => decodeGovernanceRecords(payload, "proposal", "ops.autonomy-proposals.v2"));
export const getReleaseEvidence = () => apiRequest<unknown>("/api/ops/release/latest").then((payload) => decodeGovernanceRecords(payload, "release", "ops.release-latest.v2"));
function decodeHealth(payload: unknown): OpsHealth {
  const source = object(payload);
  return {
    fact: readFact(source, "system.health.v2"),
    source: {
      status: stringValue(source, "status"),
      db: stringValue(source, "db"),
      ctrader: stringValue(source, "ctrader"),
      serverTime: stringValue(source, "server_time"),
      uptimeSeconds: numberValue(source, "uptime_seconds"),
    },
  };
}

function decodeRecovery(payload: unknown): RecoveryView {
  const source = object(payload);
  return {
    fact: readFact(source, "ops.auto-recovery.v2"),
    source: {
      status: stringValue(source, "status"),
      registered: booleanValue(source, "registered"),
      running: booleanValue(source, "running"),
      loopHealthy: booleanValue(source, "loop_healthy"),
      schedulerHealthy: booleanValue(source, "scheduler_healthy"),
      failures: numberValue(source, "failures"),
      lastCheck: numberValue(source, "last_check"),
      restartAttempts: numberValue(source, "restart_attempts"),
    },
  };
}

function decodeAlerts(payload: unknown): AlertsView {
  const source = object(payload);
  const delivery = object(source.delivery);
  return {
    fact: readFact(source, "ops.alerts.v2"),
    source: {
      status: stringValue(source, "status"),
      configStatus: stringValue(source, "config_status"),
      rulesActive: numberValue(source, "rules_active"),
      deliveryStatus: stringValue(delivery, "status"),
      deliveryRegistered: booleanValue(delivery, "registered"),
    },
  };
}

export const getHealth = () => apiRequest<unknown>("/api/health").then(decodeHealth);
function decodeIncidentControl(payload: unknown): IncidentControlView {
  const source = object(payload);
  const status = object(source.incident_control);
  const latch = object(status.local_safety_latch ?? source.local_safety_latch);
  return {
    fact: readFact(source, "ops.incident-control.v2"),
    effectiveMode: firstString(status, "effective_mode", "mode") ?? firstString(source, "effective_mode", "mode"),
    configuredMode: firstString(status, "configured_mode", "mode") ?? stringValue(source, "configured_mode"),
    localSafetyLatch: booleanValue(status, "local_safety_latch") ?? booleanValue(source, "local_safety_latch") ?? booleanValue(latch, "active"),
  };
}
export const getIncidentControl = () => apiRequest<unknown>("/api/ops/incident-control").then(decodeIncidentControl);
export const getRecovery = () => apiRequest<unknown>("/api/ops/recovery").then(decodeRecovery);
export const getAlerts = () => apiRequest<unknown>("/api/ops/alerts").then(decodeAlerts);

export async function submitStart(): Promise<MutationResult> {
  return postJson<unknown>("/api/live/start", { broker: "ctrader", strategy_name: "factor_v4" }, { "X-Confirm": "start-live" }).then(decodeMutationResult);
}

export async function submitStop(): Promise<MutationResult> {
  return postJson<unknown>("/api/live/stop", {}).then(decodeMutationResult);
}

export async function submitEmergencyClose(): Promise<MutationResult> {
  return postJson<unknown>("/api/live/emergency-close", { broker: "ctrader", symbol: null }, { "X-Confirm": "emergency" }).then(decodeMutationResult);
}

export async function submitIncidentTighten(mode: "no_new_risk" | "only_close" | "frozen"): Promise<MutationResult> {
  return postJson<unknown>("/api/ops/incident-control", {
    mode,
    reason: `workbench:${mode}`,
    confirm_thaw: false,
  }).then(decodeMutationResult);
}

export async function runReplay(decisionId?: string): Promise<ResearchSnapshot> {
  const params = new URLSearchParams({ lookback_days: decisionId ? "7" : "1", limit: "1", warmup_bars: "40", post_bars: "24" });
  if (decisionId) params.set("decision_id", decisionId);
  return postJson<unknown>(`/api/ops/replay/bar-preview?${params.toString()}`, {}).then((payload) => decodeResearchSnapshot(payload, "ops.replay-bar-preview.v2", "回放预览", "replay-preview"));
}
