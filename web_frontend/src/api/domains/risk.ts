import { apiRequest } from "@/api/client";
import { readFact } from "@/api/fact";
import type {
  ExecutionTrace,
  RiskDeskData,
  RiskMetric,
  RiskPolicyVerdict,
  RiskSnapshot,
} from "@/types/contracts";
import {
  arrayField,
  booleanValue,
  failedFactPayload,
  factStatus,
  firstString,
  identifierValue,
  numberValue,
  object,
  stringOrNumberValue,
  stringValue,
  timestampValue,
} from "@/api/domains/shared";

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
    decisionId: identifierValue(source, "decision_id"),
    positionId: identifierValue(source, "position_id"),
    eventType: firstString(source, "event_type"),
    symbol: stringValue(source, "symbol"),
    timeframe: stringValue(source, "timeframe"),
    direction: stringOrNumberValue(source, "direction") ?? stringValue(source, "side"),
    gatePassed: booleanValue(source, "gate_passed"),
    gateReason: firstString(source, "gate_reason"),
    admissionGatePassed: booleanValue(source, "admission_gate_passed"),
    riskPolicyReached: booleanValue(source, "risk_policy_reached"),
    actionReason: firstString(source, "action_reason"),
    executionStatus: firstString(source, "execution_status"),
    executionOutcome: firstString(source, "execution_outcome"),
    executionReason: firstString(source, "execution_reason"),
    executionApplied: booleanValue(source, "execution_applied"),
    executionCategory: firstString(source, "execution_category"),
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

export function decodePolicyVerdicts(payload: unknown): { fact: ReturnType<typeof readFact>; items: RiskPolicyVerdict[]; prePolicySkips: RiskPolicyVerdict[] } {
  const source = object(payload);
  const prePolicySkips = arrayField(source, "pre_policy_skips").map((value, index) => {
    const skip = object(value);
    return decodeVerdict({
      ...skip,
      action: "open_admission_gate",
      allowed: false,
      decision: "block",
      reason: firstString(skip, "action_reason", "gate_reason", "admission_owner") ?? "risk_not_reached",
      execution_category: "not_reached",
    }, index);
  });
  return {
    fact: readFact(source, "risk.policy-verdicts.v2"),
    items: arrayField(source, "items").map(decodeVerdict),
    prePolicySkips,
  };
}

export function decodeTradeTraces(payload: unknown): { fact: ReturnType<typeof readFact>; items: ExecutionTrace[] } {
  const source = object(payload);
  return { fact: readFact(source, "risk.trade-trace-recent.v2"), items: arrayField(source, "items").map(decodeExecutionTrace) };
}

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
