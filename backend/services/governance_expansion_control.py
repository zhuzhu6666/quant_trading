"""Operator-owned all-mode governance expansion kill switch."""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB
from backend.services.governance_control_plans import OperatorGovernancePausePlan
from config import runtime_config


class GovernanceExpansionControlService:
    """Typed service for pausing or resuming autonomous governance expansion.

    Pausing is risk tightening and is therefore V16-exempt.  Resuming is
    expansionary and must be classified and authorized by the coordinator (or
    the compatibility mutation boundary while the coordinator flag is off).
    An explicit operator may resume directly in the bounded Demo sandbox;
    autonomous/system actors are never allowed to own this switch.
    """

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "governance_expansion_control.v1",
            "operator_owned": True,
            "applies_to_all_autonomy_modes": True,
            "autonomous_mutation_forbidden": True,
            "pause_is_risk_tightening": True,
            "resume_requires_confirmation": True,
            "resume_requires_v16_outside_bounded_demo": True,
            "does_not_submit_orders": True,
            "does_not_block_observation_or_research": True,
        }

    def status(self) -> dict[str, Any]:
        cfg = runtime_config.shared()
        paused = bool(getattr(cfg, "governance_expansion_paused", False))
        return {
            "ok": True,
            "schema_version": "governance_expansion_control_status.v1",
            "governance_expansion_paused": paused,
            "status": "paused" if paused else "active",
            "blocks_expansion_in_all_modes": paused,
            "autonomy_mode": str(getattr(cfg, "autonomy_mode", "") or "manual"),
            "boundary": self.boundary(),
            "generated_at": time.time(),
        }

    def set_paused(
        self,
        paused: bool,
        *,
        actor: str,
        reason: str,
        confirm_resume: bool = False,
        v16_command_id: str = "",
        v16_claim_token: str = "",
    ) -> dict[str, Any]:
        if str(actor or "").startswith("system:"):
            return {
                "ok": False,
                "status": "operator_required",
                "reason": "autonomous_services_cannot_modify_operator_kill_switch",
                "boundary": self.boundary(),
            }
        if not str(reason or "").strip():
            return {
                "ok": False,
                "status": "reason_required",
                "reason": "operator governance expansion control requires an audit reason",
                "boundary": self.boundary(),
            }
        current = bool(
            getattr(runtime_config.shared(), "governance_expansion_paused", False)
        )
        target = bool(paused)
        if current == target:
            return {
                **self.status(),
                "status": "already_paused" if target else "already_active",
                "idempotent": True,
            }
        if current and not target and not confirm_resume:
            return {
                "ok": False,
                "status": "confirm_resume_required",
                "governance_expansion_paused": current,
                "boundary": self.boundary(),
            }
        run_id = f"governance_expansion_{uuid.uuid4().hex[:16]}"
        action = "pause_governance_expansion" if target else "resume_governance_expansion"
        plan = OperatorGovernancePausePlan(
            patch={"governance_expansion_paused": target},
            source="operator_governance_expansion_control",
            actor=str(actor or "operator:unknown"),
            action=action,
            run_id=run_id,
            reason=str(reason or action),
            scope_type="governance_expansion_control",
            scope_key="governance_expansion_paused",
            target_agent="governance_control",
            rollback={"governance_expansion_paused": current},
            evidence_refs={
                "operator_confirmation": bool(confirm_resume) if not target else True,
                "reason": str(reason or ""),
            },
            v16_command_id=str(v16_command_id or ""),
            v16_claim_token=str(v16_claim_token or ""),
            current_paused=current,
            target_paused=target,
        )
        try:
            mutation = plan.execute(self.db_path)
        except Exception as exc:
            mutation = {
                "ok": False,
                "status": "governance_mutation_unavailable",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        effective = bool(
            getattr(runtime_config.shared(), "governance_expansion_paused", current)
        )
        return {
            "ok": bool(mutation.get("ok")),
            "schema_version": "governance_expansion_control_mutation.v1",
            "status": str(
                mutation.get("status")
                or ("paused" if effective else "active")
            ),
            "governance_expansion_paused": effective,
            "current_paused": current,
            "target_paused": target,
            "mutation": mutation,
            "boundary": self.boundary(),
        }
