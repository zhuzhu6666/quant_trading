from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from backend.core.db import STATE_DB

logger = logging.getLogger(__name__)

MUTATION_POLICY: dict[str, dict[str, str]] = {
    "safe_read": {"confirm": "", "audit": "none"},
    "compute_only": {"confirm": "", "audit": "optional"},
    "state_mutation": {"confirm": "", "audit": "required"},
    "governance_mutation": {"confirm": "governance-change", "audit": "required"},
    "live_dangerous": {"confirm": "operation-specific", "audit": "required"},
}

_LAST_AUDIT_STATUS: dict[str, Any] = {
    "ok": True,
    "last_success_at": 0.0,
    "last_failure_at": 0.0,
    "last_error": "",
}


def _payload_fingerprint(value: Any) -> str:
    """Fingerprint a projection input without persisting a second copy."""

    raw = json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_api_mutation(
    *,
    user: str,
    endpoint: str,
    action: str,
    status: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    reason: str = "",
    required_confirm: str = "",
    confirm_ok: bool = False,
    source_agent: str = "",
    decision_type: str = "",
    canonical_event_id: str = "",
    run_id: str = "",
) -> str:
    """Best-effort audit record for high-impact authenticated API mutations."""
    try:
        from backend.services.evolution_ledger import record_evolution_decision

        actor = str(user or "")
        inferred_agent = str(source_agent or "").strip()
        if not inferred_agent:
            actor_lower = actor.lower()
            if actor_lower.startswith("system:factor_governance"):
                inferred_agent = "factor_governance"
            elif actor_lower.startswith("system:autonomous_learning"):
                inferred_agent = "autonomous_learning"
            elif actor_lower.startswith("system:parameter_template"):
                inferred_agent = "autonomous_learning"
            elif actor_lower.startswith("system:"):
                inferred_agent = "autonomous_runtime"
            else:
                inferred_agent = "operator"
        inferred_decision_type = str(decision_type or "").strip()
        if not inferred_decision_type:
            inferred_decision_type = "manual_api_mutation" if inferred_agent == "operator" else "autonomous_mutation"
        canonical_id = str(canonical_event_id or "")
        projection_evidence = {
            "user": user,
            "endpoint": endpoint,
            "source_agent": inferred_agent,
            "decision_type": inferred_decision_type,
            "required_confirm": required_confirm,
            "confirm_ok": bool(confirm_ok),
            "reason": reason,
        }
        projection_before = before or {}
        projection_after = after or {}
        projection_result = result or {}
        if canonical_id:
            # A linked API row is an auditable projection, not a second
            # canonical mutation fact. Keep request provenance and compact
            # fingerprints here; the canonical row owns exact JSON values.
            projection_evidence.update(
                {
                    "canonical_event_id": canonical_id,
                    "projection_mode": "canonical_reference",
                    "source_payload_fingerprints": {
                        "before": _payload_fingerprint(projection_before),
                        "after": _payload_fingerprint(projection_after),
                        "result": _payload_fingerprint(projection_result),
                    },
                }
            )
            projection_before = {}
            projection_after = {}
            projection_result = {"decision_id": canonical_id}
        decision_id = record_evolution_decision(
            run_id=str(run_id or ""),
            decision_type=inferred_decision_type,
            canonical_event_id=canonical_id,
            projection_type=("api" if canonical_id else "api_canonical"),
            scope_type="api",
            scope_key=endpoint,
            action=action,
            status=status,
            evidence=projection_evidence,
            before=projection_before,
            after=projection_after,
            result=projection_result,
            db_path=STATE_DB,
        )
        _LAST_AUDIT_STATUS.update(
            {
                "ok": True,
                "last_success_at": time.time(),
                "last_error": "",
            }
        )
        return decision_id
    except Exception as exc:
        logger.warning("api mutation audit failed endpoint=%s action=%s: %s", endpoint, action, exc)
        _LAST_AUDIT_STATUS.update(
            {
                "ok": False,
                "last_failure_at": time.time(),
                "last_error": str(exc)[:500],
            }
        )
        return ""


def confirm_header_valid(value: str | None, expected: str) -> bool:
    return str(value or "").strip() == expected


def audit_health() -> dict[str, Any]:
    return dict(_LAST_AUDIT_STATUS)


def mutation_policy_contract() -> dict[str, Any]:
    return {key: dict(value) for key, value in MUTATION_POLICY.items()}
