from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from research.offline_trainer import MODEL_TYPE

LIGHTGBM_MODEL_TYPES = {
    "position_quality_lightgbm",
    "factor_governance_lightgbm",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ModelPromotionGate:
    """Safety gate for moving offline model artifacts toward shadow validation.

    A passing artifact becomes a shadow candidate only. Demo influence still
    requires the explicit Demo Canary and active-stage gates.
    """

    def evaluate(
        self,
        *,
        model_type: str = MODEL_TYPE,
        artifact_path: str | Path | None = None,
        version: int | None = None,
        registry_db_path: str | None = None,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
        min_samples: int = 20,
        min_holdout_samples: int = 5,
        min_oos_acc: float = 0.52,
        min_features: int = 1,
        require_snapshot_ready: bool = True,
    ) -> dict:
        registry_version = None
        if artifact_path is None:
            from research.model_registry import ModelRegistry

            registry = ModelRegistry(db_path=registry_db_path)
            if version is not None:
                registry_version = registry.get_version(
                    model_type,
                    version=version,
                    symbol=symbol,
                    timeframe=timeframe,
                )
            else:
                registry_version = registry.best_version(
                    model_type,
                    metric="oos_acc",
                    symbol=symbol,
                    timeframe=timeframe,
                )
                if registry_version is None:
                    registry_version = registry.best_version(
                        model_type,
                        metric="holdout_accuracy",
                        symbol=symbol,
                        timeframe=timeframe,
                    )
            if registry_version is None:
                return {
                    "ok": False,
                    "decision": "reject",
                    "model_type": model_type,
                    "issues": [{"code": "model_version_not_found", "message": "no registered model version found"}],
                    "capabilities": {"live_trading": False, "shadow_validation_required": True},
                }
            artifact_path = registry_version.artifact_path

        path = Path(artifact_path)
        issues = []
        warnings = []
        checks: dict[str, Any] = {
            "artifact_exists": path.exists(),
            "artifact_path": str(path),
        }
        if not path.exists():
            return {
                "ok": False,
                "decision": "reject",
                "model_type": model_type,
                "artifact_path": str(path),
                "issues": [{"code": "artifact_missing", "message": "artifact_path does not exist"}],
                "capabilities": {"live_trading": False, "shadow_validation_required": True},
            }

        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "ok": False,
                "decision": "reject",
                "model_type": model_type,
                "artifact_path": str(path),
                "issues": [{"code": "artifact_invalid_json", "message": str(exc)}],
                "capabilities": {"live_trading": False, "shadow_validation_required": True},
            }

        artifact_model_type = str(artifact.get("model_type") or "")
        metrics = artifact.get("metrics") or {}
        holdout = metrics.get("holdout") or {}
        validation = artifact.get("dataset_validation") or {}
        readiness = artifact.get("readiness") or {}
        capabilities = artifact.get("capabilities") or {}
        promotion = artifact.get("promotion") or {}
        governance_readiness = metrics.get("governance_readiness") or {}
        artifact_hash = _sha256(path)
        is_lightgbm_artifact = artifact_model_type in LIGHTGBM_MODEL_TYPES
        has_dataset_contract = bool(validation or readiness)
        majority_baseline_accuracy = holdout.get("majority_baseline_accuracy")
        majority_baseline_balanced_accuracy = holdout.get("majority_baseline_balanced_accuracy")
        balanced_accuracy = holdout.get("balanced_accuracy")
        baseline_margin = _safe_float(governance_readiness.get("baseline_margin"), 0.01 if is_lightgbm_artifact else 0.0)

        checks.update(
            {
                "artifact_sha256": artifact_hash,
                "model_type_matches": artifact_model_type == model_type,
                "dataset_validation_valid": bool(validation.get("valid")) if validation else None,
                "snapshot_ready": bool(readiness.get("ready")) if readiness else None,
                "sample_count": int(metrics.get("sample_count") or 0),
                "holdout_count": int(holdout.get("count") or 0),
                "oos_acc": holdout.get("accuracy"),
                "feature_count": int(metrics.get("feature_count") or 0),
                "declares_live_trading": bool(capabilities.get("live_trading")),
                "declares_demo_influence_eligible": bool(promotion.get("eligible_for_demo_influence")),
                "advisory_only": bool(capabilities.get("advisory_only")),
                "shadow_only": bool(capabilities.get("shadow_only")),
                "can_place_orders": bool(capabilities.get("can_place_orders")),
                "can_close_positions": bool(capabilities.get("can_close_positions")),
                "can_change_risk_limits": bool(capabilities.get("can_change_risk_limits")),
                "majority_baseline_accuracy": majority_baseline_accuracy,
                "majority_baseline_balanced_accuracy": majority_baseline_balanced_accuracy,
                "balanced_accuracy": balanced_accuracy,
                "governance_readiness_status": governance_readiness.get("status"),
                "governance_recommended_source": governance_readiness.get("recommended_source"),
                "model_ready_for_governance": governance_readiness.get("model_ready_for_governance"),
            }
        )

        if not checks["model_type_matches"]:
            issues.append({"code": "model_type_mismatch", "expected": model_type, "actual": artifact_model_type})
        if validation and not checks["dataset_validation_valid"]:
            issues.append({"code": "dataset_validation_failed", "message": "artifact was not built from a valid dataset snapshot"})
        elif not validation and not is_lightgbm_artifact:
            issues.append({"code": "dataset_validation_missing", "message": "artifact does not declare dataset validation"})
        if require_snapshot_ready and readiness and not checks["snapshot_ready"]:
            issues.append({"code": "snapshot_not_ready", "message": "dataset readiness did not pass at artifact build time"})
        elif require_snapshot_ready and not readiness and not is_lightgbm_artifact:
            issues.append({"code": "snapshot_readiness_missing", "message": "artifact does not declare dataset readiness"})
        if is_lightgbm_artifact and not has_dataset_contract:
            warnings.append({"code": "dataset_snapshot_contract_missing", "message": "LightGBM artifact lacks legacy dataset_validation/readiness fields; evidence contract checks are enforced by its builder"})
        if checks["sample_count"] < int(min_samples):
            issues.append({"code": "insufficient_samples", "required": int(min_samples), "actual": checks["sample_count"]})
        if checks["holdout_count"] < int(min_holdout_samples):
            issues.append({"code": "insufficient_holdout_samples", "required": int(min_holdout_samples), "actual": checks["holdout_count"]})
        if checks["oos_acc"] is None:
            issues.append({"code": "missing_oos_acc", "message": "holdout accuracy is required for promotion review"})
        elif _safe_float(checks["oos_acc"]) < float(min_oos_acc):
            issues.append({"code": "oos_acc_below_threshold", "required": float(min_oos_acc), "actual": _safe_float(checks["oos_acc"])})
        if checks["feature_count"] < int(min_features):
            issues.append({"code": "insufficient_features", "required": int(min_features), "actual": checks["feature_count"]})
        if checks["declares_live_trading"]:
            issues.append({"code": "live_trading_not_allowed", "message": "offline artifacts cannot bypass live execution gates"})
        if checks["declares_demo_influence_eligible"]:
            warnings.append({"code": "demo_influence_eligibility_ignored", "message": "gate only grants shadow candidacy, not Demo influence"})
        if is_lightgbm_artifact:
            if not checks["advisory_only"] or not checks["shadow_only"]:
                issues.append({"code": "shadow_advisory_contract_missing", "message": "LightGBM governance artifacts must declare advisory_only and shadow_only"})
            forbidden = [key for key in ("can_place_orders", "can_close_positions", "can_change_risk_limits") if checks[key]]
            if forbidden:
                issues.append({"code": "unsafe_model_capability", "fields": forbidden})
            if majority_baseline_accuracy is not None:
                required_acc = _safe_float(majority_baseline_accuracy) + baseline_margin
                if _safe_float(checks["oos_acc"]) < required_acc:
                    issues.append(
                        {
                            "code": "does_not_beat_majority_baseline",
                            "required": round(required_acc, 6),
                            "actual": _safe_float(checks["oos_acc"]),
                            "baseline": _safe_float(majority_baseline_accuracy),
                            "margin": baseline_margin,
                        }
                    )
            if majority_baseline_balanced_accuracy is not None and balanced_accuracy is not None:
                if _safe_float(balanced_accuracy) < _safe_float(majority_baseline_balanced_accuracy) + baseline_margin:
                    issues.append(
                        {
                            "code": "balanced_accuracy_below_baseline",
                            "required": round(_safe_float(majority_baseline_balanced_accuracy) + baseline_margin, 6),
                            "actual": _safe_float(balanced_accuracy),
                        }
                    )
            if governance_readiness:
                ready = bool(governance_readiness.get("model_ready_for_governance"))
                recommended = str(governance_readiness.get("recommended_source") or "")
                status = str(governance_readiness.get("status") or "")
                if not ready or recommended != "model_shadow_candidate" or status != "model_shadow_candidate":
                    issues.append(
                        {
                            "code": "governance_readiness_blocked",
                            "status": status,
                            "recommended_source": recommended,
                            "model_ready_for_governance": ready,
                        }
                    )

        decision = "shadow_candidate" if not issues else "needs_more_data"
        ok = not issues
        return {
            "ok": ok,
            "decision": decision,
            "action": "queue_shadow_validation" if ok else "collect_more_verified_samples",
            "model_type": model_type,
            "artifact_path": str(path),
            "artifact_sha256": artifact_hash,
            "registry_version": registry_version.to_dict() if registry_version is not None else None,
            "checks": checks,
            "thresholds": {
                "min_samples": int(min_samples),
                "min_holdout_samples": int(min_holdout_samples),
                "min_oos_acc": float(min_oos_acc),
                "min_features": int(min_features),
                "require_snapshot_ready": bool(require_snapshot_ready),
            },
            "issues": issues,
            "warnings": warnings,
            "capabilities": {
                "live_trading": False,
                "shadow_validation_required": True,
                "demo_canary_required_before_influence": True,
            },
            "explainability": {
                "top_weights": list((artifact.get("explainability") or {}).get("top_weights") or [])[:10],
                "summary": (
                    "Artifact passes offline gates and may enter shadow validation."
                    if ok
                    else "Artifact is not ready for shadow validation; inspect issues."
                ),
            },
        }
