"""Unified risk policy service.

Phase B entry point for high-impact live decisions.  The service wraps the
existing RiskGovernor and adds action-specific checks so callers can record a
single auditable risk verdict instead of scattering pre-trade branches.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from risk.governor import GovernorState, RiskGovernor
from risk.runtime_policy import RiskLimitSnapshot, RuntimeHealthSnapshot


INCIDENT_MODES = {"normal", "shadow_only", "no_new_risk", "only_close", "frozen"}
INCIDENT_MODE_RANK = {
    "normal": 0,
    "shadow_only": 1,
    "no_new_risk": 2,
    "only_close": 3,
    "frozen": 4,
}
INCIDENT_CONTROLLED_ACTIONS = {
    "open_trade",
    "tighten_position",
    "reduce_position",
    "close_position",
    "update_weight",
    "switch_parameter_template",
    "disable_factor_live",
    "restore_factor_live",
    "retire_factor",
    "enable_context_policy",
    "activate_entry_quality_control",
    "rollback_factor_action",
    "switch_position_supervisor_template",
    "promote_factor",
    "register_factor",
    "start_shadow_model",
    "start_canary_model",
}
RISK_REDUCING_ACTIONS = {"close_position", "reduce_position", "tighten_position", "rollback_factor_action"}
SHADOW_ONLY_ACTIONS = {"start_shadow_model", "start_canary_model"}
LIVE_AUTONOMY_EXPANSION_ACTIONS = {
    "open_trade",
    "update_weight",
    "switch_parameter_template",
    "disable_factor_live",
    "restore_factor_live",
    "retire_factor",
    "enable_context_policy",
    "activate_entry_quality_control",
    "switch_position_supervisor_template",
    "promote_factor",
    "register_factor",
    "start_canary_model",
}
DEMO_NURSERY_SOFT_REASONS = {
    "loss_cooldown_active",
    "consecutive_losses",
    "learning_same_direction_cooldown",
    "learning_event_window_control",
}
DEMO_NURSERY_SOFT_SOURCES = {
    "var_gate",
    "cvar_gate",
    "entry_quality_gate",
}


@dataclass
class RiskVerdict:
    allowed: bool
    reason: str = "ok"
    severity: str = "info"
    max_size: float = 0.0
    required_mode: str = "live"
    audit_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RiskPolicyService:
    """Single facade for risk decisions.

    Supported actions start with ``open_trade`` and now include conservative
    non-execution gates for policy/model governance.
    """

    _instance: "RiskPolicyService | None" = None

    def __init__(self, governor: RiskGovernor | None = None):
        self.governor = governor or RiskGovernor.shared()

    @classmethod
    def shared(cls) -> "RiskPolicyService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def evaluate(self, action: str, context: dict[str, Any] | None = None) -> RiskVerdict:
        context = context or {}
        if action == "set_incident_control":
            return self._evaluate_incident_control_change(context)
        incident_gate = self._evaluate_incident_runtime_mode(action, context)
        if incident_gate is not None and not incident_gate.allowed:
            return incident_gate
        if action == "live_autonomy_budget":
            return self._evaluate_live_autonomy_budget(context)
        live_autonomy_gate = self._evaluate_live_autonomy_gate(action, context)
        if live_autonomy_gate is not None and not live_autonomy_gate.allowed:
            return live_autonomy_gate
        if action == "open_trade":
            return self._evaluate_open_trade(context)
        if action == "tighten_position":
            return self._evaluate_position_adjustment(action, context)
        if action == "reduce_position":
            return self._evaluate_position_adjustment(action, context)
        if action == "close_position":
            return self._evaluate_close_position(context)
        if action == "update_weight":
            return self._evaluate_governor_action(action, context, "allow_weight_update")
        if action == "switch_parameter_template":
            return self._evaluate_governor_action(action, context, "allow_template_switch")
        if action == "disable_factor_live":
            return self._evaluate_governor_action(action, context, "allow_factor_disable")
        if action == "restore_factor_live":
            return self._evaluate_governor_action(action, context, "allow_factor_restore")
        if action == "retire_factor":
            return self._evaluate_governor_action(action, context, "allow_factor_retire")
        if action == "enable_context_policy":
            return self._evaluate_governor_action(action, context, "allow_context_policy")
        if action == "activate_entry_quality_control":
            return self._evaluate_governor_action(action, context, "allow_context_policy")
        if action == "rollback_factor_action":
            return self._evaluate_governor_action(action, context, "allow_factor_rollback")
        if action == "switch_position_supervisor_template":
            return self._evaluate_position_supervisor_template_switch(context)
        if action == "promote_factor":
            return self._evaluate_governor_action(action, context, "allow_promotion")
        if action == "register_factor":
            return self._evaluate_governor_action(action, context, "allow_new_factor")
        if action == "start_shadow_model":
            return self._evaluate_model_stage(action, context, required_mode="shadow")
        if action == "start_canary_model":
            return self._evaluate_model_stage(action, context, required_mode="canary")
        if action == "promote_model_influence":
            return self._evaluate_model_influence(context, promotion=True)
        if action == "demote_model_influence":
            return self._evaluate_model_influence(context, promotion=False)
        if action == "run_replay_job":
            return self._evaluate_low_impact_replay_job(context)
        return RiskVerdict(
            allowed=False,
            reason="unsupported_action",
            severity="error",
            audit_payload={"action": action},
        )

    @staticmethod
    def _current_incident_mode(context: dict[str, Any]) -> str:
        raw = context.get("runtime_incident_mode")
        if raw is None:
            incident_control = context.get("incident_control") or {}
            if isinstance(incident_control, dict):
                raw = incident_control.get("mode")
        if raw is None:
            try:
                from config.runtime_config import shared as runtime_config

                raw = getattr(runtime_config(), "runtime_incident_mode", "normal")
            except Exception:
                raw = "normal"
        mode = str(raw or "normal").strip().lower()
        # Runtime corruption must fail closed.  Config loading rejects invalid
        # values; this branch protects already-constructed/in-memory objects.
        return mode if mode in INCIDENT_MODES else "frozen"

    def _evaluate_incident_runtime_mode(self, action: str, context: dict[str, Any]) -> RiskVerdict | None:
        if action not in INCIDENT_CONTROLLED_ACTIONS:
            return None
        mode = self._current_incident_mode(context)
        if mode == "normal":
            return None
        allowed = False
        if mode == "shadow_only":
            allowed = action in RISK_REDUCING_ACTIONS or action in SHADOW_ONLY_ACTIONS
        elif mode == "no_new_risk":
            allowed = action in RISK_REDUCING_ACTIONS or action == "start_shadow_model"
        elif mode == "only_close":
            allowed = action == "close_position"
        elif mode == "frozen":
            allowed = action in {"close_position", "rollback_factor_action"}
        if allowed:
            return None
        return RiskVerdict(
            allowed=False,
            reason=f"incident_{mode}",
            severity="error",
            required_mode=str(context.get("required_mode") or context.get("mode") or "incident_control"),
            audit_payload={
                "action": action,
                "source": "runtime_incident_control",
                "runtime_incident_mode": mode,
                "allowed_risk_reducing_actions": sorted(RISK_REDUCING_ACTIONS),
                "shadow_only_actions": sorted(SHADOW_ONLY_ACTIONS),
            },
        )

    def _evaluate_incident_control_change(self, context: dict[str, Any]) -> RiskVerdict:
        target = str(context.get("target_mode") or context.get("mode") or "").strip().lower()
        current = str(context.get("current_mode") or self._current_incident_mode(context)).strip().lower()
        local_latch_causes = sorted(
            {
                str(item or "").strip()
                for item in list(context.get("local_latch_causes") or [])
                if str(item or "").strip()
            }
        )
        release_cause = str(context.get("release_cause") or "").strip()
        if target not in INCIDENT_MODES:
            return RiskVerdict(
                allowed=False,
                reason="invalid_incident_mode",
                severity="error",
                required_mode="operator_control",
                audit_payload={
                    "action": "set_incident_control",
                    "source": "risk_policy",
                    "target_mode": target,
                    "valid_modes": sorted(INCIDENT_MODES),
                },
            )
        if current not in INCIDENT_MODES:
            current = "frozen"
        relaxing = INCIDENT_MODE_RANK[target] < INCIDENT_MODE_RANK[current]
        if relaxing and not bool(context.get("confirm_thaw", False)):
            return RiskVerdict(
                allowed=False,
                reason="incident_control_relax_requires_confirm",
                severity="error",
                required_mode="operator_control",
                audit_payload={
                    "action": "set_incident_control",
                    "source": "risk_policy",
                    "current_mode": current,
                    "target_mode": target,
                    "relaxing": True,
                    "local_latch_causes": local_latch_causes,
                    "release_cause": release_cause,
                },
            )
        return RiskVerdict(
            allowed=True,
            reason="ok",
            required_mode="operator_control",
            audit_payload={
                "action": "set_incident_control",
                "source": "risk_policy",
                "current_mode": current,
                "target_mode": target,
                "relaxing": relaxing,
                "reason": context.get("reason", ""),
                "local_latch_causes": local_latch_causes,
                "release_cause": release_cause,
            },
        )

    @staticmethod
    def _runtime_autonomy_context(context: dict[str, Any]) -> dict[str, Any]:
        mode = context.get("autonomy_mode")
        unlocked = context.get("live_autonomy_unlocked")
        unlock_id = context.get("live_autonomy_unlock_id")
        if mode is None or unlocked is None or unlock_id is None:
            try:
                from config.runtime_config import shared as runtime_config

                cfg = runtime_config()
                if mode is None:
                    mode = getattr(cfg, "autonomy_mode", "manual")
                if unlocked is None:
                    unlocked = getattr(cfg, "live_autonomy_unlocked", False)
                if unlock_id is None:
                    unlock_id = getattr(cfg, "live_autonomy_unlock_id", "")
            except Exception:
                mode = mode if mode is not None else "manual"
                unlocked = unlocked if unlocked is not None else False
                unlock_id = unlock_id if unlock_id is not None else ""
        mode = str(mode or "manual").strip().lower()
        return {
            "autonomy_mode": mode,
            "live_autonomy_unlocked": bool(unlocked),
            "live_autonomy_unlock_id": str(unlock_id or ""),
            "runtime_incident_mode": RiskPolicyService._current_incident_mode(context),
        }

    @classmethod
    def _demo_nursery_enabled(cls, context: dict[str, Any]) -> bool:
        return cls._runtime_autonomy_context(context)["autonomy_mode"] == "demo_nursery"

    @staticmethod
    def _demo_nursery_observation(verdict: RiskVerdict) -> dict[str, Any]:
        payload = dict(verdict.audit_payload or {})
        return {
            "schema_version": "demo_nursery_observation.v1",
            "would_block": True,
            "reason": str(verdict.reason or ""),
            "severity": str(verdict.severity or ""),
            "source": str(payload.get("source") or ""),
            "blocked_by": str(payload.get("blocked_by") or ""),
            "audit_payload": payload,
        }

    @staticmethod
    def _attach_demo_nursery_observations(
        verdict: RiskVerdict,
        observations: list[dict[str, Any]],
    ) -> RiskVerdict:
        if observations:
            verdict.audit_payload = dict(verdict.audit_payload or {})
            verdict.audit_payload["demo_nursery_observations"] = list(observations)
        return verdict

    @staticmethod
    def _is_demo_nursery_soft_block(verdict: RiskVerdict) -> bool:
        reason = str(verdict.reason or "")
        payload = verdict.audit_payload or {}
        source = str(payload.get("source") or "")
        blocked_by = str(payload.get("blocked_by") or "")
        return (
            reason in DEMO_NURSERY_SOFT_REASONS
            or reason.startswith("var_gate:")
            or reason.startswith("cvar_gate:")
            or source in DEMO_NURSERY_SOFT_SOURCES
            or blocked_by in DEMO_NURSERY_SOFT_SOURCES
        )

    def _observe_or_return_demo_nursery(
        self,
        context: dict[str, Any],
        verdict: RiskVerdict,
        observations: list[dict[str, Any]],
    ) -> RiskVerdict | None:
        if self._demo_nursery_enabled(context) and self._is_demo_nursery_soft_block(verdict):
            observations.append(self._demo_nursery_observation(verdict))
            return None
        return self._attach_demo_nursery_observations(verdict, observations)

    def _evaluate_live_autonomy_gate(self, action: str, context: dict[str, Any]) -> RiskVerdict | None:
        autonomy = self._runtime_autonomy_context(context)
        if autonomy["autonomy_mode"] != "live_autonomous":
            return None
        if action in RISK_REDUCING_ACTIONS:
            return None
        if action in LIVE_AUTONOMY_EXPANSION_ACTIONS and (
            not autonomy["live_autonomy_unlocked"] or not autonomy["live_autonomy_unlock_id"]
        ):
            return RiskVerdict(
                allowed=False,
                reason="live_autonomy_not_unlocked",
                severity="error",
                required_mode="live_autonomy_unlock",
                audit_payload={
                    "action": action,
                    "source": "live_autonomy_gate",
                    **autonomy,
                    "allowed_risk_reducing_actions": sorted(RISK_REDUCING_ACTIONS),
                },
            )
        if action in LIVE_AUTONOMY_EXPANSION_ACTIONS:
            budget = self._live_autonomy_budget_state(context)
            if budget["breached"]:
                return RiskVerdict(
                    allowed=False,
                    reason="live_autonomy_budget_breach",
                    severity="error",
                    required_mode="no_new_risk",
                    audit_payload={
                        "action": action,
                        "source": "live_autonomy_budget",
                        **autonomy,
                        "budget": budget,
                        "recommended_incident_mode": "no_new_risk",
                    },
                )
        return None

    def _evaluate_live_autonomy_budget(self, context: dict[str, Any]) -> RiskVerdict:
        budget = self._live_autonomy_budget_state(context)
        autonomy = self._runtime_autonomy_context(context)
        return RiskVerdict(
            allowed=not budget["breached"],
            reason="ok" if not budget["breached"] else "live_autonomy_budget_breach",
            severity="info" if not budget["breached"] else "error",
            required_mode="live_autonomy_budget",
            audit_payload={
                "action": "live_autonomy_budget",
                "source": "risk_policy",
                **autonomy,
                "budget": budget,
                "recommended_incident_mode": "normal" if not budget["breached"] else "no_new_risk",
            },
        )

    def _live_autonomy_budget_state(self, context: dict[str, Any]) -> dict[str, Any]:
        state = self._build_governor_state(context)
        risk_limits = RiskLimitSnapshot.from_context(context)
        breaches: list[dict[str, Any]] = []
        if risk_limits.max_daily_loss_pct > 0 and state.daily_loss_pct >= risk_limits.max_daily_loss_pct:
            breaches.append({
                "metric": "daily_loss_pct",
                "value": state.daily_loss_pct,
                "limit": risk_limits.max_daily_loss_pct,
            })
        if risk_limits.max_drawdown_pct > 0 and state.drawdown_pct >= risk_limits.max_drawdown_pct:
            breaches.append({
                "metric": "drawdown_pct",
                "value": state.drawdown_pct,
                "limit": risk_limits.max_drawdown_pct,
            })
        if risk_limits.max_daily_trades > 0 and state.daily_trades >= risk_limits.max_daily_trades:
            breaches.append({
                "metric": "daily_trades",
                "value": state.daily_trades,
                "limit": risk_limits.max_daily_trades,
            })
        return {
            "schema_version": "live_autonomy_budget.v1",
            "breached": bool(breaches),
            "breaches": breaches,
            "limits": {
                "max_daily_loss_pct": risk_limits.max_daily_loss_pct,
                "max_drawdown_pct": risk_limits.max_drawdown_pct,
                "max_daily_trades": risk_limits.max_daily_trades,
            },
            "state": {
                "daily_loss_pct": state.daily_loss_pct,
                "drawdown_pct": state.drawdown_pct,
                "daily_trades": state.daily_trades,
                "loop_running": state.loop_running,
                "bridge_connected": state.bridge_connected,
            },
        }

    def _evaluate_open_trade(self, context: dict[str, Any]) -> RiskVerdict:
        state = self._build_governor_state(context)
        demo_nursery_observations: list[dict[str, Any]] = []
        gov_verdict = self.governor.allow_trade(state)
        if not gov_verdict.allowed:
            verdict = RiskVerdict(
                allowed=False,
                reason=gov_verdict.reason or "governor_block",
                severity="error",
                audit_payload={
                    "action": "open_trade",
                    "source": "RiskGovernor",
                    "suggestion": gov_verdict.suggestion,
                    "state": state.extra,
                    "temporal_context": context.get("temporal_context") or {},
                },
            )
            observed = self._observe_or_return_demo_nursery(context, verdict, demo_nursery_observations)
            if observed is not None:
                return observed

        decision_freshness = context.get("decision_freshness") or {}
        if (
            isinstance(decision_freshness, dict)
            and decision_freshness.get("schema_version") == "decision_bar_freshness.v1"
            and not bool(decision_freshness.get("fresh", True))
        ):
            return self._attach_demo_nursery_observations(RiskVerdict(
                allowed=False,
                reason="decision_bar_stale",
                severity="warn",
                audit_payload={
                    "action": "open_trade",
                    "source": "live_decision_bar_freshness",
                    "blocked_by": "decision_bar_freshness",
                    "decision_freshness": decision_freshness,
                    "trade": context.get("trade") or {},
                    "temporal_context": context.get("temporal_context") or {},
                },
            ), demo_nursery_observations)

        if (
            isinstance(decision_freshness, dict)
            and decision_freshness.get("schema_version") == "decision_bar_freshness.v1"
        ):
            raw_signal_age = decision_freshness.get("age_seconds")
            try:
                signal_age_seconds = float(raw_signal_age)
            except (TypeError, ValueError):
                signal_age_seconds = float("inf")
            if (
                raw_signal_age is None
                or not math.isfinite(signal_age_seconds)
                or signal_age_seconds < 0
            ):
                return self._attach_demo_nursery_observations(RiskVerdict(
                    allowed=False,
                    reason="decision_signal_timestamp_unknown",
                    severity="warn",
                    audit_payload={
                        "action": "open_trade",
                        "source": "live_decision_signal_age",
                        "blocked_by": "decision_signal_timestamp",
                        "decision_freshness": decision_freshness,
                        "signal_age_seconds": None,
                        "trade": context.get("trade") or {},
                        "temporal_context": context.get("temporal_context") or {},
                    },
                ), demo_nursery_observations)
            temporal_context = context.get("temporal_context") or {}
            try:
                timeframe_seconds = float(
                    decision_freshness.get("timeframe_seconds")
                    or temporal_context.get("timeframe_seconds")
                    or 0.0
                )
            except (TypeError, ValueError):
                timeframe_seconds = 0.0
            stale_after_seconds = max(180.0, timeframe_seconds * 1.5)
            if signal_age_seconds > stale_after_seconds:
                return self._attach_demo_nursery_observations(RiskVerdict(
                    allowed=False,
                    reason="decision_signal_age_stale",
                    severity="warn",
                    audit_payload={
                        "action": "open_trade",
                        "source": "live_decision_signal_age",
                        "blocked_by": "decision_signal_age",
                        "decision_freshness": decision_freshness,
                        "signal_age_seconds": round(signal_age_seconds, 3),
                        "stale_after_seconds": round(stale_after_seconds, 3),
                        "trade": context.get("trade") or {},
                        "temporal_context": temporal_context,
                    },
                ), demo_nursery_observations)

        supervisor_block = context.get("supervisor_reentry_block") or {}
        if bool(supervisor_block.get("active", False)):
            remaining_seconds = float(supervisor_block.get("remaining_seconds", 0.0) or 0.0)
            reason = str(supervisor_block.get("reason") or "position_supervisor")
            action = str(supervisor_block.get("action") or "")
            source = str(supervisor_block.get("source") or "position_supervisor")
            return self._attach_demo_nursery_observations(RiskVerdict(
                allowed=False,
                reason="supervisor_reentry_cooldown",
                severity="warn",
                audit_payload={
                    "action": "open_trade",
                    "source": source,
                    "blocked_by": "position_supervisor",
                    "supervisor_action": action,
                    "supervisor_reason": reason,
                    "symbol": supervisor_block.get("symbol"),
                    "direction": supervisor_block.get("direction"),
                    "position_id": supervisor_block.get("position_id"),
                    "remaining_seconds": round(remaining_seconds, 3),
                    "trade": context.get("trade") or {},
                    "temporal_context": context.get("temporal_context") or {},
                },
            ), demo_nursery_observations)

        entry_cluster_policy = context.get("entry_cluster_learning_policy") or {}
        entry_cluster = context.get("entry_cluster") or {}
        if bool(entry_cluster_policy.get("active", False)):
            same_count = int(
                entry_cluster.get("same_direction_open_count_before")
                or context.get("same_direction_open_count")
                or 0
            )
            min_same_count = int(entry_cluster_policy.get("min_same_direction_open_count") or 0)
            seconds_since_raw = entry_cluster.get(
                "seconds_since_last_same_direction_open"
            )
            timestamp_state = str(
                entry_cluster.get("same_direction_open_timestamp_state")
                or ("known" if seconds_since_raw is not None else "unknown")
            )
            seconds_since = (
                float(seconds_since_raw)
                if seconds_since_raw is not None
                else None
            )
            cooldown_seconds = float(context.get("same_direction_cooldown_seconds") or 0.0)
            if (
                min_same_count > 0
                and same_count >= min_same_count
                and timestamp_state != "known"
            ):
                verdict = RiskVerdict(
                    allowed=False,
                    reason="entry_cluster_timestamp_unknown",
                    severity="error",
                    audit_payload={
                        "action": "open_trade",
                        "source": "broker_positions",
                        "blocked_by": "entry_cluster_timestamp_freshness",
                        "same_direction_open_count": same_count,
                        "min_same_direction_open_count": min_same_count,
                        "timestamp_state": timestamp_state,
                        "unknown_position_ids": list(
                            entry_cluster.get("unknown_open_timestamp_position_ids") or []
                        ),
                        "trade": context.get("trade") or {},
                        "temporal_context": context.get("temporal_context") or {},
                    },
                )
                observed = self._observe_or_return_demo_nursery(
                    context,
                    verdict,
                    demo_nursery_observations,
                )
                if observed is not None:
                    return observed
            elif (
                min_same_count > 0
                and same_count >= min_same_count
                and seconds_since is not None
                and 0.0 < seconds_since < cooldown_seconds
            ):
                verdict = RiskVerdict(
                    allowed=False,
                    reason="learning_same_direction_cooldown",
                    severity="warn",
                    audit_payload={
                        "action": "open_trade",
                        "source": "autonomous_learning",
                        "blocked_by": "entry_cluster_learning_policy",
                        "same_direction_open_count": same_count,
                        "min_same_direction_open_count": min_same_count,
                        "seconds_since_last_same_direction_open": round(seconds_since, 3),
                        "cooldown_seconds": round(cooldown_seconds, 3),
                        "controls": entry_cluster_policy.get("controls") or [],
                        "trade": context.get("trade") or {},
                        "temporal_context": context.get("temporal_context") or {},
                    },
                )
                observed = self._observe_or_return_demo_nursery(context, verdict, demo_nursery_observations)
                if observed is not None:
                    return observed

        entry_quality_gate = context.get("entry_quality_gate") or {}
        if bool(entry_quality_gate.get("active", False)) and not bool(entry_quality_gate.get("allowed", True)):
            verdict = RiskVerdict(
                allowed=False,
                reason=str(entry_quality_gate.get("reason") or "learning_entry_quality_gate"),
                severity="warn",
                audit_payload={
                    "action": "open_trade",
                    "source": str(entry_quality_gate.get("source") or "entry_quality_gate"),
                    "blocked_by": "entry_quality_gate",
                    "entry_quality_gate": entry_quality_gate,
                    "trade": context.get("trade") or {},
                    "temporal_context": context.get("temporal_context") or {},
                },
            )
            observed = self._observe_or_return_demo_nursery(context, verdict, demo_nursery_observations)
            if observed is not None:
                return observed

        event_filter = context.get("event_filter") or context.get("event_risk_filter") or {}
        if bool(event_filter.get("active", False)) and bool(event_filter.get("blocked", False)):
            return self._attach_demo_nursery_observations(RiskVerdict(
                allowed=False,
                reason=str(event_filter.get("reason") or "event_risk_filter"),
                severity="warn",
                audit_payload={
                    "action": "open_trade",
                    "source": str(event_filter.get("source") or "event_risk_filter"),
                    "blocked_by": "event_risk_filter",
                    "event_filter": event_filter,
                    "trade": context.get("trade") or {},
                    "temporal_context": context.get("temporal_context") or {},
                },
            ), demo_nursery_observations)

        event_window_policy = context.get("event_window_learning_policy") or {}
        event_sizing = context.get("event_sizing") or {}
        if bool(event_window_policy.get("active", False)):
            event_name = str(event_sizing.get("event_type") or event_sizing.get("event") or "").strip()
            window_bucket = str(event_sizing.get("window_bucket") or "").strip()
            schema_version = str(event_sizing.get("schema_version") or "").strip()
            current_key = f"{event_name}:{window_bucket}" if event_name and window_bucket else ""
            matching_controls = [
                item
                for item in (event_window_policy.get("controls") or [])
                if str(item.get("scope_key") or "") == current_key
                and str(item.get("action") or "") in {"tighten_event_window_sizing", "extend_event_post_window_review"}
            ]
            if matching_controls and schema_version == "event_sizing.short_window.v2":
                verdict = RiskVerdict(
                    allowed=False,
                    reason="learning_event_window_control",
                    severity="warn",
                    audit_payload={
                        "action": "open_trade",
                        "source": "autonomous_learning",
                        "blocked_by": "event_window_learning_policy",
                        "event_window_key": current_key,
                        "event_sizing": event_sizing,
                        "controls": matching_controls,
                        "trade": context.get("trade") or {},
                        "temporal_context": context.get("temporal_context") or {},
                    },
                )
                observed = self._observe_or_return_demo_nursery(context, verdict, demo_nursery_observations)
                if observed is not None:
                    return observed

        risk_snapshot = context.get("risk_snapshot") or {}
        risk_limits = RiskLimitSnapshot.from_context(context)
        var_cfg = context.get("var") or {}
        forward_var_audit: dict[str, Any] = {}
        if bool(var_cfg.get("enabled", False)):
            snapshot_meta = risk_snapshot.get("snapshot") or {}
            if (
                snapshot_meta.get("schema_version")
                != "risk_metrics_snapshot.v2"
            ):
                return self._attach_demo_nursery_observations(RiskVerdict(
                    allowed=False,
                    reason="risk_metrics_contract_missing",
                    severity="error",
                    audit_payload={
                        "action": "open_trade",
                        "source": "risk_metrics_snapshot.v2",
                        "blocked_by": "risk_metrics_snapshot_contract",
                        "temporal_context": context.get("temporal_context") or {},
                    },
                ), demo_nursery_observations)
            snapshot_status = str(snapshot_meta.get("status") or "")
            if (
                snapshot_meta.get("schema_version")
                == "risk_metrics_snapshot.v2"
                and snapshot_status in {"unknown", "stale", "error"}
            ):
                return self._attach_demo_nursery_observations(RiskVerdict(
                    allowed=False,
                    reason=f"risk_metrics_{snapshot_status or 'unknown'}",
                    severity="error",
                    audit_payload={
                        "action": "open_trade",
                        "source": "risk_metrics_snapshot.v2",
                        "blocked_by": "risk_metrics_snapshot_status",
                        "snapshot_status": snapshot_status or "unknown",
                        "temporal_context": context.get("temporal_context") or {},
                    },
                ), demo_nursery_observations)
            var_data = risk_snapshot.get("var") or {}
            forward_var_audit = {
                "candidate_forward_var": var_data,
                "candidate_forward_var_shadow_99": (
                    risk_snapshot.get("var_shadow_99") or {}
                ),
            }
            var_status = str(var_data.get("status") or "unknown")
            if var_status != "known":
                return self._attach_demo_nursery_observations(RiskVerdict(
                    allowed=False,
                    reason=f"var_metrics_{var_status}",
                    severity="error",
                    audit_payload={
                        "action": "open_trade",
                        "source": "risk_metrics_snapshot.v2",
                        "blocked_by": "var_metrics_status",
                        "var_status": var_status,
                        **forward_var_audit,
                        "temporal_context": context.get("temporal_context") or {},
                    },
                ), demo_nursery_observations)
            try:
                var_pct = float(var_data.get("var_pct"))
                cvar_pct = float(var_data.get("cvar_pct"))
                valid_var = (
                    math.isfinite(var_pct)
                    and math.isfinite(cvar_pct)
                    and var_pct >= 0
                    and cvar_pct >= var_pct
                )
            except (TypeError, ValueError, OverflowError):
                valid_var = False
                var_pct = cvar_pct = 0.0
            if not valid_var:
                return self._attach_demo_nursery_observations(RiskVerdict(
                    allowed=False,
                    reason="var_metrics_invalid",
                    severity="error",
                    audit_payload={
                        "action": "open_trade",
                        "source": "risk_metrics_snapshot.v2",
                        "blocked_by": "var_metrics_values",
                        **forward_var_audit,
                        "temporal_context": context.get("temporal_context") or {},
                    },
                ), demo_nursery_observations)
            threshold_pct = float(var_cfg.get("threshold_pct", risk_limits.var_threshold_pct) or 0.0)
            if threshold_pct > 0 and var_pct > threshold_pct:
                verdict = RiskVerdict(
                    allowed=False,
                    reason=f"var_gate: VaR={var_pct:.1f}% > {threshold_pct:.1f}%",
                    severity="error",
                    audit_payload={
                        "action": "open_trade",
                        "source": "var_gate",
                        "var_pct": var_pct,
                        "threshold_pct": threshold_pct,
                        **forward_var_audit,
                        "temporal_context": context.get("temporal_context") or {},
                    },
                )
                observed = self._observe_or_return_demo_nursery(context, verdict, demo_nursery_observations)
                if observed is not None:
                    return observed
            cvar_threshold_pct = float(var_cfg.get("cvar_threshold_pct", risk_limits.cvar_threshold_pct) or 0.0)
            if cvar_threshold_pct > 0 and cvar_pct > cvar_threshold_pct:
                verdict = RiskVerdict(
                    allowed=False,
                    reason=f"cvar_gate: CVaR={cvar_pct:.1f}% > {cvar_threshold_pct:.1f}%",
                    severity="error",
                    audit_payload={
                        "action": "open_trade",
                        "source": "cvar_gate",
                        "cvar_pct": cvar_pct,
                        "threshold_pct": cvar_threshold_pct,
                        "risk_limits": risk_limits.to_dict(),
                        **forward_var_audit,
                        "temporal_context": context.get("temporal_context") or {},
                    },
                )
                observed = self._observe_or_return_demo_nursery(context, verdict, demo_nursery_observations)
                if observed is not None:
                    return observed

        event_block_reason = str(event_sizing.get("blocked_reason") or "")
        if event_block_reason:
            try:
                event_importance = int(event_sizing.get("event_importance") or 0)
            except (TypeError, ValueError):
                event_importance = 0
            try:
                minutes_until_event = float(event_sizing.get("minutes_until_event"))
            except (TypeError, ValueError):
                minutes_until_event = 999999.0
            window_bucket = str(event_sizing.get("window_bucket") or "")
            is_post_event = bool(event_sizing.get("is_post_event", False))
            is_hard_event_window = (
                event_importance >= 3
                and (
                    -5.0 <= minutes_until_event <= 15.0
                    or is_post_event
                    or window_bucket in {"pre_0_15m", "post_0_5m"}
                )
            )
            if is_hard_event_window:
                return self._attach_demo_nursery_observations(RiskVerdict(
                    allowed=False,
                    reason=f"event_hard_window: {event_block_reason}",
                    severity="warn",
                    audit_payload={
                        "action": "open_trade",
                        "source": "event_sizing",
                        "event_sizing": event_sizing,
                        "temporal_context": context.get("temporal_context") or {},
                    },
                ), demo_nursery_observations)

        open_position_count = int(context.get("open_position_count", 0) or 0)
        max_position_count = int(context.get("max_position_count", 0) or 0)
        entry_cluster = context.get("entry_cluster") or {}
        opposite_direction_open_count = int(entry_cluster.get("opposite_direction_open_count_before", 0) or 0)
        if opposite_direction_open_count > 0 and not bool(context.get("allow_opposite_direction_open", False)):
            return self._attach_demo_nursery_observations(RiskVerdict(
                allowed=False,
                reason="opposite_direction_position_open",
                severity="error",
                audit_payload={
                    "action": "open_trade",
                    "source": "entry_cluster",
                    "entry_cluster": entry_cluster,
                    "opposite_direction_open_count": opposite_direction_open_count,
                    "temporal_context": context.get("temporal_context") or {},
                },
            ), demo_nursery_observations)
        if max_position_count > 0 and open_position_count >= max_position_count:
            return self._attach_demo_nursery_observations(RiskVerdict(
                allowed=False,
                reason=f"仓位上限: {open_position_count}/{max_position_count}",
                severity="error",
                audit_payload={
                    "action": "open_trade",
                    "source": "position_count",
                    "open_position_count": open_position_count,
                    "max_position_count": max_position_count,
                    "temporal_context": context.get("temporal_context") or {},
                },
            ), demo_nursery_observations)

        requested_api_volume = float(context.get("requested_api_volume", 0.0) or 0.0)
        if "requested_api_volume" in context and requested_api_volume <= 0:
            return self._attach_demo_nursery_observations(RiskVerdict(
                allowed=False,
                reason="non_positive_requested_volume",
                severity="warn",
                audit_payload={
                    "action": "open_trade",
                    "source": "api_volume",
                    "requested_api_volume": requested_api_volume,
                    "event_sizing": event_sizing,
                    "entry_cluster": entry_cluster,
                    "temporal_context": context.get("temporal_context") or {},
                },
            ), demo_nursery_observations)
        total_api_volume = float(context.get("total_api_volume", 0.0) or 0.0)
        max_api_volume = float(context.get("max_position_api_volume", 0.0) or 0.0)
        remaining_api_volume = max(0.0, max_api_volume - total_api_volume) if max_api_volume > 0 else 0.0
        if max_api_volume > 0 and total_api_volume + requested_api_volume > max_api_volume:
            return self._attach_demo_nursery_observations(RiskVerdict(
                allowed=False,
                reason=f"API量上限: {total_api_volume:.0f}+{requested_api_volume:.0f}>{max_api_volume:.0f}",
                severity="error",
                max_size=remaining_api_volume,
                audit_payload={
                    "action": "open_trade",
                    "source": "api_volume",
                    "total_api_volume": total_api_volume,
                    "requested_api_volume": requested_api_volume,
                    "max_position_api_volume": max_api_volume,
                    "temporal_context": context.get("temporal_context") or {},
                },
            ), demo_nursery_observations)

        if bool(context.get("pyramid_enabled", True)) and open_position_count > 0:
            max_entry_score = float(context.get("max_abs_entry_score", 0.0) or 0.0)
            signal_score = abs(float(context.get("signal_score", 0.0) or 0.0))
            if max_entry_score > 0 and max_entry_score >= signal_score:
                return self._attach_demo_nursery_observations(RiskVerdict(
                    allowed=False,
                    reason=f"金字塔: 需超 {max_entry_score:.4f}",
                    severity="warn",
                    audit_payload={
                        "action": "open_trade",
                    "source": "pyramid",
                    "max_abs_entry_score": max_entry_score,
                    "signal_score": signal_score,
                    "temporal_context": context.get("temporal_context") or {},
                },
            ), demo_nursery_observations)

        return self._attach_demo_nursery_observations(RiskVerdict(
            allowed=True,
            reason="ok",
            max_size=remaining_api_volume,
            audit_payload={
                "action": "open_trade",
                "source": "risk_policy",
                "open_position_count": open_position_count,
                "total_api_volume": total_api_volume,
                "requested_api_volume": requested_api_volume,
                "max_position_count": max_position_count,
                "max_position_api_volume": max_api_volume,
                "event_sizing": event_sizing,
                "risk_limits": risk_limits.to_dict(),
                **forward_var_audit,
                "state": state.extra,
                "temporal_context": context.get("temporal_context") or {},
                "decision_freshness": context.get("decision_freshness") or {},
            },
        ), demo_nursery_observations)

    def _build_governor_state(self, context: dict[str, Any]) -> GovernorState:
        risk_limits = RiskLimitSnapshot.from_context(context)
        runtime_health_snapshot = RuntimeHealthSnapshot.from_context(context)
        account = context.get("account") or {}
        session = context.get("session") or {}
        temporal_context = context.get("temporal_context") or {}
        session_pnl = float(session.get("pnl", 0.0) or 0.0)
        start_balance = float(session.get("start_balance", 0.0) or 0.0)
        daily_loss_pct = float(session.get("daily_loss_pct", 0.0) or 0.0)
        if daily_loss_pct <= 0 and session_pnl < 0 and start_balance > 0:
            daily_loss_pct = abs(session_pnl) / start_balance * 100.0
        extra = {
            "open_position_count": int(context.get("open_position_count", 0) or 0),
            "requested_api_volume": float(context.get("requested_api_volume", 0.0) or 0.0),
            "total_api_volume": float(context.get("total_api_volume", 0.0) or 0.0),
            "risk_limits": risk_limits.to_dict(),
            "runtime_health_snapshot": runtime_health_snapshot.to_dict(),
        }
        runtime_health = context.get("runtime_health") or {}
        if runtime_health:
            extra["runtime_health"] = runtime_health
        if temporal_context:
            extra["temporal_context"] = temporal_context
        extra["block_on_disk_critical"] = bool(
            context.get("block_on_disk_critical", risk_limits.block_on_disk_critical)
        )
        extra["loss_cooldown_after_losses"] = int(
            context.get("loss_cooldown_after_losses", risk_limits.loss_cooldown_after_losses) or 0
        )
        extra["loss_cooldown_bars"] = int(
            context.get("loss_cooldown_bars", risk_limits.loss_cooldown_bars) or 0
        )
        return GovernorState(
            balance=float(account.get("balance", 0.0) or 0.0),
            equity=float(account.get("equity", 0.0) or 0.0),
            drawdown_pct=float(session.get("drawdown_pct", 0.0) or 0.0),
            open_positions=int(context.get("open_position_count", 0) or 0),
            consecutive_losses=int(session.get("consecutive_losses", 0) or 0),
            daily_trades=int(session.get("trades", 0) or 0),
            daily_loss_pct=daily_loss_pct,
            circuit_broken=bool(session.get("circuit_breaker", False)),
            data_lag_seconds=float(context.get("data_lag_seconds", runtime_health_snapshot.data_lag_seconds) or 0.0),
            loop_running=bool(context.get("loop_running", True)),
            bridge_connected=bool(context.get("bridge_connected", True)),
            timeframe_seconds=int(temporal_context.get("timeframe_seconds", 0) or 0),
            seconds_since_last_trade=float(temporal_context.get("seconds_since_last_trade", 0.0) or 0.0),
            bars_since_last_trade=float(temporal_context.get("bars_since_last_trade", 0.0) or 0.0),
            extra=extra,
        )

    def _evaluate_close_position(self, context: dict[str, Any]) -> RiskVerdict:
        runtime_gate = self._evaluate_runtime_gate("close_position", context)
        if runtime_gate is not None and not runtime_gate.allowed:
            return runtime_gate

        temporal_context = context.get("temporal_context") or {}
        holding_seconds = float(context.get("holding_seconds", 0.0) or 0.0)
        timeframe_seconds = int(
            context.get("timeframe_seconds", temporal_context.get("timeframe_seconds", 0)) or 0
        )
        max_holding_bars = int(context.get("max_holding_bars", 0) or 0)
        max_holding_seconds = float(context.get("max_holding_seconds", 0.0) or 0.0)
        if max_holding_seconds <= 0 and max_holding_bars > 0 and timeframe_seconds > 0:
            max_holding_seconds = float(max_holding_bars * timeframe_seconds)
        holding_timeout_exceeded = bool(max_holding_seconds > 0 and holding_seconds >= max_holding_seconds)
        return RiskVerdict(
            allowed=True,
            reason="risk_reducing_action",
            required_mode=str(context.get("mode") or "live"),
            audit_payload={
                "action": "close_position",
                "source": "risk_policy",
                "position_id": context.get("position_id", ""),
                "close_reason": context.get("close_reason", ""),
                "holding_seconds": holding_seconds,
                "holding_minutes": round(holding_seconds / 60.0, 3) if holding_seconds > 0 else 0.0,
                "max_holding_bars": max_holding_bars,
                "max_holding_seconds": max_holding_seconds,
                "holding_timeout_exceeded": holding_timeout_exceeded,
                "temporal_context": temporal_context,
            },
        )

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _position_field(position: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in position and position.get(key) not in (None, ""):
                return position.get(key)
        return None

    def _position_direction(self, position: dict[str, Any], context: dict[str, Any]) -> int:
        raw = self._position_field(position, "direction", "side", "trade_side", "type")
        if raw is None:
            raw = context.get("direction") or context.get("side")
        text = str(raw or "").strip().lower()
        if text in {"buy", "long", "1", "true"}:
            return 1
        if text in {"sell", "short", "-1"}:
            return -1
        numeric = self._safe_float(raw)
        if numeric > 0:
            return 1
        if numeric < 0:
            return -1
        return 0

    def _evaluate_tp_extension_guard(
        self,
        action: str,
        context: dict[str, Any],
        recommended_controls: dict[str, Any],
    ) -> RiskVerdict | None:
        if action != "tighten_position":
            return None
        position = context.get("position") or {}
        if not isinstance(position, dict):
            position = {}
        target_tp = self._safe_float(recommended_controls.get("target_take_profit"))
        if target_tp <= 0:
            return None
        current_tp = self._safe_float(
            self._position_field(position, "tp", "take_profit", "takeProfit", "current_take_profit")
        )
        if current_tp <= 0:
            return None
        entry_price = self._safe_float(
            self._position_field(position, "entry_price", "price_open", "open_price", "entry", "openPrice")
        )
        direction = self._position_direction(position, context)
        audit_base = {
            "action": action,
            "source": "tp_extension_guard",
            "position_id": context.get("position_id", ""),
            "target_take_profit": target_tp,
            "current_take_profit": current_tp,
            "entry_price": entry_price,
            "direction": direction,
            "recommended_controls": recommended_controls,
        }
        if direction == 0 or entry_price <= 0:
            return RiskVerdict(
                allowed=False,
                reason="tp_extension_context_missing",
                severity="error",
                required_mode=str(context.get("mode") or "live"),
                audit_payload=audit_base,
            )

        epsilon = max(abs(entry_price) * 1e-7, 1e-6)
        if direction > 0:
            is_extension = target_tp > current_tp + epsilon
            correct_direction = target_tp > entry_price + epsilon
        else:
            is_extension = target_tp < current_tp - epsilon
            correct_direction = target_tp < entry_price - epsilon
        if not is_extension:
            return None
        if not correct_direction:
            return RiskVerdict(
                allowed=False,
                reason="invalid_tp_extension_direction",
                severity="error",
                required_mode=str(context.get("mode") or "live"),
                audit_payload=audit_base,
            )

        target_sl = self._safe_float(recommended_controls.get("target_stop_loss"))
        current_sl = self._safe_float(self._position_field(position, "sl", "stop_loss", "stopLoss", "current_stop_loss"))
        effective_sl = target_sl if target_sl > 0 else current_sl
        if effective_sl <= 0:
            return RiskVerdict(
                allowed=False,
                reason="tp_extension_requires_stop_loss",
                severity="error",
                required_mode=str(context.get("mode") or "live"),
                audit_payload={**audit_base, "target_stop_loss": target_sl, "current_stop_loss": current_sl},
            )
        profit_locked = effective_sl >= entry_price - epsilon if direction > 0 else effective_sl <= entry_price + epsilon
        if not profit_locked:
            return RiskVerdict(
                allowed=False,
                reason="tp_extension_requires_profit_lock",
                severity="error",
                required_mode=str(context.get("mode") or "live"),
                audit_payload={
                    **audit_base,
                    "target_stop_loss": target_sl,
                    "current_stop_loss": current_sl,
                    "effective_stop_loss": effective_sl,
                },
            )

        current_distance = abs(current_tp - entry_price)
        target_distance = abs(target_tp - entry_price)
        if current_distance <= epsilon:
            return RiskVerdict(
                allowed=False,
                reason="tp_extension_invalid_current_distance",
                severity="error",
                required_mode=str(context.get("mode") or "live"),
                audit_payload=audit_base,
            )
        max_extension_factor = self._safe_float(
            recommended_controls.get("max_tp_extension_factor")
            or context.get("max_tp_extension_factor")
            or (context.get("tp_extension_policy") or {}).get("max_tp_extension_factor"),
            0.35,
        )
        max_extension_factor = min(1.0, max(0.0, max_extension_factor))
        extension_factor = (target_distance / current_distance) - 1.0
        if extension_factor > max_extension_factor + 1e-9:
            return RiskVerdict(
                allowed=False,
                reason="tp_extension_exceeds_max_factor",
                severity="error",
                required_mode=str(context.get("mode") or "live"),
                audit_payload={
                    **audit_base,
                    "current_distance": current_distance,
                    "target_distance": target_distance,
                    "extension_factor": round(extension_factor, 6),
                    "max_tp_extension_factor": max_extension_factor,
                },
            )

        extension_count = int(
            self._safe_float(
                context.get("tp_extension_count")
                or position.get("tp_extension_count")
                or recommended_controls.get("tp_extension_count")
            )
        )
        max_extensions = int(
            self._safe_float(
                recommended_controls.get("max_tp_extensions_per_position")
                or context.get("max_tp_extensions_per_position")
                or (context.get("tp_extension_policy") or {}).get("max_tp_extensions_per_position"),
                2.0,
            )
        )
        if extension_count >= max_extensions:
            return RiskVerdict(
                allowed=False,
                reason="tp_extension_count_exceeded",
                severity="error",
                required_mode=str(context.get("mode") or "live"),
                audit_payload={
                    **audit_base,
                    "tp_extension_count": extension_count,
                    "max_tp_extensions_per_position": max_extensions,
                },
            )
        return None

    def _evaluate_position_adjustment(self, action: str, context: dict[str, Any]) -> RiskVerdict:
        runtime_gate = self._evaluate_runtime_gate(action, context)
        if runtime_gate is not None and not runtime_gate.allowed:
            return runtime_gate

        temporal_context = context.get("temporal_context") or {}
        supervisor_action = str(context.get("supervisor_action") or action)
        recommended_controls = context.get("recommended_controls") or {}
        tp_extension_guard = self._evaluate_tp_extension_guard(action, context, recommended_controls)
        if tp_extension_guard is not None:
            return tp_extension_guard
        return RiskVerdict(
            allowed=True,
            reason="risk_reducing_action",
            required_mode=str(context.get("mode") or "live"),
            audit_payload={
                "action": action,
                "source": "risk_policy",
                "position_id": context.get("position_id", ""),
                "supervisor_action": supervisor_action,
                "supervisor_reason": context.get("supervisor_reason", ""),
                "supervisor_confidence": float(context.get("supervisor_confidence", 0.0) or 0.0),
                "supervisor_evidence": context.get("supervisor_evidence") or {},
                "recommended_controls": recommended_controls,
                "temporal_context": temporal_context,
            },
        )

    def _evaluate_position_supervisor_template_switch(self, context: dict[str, Any]) -> RiskVerdict:
        target_template_id = str(context.get("target_template_id") or "").strip()
        suggestion_status = str(context.get("suggestion_status") or "").strip().lower()
        if suggestion_status != "approved":
            return RiskVerdict(
                allowed=False,
                reason="suggestion_not_approved",
                severity="error",
                required_mode="governed",
                audit_payload={
                    "action": "switch_position_supervisor_template",
                    "source": "risk_policy",
                    "target_template_id": target_template_id,
                    "suggestion_status": suggestion_status,
                },
            )
        try:
            from backend.services.position_supervisor_templates import list_position_supervisor_templates

            templates = list_position_supervisor_templates()
            valid_templates = {str(item.get("template_id") or "") for item in templates}
            template_meta = next((item for item in templates if str(item.get("template_id") or "") == target_template_id), {})
        except Exception:
            valid_templates = set()
            template_meta = {}
        if not target_template_id or target_template_id not in valid_templates:
            return RiskVerdict(
                allowed=False,
                reason="invalid_position_supervisor_template",
                severity="error",
                required_mode="governed",
                audit_payload={
                    "action": "switch_position_supervisor_template",
                    "source": "risk_policy",
                    "target_template_id": target_template_id,
                    "valid_templates": sorted(valid_templates),
                },
            )
        evidence = context.get("evidence") or {}
        has_replay = bool(evidence.get("replay_summary") or evidence.get("replay") or evidence.get("day"))
        has_counterfactual = bool(evidence.get("counterfactual_summary") or evidence.get("counterfactual"))
        if not (has_replay and has_counterfactual):
            return RiskVerdict(
                allowed=False,
                reason="missing_supervisor_switch_evidence",
                severity="error",
                required_mode="governed",
                audit_payload={
                    "action": "switch_position_supervisor_template",
                    "source": "risk_policy",
                    "target_template_id": target_template_id,
                    "has_replay": has_replay,
                    "has_counterfactual": has_counterfactual,
                },
            )
        if bool(context.get("autonomous_apply", False)):
            try:
                from config.runtime_config import shared as runtime_config

                autonomy_mode = str(getattr(runtime_config(), "autonomy_mode", "") or "manual")
            except Exception:
                autonomy_mode = str(context.get("autonomy_mode") or "manual")
            boundary = template_meta.get("risk_boundary") or {}
            allowed_modes = set(boundary.get("auto_deploy_modes") or [])
            mode_allowed = autonomy_mode in allowed_modes or (
                autonomy_mode == "demo_nursery" and "demo_autonomous" in allowed_modes
            )
            if not mode_allowed:
                return RiskVerdict(
                    allowed=False,
                    reason="autonomous_deploy_mode_not_allowed",
                    severity="error",
                    required_mode="governed",
                    audit_payload={
                        "action": "switch_position_supervisor_template",
                        "source": "risk_policy",
                        "target_template_id": target_template_id,
                        "autonomy_mode": autonomy_mode,
                        "allowed_modes": sorted(allowed_modes),
                    },
                )
        return RiskVerdict(
            allowed=True,
            reason="ok",
            required_mode="governed",
            audit_payload={
                "action": "switch_position_supervisor_template",
                "source": "risk_policy",
                "target_template_id": target_template_id,
                "previous_template_id": context.get("previous_template_id", ""),
                "suggestion_id": context.get("suggestion_id", ""),
            },
        )

    def _evaluate_runtime_gate(self, action: str, context: dict[str, Any]) -> RiskVerdict | None:
        close_action = str(action or "").lower()
        close_reason = str(context.get("close_reason") or "").strip().lower()
        close_reason_whitelist = {"manual", "manual_close", "emergency_close", "restart_replay"}

        # Emergency/manual closes are operational override paths and should stay permissive.
        if close_action == "close_position" and close_reason in close_reason_whitelist:
            return None

        has_loop_running = "loop_running" in context
        has_bridge_connected = "bridge_connected" in context
        if has_loop_running and not bool(context.get("loop_running", True)):
            return RiskVerdict(
                allowed=False,
                reason="loop_not_running",
                severity="error",
                audit_payload={
                    "action": action,
                    "source": "runtime_gate",
                    "loop_running": context.get("loop_running", None),
                },
            )
        if has_bridge_connected and not bool(context.get("bridge_connected", True)):
            return RiskVerdict(
                allowed=False,
                reason="bridge_disconnected",
                severity="error",
                audit_payload={
                    "action": action,
                    "source": "runtime_gate",
                    "bridge_connected": context.get("bridge_connected", None),
                },
            )

        # For active execution mutation (tighten/reduce), if runtime fields are partially
        # present but incomplete, fail fast.
        if action in {"tighten_position", "reduce_position"} and (not has_loop_running or not has_bridge_connected):
            return RiskVerdict(
                allowed=False,
                reason="runtime_state_missing",
                severity="error",
                audit_payload={
                    "action": action,
                    "source": "runtime_gate",
                    "runtime_state_present": {
                        "loop_running": has_loop_running,
                        "bridge_connected": has_bridge_connected,
                    },
                },
            )

        # For close actions, tolerate absent runtime fields, but block incomplete explicit
        # runtime hints to keep runtime-gate lightweight and non-intrusive.
        if (
            close_action == "close_position"
            and (not has_loop_running or not has_bridge_connected)
            and not (not has_loop_running and not has_bridge_connected)
        ):
            return RiskVerdict(
                allowed=False,
                reason="runtime_state_missing",
                severity="error",
                audit_payload={
                    "action": action,
                    "source": "runtime_gate",
                    "runtime_state_present": {
                        "loop_running": has_loop_running,
                        "bridge_connected": has_bridge_connected,
                    },
                },
            )

        return None

    def _evaluate_governor_action(
        self,
        action: str,
        context: dict[str, Any],
        method_name: str,
    ) -> RiskVerdict:
        state = self._build_governor_state(context)
        method = getattr(self.governor, method_name)
        gov_verdict = method(state)
        if not gov_verdict.allowed:
            return RiskVerdict(
                allowed=False,
                reason=gov_verdict.reason or "governor_block",
                severity="error",
                required_mode=str(context.get("required_mode") or "governed"),
                audit_payload={
                    "action": action,
                    "source": "RiskGovernor",
                    "suggestion": gov_verdict.suggestion,
                    "state": state.extra,
                },
            )
        return RiskVerdict(
            allowed=True,
            reason="ok",
            required_mode=str(context.get("required_mode") or "governed"),
            audit_payload={
                "action": action,
                "source": "risk_policy",
                "state": state.extra,
            },
        )

    def _evaluate_model_stage(
        self,
        action: str,
        context: dict[str, Any],
        *,
        required_mode: str,
    ) -> RiskVerdict:
        capabilities = context.get("capabilities") or {}
        artifact = dict(context.get("artifact") or {})
        artifact.setdefault("capabilities", capabilities)
        artifact.setdefault("model_type", context.get("model_type") or action)
        artifact.setdefault("artifact_path", context.get("artifact_path", ""))
        try:
            from backend.services.model_permissions import evaluate_model_permissions

            permission = evaluate_model_permissions(
                artifact,
                model_type=str(context.get("model_type") or action),
                artifact_path=str(context.get("artifact_path") or ""),
                require_shadow=required_mode == "shadow",
            )
        except Exception as exc:
            permission = {
                "ok": False,
                "status": "blocked",
                "reason": "model_permission_check_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "capabilities": capabilities,
            }
        live_trading = bool(context.get("live_trading", False) or capabilities.get("live_trading", False))
        if live_trading:
            return RiskVerdict(
                allowed=False,
                reason="live_trading_capability_not_allowed",
                severity="error",
                required_mode=required_mode,
                audit_payload={
                    "action": action,
                    "source": "model_guardrail",
                    "candidate_id": context.get("candidate_id", ""),
                    "capabilities": capabilities,
                    "model_permission": permission,
                },
            )
        if not bool(permission.get("ok", False)):
            return RiskVerdict(
                allowed=False,
                reason=str(permission.get("reason") or "model_permission_violation"),
                severity="error",
                required_mode=required_mode,
                audit_payload={
                    "action": action,
                    "source": "model_permissions",
                    "candidate_id": context.get("candidate_id", ""),
                    "capabilities": capabilities,
                    "model_permission": permission,
                },
            )
        allowed_statuses = set(context.get("allowed_statuses") or [])
        candidate_status = str(context.get("candidate_status") or "")
        if allowed_statuses and candidate_status and candidate_status not in allowed_statuses:
            return RiskVerdict(
                allowed=False,
                reason="candidate_status_not_allowed",
                severity="error",
                required_mode=required_mode,
                audit_payload={
                    "action": action,
                    "source": "model_stage",
                    "candidate_id": context.get("candidate_id", ""),
                    "candidate_status": candidate_status,
                    "allowed_statuses": sorted(allowed_statuses),
                },
            )
        return RiskVerdict(
            allowed=True,
            reason="ok",
            required_mode=required_mode,
            audit_payload={
                "action": action,
                "source": "risk_policy",
                "candidate_id": context.get("candidate_id", ""),
                "candidate_status": candidate_status,
                "capabilities": capabilities,
                "model_permission": permission,
            },
        )

    def _evaluate_model_influence(self, context: dict[str, Any], *, promotion: bool) -> RiskVerdict:
        """Authorize a bounded model stage transition, never direct execution."""
        if not promotion:
            return RiskVerdict(
                allowed=True,
                reason="risk_reducing_model_demotion",
                required_mode="shadow",
                audit_payload={"action": "demote_model_influence", "source": "risk_policy"},
            )
        runtime = self._runtime_autonomy_context(context)
        if runtime.get("autonomy_mode") not in {"demo_nursery", "demo_autonomous"}:
            return RiskVerdict(
                allowed=False,
                reason="model_influence_demo_only",
                severity="error",
                required_mode="demo_canary",
                audit_payload={"runtime": runtime},
            )
        if str(runtime.get("runtime_incident_mode") or "normal") != "normal":
            return RiskVerdict(
                allowed=False,
                reason="incident_mode_blocks_model_promotion",
                severity="error",
                required_mode="demo_canary",
                audit_payload={"runtime": runtime},
            )
        if not bool(context.get("demo_model_influence_enabled")):
            return RiskVerdict(
                allowed=False,
                reason="demo_model_influence_not_unlocked",
                severity="error",
                required_mode="demo_canary",
                audit_payload={"runtime": runtime},
            )
        capabilities = dict(context.get("capabilities") or {})
        forbidden = [
            key for key in ("live_trading", "can_place_orders", "can_close_positions",
                            "can_change_risk_limits", "can_change_factor_weights", "can_bypass_risk_policy")
            if bool(capabilities.get(key))
        ]
        if forbidden:
            return RiskVerdict(
                allowed=False,
                reason="unsafe_model_influence_capability",
                severity="error",
                required_mode="demo_canary",
                audit_payload={"forbidden": forbidden, "capabilities": capabilities},
            )
        if not str(context.get("feature_schema_version") or "").startswith("pit.v2"):
            return RiskVerdict(
                allowed=False,
                reason="model_influence_requires_pit_v2",
                severity="error",
                required_mode="demo_canary",
                audit_payload={"feature_schema_version": context.get("feature_schema_version")},
            )
        if not bool(context.get("promotion_gate_passed")):
            return RiskVerdict(
                allowed=False,
                reason="model_promotion_evidence_not_ready",
                severity="error",
                required_mode="demo_canary",
                audit_payload={"gate": context.get("promotion_gate") or {}},
            )
        return RiskVerdict(
            allowed=True,
            reason="bounded_demo_model_influence",
            required_mode="demo_canary",
            audit_payload={
                "action": "promote_model_influence",
                "model_type": context.get("model_type"),
                "allowed_effects": context.get("allowed_effects") or [],
                "direct_execution": False,
            },
        )

    @staticmethod
    def _evaluate_low_impact_replay_job(context: dict[str, Any]) -> RiskVerdict:
        if bool(context.get("mutates_runtime", False)):
            return RiskVerdict(
                allowed=False,
                reason="replay_job_must_be_read_only",
                severity="error",
                required_mode="audit",
                audit_payload={
                    "action": "run_replay_job",
                    "source": "risk_policy",
                    "mutates_runtime": True,
                },
            )
        return RiskVerdict(
            allowed=True,
            reason="low_impact_read_only_replay",
            required_mode="audit",
            audit_payload={
                "action": "run_replay_job",
                "source": "risk_policy",
                "read_only": True,
                "plan_id": context.get("plan_id", ""),
                "eval_id": context.get("eval_id", ""),
                "evidence_score": context.get("evidence_score", 0.0),
                "critic_verdict": context.get("critic_verdict", ""),
                "comparison_verdict": context.get("comparison_verdict", ""),
            },
        )
