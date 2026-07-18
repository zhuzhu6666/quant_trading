"""Single fail-closed eligibility contract for executable governance evidence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from backend.services.research_evidence import (
    evaluate_research_evidence,
    has_research_trust_metadata,
)


GOVERNANCE_ELIGIBILITY_VERSION = "governance_eligibility.v1"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip().lower()
        if text:
            return text
    return ""


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _first_bool(*values: Any) -> bool:
    for value in values:
        if value is not None:
            return _as_bool(value)
    return False


@dataclass(frozen=True)
class GovernanceEligibility:
    eligible: bool
    effective_weight: float
    integrity: str
    exclusion_reasons: tuple[str, ...]
    matured: bool
    uncontaminated: bool
    model_ready: bool
    executable_governance_allowed: bool
    lineage_unique_complete: bool
    eligibility_fingerprint: str
    eligibility_version: str = GOVERNANCE_ELIGIBILITY_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["exclusion_reasons"] = list(self.exclusion_reasons)
        payload["governance_eligible"] = payload.pop("eligible")
        payload["governance_effective_weight"] = payload.pop("effective_weight")
        payload["governance_eligibility_fingerprint"] = payload.pop("eligibility_fingerprint")
        payload["governance_eligibility_version"] = payload.pop("eligibility_version")
        return payload


def evaluate_governance_eligibility(sample: Mapping[str, Any] | None) -> GovernanceEligibility:
    """Evaluate the only evidence class allowed to drive live governance.

    Full, mature, uncontaminated, model-ready samples receive weight 1.  A
    verified recovered trace is capped at 0.5.  Partial/missing/unverified
    evidence and any failed gate receive weight zero.
    """
    item = _mapping(sample)
    quality = _mapping(item.get("quality"))
    maturity = _mapping(item.get("maturity"))
    features = _mapping(item.get("features"))
    feature_maturity = _mapping(features.get("maturity"))
    label = _mapping(item.get("label"))
    verdict = _mapping(item.get("verdict"))
    issue = _mapping(item.get("system_issue_context"))
    trace = _mapping(item.get("trace"))
    recovery = _mapping(item.get("recovery"))
    lineage = _mapping(item.get("lineage"))
    evidence_contract = _mapping(item.get("evidence_contract"))

    maturity_status = _first_text(
        item.get("label_status"),
        maturity.get("status"),
        feature_maturity.get("status"),
    )
    matured = _first_bool(
        item.get("matured"),
        maturity.get("governance_eligible"),
        feature_maturity.get("governance_eligible"),
        maturity_status in {"matured", "governance_ready"},
    )

    integrity = _first_text(
        item.get("integrity"),
        item.get("trace_integrity"),
        trace.get("trace_integrity"),
        quality.get("integrity"),
    ) or "missing"
    verified_recovered = integrity == "recovered" and _first_bool(
        item.get("verified_recovered"),
        recovery.get("verified"),
        quality.get("verified_recovered"),
    )
    integrity_allowed = integrity == "full" or verified_recovered

    contaminated = any(
        _as_bool(value)
        for value in (
            item.get("system_contaminated"),
            item.get("contaminated"),
            label.get("system_contaminated"),
            verdict.get("system_contaminated"),
            issue.get("contaminates_learning"),
            issue.get("contaminated"),
            _mapping(item.get("system_contamination")).get("contaminated"),
            _mapping(label.get("system_contamination")).get("contaminated"),
            _mapping(verdict.get("system_contamination")).get("contaminated"),
            _mapping(features.get("system_contamination")).get("contaminated"),
            _mapping(features.get("system_issue_context")).get("contaminates_learning"),
        )
    )
    model_ready = _first_bool(
        item.get("model_ready"),
        quality.get("model_ready"),
        evidence_contract.get("model_ready"),
        _mapping(evidence_contract.get("quality")).get("model_ready"),
    )

    allowed_uses = item.get("allowed_uses")
    if allowed_uses is None:
        allowed_uses = quality.get("allowed_uses")
    if allowed_uses is None:
        allowed_uses = evidence_contract.get("allowed_uses")
    if isinstance(allowed_uses, str):
        allowed_uses = [allowed_uses]
    allowed = {str(value).strip().lower() for value in (allowed_uses or [])}
    executable_allowed = _first_bool(
        item.get("executable_governance_allowed"),
        quality.get("executable_governance_allowed"),
        bool(allowed & {"executable_governance", "governance_mutation"}),
    )

    lineage_ids = item.get("lineage_ids") or lineage.get("ids") or []
    if isinstance(lineage_ids, str):
        lineage_ids = [lineage_ids]
    normalized_ids = [str(value).strip() for value in lineage_ids if str(value).strip()]
    lineage_complete = _first_bool(
        item.get("lineage_complete"),
        lineage.get("complete"),
        quality.get("lineage_complete"),
    )
    lineage_unique = _first_bool(
        item.get("lineage_unique"),
        lineage.get("unique"),
        bool(normalized_ids) and len(normalized_ids) == len(set(normalized_ids)),
    )
    lineage_unique_complete = lineage_complete and lineage_unique

    reasons: list[str] = []
    if not matured:
        reasons.append("not_matured")
    if not integrity_allowed:
        reasons.append(f"integrity_{integrity}")
    if contaminated:
        reasons.append("system_contaminated")
    if not model_ready:
        reasons.append("not_model_ready")
    if not executable_allowed:
        reasons.append("executable_governance_not_allowed")
    if not lineage_unique_complete:
        reasons.append("lineage_not_unique_complete")
    if has_research_trust_metadata(item):
        research_verdict = evaluate_research_evidence(item, executable_use="governance")
        if not research_verdict.allowed:
            reasons.append(f"research_evidence_{research_verdict.reason}")

    eligible = not reasons
    weight = 1.0 if eligible and integrity == "full" else (0.5 if eligible else 0.0)
    fingerprint_payload = {
        "schema_version": GOVERNANCE_ELIGIBILITY_VERSION,
        "sample_id": str(item.get("sample_id") or ""),
        "sample_type": str(item.get("sample_type") or ""),
        "source_table": str(item.get("source_table") or ""),
        "source_id": str(item.get("source_id") or ""),
        "label_status": maturity_status,
        "integrity": integrity,
        "verified_recovered": verified_recovered,
        "system_contaminated": contaminated,
        "model_ready": model_ready,
        "allowed_uses": sorted(allowed),
        "lineage_ids": sorted(normalized_ids),
        "lineage_complete": lineage_complete,
        "lineage_unique": lineage_unique,
        "evidence_hashes": dict(_mapping(evidence_contract.get("hashes"))),
        "eligible": eligible,
        "effective_weight": weight,
        "exclusion_reasons": reasons,
    }
    eligibility_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return GovernanceEligibility(
        eligible=eligible,
        effective_weight=weight,
        integrity=integrity,
        exclusion_reasons=tuple(reasons),
        matured=matured,
        uncontaminated=not contaminated,
        model_ready=model_ready,
        executable_governance_allowed=executable_allowed,
        lineage_unique_complete=lineage_unique_complete,
        eligibility_fingerprint=eligibility_fingerprint,
    )
