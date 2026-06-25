from __future__ import annotations

from typing import Any

from research.features.feature_provider import (
    DECISION_SCHEMA_VERSION,
    SCHEMA_VERSION,
    LearningFeatureProvider,
)


TRADE_REQUIRED_FIELDS = [
    "schema_version",
    "sample_id",
    "quality",
    "target",
    "decision",
    "factor_outcomes",
    "attribution_alignment",
    "review",
    "experience",
    "execution_trace",
    "llm_context",
    "explainability",
]

DECISION_REQUIRED_FIELDS = [
    "schema_version",
    "sample_id",
    "quality",
    "target",
    "decision",
    "execution_trace",
    "llm_context",
    "explainability",
]


def _quality_summary(items: list[dict]) -> dict[str, Any]:
    ready = [item for item in items if item.get("quality", {}).get("model_ready")]
    missing: dict[str, int] = {}
    scores: list[float] = []
    for item in items:
        quality = item.get("quality") or {}
        try:
            scores.append(float(quality.get("quality_score") or 0.0))
        except Exception:
            scores.append(0.0)
        for key in quality.get("missing", []) or []:
            key = str(key)
            missing[key] = missing.get(key, 0) + 1
    return {
        "total": len(items),
        "model_ready": len(ready),
        "needs_attention": len(items) - len(ready),
        "ready_ratio": round(len(ready) / max(len(items), 1), 6),
        "avg_quality_score": round(sum(scores) / max(len(scores), 1), 6),
        "missing": dict(sorted(missing.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


class LearningDatasetReadiness:
    """Read-only dataset readiness audit for downstream models."""

    def __init__(self, db_path: str | None = None):
        self.provider = LearningFeatureProvider(db_path)

    @staticmethod
    def _validate_item(
        item: dict,
        *,
        expected_schema: str,
        required_fields: list[str],
        kind: str,
    ) -> list[dict]:
        issues = []
        sample_id = str(item.get("sample_id") or "")
        if item.get("schema_version") != expected_schema:
            issues.append(
                {
                    "sample_id": sample_id,
                    "kind": kind,
                    "field": "schema_version",
                    "issue": "schema_mismatch",
                    "expected": expected_schema,
                    "actual": item.get("schema_version"),
                }
            )
        for field in required_fields:
            if field not in item:
                issues.append(
                    {
                        "sample_id": sample_id,
                        "kind": kind,
                        "field": field,
                        "issue": "missing_field",
                    }
                )

        quality = item.get("quality") or {}
        if quality.get("model_ready"):
            if kind == "trade":
                if not item.get("factor_outcomes"):
                    issues.append({"sample_id": sample_id, "kind": kind, "field": "factor_outcomes", "issue": "empty_model_ready_field"})
                if not item.get("attribution_alignment"):
                    issues.append({"sample_id": sample_id, "kind": kind, "field": "attribution_alignment", "issue": "empty_model_ready_field"})
                if not item.get("experience"):
                    issues.append({"sample_id": sample_id, "kind": kind, "field": "experience", "issue": "empty_model_ready_field"})
                if not (item.get("llm_context") or {}).get("evidence_bullets"):
                    issues.append({"sample_id": sample_id, "kind": kind, "field": "llm_context.evidence_bullets", "issue": "empty_model_ready_field"})
            if kind == "decision":
                decision = item.get("decision") or {}
                if not decision.get("factor_evidence"):
                    issues.append({"sample_id": sample_id, "kind": kind, "field": "decision.factor_evidence", "issue": "empty_model_ready_field"})
                if not (item.get("target") or {}).get("gate_reason") and not decision.get("action_reason"):
                    issues.append({"sample_id": sample_id, "kind": kind, "field": "target.gate_reason", "issue": "missing_reason"})
                if not (item.get("llm_context") or {}).get("evidence_bullets"):
                    issues.append({"sample_id": sample_id, "kind": kind, "field": "llm_context.evidence_bullets", "issue": "empty_model_ready_field"})
        return issues

    def analyze(
        self,
        *,
        trade_limit: int = 1000,
        decision_limit: int = 5000,
        min_ready_trades: int = 50,
        min_ready_decisions: int = 200,
        max_schema_issues: int = 0,
    ) -> dict:
        trade_samples = self.provider.build_training_samples(limit=trade_limit)
        decision_samples = self.provider.build_decision_samples(limit=decision_limit)

        trade_quality = _quality_summary(trade_samples)
        decision_quality = _quality_summary(decision_samples)

        schema_issues: list[dict] = []
        for item in trade_samples:
            schema_issues.extend(
                self._validate_item(
                    item,
                    expected_schema=SCHEMA_VERSION,
                    required_fields=TRADE_REQUIRED_FIELDS,
                    kind="trade",
                )
            )
        for item in decision_samples:
            schema_issues.extend(
                self._validate_item(
                    item,
                    expected_schema=DECISION_SCHEMA_VERSION,
                    required_fields=DECISION_REQUIRED_FIELDS,
                    kind="decision",
                )
            )

        blockers = []
        warnings = []
        if trade_quality["model_ready"] < min_ready_trades:
            blockers.append(
                {
                    "code": "insufficient_model_ready_trades",
                    "required": int(min_ready_trades),
                    "actual": int(trade_quality["model_ready"]),
                }
            )
        if decision_quality["model_ready"] < min_ready_decisions:
            blockers.append(
                {
                    "code": "insufficient_model_ready_decisions",
                    "required": int(min_ready_decisions),
                    "actual": int(decision_quality["model_ready"]),
                }
            )
        if len(schema_issues) > max_schema_issues:
            blockers.append(
                {
                    "code": "schema_contract_issues",
                    "required": int(max_schema_issues),
                    "actual": len(schema_issues),
                }
            )
        if trade_quality["missing"]:
            warnings.append({"code": "trade_quality_missing_fields", "items": trade_quality["missing"]})
        if decision_quality["missing"]:
            warnings.append({"code": "decision_quality_missing_fields", "items": decision_quality["missing"]})

        has_any_ready = trade_quality["model_ready"] > 0 or decision_quality["model_ready"] > 0
        ready = not blockers
        level = "ready" if ready else "warming_up" if has_any_ready and len(schema_issues) <= max_schema_issues else "not_ready"
        return {
            "ready": ready,
            "level": level,
            "thresholds": {
                "min_ready_trades": int(min_ready_trades),
                "min_ready_decisions": int(min_ready_decisions),
                "max_schema_issues": int(max_schema_issues),
            },
            "schemas": {
                "trade": SCHEMA_VERSION,
                "decision": DECISION_SCHEMA_VERSION,
            },
            "contracts": {
                "trade_required_fields": TRADE_REQUIRED_FIELDS,
                "decision_required_fields": DECISION_REQUIRED_FIELDS,
            },
            "quality": {
                "trade": trade_quality,
                "decision": decision_quality,
            },
            "schema_issues": schema_issues[:50],
            "schema_issue_count": len(schema_issues),
            "blockers": blockers,
            "warnings": warnings,
        }
