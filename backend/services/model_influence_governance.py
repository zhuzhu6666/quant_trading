"""Evidence-gated stage changes for bounded demo model influence."""
from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB
from backend.services.model_influence import ACTIVE_STAGES, ModelInfluenceService, normalized_model_influence_config
from backend.services.research_evidence import (
    evaluate_research_evidence,
    has_research_trust_metadata,
)
from backend.services.governance_control_plans import (
    ModelPolicyActivationPlan,
    governance_coordinator_mode,
)
from config.runtime_config import shared as runtime_config
from risk.policy_service import RiskPolicyService


MODEL_EFFECTS: dict[str, dict[str, Any]] = {
    "open_quality_lightgbm": {
        "allowed_effects": ["veto"], "veto_threshold": 0.25,
    },
    "position_quality_lightgbm": {
        "allowed_effects": ["tighten", "reduce"], "tighten_threshold": 0.70,
        "reduce_threshold": 0.88, "max_reduce_fraction": 0.25,
    },
    "factor_governance_lightgbm": {
        "allowed_effects": ["suggest_downweight"],
    },
}

MODEL_FEATURE_SCHEMAS = {
    "open_quality_lightgbm": "pit.v2.open_lineage",
    "position_quality_lightgbm": "pit.v2.position_h30",
    "factor_governance_lightgbm": "pit.v4.factor_regime_decision_lineage",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ModelInfluenceGovernanceService:
    """Promote only PIT-v2 artifacts that beat explicit holdout baselines."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    def evaluate_artifact(self, artifact_path: str | Path) -> dict[str, Any]:
        path = Path(artifact_path).resolve()
        if not path.exists():
            return {"passed": False, "reason": "artifact_missing", "artifact_path": str(path), "checks": []}
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"passed": False, "reason": "artifact_invalid", "error": str(exc), "checks": []}
        model_type = str(artifact.get("model_type") or "")
        metrics = dict(artifact.get("metrics") or {})
        holdout = dict(metrics.get("holdout") or {})
        train = dict(metrics.get("train") or {})
        sample_count = int(metrics.get("sample_count") or (artifact.get("sample_window") or {}).get("sample_count") or 0)
        holdout_count = int(metrics.get("holdout_count") or holdout.get("count") or 0)
        accuracy = _safe_float(holdout.get("accuracy"))
        balanced = _safe_float(holdout.get("balanced_accuracy"))
        auc = _safe_float(holdout.get("auc"))
        majority = _safe_float(holdout.get("majority_baseline_accuracy"))
        rule = _safe_float(holdout.get("rule_accuracy"))
        checks: list[dict[str, Any]] = []

        def check(name: str, passed: bool, actual: Any, required: Any) -> None:
            checks.append({"name": name, "passed": bool(passed), "actual": actual, "required": required})

        # Pass the whole artifact whenever trust metadata is present so an
        # outer legacy envelope cannot be hidden by optimistic nested claims.
        research_evidence = (
            artifact if has_research_trust_metadata(artifact)
            else artifact.get("research_evidence")
        )
        research_verdict = None
        if isinstance(research_evidence, dict) and has_research_trust_metadata(research_evidence):
            research_verdict = evaluate_research_evidence(
                research_evidence,
                executable_use="model_promotion",
            )
            check(
                "research_evidence",
                research_verdict.allowed,
                research_verdict.reason,
                "executable_research_evidence_verified",
            )

        check("known_model_type", model_type in MODEL_EFFECTS, model_type, sorted(MODEL_EFFECTS))
        feature_schema_version = str(artifact.get("feature_schema_version") or "")
        expected_feature_schema = MODEL_FEATURE_SCHEMAS.get(model_type, "")
        check(
            "feature_schema",
            bool(expected_feature_schema) and feature_schema_version == expected_feature_schema,
            feature_schema_version,
            expected_feature_schema or "known pit.v2 model schema",
        )
        check("time_ordered_split", str(metrics.get("split") or "").startswith("time_ordered"), metrics.get("split"), "time_ordered*")
        check("artifact_fresh", time.time() - _safe_float(artifact.get("created_at")) <= 7 * 86400, artifact.get("created_at"), "age<=7d")
        model_file = Path(str(artifact.get("model_file") or ""))
        check("model_file", model_file.is_file(), str(model_file), "regular_file_exists")
        if model_file.is_file():
            check("model_file_sha256", _sha256(model_file) == str(artifact.get("model_file_sha256") or ""), _sha256(model_file), artifact.get("model_file_sha256"))

        if model_type == "position_quality_lightgbm":
            distinct_positions = int(metrics.get("train_position_count") or 0) + int(metrics.get("holdout_position_count") or 0)
            check("distinct_positions", distinct_positions >= 300, distinct_positions, ">=300")
            check("holdout_positions", int(metrics.get("holdout_position_count") or 0) >= 60, metrics.get("holdout_position_count"), ">=60")
            check("balanced_accuracy", balanced >= 0.75, balanced, ">=0.75")
            check("auc", auc >= 0.80, auc, ">=0.80")
            check("majority_lift", accuracy >= majority + 0.08, accuracy - majority, ">=0.08")
        elif model_type == "open_quality_lightgbm":
            check("sample_count", sample_count >= 400, sample_count, ">=400")
            check("holdout_count", holdout_count >= 80, holdout_count, ">=80")
            check("balanced_accuracy", balanced >= 0.60, balanced, ">=0.60")
            check("auc", auc >= 0.70, auc, ">=0.70")
            check("majority_lift", accuracy >= majority + 0.03, accuracy - majority, ">=0.03")
        elif model_type == "factor_governance_lightgbm":
            distinct_trades = int(metrics.get("distinct_trade_count") or 0)
            holdout_trades = int(metrics.get("holdout_trade_count") or 0)
            check("distinct_trades", distinct_trades >= 300, distinct_trades, ">=300")
            check("holdout_trades", holdout_trades >= 60, holdout_trades, ">=60")
            check("balanced_accuracy", balanced >= 0.60, balanced, ">=0.60")
            check("auc", auc >= 0.65, auc, ">=0.65")
            check("majority_lift", accuracy >= majority + 0.03, accuracy - majority, ">=0.03")
            check("generalization_gap", _safe_float(train.get("accuracy")) - accuracy <= 0.15, _safe_float(train.get("accuracy")) - accuracy, "<=0.15")
        passed = bool(checks) and all(item["passed"] for item in checks)
        return {
            "schema_version": "model_promotion_gate.v1",
            "passed": passed,
            "reason": "promotion_gate_passed" if passed else "promotion_gate_failed",
            "model_type": model_type,
            "artifact_path": str(path),
            "artifact_sha256": _sha256(path),
            "feature_schema_version": str(artifact.get("feature_schema_version") or ""),
            "metrics": metrics,
            "research_evidence_verdict": research_verdict.to_dict() if research_verdict else {},
            "checks": checks,
            "failed_checks": [item["name"] for item in checks if not item["passed"]],
        }

    def promote(
        self,
        artifact_path: str | Path,
        *,
        stage: str = "demo_canary",
        v16_command_id: str = "",
    ) -> dict[str, Any]:
        gate = self.evaluate_artifact(artifact_path)
        if not gate.get("passed"):
            return {"ok": False, "status": "blocked_by_promotion_gate", "gate": gate}
        # Initial promotion is always canary.  A future canary-to-active
        # transition must use matured effect evidence, not this artifact gate.
        if stage != "demo_canary":
            return {"ok": False, "status": "invalid_stage", "stage": stage, "gate": gate}
        cfg = runtime_config()
        model_type = str(gate.get("model_type") or "")
        effects = dict(MODEL_EFFECTS[model_type])
        verdict = RiskPolicyService.shared().evaluate("promote_model_influence", {
            "autonomy_mode": getattr(cfg, "autonomy_mode", ""),
            "runtime_incident_mode": getattr(cfg, "runtime_incident_mode", "normal"),
            "demo_model_influence_enabled": True,
            "model_type": model_type,
            "feature_schema_version": gate.get("feature_schema_version"),
            "promotion_gate_passed": True,
            "promotion_gate": gate,
            "allowed_effects": effects.get("allowed_effects") or [],
            "capabilities": {
                "live_trading": False, "can_place_orders": False, "can_close_positions": False,
                "can_change_risk_limits": False, "can_change_factor_weights": False,
                "can_bypass_risk_policy": False,
            },
        })
        if not verdict.allowed:
            return {"ok": False, "status": "blocked_by_risk_policy", "reason": verdict.reason, "gate": gate}
        config = normalized_model_influence_config(getattr(cfg, "model_influence_config", {}) or {})
        previous_config = deepcopy(config)
        config["models"][model_type] = {
            **config["models"][model_type], **effects,
            "stage": stage,
            "artifact_path": str(gate["artifact_path"]),
            "artifact_sha256": str(gate["artifact_sha256"]),
            "feature_schema_version": str(gate["feature_schema_version"]),
            "promoted_at": time.time(),
            "promotion_gate": {"schema_version": gate["schema_version"], "checks": gate["checks"]},
        }
        mutation = ModelPolicyActivationPlan(
            patch={"demo_model_influence_enabled": True, "model_influence_config": config},
            source="model_influence_governance",
            run_id=f"model-promote:{model_type}:{int(time.time())}",
            actor="system:factor_governance",
            action="promote_model_influence",
            reason="PIT-v2 artifact passed bounded demo promotion gate",
            scope_type="model_stage",
            scope_key=model_type,
            target_agent="factor_governance",
            model_type=model_type,
            target_stage=stage,
            rollback={
                "demo_model_influence_enabled": bool(
                    getattr(cfg, "demo_model_influence_enabled", False)
                ),
                "model_influence_config": previous_config,
            },
            evidence_refs={"promotion_gate": gate},
            idempotency_key=f"model-promote:{model_type}:{gate['artifact_sha256']}:{stage}",
            v16_command_id=v16_command_id,
        ).execute(self.db_path)
        return {"ok": bool(mutation.get("ok")), "status": mutation.get("status"), "gate": gate, "mutation": mutation}

    def demote(self, model_type: str, *, reason: str = "automatic_model_demotion") -> dict[str, Any]:
        cfg = runtime_config()
        config = normalized_model_influence_config(getattr(cfg, "model_influence_config", {}) or {})
        if model_type not in config["models"]:
            return {"ok": False, "status": "unknown_model_type"}
        previous_config = deepcopy(getattr(cfg, "model_influence_config", {}) or {})
        previous_stage = str(config["models"][model_type].get("stage") or "shadow")
        plan = ModelPolicyActivationPlan(
            # Keep the restrictive patch minimal.  Audit reason/timestamp live
            # in evidence_refs; adding arbitrary runtime metadata would turn a
            # pure stage tightening into an expansionary mixed mutation.
            patch={
                "model_influence_config": {
                    "models": {model_type: {"stage": "quarantined"}}
                }
            },
            source="model_influence_governance_demotion",
            run_id=f"model-demote:{model_type}:{int(time.time())}",
            actor="system:factor_governance",
            action="demote_model_influence",
            reason=reason,
            scope_type="model_stage",
            scope_key=model_type,
            target_agent="factor_governance",
            model_type=model_type,
            target_stage="quarantined",
            rollback={"model_influence_config": previous_config},
            evidence_refs={
                "model_type": model_type,
                "previous_stage": previous_stage,
                "target_stage": "quarantined",
                "reason": reason,
            },
            idempotency_key=f"model-demote:{model_type}:{previous_stage}:{reason}",
        )
        if governance_coordinator_mode() == "off":
            # One-release compatibility path.  dual/enforce must derive the
            # tightening classification from before/after and cannot consume
            # this historical caller assertion.
            from backend.services.runtime_config_mutation import RuntimeConfigMutationService

            mutation = RuntimeConfigMutationService(self.db_path).apply_patch(
                dict(plan.patch),
                source=plan.source,
                run_id=plan.run_id,
                actor=plan.actor,
                action=plan.action,
                reason=plan.reason,
                risk_reduction=True,
            )
        else:
            mutation = plan.execute(self.db_path)
        return {"ok": bool(mutation.get("ok")), "status": mutation.get("status"), "mutation": mutation}

    def reconcile_active_models(self) -> dict[str, Any]:
        """Fail closed on artifact drift, failed evidence, or excessive action rate."""
        cfg = runtime_config()
        config = normalized_model_influence_config(getattr(cfg, "model_influence_config", {}) or {})
        checks: list[dict[str, Any]] = []
        demotions: list[dict[str, Any]] = []
        influence = ModelInfluenceService(self.db_path)
        for model_type, policy in config["models"].items():
            if str(policy.get("stage") or "shadow") not in ACTIVE_STAGES:
                continue
            path = Path(str(policy.get("artifact_path") or ""))
            reason = ""
            gate: dict[str, Any] = {}
            if not path.exists():
                reason = "active_model_artifact_missing"
            elif _sha256(path) != str(policy.get("artifact_sha256") or ""):
                reason = "active_model_artifact_hash_drift"
            else:
                gate = self.evaluate_artifact(path)
                if not gate.get("passed"):
                    reason = "active_model_promotion_gate_regressed"
            conn = influence._conn()
            try:
                row = influence._execute(conn, """
                    SELECT COUNT(*) AS decisions,
                           SUM(CASE WHEN applied=1 THEN 1 ELSE 0 END) AS applied
                    FROM model_influence_decision
                    WHERE model_type=? AND created_at>=?
                """, (model_type, time.time() - 86400.0)).fetchone()
            finally:
                conn.close()
            try:
                decisions = int(row["decisions"] or 0)
                applied = int(row["applied"] or 0)
            except (KeyError, TypeError, IndexError):
                decisions = int((row[0] if row else 0) or 0)
                applied = int((row[1] if row else 0) or 0)
            action_rate = applied / max(decisions, 1)
            max_rate = {
                "open_quality_lightgbm": 0.30,
                "position_quality_lightgbm": 0.35,
                "factor_governance_lightgbm": 0.25,
            }.get(model_type, 0.30)
            if not reason and decisions >= 20 and action_rate > max_rate:
                reason = "active_model_action_rate_exceeded"
            item = {
                "model_type": model_type, "passed": not bool(reason), "reason": reason or "healthy",
                "decisions_24h": decisions, "applied_24h": applied,
                "action_rate_24h": round(action_rate, 6), "max_action_rate": max_rate,
                "gate": gate,
            }
            checks.append(item)
            if reason:
                demotions.append(self.demote(model_type, reason=reason))
        return {
            "schema_version": "model_influence_reconcile.v1",
            "ok": not demotions,
            "checks": checks,
            "demotions": demotions,
        }
