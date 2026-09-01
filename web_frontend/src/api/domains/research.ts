import { apiRequest, postJson } from "@/api/client";
import { readFact } from "@/api/fact";
import type {
  DecisionTrace,
  ResearchRow,
  ResearchSnapshot,
} from "@/types/contracts";
import {
  arrayField,
  firstString,
  identifierValue,
  numberValue,
  numericValue,
  object,
  stringOrNumberValue,
  stringValue,
  timestampValue,
  type UnknownObject,
} from "@/api/domains/shared";

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

export function decodeDecisionTrace(payload: unknown, contract = "research.decision-trace.v1"): { fact: ReturnType<typeof readFact>; items: DecisionTrace[] } {
  const source = object(payload);
  return {
    fact: readFact(source, contract),
    items: arrayField(source, "items").map((value, index) => {
      const row = object(value);
      const outcome = object(row.outcome);
      const systemView = object(row.system_view);
      const entryTs = numericValue(row, "entry_ts") ?? numericValue(row, "decision_ts");
      const exitTs = numericValue(row, "exit_ts") ?? numericValue(row, "close_ts") ?? numericValue(outcome, "close_ts");
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
        direction: stringOrNumberValue(row, "direction") ?? stringValue(row, "direction_label") ?? stringOrNumberValue(systemView, "direction"),
        actionReason: stringValue(row, "action_reason"),
        systemView: Object.keys(systemView).length ? {
          direction: stringOrNumberValue(systemView, "direction") ?? stringOrNumberValue(row, "direction"),
          directionLabel: stringValue(systemView, "direction_label") ?? stringValue(row, "direction_label"),
          score: numericValue(systemView, "score") ?? numericValue(row, "action_score"),
          actionReason: stringValue(systemView, "action_reason") ?? stringValue(row, "action_reason"),
          outcomeStatus: stringValue(systemView, "outcome_status") ?? stringValue(row, "outcome_status"),
          outcomeResult: stringValue(systemView, "outcome_result") ?? stringValue(row, "outcome_result"),
          outcomeLabel: stringValue(systemView, "outcome_label") ?? stringValue(row, "outcome_label"),
          pnl: numericValue(systemView, "pnl") ?? numericValue(row, "pnl"),
          closeReason: stringValue(systemView, "close_reason") ?? stringValue(row, "close_reason"),
          summary: stringValue(systemView, "summary"),
        } : null,
        entryTs,
        exitTs,
        exitDecisionId: identifierValue(row, "exit_decision_id") ?? identifierValue(outcome, "exit_decision_id"),
        closeReason: stringValue(row, "close_reason") ?? stringValue(outcome, "close_reason"),
        holdingSeconds: numericValue(row, "holding_seconds") ?? (entryTs !== null && exitTs !== null && exitTs > entryTs ? exitTs - entryTs : null),
        outcomeStatus: stringValue(row, "outcome_status") ?? stringValue(outcome, "status"),
        outcomeResult: stringValue(row, "outcome_result") ?? stringValue(outcome, "result"),
        outcomeLabel: stringValue(row, "outcome_label") ?? stringValue(outcome, "outcome_label"),
        pnl: numericValue(row, "pnl") ?? numericValue(outcome, "pnl"),
        learningStatus: stringValue(row, "learning_status"),
        actionScore: numberValue(row, "action_score"),
      };
    }),
  };
}

export const getFactorCatalogSnapshot = () => apiRequest<unknown>("/api/v4/catalog?snapshot=latest").then((payload) => decodeResearchSnapshot(payload, "factor.catalog.v4", "因子目录", "factor"));
export const getReplaySnapshot = () => apiRequest<unknown>("/api/ops/replay/latest").then((payload) => decodeResearchSnapshot(payload, "ops.replay-latest.v2", "最近回放", "replay"));
export const getReplayDecisionTrace = (lookbackDays = 30, limit = 60) => {
  const params = new URLSearchParams({ lookback_days: String(lookbackDays), limit: String(limit) });
  return apiRequest<unknown>(`/api/ops/replay/bar-decisions?${params.toString()}`).then((payload) => decodeDecisionTrace(payload, "ops.replay-bar-decisions.v2"));
};

export async function runReplay(decisionId?: string, warmupBars = 40, postBars = 24): Promise<ResearchSnapshot> {
  const params = new URLSearchParams({ lookback_days: decisionId ? "7" : "1", limit: "1", warmup_bars: String(warmupBars), post_bars: String(postBars) });
  if (decisionId) params.set("decision_id", decisionId);
  return postJson<unknown>(`/api/ops/replay/bar-preview?${params.toString()}`, {}).then((payload) => decodeResearchSnapshot(payload, "ops.replay-bar-preview.v2", "回放预览", "replay-preview"));
}
