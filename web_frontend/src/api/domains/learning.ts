import { apiRequest } from "@/api/client";
import { readFact } from "@/api/fact";
import type {
  LearningApplicationRecord,
  LearningDatasetBlocker,
  LearningDatasetQuality,
  LearningDatasetReadinessView,
  LearningEffectQualityView,
  LearningFactList,
  LearningLoopData,
  LearningModelRecord,
  LearningQualityHealthView,
  LearningReviewRecord,
  LearningSampleRecord,
  LearningSuggestionRecord,
} from "@/types/contracts";
import type { FactEnvelope } from "@/api/fact";
import type { GovernanceRecord } from "@/types/contracts";
import {
  array,
  arrayField,
  booleanValue,
  failedFactPayload,
  firstString,
  identifierValue,
  numberValue,
  numericValue,
  object,
  stringList,
  stringOrNumberValue,
  stringValue,
  timestampValue,
} from "@/api/domains/shared";
import { decodeGovernanceRecords, getGovernanceCandidates, getGovernanceProposals, getGovernanceReviews } from "@/api/domains/governance";
import { decodeResearchSnapshot } from "@/api/domains/research";

function numericMap(value: unknown): Record<string, number> {
  return Object.fromEntries(
    Object.entries(object(value)).flatMap(([key, raw]) => {
      const parsed = typeof raw === "number" && Number.isFinite(raw)
        ? raw
        : typeof raw === "string" && raw.trim() && Number.isFinite(Number(raw))
          ? Number(raw)
          : null;
      return parsed === null ? [] : [[key, parsed]];
    }),
  );
}

function decodeLearningFactList<T>(
  payload: unknown,
  contract: string,
  decodeItem: (value: unknown, index: number) => T,
): LearningFactList<T> {
  const source = object(payload);
  const items = arrayField(source, "items").map(decodeItem);
  return {
    fact: readFact(source, contract),
    items,
    count: numberValue(source, "count") ?? items.length,
  };
}

function decodeLearningSample(value: unknown, index: number): LearningSampleRecord {
  const source = object(value);
  const evidence = object(source.evidence_contract);
  const quality = object(evidence.quality);
  return {
    id: identifierValue(source, "sample_id") ?? `learning-sample-${index}`,
    sampleType: firstString(source, "sample_type") ?? "unknown",
    labelStatus: firstString(source, "label_status") ?? "unknown",
    integrity: firstString(source, "integrity") ?? "unknown",
    trainWeight: numericValue(source, "train_weight"),
    modelReady: booleanValue(quality, "model_ready") ?? booleanValue(evidence, "model_ready") ?? booleanValue(source, "model_ready"),
    governanceEligible: booleanValue(source, "governance_eligible"),
    systemContaminated: booleanValue(source, "system_contaminated"),
    evidenceBlockers: stringList(evidence.blockers ?? quality.missing),
    observedAt: timestampValue(source),
    updatedAt: stringOrNumberValue(source, "updated_at"),
    symbol: stringValue(source, "symbol"),
    positionId: identifierValue(source, "position_id"),
  };
}

function decodeLearningReview(value: unknown, index: number): LearningReviewRecord {
  const source = object(value);
  return {
    id: identifierValue(source, "review_id") ?? `learning-review-${index}`,
    tradeId: identifierValue(source, "trade_id"),
    outcomeLabel: firstString(source, "outcome_label", "status"),
    pnl: numericValue(source, "pnl"),
    status: firstString(source, "status", "outcome_label"),
    reasonCode: firstString(source, "trace_locator", "reason_code", "failure_tags"),
    observedAt: timestampValue(source),
  };
}

function aggregateNumber(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  return Object.values(object(value)).reduce<number>((sum, entry) => {
    const parsed = typeof entry === "number" && Number.isFinite(entry) ? entry : Number(entry);
    return sum + (Number.isFinite(parsed) ? parsed : 0);
  }, 0);
}

function decodeLearningReviewSummary(payload: unknown): LearningFactList<LearningReviewRecord> {
  const source = object(payload);
  const latest = object(source.latest_review);
  const items = Object.keys(latest).length ? [decodeLearningReview(latest, 0)] : [];
  return {
    fact: readFact(source, "learning.summary.v2"),
    items,
    count: Math.max(items.length, aggregateNumber(source.reviews)),
  };
}

function decodeLearningModelRecord(value: unknown, index: number): LearningModelRecord {
  const source = object(value);
  return {
    id: identifierValue(source, "candidate_id") ?? identifierValue(source, "inference_id") ?? identifierValue(source, "audit_id") ?? `learning-model-${index}`,
    status: firstString(source, "status", "decision", "mode") ?? "unknown",
    modelType: firstString(source, "model_type", "model", "model_name"),
    reasonCode: firstString(source, "reason_code", "error", "failure_reason"),
    observedAt: timestampValue(source),
  };
}

function decodeLearningSuggestion(value: unknown, index: number): LearningSuggestionRecord {
  const source = object(value);
  const display = object(source.parameter_template_display);
  return {
    id: identifierValue(source, "suggestion_id") ?? `learning-suggestion-${index}`,
    status: firstString(source, "status") ?? "unknown",
    action: firstString(source, "action", "proposal_action"),
    factorId: firstString(source, "factor_id", "scope_key") ?? firstString(display, "factor_id", "template_id"),
    reasonCode: firstString(source, "reason_code", "route_recommendation", "governance_action"),
    observedAt: timestampValue(source),
  };
}

function decodeLearningApplication(value: unknown, index: number): LearningApplicationRecord {
  const source = object(value);
  return {
    id: identifierValue(source, "application_id") ?? `learning-application-${index}`,
    status: firstString(source, "status") ?? "unknown",
    action: firstString(source, "action"),
    scope: [firstString(source, "scope_type"), firstString(source, "scope_key")].filter(Boolean).join(" / ") || null,
    observedAt: timestampValue(source),
    deltaAvgReward: numericValue(source, "delta_avg_reward"),
    postWinRate: numericValue(source, "post_win_rate"),
    baselineWinRate: numericValue(source, "baseline_win_rate"),
  };
}

function datasetQuality(value: unknown): LearningDatasetQuality {
  const source = object(value);
  return {
    total: numericValue(source, "total") ?? 0,
    modelReady: numericValue(source, "model_ready") ?? 0,
    needsAttention: numericValue(source, "needs_attention") ?? 0,
    readyRatio: numericValue(source, "ready_ratio"),
    avgQualityScore: numericValue(source, "avg_quality_score"),
    missing: numericMap(source.missing),
  };
}

function decodeLearningDatasetBlocker(value: unknown): LearningDatasetBlocker {
  const source = object(value);
  return {
    code: firstString(source, "code", "reason_code", "blocker") ?? "unknown_blocker",
    required: numericValue(source, "required"),
    actual: numericValue(source, "actual"),
  };
}

function decodeLearningDatasetReadiness(payload: unknown): LearningDatasetReadinessView {
  const source = object(payload);
  const quality = object(source.quality);
  return {
    fact: readFact(source, "learning.dataset-readiness.v2"),
    ready: booleanValue(source, "ready"),
    level: firstString(source, "level", "status"),
    thresholds: numericMap(source.thresholds),
    quality: {
      trade: datasetQuality(quality.trade),
      decision: datasetQuality(quality.decision),
    },
    schemaIssueCount: numericValue(source, "schema_issue_count") ?? 0,
    blockers: arrayField(source, "blockers").map(decodeLearningDatasetBlocker),
    warnings: stringList(source.warnings),
  };
}

function decodeLearningQualityHealth(payload: unknown): LearningQualityHealthView {
  const source = object(payload);
  const evidence = object(source.evidence_contract);
  const entry = object(source.entry_context);
  const samples = object(entry.samples);
  return {
    fact: readFact(source, "learning.dataset-quality-health.v2"),
    evidenceCounts: numericMap(evidence.counts),
    evidenceExamples: array(evidence.examples).map((value) => {
      const item = object(value);
      return {
        sampleId: identifierValue(item, "sample_id") ?? "unknown",
        codes: stringList(item.codes),
      };
    }),
    entryContextStatus: firstString(entry, "status"),
    openDecisions: numericValue(entry, "open_decisions") ?? 0,
    coverageRatio: numericMap(entry.coverage_ratio),
    missingTotal: numericValue(entry, "missing_total") ?? 0,
    maturedOpenOutcome: numericValue(samples, "matured_open_outcome") ?? 0,
  };
}

function decodeLearningEffectQuality(payload: unknown): LearningEffectQualityView {
  const source = object(payload);
  return {
    ok: booleanValue(source, "ok"),
    status: firstString(source, "status"),
    statusCounts: numericMap(source.status_counts),
    reasonCounts: numericMap(source.reason_counts),
    activeCount: numericValue(source, "active_count") ?? 0,
    terminalCount: numericValue(source, "terminal_count") ?? 0,
    closureRatio: numericValue(source, "closure_ratio"),
    boundedNonterminalCount: numericValue(source, "bounded_nonterminal_count") ?? 0,
    retryCandidateCount: numericValue(source, "retry_candidate_count") ?? 0,
  };
}

export const getLearningAutonomousSamples = (limit = 300) => apiRequest<unknown>(`/api/learning/autonomous/samples?limit=${limit}`).then((payload) => decodeLearningFactList(payload, "learning.autonomous-samples.v2", decodeLearningSample));
export const getLearningReviews = (limit = 100) => apiRequest<unknown>(`/api/learning/reviews?limit=${limit}`).then((payload) => decodeLearningFactList(payload, "learning.reviews.v2", decodeLearningReview));
export const getLearningReviewSummary = () => apiRequest<unknown>("/api/learning/summary").then(decodeLearningReviewSummary);
export const getLearningDatasetReadiness = () => apiRequest<unknown>("/api/learning/dataset/readiness").then(decodeLearningDatasetReadiness);
export const getLearningDatasetQualityHealth = (limit = 500) => apiRequest<unknown>(`/api/learning/dataset/quality-health?limit=${limit}`).then(decodeLearningQualityHealth);
export const getLearningModelShadowQueue = (limit = 50) => apiRequest<unknown>(`/api/learning/model/shadow-queue?limit=${limit}`).then((payload) => decodeLearningFactList(payload, "learning.model-shadow-queue.v2", decodeLearningModelRecord));
export const getLearningModelInferenceAudits = (limit = 50) => apiRequest<unknown>(`/api/learning/model/inference?limit=${limit}`).then((payload) => decodeLearningFactList(payload, "learning.model-inference-audits.v2", decodeLearningModelRecord));
export const getLearningSuggestions = (limit = 100) => apiRequest<unknown>(`/api/learning/suggestions?limit=${limit}`).then((payload) => decodeLearningFactList(payload, "learning.suggestions.v2", decodeLearningSuggestion));
export const getLearningApplications = (limit = 100) => apiRequest<unknown>(`/api/learning/applications?limit=${limit}`).then((payload) => decodeLearningFactList(payload, "learning.applications.v2", decodeLearningApplication));
export const getLearningEffectQuality = (limit = 500) => apiRequest<unknown>(`/api/learning/effect-quality?limit=${limit}`).then(decodeLearningEffectQuality);

export const getLearningResearchSnapshot = () => apiRequest<unknown>("/api/learning/summary").then((payload) => decodeResearchSnapshot(payload, "learning.summary.v2", "学习证据", "learning"));

function settledValue<T>(result: PromiseSettledResult<T>, alternate: T): T {
  return result.status === "fulfilled" ? result.value : alternate;
}

export async function getLearningLoopData(): Promise<LearningLoopData> {
  const results = await Promise.allSettled([
    getLearningAutonomousSamples(),
    getLearningReviewSummary(),
    getLearningDatasetQualityHealth(),
    getLearningDatasetReadiness(),
    getLearningModelShadowQueue(),
    getLearningModelInferenceAudits(),
    getLearningSuggestions(),
    getGovernanceCandidates(),
    getGovernanceReviews(),
    getGovernanceProposals(),
    getLearningApplications(),
    getLearningEffectQuality(),
  ]);
  const [samples, reviews, quality, dataset, shadowQueue, inferenceAudits, suggestions, governanceCandidates, governanceReviews, governanceProposals, applications, effectQuality] = results;
  const failedList = <T,>(contract: string, reasonCode: string, decodeItem: (value: unknown, index: number) => T): LearningFactList<T> => decodeLearningFactList(failedFactPayload(contract, reasonCode), contract, decodeItem);
  const failedGovernance = (contract: string, kind: GovernanceRecord["kind"]): { fact: FactEnvelope; items: GovernanceRecord[] } => decodeGovernanceRecords(failedFactPayload(contract, "learning_governance_request_failed"), kind, contract);
  const failedDataset = (): LearningDatasetReadinessView => decodeLearningDatasetReadiness(failedFactPayload("learning.dataset-readiness.v2", "learning_dataset_request_failed"));
  const failedQuality = (): LearningQualityHealthView => decodeLearningQualityHealth(failedFactPayload("learning.dataset-quality-health.v2", "learning_quality_request_failed"));
  return {
    samples: settledValue(samples, failedList("learning.autonomous-samples.v2", "learning_samples_request_failed", decodeLearningSample)),
    reviews: settledValue(reviews, failedList("learning.summary.v2", "learning_summary_request_failed", decodeLearningReview)),
    quality: settledValue(quality, failedQuality()),
    dataset: settledValue(dataset, failedDataset()),
    shadowQueue: settledValue(shadowQueue, failedList("learning.model-shadow-queue.v2", "learning_shadow_queue_request_failed", decodeLearningModelRecord)),
    inferenceAudits: settledValue(inferenceAudits, failedList("learning.model-inference-audits.v2", "learning_inference_request_failed", decodeLearningModelRecord)),
    suggestions: settledValue(suggestions, failedList("learning.suggestions.v2", "learning_suggestions_request_failed", decodeLearningSuggestion)),
    governanceCandidates: settledValue(governanceCandidates, failedGovernance("ops.v16-governance-candidates.v2", "candidate")),
    governanceReviews: settledValue(governanceReviews, failedGovernance("ops.v16-governance-candidate-reviews.v2", "review")),
    governanceProposals: settledValue(governanceProposals, failedGovernance("ops.autonomy-proposals.v2", "proposal")),
    applications: settledValue(applications, failedList("learning.applications.v2", "learning_applications_request_failed", decodeLearningApplication)),
    effectQuality: effectQuality.status === "fulfilled" ? effectQuality.value : null,
    effectQualityRequestFailed: effectQuality.status === "rejected",
  };
}
