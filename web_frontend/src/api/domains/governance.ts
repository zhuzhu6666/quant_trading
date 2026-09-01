import { apiRequest } from "@/api/client";
import { readFact } from "@/api/fact";
import type { GovernanceRecord } from "@/types/contracts";
import {
  arrayField,
  booleanValue,
  firstString,
  identifierValue,
  numberValue,
  object,
  stringList,
  timestampValue,
} from "@/api/domains/shared";

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

export const getGovernanceCandidates = () => apiRequest<unknown>("/api/ops/brain/governance-candidates?limit=40").then((payload) => decodeGovernanceRecords(payload, "candidate", "ops.v16-governance-candidates.v2"));
export const getGovernanceReviews = () => apiRequest<unknown>("/api/ops/brain/governance-candidate-reviews?limit=40").then((payload) => decodeGovernanceRecords(payload, "review", "ops.v16-governance-candidate-reviews.v2"));
export const getGovernanceProposals = () => apiRequest<unknown>("/api/ops/autonomy/proposals?limit=40").then((payload) => decodeGovernanceRecords(payload, "proposal", "ops.autonomy-proposals.v2"));
export const getReleaseEvidence = () => apiRequest<unknown>("/api/ops/release/latest").then((payload) => decodeGovernanceRecords(payload, "release", "ops.release-latest.v2"));
