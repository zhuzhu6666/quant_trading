"""Compatibility façade for durable factor lifecycle mutations.

The legacy endpoints keep their names, but no longer mutate
``RegistryAdapter`` directly.  Promotion only prepares a candidate; activation
is a separate, proof-gated lifecycle transition.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Mapping

from alpha.registry_adapter import RegistryAdapter
from backend.services.factor_lifecycle_service import (
    FactorLifecycleService,
    FactorLifecycleStage,
    FactorV16Binding,
)
from risk.policy_service import RiskPolicyService


logger = logging.getLogger(__name__)
_service: FactorLifecycleService | None = None
_service_lock = threading.Lock()


def _get_service() -> FactorLifecycleService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = FactorLifecycleService(adapter=RegistryAdapter.shared())
    return _service


def _get_adapter() -> RegistryAdapter:
    """Deprecated read-only compatibility hook for older tests/callers."""
    return _get_service().adapter


def reset_service_for_tests() -> None:
    global _service
    with _service_lock:
        _service = None


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def list_shadows() -> list[dict[str, Any]]:
    """Read SHADOW/PROMOTION_PREPARED facts from the durable state store."""
    try:
        states = _get_service().list_states(
            stages={
                FactorLifecycleStage.SHADOW.value,
                FactorLifecycleStage.PROMOTION_PREPARED.value,
            }
        )
    except Exception:
        logger.exception("factor lifecycle state is unavailable")
        return []
    out: list[dict[str, Any]] = []
    for state in states:
        metadata = _loads(state.get("metadata_json"))
        updated_at = float(state.get("updated_at") or 0.0)
        ts_iso = datetime.fromtimestamp(updated_at, tz=timezone.utc).isoformat() if updated_at else ""
        out.append(
            {
                "name": str(state.get("factor_name") or ""),
                "factor_id": str(state.get("factor_id") or ""),
                "status": str(state.get("lifecycle_stage") or ""),
                "source": "factor_lifecycle_state",
                "ts": ts_iso,
                "expr": str(metadata.get("expression") or ""),
                "description": str(metadata.get("expression") or ""),
                "artifact_hash": str(state.get("artifact_hash") or ""),
                "runtime_admission": str(state.get("runtime_admission") or "blocked"),
                "mutation_id": str(state.get("mutation_id") or ""),
            }
        )
    return out


def promote(
    name: str,
    *,
    expression: str = "",
    artifact_hash: str = "",
    actor: str = "operator:shadow_api",
    reason: str = "manual shadow promotion preparation",
    evidence_refs: Mapping[str, Any] | None = None,
    idempotency_key: str = "",
    v16: FactorV16Binding | None = None,
) -> dict[str, Any]:
    """Compatibility operation: submit PROMOTION_PREPARED, never ACTIVE."""
    verdict = RiskPolicyService.shared().evaluate(
        "promote_factor",
        {
            "required_mode": "promotion_prepared",
            "factor": name,
            "source": "shadow",
        },
    ).to_dict()
    if not verdict.get("allowed", False):
        return {
            "name": name,
            "ok": False,
            "status": "blocked",
            "error": f"risk_policy_block: {verdict.get('reason', 'unknown')}",
            "risk_verdict": verdict,
        }
    result = _get_service().prepare_promotion(
        name=name,
        expression=expression,
        artifact_hash=artifact_hash,
        actor=actor,
        reason=reason,
        evidence_refs={**dict(evidence_refs or {}), "risk_verdict": verdict},
        idempotency_key=idempotency_key,
        v16=v16,
    )
    return {
        "name": name,
        **result,
        "new_status": FactorLifecycleStage.PROMOTION_PREPARED.value
        if result.get("ok")
        else str(result.get("lifecycle_stage") or ""),
        "risk_verdict": verdict,
    }


def demote(
    name: str,
    *,
    target_stage: str = FactorLifecycleStage.QUARANTINED.value,
    expression: str = "",
    artifact_hash: str = "",
    actor: str = "operator:shadow_api",
    reason: str = "manual factor quarantine",
    evidence_refs: Mapping[str, Any] | None = None,
    idempotency_key: str = "",
) -> dict[str, Any]:
    """Submit a typed restrictive lifecycle mutation through Coordinator."""
    target = str(target_stage or FactorLifecycleStage.QUARANTINED.value).strip().upper()
    service = _get_service()
    kwargs = {
        "name": name,
        "expression": expression,
        "artifact_hash": artifact_hash,
        "actor": actor,
        "reason": reason,
        "evidence_refs": dict(evidence_refs or {}),
        "idempotency_key": idempotency_key,
    }
    if target == FactorLifecycleStage.QUARANTINED.value:
        result = service.quarantine(**kwargs)
    elif target == FactorLifecycleStage.RETIRED.value:
        result = service.retire(**kwargs)
    else:
        return {
            "name": name,
            "ok": False,
            "status": "blocked",
            "error": "target_stage must be QUARANTINED or RETIRED",
        }
    return {
        "name": name,
        **result,
        "new_status": target if result.get("ok") else str(result.get("lifecycle_stage") or ""),
    }
