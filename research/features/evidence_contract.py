from __future__ import annotations

import hashlib
import json
from typing import Any


EVIDENCE_CONTRACT_VERSION = "learning_evidence_contract.v1"

CAUSAL_LEVELS = {
    "observational",
    "counterfactual",
    "replay_validated",
    "intervention_observed",
}

INTEGRITY_LEVELS = {"full", "recovered", "partial", "missing"}


def stable_hash(payload: Any) -> str:
    text = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_level(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default


def _weight_for(*, integrity: str, causal_level: str, quality_score: float, label_status: str) -> float:
    integrity_weight = {
        "full": 1.0,
        "recovered": 0.75,
        "partial": 0.45,
        "missing": 0.0,
    }.get(integrity, 0.0)
    causal_weight = {
        "intervention_observed": 1.0,
        "replay_validated": 0.85,
        "counterfactual": 0.7,
        "observational": 0.55,
    }.get(causal_level, 0.0)
    label_weight = 1.0 if label_status == "matured" else 0.55 if label_status == "pending" else 0.0
    return round(max(0.0, min(1.0, quality_score)) * integrity_weight * causal_weight * label_weight, 6)


def build_evidence_contract(
    *,
    sample_id: str,
    sample_kind: str,
    source: dict[str, Any],
    features: dict[str, Any],
    label: dict[str, Any],
    trace: dict[str, Any],
    quality: dict[str, Any],
    integrity: str = "full",
    causal_level: str = "observational",
    label_status: str = "matured",
    explanation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    integrity = _clean_level(integrity, INTEGRITY_LEVELS, "missing")
    causal_level = _clean_level(causal_level, CAUSAL_LEVELS, "observational")
    label_status = str(label_status or "pending")
    quality_score = float((quality or {}).get("quality_score") or 0.0)
    trace = trace or {}
    features = features or {}
    label = label or {}
    explanation = explanation or {}

    allowed_uses = ["audit", "explainability"]
    if integrity in {"full", "recovered", "partial"} and label_status == "matured":
        allowed_uses.append("weak_supervision")
    if (
        integrity in {"full", "recovered"}
        and causal_level in {"replay_validated", "intervention_observed"}
        and label_status == "matured"
    ):
        allowed_uses.append("supervised_training")
    if integrity == "full" and causal_level == "intervention_observed" and label_status == "matured":
        allowed_uses.append("strong_governance")

    blockers = []
    if integrity == "missing":
        blockers.append("missing_integrity")
    if not trace:
        blockers.append("missing_trace")
    if not features:
        blockers.append("missing_features")
    if not label:
        blockers.append("missing_label")
    if label_status != "matured":
        blockers.append("label_not_matured")
    if "supervised_training" not in allowed_uses:
        blockers.append("not_supervised_training_grade")

    contract = {
        "schema_version": EVIDENCE_CONTRACT_VERSION,
        "sample_id": str(sample_id or ""),
        "sample_kind": str(sample_kind or ""),
        "source": dict(source or {}),
        "integrity": integrity,
        "causal_level": causal_level,
        "label_status": label_status,
        "train_weight": _weight_for(
            integrity=integrity,
            causal_level=causal_level,
            quality_score=quality_score,
            label_status=label_status,
        ),
        "allowed_uses": allowed_uses,
        "blockers": blockers,
        "hashes": {
            "features_sha256": stable_hash(features),
            "label_sha256": stable_hash(label),
            "trace_sha256": stable_hash(trace),
            "explanation_sha256": stable_hash(explanation),
        },
        "quality": {
            "quality_score": quality_score,
            "model_ready": bool((quality or {}).get("model_ready")),
            "missing": list((quality or {}).get("missing") or []),
        },
    }
    contract["model_ready"] = bool((quality or {}).get("model_ready")) and "supervised_training" in allowed_uses and not blockers
    return contract


def validate_evidence_contract(item: dict[str, Any], *, kind: str = "") -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    sample_id = str(item.get("sample_id") or "")
    contract = item.get("evidence_contract")
    if not isinstance(contract, dict):
        return [{"sample_id": sample_id, "kind": kind, "field": "evidence_contract", "issue": "missing_field"}]
    if contract.get("schema_version") != EVIDENCE_CONTRACT_VERSION:
        issues.append(
            {
                "sample_id": sample_id,
                "kind": kind,
                "field": "evidence_contract.schema_version",
                "issue": "schema_mismatch",
                "expected": EVIDENCE_CONTRACT_VERSION,
                "actual": contract.get("schema_version"),
            }
        )
    for field in ("source", "integrity", "causal_level", "label_status", "train_weight", "allowed_uses", "hashes"):
        if field not in contract:
            issues.append({"sample_id": sample_id, "kind": kind, "field": f"evidence_contract.{field}", "issue": "missing_field"})
    if contract.get("integrity") not in INTEGRITY_LEVELS:
        issues.append({"sample_id": sample_id, "kind": kind, "field": "evidence_contract.integrity", "issue": "invalid_value"})
    if contract.get("causal_level") not in CAUSAL_LEVELS:
        issues.append({"sample_id": sample_id, "kind": kind, "field": "evidence_contract.causal_level", "issue": "invalid_value"})
    try:
        weight = float(contract.get("train_weight"))
    except Exception:
        weight = -1.0
    if weight < 0.0 or weight > 1.0:
        issues.append({"sample_id": sample_id, "kind": kind, "field": "evidence_contract.train_weight", "issue": "invalid_value"})
    if (item.get("quality") or {}).get("model_ready") and not contract.get("model_ready"):
        issues.append(
            {
                "sample_id": sample_id,
                "kind": kind,
                "field": "evidence_contract.model_ready",
                "issue": "quality_ready_but_evidence_not_training_grade",
            }
        )
    return issues
