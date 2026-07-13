"""Unified runtime config mutation path for autonomous services."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, is_state_db_path
from backend.services.mutation_audit import record_api_mutation
from backend.services.runtime_config_overlay import RuntimeConfigOverlayService
from config import runtime_config


def _slice_config(config_dict: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: config_dict.get(key) for key in keys if key in config_dict}


def _refresh_yaml_overlay_base(db_path: str | Path) -> None:
    """Refresh the persisted-overlay base before a production mutation.

    Long-lived learning workers can outlive an operator change to
    ``settings.yaml``.  If they keep the old in-memory base, the next overlay
    snapshot can incorrectly restore unrelated fields (notably the cTrader
    send-orders flag) to dataclass defaults.  The PostgreSQL-backed runtime
    path must always rebuild from the current YAML base before applying the
    narrow autonomous overlay.
    """
    if not is_state_db_path(db_path):
        return
    from backend.services.runtime_config_startup import load_yaml_runtime_config

    base_cfg, _yaml_cfg = load_yaml_runtime_config()
    runtime_config.register_overlay_base(base_cfg, db_path, replace_existing=True)


class RuntimeConfigMutationService:
    """Apply autonomous runtime config patches through overlay + snapshot.

    DecisionPolicy remains the policy engine for weight decisions. This service
    is only the persistence/audit boundary for runtime config mutations.
    """

    def __init__(
        self,
        db_path: str | Path = STATE_DB,
        *,
        overlay: RuntimeConfigOverlayService | None = None,
    ):
        self.db_path = db_path
        self.overlay = overlay or RuntimeConfigOverlayService(db_path)

    def apply_patch(
        self,
        patch: dict[str, Any],
        *,
        source: str,
        run_id: str = "",
        actor: str = "system:runtime_config_mutation",
        action: str | None = None,
        reason: str = "",
        audit: bool | None = None,
        require_v16_command: bool | None = None,
        v16_command_id: str = "",
        v16_claim_token: str = "",
        v16_target_agent: str = "",
        v16_scope_type: str = "",
        v16_scope_key: str = "",
        v16_action: str = "",
        risk_reduction: bool = False,
    ) -> dict[str, Any]:
        v16_authority: dict[str, Any] = {}
        governance_mutation = self._requires_v16_command(
            patch=patch,
            source=source,
            action=action or source,
            actor=actor,
            risk_reduction=risk_reduction,
        )
        production_state = is_state_db_path(self.db_path) and Path(self.db_path).resolve() == Path(STATE_DB).resolve()
        # System governance writes cannot opt out by forgetting the old
        # boolean flag.  Explicit ``False`` remains meaningful only for
        # non-governance operational patches (incident controls, live unlock,
        # and startup restore are outside the V16 mutation surface).
        should_require_v16 = governance_mutation or bool(require_v16_command)
        if should_require_v16:
            if not production_state:
                v16_authority = {
                    "ok": True,
                    "allowed": True,
                    "status": "isolated_test_state",
                    "boundary": {"production_requires_v16_command": True},
                }
            else:
                from backend.services.v16_command_gate import V16CommandGate

                target_agent = v16_target_agent or self._target_agent(actor=actor, source=source)
                scope_type = v16_scope_type or self._scope_type(action=action or source)
                requested_action = self._canonical_v16_action(v16_action or action or source)
                if v16_claim_token and v16_command_id:
                    v16_authority = {
                        "ok": True,
                        "allowed": True,
                        "status": "v16_command_claim_supplied",
                        "command_id": str(v16_command_id),
                        "claim_token": str(v16_claim_token),
                        "target_agent": target_agent,
                        "scope_type": scope_type,
                        "scope_key": v16_scope_key,
                        "requested_action": requested_action,
                    }
                else:
                    v16_authority = V16CommandGate.claim(
                        self.db_path,
                        target_agent=target_agent,
                        scope_type=scope_type,
                        scope_key=v16_scope_key,
                        action=requested_action,
                        command_id=v16_command_id,
                        risk_reduction=risk_reduction,
                    )
            if not v16_authority.get("allowed"):
                return {
                    "ok": False,
                    "status": "blocked_v16_command_required",
                    "reason": "v16_command_required",
                    "v16_authority": v16_authority,
                    "mutation_source": source,
                    "mutation_action": action or source,
                }
            if production_state and v16_authority.get("claim_token"):
                from backend.services.v16_command_gate import V16CommandGate

                consumed = V16CommandGate.consume(
                    self.db_path,
                    command_id=str(v16_authority.get("command_id") or v16_command_id),
                    claim_token=str(v16_authority.get("claim_token") or ""),
                    mutation_id=str(run_id or f"{source}:{int(time.time() * 1000)}"),
                )
                v16_authority["consumed"] = consumed
                if not consumed.get("allowed"):
                    return {
                        "ok": False,
                        "status": "blocked_v16_command_consume_failed",
                        "reason": "v16_command_consume_failed",
                        "v16_authority": v16_authority,
                        "mutation_source": source,
                        "mutation_action": action or source,
                    }
        _refresh_yaml_overlay_base(self.db_path)
        keys = sorted((patch or {}).keys())
        before = _slice_config(runtime_config.shared().to_dict(), keys)
        try:
            result = self.overlay.apply_patch(patch, source=source, run_id=run_id)
        except Exception as exc:
            result = {
                "ok": False,
                "status": "mutation_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        after = _slice_config(runtime_config.shared().to_dict(), keys)

        should_audit = is_state_db_path(self.db_path) if audit is None else bool(audit)
        if should_audit:
            record_api_mutation(
                user=actor,
                endpoint="backend.services.runtime_config_mutation",
                action=action or source,
                status=str(result.get("status") or ("applied" if result.get("ok") else "failed")),
                before=before,
                after=after,
                result=result,
                reason=reason or source,
                required_confirm="autonomous-runtime-config",
                confirm_ok=bool(result.get("ok")),
                source_agent=self._target_agent(actor=actor, source=source),
                decision_type="autonomous_mutation" if str(actor or "").startswith("system:") else "manual_api_mutation",
            )
        return {
            **result,
            "mutation_source": source,
            "mutation_action": action or source,
            "v16_authority": v16_authority,
            "mutated_at": time.time(),
        }

    @staticmethod
    def _target_agent(*, actor: str, source: str) -> str:
        value = f"{actor} {source}".lower()
        if "position_supervisor" in value or "supervisor_template" in value:
            return "position_supervisor_governance"
        if "parameter_template" in value or "context_policy" in value:
            return "autonomous_learning"
        return "factor_governance"

    @staticmethod
    def _scope_type(*, action: str) -> str:
        value = str(action or "").lower()
        if "supervisor" in value:
            return "supervisor_template"
        if "parameter" in value or "template" in value:
            return "parameter_template"
        return "factor_weight"

    @classmethod
    def _requires_v16_command(
        cls,
        *,
        patch: dict[str, Any],
        source: str,
        action: str,
        actor: str,
        risk_reduction: bool,
    ) -> bool:
        if any(token in f"{source} {action}".lower() for token in ("restore", "startup")):
            return False
        if risk_reduction:
            return False
        keys = {str(key) for key in (patch or {})}
        governance_keys = {
            "factor_portfolio_weights",
            "factor_signal_config",
            "position_supervisor_template_id",
            "active_parameter_templates",
            "context_policy",
        }
        governance_tokens = (
            "factor_governance",
            "parameter_template",
            "supervisor_template",
            "position_supervisor",
            "promote_factor",
            "retire_factor",
            "update_weight",
            "context_policy",
            "switch_parameter",
            "switch_position",
        )
        return bool(keys & governance_keys) or any(
            token in f"{source} {action}".lower() for token in governance_tokens
        )

    @staticmethod
    def _canonical_v16_action(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if "parameter" in normalized:
            return "switch_parameter_template"
        if "supervisor" in normalized and "rollback" not in normalized:
            return "switch_position_supervisor_template"
        if "rollback" in normalized or "downsize" in normalized or "disable" in normalized or "retire" in normalized or "quarantine" in normalized:
            return "retire_factor"
        if "promot" in normalized or "activat" in normalized:
            return "promote_factor"
        if "redundan" in normalized:
            return "factor_governance_cycle"
        if "weight" in normalized or normalized in {"boost", "downweight"}:
            return "update_weight"
        return normalized or "factor_governance_cycle"
