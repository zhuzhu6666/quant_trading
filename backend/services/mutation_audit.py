from __future__ import annotations

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
) -> str:
    """Best-effort audit record for high-impact authenticated API mutations."""
    try:
        from backend.services.evolution_ledger import record_evolution_decision

        decision_id = record_evolution_decision(
            decision_type="manual_api_mutation",
            scope_type="api",
            scope_key=endpoint,
            action=action,
            status=status,
            evidence={
                "user": user,
                "endpoint": endpoint,
                "required_confirm": required_confirm,
                "confirm_ok": bool(confirm_ok),
                "reason": reason,
            },
            before=before or {},
            after=after or {},
            result=result or {},
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
