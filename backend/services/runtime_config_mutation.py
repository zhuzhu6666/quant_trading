"""Unified runtime config mutation path for autonomous services."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Mapping

from backend.core.db import STATE_DB, is_state_db_path
from backend.services.mutation_audit import record_api_mutation
from backend.services.runtime_config_overlay import (
    RuntimeConfigOverlayService,
    _deep_merge,
    _sanitize_patch,
)
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


def _classify_runtime_patch(
    patch: Mapping[str, Any],
    *,
    current: Any | None = None,
) -> dict[str, Any]:
    """Derive governance risk from the effective before/target values.

    This compatibility-path precheck is intentionally based on facts rather
    than ``risk_reduction`` or action-name hints supplied by a caller.  The
    coordinator performs the authoritative recheck under the scope lock.
    """

    from backend.services.governance_mutation_coordinator import (
        classify_governance_risk,
    )

    sanitized = _sanitize_patch(dict(patch or {}))
    current = current or runtime_config.shared()
    current_payload = current.to_dict()
    target = runtime_config.RuntimeConfig.from_dict(
        _deep_merge(current_payload, sanitized)
    )
    keys = sorted(sanitized)
    return classify_governance_risk(
        _slice_config(current_payload, keys),
        _slice_config(target.to_dict(), keys),
    ).to_dict()


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
        v16_candidate_id: str = "",
        v16_posterior_fingerprint: str = "",
        risk_reduction: bool = False,
        governance_mutation_id: str = "",
        governance_idempotency_key: str = "",
        governance_evidence_refs: dict[str, Any] | None = None,
        governance_evidence_fingerprint: str = "",
        governance_rollback: dict[str, Any] | None = None,
        governance_transaction_writer: Callable[
            [Any, str, Any], Mapping[str, Any] | None
        ] | None = None,
    ) -> dict[str, Any]:
        static_release_flags = {
            "live_safety_plane_v2_mode",
            "live_generation_controller_v2_enabled",
            "ctrader_execution_outcome_v2_enabled",
            "governance_mutation_coordinator_v2_mode",
            "pg_job_queue_v2_enabled",
        }
        attempted_static_flags = sorted(static_release_flags & set(patch or {}))
        if attempted_static_flags:
            return {
                "ok": False,
                "status": "static_feature_flag_mutation_forbidden",
                "reason": "release_flags_require_deployment_and_restart",
                "forbidden_keys": attempted_static_flags,
                "mutation_source": source,
                "mutation_action": action or source,
            }
        if "governance_expansion_paused" in (patch or {}) and str(actor or "").startswith("system:"):
            return {
                "ok": False,
                "status": "operator_governance_pause_required",
                "reason": "autonomous_services_cannot_modify_operator_kill_switch",
                "mutation_source": source,
                "mutation_action": action or source,
            }
        v16_authority: dict[str, Any] = {}
        governance_surface = self._requires_v16_command(
            patch=patch,
            source=source,
            action=action or source,
            actor=actor,
            risk_reduction=False,
        )
        risk_classification: dict[str, Any] = {
            "risk_class": "not_governance",
            "classification_source": "not_governance",
            "v16_required": False,
        }
        current_config = runtime_config.shared()
        governance_paused_before = runtime_config.governance_expansion_is_paused(
            current_config
        )
        if governance_surface:
            try:
                _refresh_yaml_overlay_base(self.db_path)
                current_config = runtime_config.shared()
                if is_state_db_path(self.db_path):
                    latest_overlay = self.overlay.latest()
                    current_config = runtime_config.config_from_overlay(
                        dict(latest_overlay.get("overlay") or {}),
                        self.db_path,
                    )
                risk_classification = _classify_runtime_patch(
                    patch,
                    current=current_config,
                )
                governance_paused_before = bool(
                    getattr(current_config, "governance_expansion_paused", False)
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "status": (
                        "invalid_governance_patch"
                        if isinstance(exc, (TypeError, ValueError))
                        else "governance_before_state_unavailable"
                    ),
                    "reason": f"{type(exc).__name__}: {exc}",
                    "mutation_source": source,
                    "mutation_action": action or source,
                }
        operator_demo_control = (
            runtime_config.operator_bounded_demo_control_exempt(
                actor=actor,
                patch=patch,
                cfg=current_config,
            )
            or runtime_config.operator_classic_builtin_factor_activation_exempt(
                actor=actor,
                patch=patch,
                cfg=current_config,
            )
        )
        operator_pause_resume = (
            set(patch or {}) == {"governance_expansion_paused"}
            and not str(actor or "").startswith("system:")
        )
        if (
            governance_surface
            and governance_paused_before
            and risk_classification.get("risk_class") == "risk_expanding"
            and not operator_pause_resume
            and not operator_demo_control
        ):
            return {
                "ok": False,
                "status": "blocked_governance_expansion_paused",
                "reason": "operator_all_mode_governance_expansion_pause",
                "mutation_source": source,
                "mutation_action": action or source,
            }
        coordinator_mode = "off"
        if governance_surface:
            try:
                from backend.core.static_feature_flags import shared_static_feature_flags

                coordinator_mode = shared_static_feature_flags().governance_mutation_coordinator_v2_mode
            except Exception as exc:
                return {
                    "ok": False,
                    "status": "governance_coordinator_flag_invalid",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "mutation_source": source,
                    "mutation_action": action or source,
                }
        if governance_surface and coordinator_mode in {"dual_record", "enforce"}:
            return self._apply_coordinated_patch(
                patch,
                source=source,
                run_id=run_id,
                actor=actor,
                action=action,
                reason=reason,
                audit=audit,
                v16_command_id=v16_command_id,
                v16_claim_token=v16_claim_token,
                v16_target_agent=v16_target_agent,
                v16_scope_type=v16_scope_type,
                v16_scope_key=v16_scope_key,
                v16_action=v16_action,
                v16_candidate_id=v16_candidate_id,
                v16_posterior_fingerprint=v16_posterior_fingerprint,
                caller_risk_reduction=risk_reduction,
                governance_mutation_id=governance_mutation_id,
                governance_idempotency_key=governance_idempotency_key,
                governance_evidence_refs=governance_evidence_refs,
                governance_evidence_fingerprint=governance_evidence_fingerprint,
                governance_rollback=governance_rollback,
                governance_transaction_writer=governance_transaction_writer,
                coordinator_mode=coordinator_mode,
            )
        production_state = (
            is_state_db_path(self.db_path)
            and Path(self.db_path).resolve() == Path(STATE_DB).resolve()
        )
        if production_state and governance_surface and coordinator_mode not in {
            "dual_record",
            "enforce",
        }:
            return {
                "ok": False,
                "status": "governance_coordinator_required",
                "reason": "production_governance_mutations_cannot_bypass_coordinator",
                "mutation_source": source,
                "mutation_action": action or source,
                "coordinator_mode": coordinator_mode,
            }
        # Callers cannot exempt a governance mutation by supplying the legacy
        # ``risk_reduction`` boolean.  Only derived before/target facts decide
        # whether the V16 expansion command is required.
        should_require_v16 = bool(
            (
                governance_surface
                and risk_classification.get("risk_class") == "risk_expanding"
                and not operator_demo_control
            )
            or (not governance_surface and require_v16_command)
        )
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
                        risk_reduction=False,
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
            "risk_classification": risk_classification,
            "operator_bounded_demo_control_exempt": operator_demo_control,
            "caller_risk_reduction_ignored": bool(risk_reduction),
            "mutated_at": time.time(),
        }

    def _apply_coordinated_patch(
        self,
        patch: dict[str, Any],
        *,
        source: str,
        run_id: str,
        actor: str,
        action: str | None,
        reason: str,
        audit: bool | None,
        v16_command_id: str,
        v16_claim_token: str,
        v16_target_agent: str,
        v16_scope_type: str,
        v16_scope_key: str,
        v16_action: str,
        v16_candidate_id: str,
        v16_posterior_fingerprint: str,
        caller_risk_reduction: bool,
        governance_mutation_id: str,
        governance_idempotency_key: str,
        governance_evidence_refs: dict[str, Any] | None,
        governance_evidence_fingerprint: str,
        governance_rollback: dict[str, Any] | None,
        governance_transaction_writer: Callable[
            [Any, str, Any], Mapping[str, Any] | None
        ] | None,
        coordinator_mode: str,
    ) -> dict[str, Any]:
        from backend.services.governance_mutation_coordinator import (
            GovernanceMutationCoordinator,
            GovernanceMutationPlan,
        )

        _refresh_yaml_overlay_base(self.db_path)
        keys = sorted((patch or {}).keys())
        before = _slice_config(runtime_config.shared().to_dict(), keys)
        scope_type = v16_scope_type or self._scope_type(action=action or source)
        requested_action = self._canonical_v16_action(v16_action or action or source)
        target_agent = v16_target_agent or self._target_agent(actor=actor, source=source)
        scope_key = v16_scope_key or self._scope_key(patch=patch, scope_type=scope_type)
        result = GovernanceMutationCoordinator(
            self.db_path,
            overlay=self.overlay,
        ).execute(
            GovernanceMutationPlan(
                patch=patch,
                source=source,
                actor=actor,
                action=requested_action,
                run_id=run_id,
                reason=reason,
                control_surface=scope_type,
                scope_type=scope_type,
                scope_key=scope_key,
                rollback=dict(governance_rollback or {}),
                evidence_refs=dict(governance_evidence_refs or {}),
                evidence_fingerprint=str(governance_evidence_fingerprint or ""),
                idempotency_key=str(governance_idempotency_key or ""),
                mutation_id=str(governance_mutation_id or ""),
                v16_command_id=str(v16_command_id or ""),
                v16_claim_token=str(v16_claim_token or ""),
                v16_target_agent=target_agent,
                v16_candidate_id=str(v16_candidate_id or ""),
                v16_posterior_fingerprint=str(
                    v16_posterior_fingerprint or ""
                ),
            ),
            transaction_writer=governance_transaction_writer,
        )
        after = _slice_config(runtime_config.shared().to_dict(), keys)
        should_audit = is_state_db_path(self.db_path) if audit is None else bool(audit)
        if should_audit:
            record_api_mutation(
                user=actor,
                endpoint="backend.services.runtime_config_mutation",
                action=action or source,
                status=str(result.get("status") or ("committed" if result.get("ok") else "failed")),
                before=before,
                after=after,
                result=result,
                reason=reason or source,
                required_confirm="autonomous-runtime-config",
                confirm_ok=bool(result.get("ok")),
                source_agent=target_agent,
                decision_type="autonomous_mutation" if str(actor or "").startswith("system:") else "manual_api_mutation",
            )
        return {
            **result,
            "version": runtime_config.version(),
            "updated_keys": keys if result.get("status") in {"committed", "committed_projection_degraded"} else [],
            "mutation_source": source,
            "mutation_action": action or source,
            "coordinator_mode": coordinator_mode,
            "caller_risk_reduction_ignored": bool(caller_risk_reduction),
            "mutated_at": time.time(),
        }

    @staticmethod
    def _target_agent(*, actor: str, source: str) -> str:
        value = f"{actor} {source}".lower()
        if any(token in value for token in ("incident_control", "live_autonomy", "governance_pause", "auto_unfreeze")):
            return "governance_control"
        if "position_supervisor" in value or "supervisor_template" in value:
            return "position_supervisor_governance"
        if "parameter_template" in value or "context_policy" in value:
            return "autonomous_learning"
        return "factor_governance"

    @staticmethod
    def _scope_type(*, action: str) -> str:
        value = str(action or "").lower()
        if "incident" in value:
            return "incident_control"
        if "live_autonomy" in value or "unfreeze" in value:
            return "autonomy_control"
        if "governance" in value and any(token in value for token in ("pause", "resume")):
            return "governance_expansion_control"
        if "model" in value:
            return "model_stage"
        if "supervisor" in value:
            return "supervisor_template"
        if "parameter" in value or "template" in value:
            return "parameter_template"
        return "factor_weight"

    @staticmethod
    def _scope_key(*, patch: dict[str, Any], scope_type: str) -> str:
        if scope_type == "incident_control":
            return "runtime_incident_mode"
        if scope_type == "autonomy_control":
            return "live_autonomy"
        if scope_type == "governance_expansion_control":
            return "governance_expansion_paused"
        if scope_type == "factor_weight":
            weights = patch.get("factor_portfolio_weights")
            if isinstance(weights, dict) and len(weights) == 1:
                return str(next(iter(weights)))
            return "alpha_weight_policy"
        if scope_type == "supervisor_template":
            return "position_supervisor"
        if scope_type in {"parameter_template", "context_policy"}:
            return "threshold_and_sizing"
        if scope_type == "model_stage":
            return "model_influence"
        return "global"

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
        keys = {str(key) for key in (patch or {})}
        governance_keys = {
            "factor_portfolio_weights",
            "factor_signal_config",
            "position_supervisor_template_id",
            "active_parameter_templates",
            "context_policy",
            "demo_model_influence_enabled",
            "model_influence_config",
            "runtime_incident_mode",
            "autonomy_mode",
            "live_autonomy_unlocked",
            "live_autonomy_unlock_id",
            "autonomy_expansion_frozen",
            "governance_expansion_paused",
            "risk_cvar_threshold_pct",
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
            "model_influence",
            "incident_control",
            "live_autonomy",
            "auto_unfreeze",
            "governance_expansion",
        )
        return bool(keys & governance_keys) or any(
            token in f"{source} {action}".lower() for token in governance_tokens
        )

    @staticmethod
    def _canonical_v16_action(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if "incident" in normalized:
            return "set_incident_control"
        if "live_autonomy" in normalized and "revoke" in normalized:
            return "revoke_live_autonomy"
        if "live_autonomy" in normalized:
            return "unlock_live_autonomy"
        if "unfreeze" in normalized:
            return "unfreeze_autonomy_expansion"
        if "governance" in normalized and "pause" in normalized:
            return "pause_governance_expansion"
        if "governance" in normalized and "resume" in normalized:
            return "resume_governance_expansion"
        if "parameter" in normalized:
            return "switch_parameter_template"
        if "supervisor" in normalized and "rollback" not in normalized:
            return "switch_position_supervisor_template"
        if "model" in normalized and any(token in normalized for token in ("demot", "quarant", "rollback", "disable")):
            return "demote_model_influence"
        if "model" in normalized:
            return "promote_model_influence"
        if "rollback" in normalized or "downsize" in normalized or "disable" in normalized or "retire" in normalized or "quarantine" in normalized:
            return "retire_factor"
        if "promot" in normalized or "activat" in normalized:
            return "promote_factor"
        if "redundan" in normalized:
            return "factor_governance_cycle"
        if "weight" in normalized or normalized in {"boost", "downweight"}:
            return "update_weight"
        return normalized or "factor_governance_cycle"
