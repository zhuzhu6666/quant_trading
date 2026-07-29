"""Autonomous factor governance V3.

This orchestrator is the single decision loop for factor lifecycle actions.  It
does not write directional signals and it does not bypass DecisionPolicy for
weights.  Every mutation is gated by RiskPolicyService and recorded in the
evolution ledger plus learning/policy audit tables.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from alpha.decision_policy import DecisionPolicy
from alpha.portfolio_compositor import resolve_factor_role
from alpha.registry_adapter import RegistryAdapter
from backend.core.db import connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists
from backend.services.factor_catalog import build_factor_catalog, persist_factor_catalog_snapshot
from backend.services.factor_lifecycle_service import (
    FactorLifecycleService,
    FactorLifecycleStage,
    FactorV16Binding,
)
from backend.services.factor_redundancy import RedundancyDetector
from backend.services.agent_authority import AgentAuthorityRegistryService
from backend.services.factor_weight_change import FactorWeightChangeService
from backend.services.learning_experiment_admission import (
    LearningExperimentAdmissionService,
    STRUCTURAL_AUDIT_ACTIONS,
)
from backend.services.mutation_audit import record_api_mutation
from backend.services.policy_suggestion_context import attach_policy_suggestion_agent_context
from backend.services.runtime_config_mutation import RuntimeConfigMutationService
from backend.services.runtime_config_overlay import RuntimeConfigOverlayService
from config import runtime_config
from config.runtime_config import RuntimeConfig
from risk.policy_service import RiskPolicyService, RiskVerdict

logger = logging.getLogger(__name__)

_EVIDENCE_STREAK_KEY = "factor_governance_evidence_streak.v1"


@dataclass(frozen=True)
class FactorGovernanceProfile:
    name: str
    balanced_demo: bool
    min_live_weight: float
    builtin_activation_min_health_score: float
    builtin_activation_min_n_obs: int
    restore_cooldown_seconds: float
    restore_min_health_score: float
    restore_min_n_obs: int
    restore_model_min_samples: int
    restore_max_weakness: float
    hard_health_score: float
    hard_health_min_n_obs: int
    hard_model_min_samples: int
    hard_model_min_weak_samples: int
    hard_model_weakness: float
    hard_model_health_ceiling: float
    hard_disable_streak_cycles: int
    health_max_age_seconds: float


def _p(sql: str) -> str:
    return sql.replace("?", "%s")


def _json_default(value: Any) -> Any:
    """Normalize governance result objects before they enter JSON ledgers."""
    if hasattr(value, "to_api"):
        return value.to_api()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


class FactorGovernanceOrchestrator:
    """Conservative autonomous lifecycle and weight governance."""

    _instance: "FactorGovernanceOrchestrator | None" = None

    def __init__(self, risk_policy: RiskPolicyService | None = None):
        self.risk_policy = risk_policy or RiskPolicyService.shared()
        self.overlay = RuntimeConfigOverlayService()

    @classmethod
    def shared(cls) -> "FactorGovernanceOrchestrator":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def run_cycle(
        self,
        *,
        trigger_source: str = "scheduled",
        v16_handoff: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cfg = runtime_config.shared()
        profile = self._governance_profile(cfg)
        if not bool(getattr(cfg, "factor_governance_enabled", True)):
            return {"status": "disabled", "actions": []}

        from backend.services.evolution_ledger import (
            finish_evolution_run,
            start_evolution_run,
        )

        run = start_evolution_run(
            run_type="factor_governance_autonomous",
            trigger_source=trigger_source,
            config=cfg,
            summary={"version": "factor_governance.v3"},
        )
        actions: list[dict[str, Any]] = []
        status = "completed"
        try:
            catalog = build_factor_catalog()
            actions.extend(self._rollback_failed_actions(run))
            catalog_snapshot = persist_factor_catalog_snapshot(
                catalog,
                run_id=str(run.get("run_id") or ""),
                source="factor_governance_cycle",
            )
            actions.extend(self._rollback_canary_regressions(catalog, run))
            if actions:
                catalog = build_factor_catalog()
            # Tightening is always evaluated before any expansion posture or
            # V16 authorization gate. A freeze/missing delegate may stop
            # promotion, restore and template expansion, but must never defer
            # downweight, quarantine or terminal retirement.
            actions.extend(
                self._downweight_weak_alpha(catalog, run, cfg=cfg, profile=profile)
            )
            if actions:
                catalog = build_factor_catalog()
            actions.extend(
                self._disable_weak_live_alpha(catalog, run, cfg=cfg, profile=profile)
            )
            if actions:
                catalog = build_factor_catalog()
            actions.extend(self._retire_quarantined_discovered(catalog, run))
            if actions:
                catalog = build_factor_catalog()
            posture = self._autonomy_posture()
            expansion_frozen = runtime_config.autonomy_expansion_freeze_applies(cfg)
            if posture in {"shadow_only", "frozen"} or expansion_frozen:
                summary = {
                    "status": "observation_only",
                    "reason": "autonomy_expansion_frozen" if expansion_frozen else f"autonomy_posture:{posture}",
                    "catalog_count": len(catalog),
                    "actions": actions,
                    "catalog_snapshot": catalog_snapshot,
                    "redundancy_report": {},
                }
                finish_evolution_run(run["run_id"], status=status, summary=summary)
                return summary
            cfg = runtime_config.shared()
            redundancy_report = RedundancyDetector().build_report(
                catalog,
                min_samples=int(
                    getattr(cfg, "factor_redundancy_min_samples", 200) or 200
                ),
                corr_threshold=float(
                    getattr(cfg, "factor_redundancy_corr_threshold", 0.85)
                    or 0.85
                ),
            )
            # Template recommendations are evidence handoffs only. Their
            # specialist owns any mutation and its own V16 authorization.
            actions.extend(self._apply_parameter_template_actions(catalog, run))
            expansion_preflight = self._expansion_preflight(
                catalog,
                cfg=cfg,
                profile=profile,
                redundancy_report=redundancy_report,
            )
            if not expansion_preflight["required"]:
                summary = {
                    "status": "idle_no_expansion_action",
                    "reason": "no_factor_expansion_actionable",
                    "catalog_count": len(catalog),
                    "actions": actions,
                    "catalog_snapshot": catalog_snapshot,
                    "redundancy_report": redundancy_report,
                    "expansion_preflight": expansion_preflight,
                    "governance_profile": profile.name,
                }
                finish_evolution_run(
                    run["run_id"],
                    status=status,
                    summary=summary,
                )
                return summary
            v16_authority: dict[str, Any] = {}
            if is_state_db_path(self.overlay.db_path):
                v16_delegation: dict[str, Any] = {}
                if v16_handoff:
                    from backend.services.v16_brain_orchestrator import (
                        V16BrainOrchestratorService,
                    )

                    v16_delegation = (
                        V16BrainOrchestratorService(
                            db_path=self.overlay.db_path
                        ).delegate_factor_governance_cycle(
                            {
                                "snapshot_id": str(
                                    v16_handoff.get("snapshot_id") or ""
                                ),
                                "health_cycle_id": str(
                                    v16_handoff.get("health_cycle_id") or ""
                                ),
                                "posterior_fingerprint": str(
                                    v16_handoff.get(
                                        "posterior_fingerprint"
                                    )
                                    or ""
                                ),
                                "expansion_preflight": expansion_preflight,
                            }
                        )
                    )
                from backend.services.v16_command_gate import V16CommandGate

                v16_authority = V16CommandGate.authorize(
                    self.overlay.db_path,
                    target_agent="factor_governance",
                    scope_type="factor_weight",
                    scope_key="alpha_weight_policy",
                    action="factor_governance_cycle",
                )
                if not v16_authority.get("allowed"):
                    summary = {
                        "status": "waiting_v16_command",
                        "reason": "factor_governance_mutation_requires_current_v16_delegate",
                        "catalog_count": len(catalog),
                        "actions": actions,
                        "catalog_snapshot": catalog_snapshot,
                        "redundancy_report": redundancy_report,
                        "expansion_preflight": expansion_preflight,
                        "v16_delegation": v16_delegation,
                        "v16_authority": v16_authority,
                    }
                    finish_evolution_run(
                        run["run_id"],
                        status="blocked_by_v16_command",
                        summary=summary,
                    )
                    return summary
            expansion_actions = self._restore_quarantined_builtin_alpha(
                    catalog,
                    run,
                    cfg=cfg,
                    profile=profile,
                    v16_authority=v16_authority,
                )
            actions.extend(expansion_actions)
            expansion_committed = self._expansion_command_consumed(
                expansion_actions
            )
            if expansion_actions:
                catalog = build_factor_catalog()
            if not expansion_committed:
                expansion_actions = self._activate_healthy_builtin_shadow(
                    catalog,
                    run,
                    v16_authority=v16_authority,
                    cfg=cfg,
                    profile=profile,
                )
                actions.extend(expansion_actions)
                expansion_committed = self._expansion_command_consumed(
                    expansion_actions
                )
                if expansion_actions:
                    catalog = build_factor_catalog()
            if not expansion_committed:
                expansion_actions = self._apply_redundancy_report(
                    catalog,
                    redundancy_report,
                    run,
                )
                actions.extend(expansion_actions)
                expansion_committed = self._expansion_command_consumed(
                    expansion_actions
                )
                if expansion_actions:
                    catalog = build_factor_catalog()
            if not expansion_committed:
                expansion_actions = self._promote_shadow_candidates(
                    catalog,
                    run,
                    v16_authority=v16_authority,
                )
                actions.extend(expansion_actions)
                expansion_committed = self._expansion_command_consumed(
                    expansion_actions
                )
            if v16_delegation and not expansion_committed:
                from backend.services.v16_brain_orchestrator import (
                    V16BrainOrchestratorService,
                )

                command = dict(v16_delegation.get("command") or {})
                V16BrainOrchestratorService(
                    db_path=self.overlay.db_path
                ).cancel_factor_governance_delegation(
                    command_id=str(command.get("command_id") or ""),
                    reason="factor_governance_cycle_no_committed_action",
                )
            catalog = build_factor_catalog()
            summary = {
                "status": "completed_with_errors" if any(
                    str(item.get("status") or "") in {"failed", "error", "mutation_failed"}
                    for item in actions
                ) else "ok",
                "catalog_count": len(catalog),
                "actions": actions,
                "catalog_snapshot": catalog_snapshot,
                "redundancy_report": redundancy_report,
                "expansion_preflight": expansion_preflight,
                "v16_delegation": v16_delegation,
                "v16_authority": v16_authority,
                "governance_profile": profile.name,
            }
            finish_evolution_run(
                run["run_id"],
                status="completed_with_errors" if summary["status"] == "completed_with_errors" else status,
                summary=summary,
            )
            return summary
        except Exception as exc:
            status = "failed"
            logger.exception("[factor_governance] cycle failed")
            finish_evolution_run(
                run["run_id"],
                status=status,
                summary={"status": status, "error": str(exc), "actions": actions},
            )
            return {"status": status, "error": str(exc), "actions": actions}

    # ── Action selection ────────────────────────────────────────────

    @staticmethod
    def _expansion_command_consumed(
        actions: list[dict[str, Any]],
    ) -> bool:
        return any(
            str(item.get("status") or "")
            in {
                "applied",
                "promotion_prepared",
                "shadow_registered",
                "projection_degraded",
            }
            for item in actions
        )

    @staticmethod
    def _governance_profile(cfg: Any) -> FactorGovernanceProfile:
        balanced_demo = runtime_config.bounded_demo_mode_active(cfg)
        if balanced_demo:
            return FactorGovernanceProfile(
                name="balanced_demo",
                balanced_demo=True,
                min_live_weight=float(
                    getattr(cfg, "factor_governance_demo_min_live_weight", 0.05)
                    or 0.05
                ),
                builtin_activation_min_health_score=float(
                    getattr(
                        cfg,
                        "factor_governance_demo_builtin_activation_min_health_score",
                        60.0,
                    )
                    or 60.0
                ),
                builtin_activation_min_n_obs=int(
                    getattr(
                        cfg,
                        "factor_governance_builtin_activation_min_n_obs",
                        500,
                    )
                    or 500
                ),
                restore_cooldown_seconds=3600.0
                * float(
                    getattr(
                        cfg,
                        "factor_governance_demo_restore_cooldown_hours",
                        24.0,
                    )
                    or 24.0
                ),
                restore_min_health_score=float(
                    getattr(cfg, "factor_governance_restore_health_threshold", 60.0)
                    or 60.0
                ),
                restore_min_n_obs=int(
                    getattr(
                        cfg,
                        "factor_governance_demo_restore_min_n_obs",
                        500,
                    )
                    or 500
                ),
                restore_model_min_samples=int(
                    getattr(
                        cfg,
                        "factor_governance_demo_restore_model_min_samples",
                        10,
                    )
                    or 10
                ),
                restore_max_weakness=float(
                    getattr(cfg, "factor_governance_restore_max_weakness", 0.65)
                    or 0.65
                ),
                hard_health_score=float(
                    getattr(
                        cfg,
                        "factor_governance_demo_health_disable_score",
                        20.0,
                    )
                    or 20.0
                ),
                hard_health_min_n_obs=int(
                    getattr(
                        cfg,
                        "factor_governance_demo_health_disable_min_n_obs",
                        500,
                    )
                    or 500
                ),
                hard_model_min_samples=int(
                    getattr(
                        cfg,
                        "factor_governance_demo_model_disable_min_samples",
                        30,
                    )
                    or 30
                ),
                hard_model_min_weak_samples=int(
                    getattr(
                        cfg,
                        "factor_governance_demo_model_disable_min_weak_samples",
                        15,
                    )
                    or 15
                ),
                hard_model_weakness=float(
                    getattr(
                        cfg,
                        "factor_governance_demo_model_disable_threshold",
                        0.90,
                    )
                    or 0.90
                ),
                hard_model_health_ceiling=float(
                    getattr(
                        cfg,
                        "factor_governance_demo_disable_health_ceiling",
                        40.0,
                    )
                    or 40.0
                ),
                hard_disable_streak_cycles=int(
                    getattr(
                        cfg,
                        "factor_governance_demo_disable_streak_cycles",
                        3,
                    )
                    or 3
                ),
                health_max_age_seconds=float(
                    getattr(
                        cfg,
                        "factor_governance_demo_health_max_age_seconds",
                        300.0,
                    )
                    or 300.0
                ),
            )
        return FactorGovernanceProfile(
            name="strict_live",
            balanced_demo=False,
            min_live_weight=0.0,
            builtin_activation_min_health_score=float(
                getattr(
                    cfg,
                    "factor_governance_builtin_activation_min_health_score",
                    70.0,
                )
                or 70.0
            ),
            builtin_activation_min_n_obs=int(
                getattr(
                    cfg,
                    "factor_governance_builtin_activation_min_n_obs",
                    500,
                )
                or 500
            ),
            restore_cooldown_seconds=86400.0
            * float(
                getattr(cfg, "factor_governance_restore_cooldown_days", 7)
                or 0.0
            ),
            restore_min_health_score=float(
                getattr(cfg, "factor_governance_restore_health_threshold", 60.0)
                or 60.0
            ),
            restore_min_n_obs=int(
                getattr(cfg, "factor_health_min_n_obs", 100) or 100
            ),
            restore_model_min_samples=int(
                getattr(cfg, "factor_governance_model_min_samples", 3) or 3
            ),
            restore_max_weakness=float(
                getattr(cfg, "factor_governance_restore_max_weakness", 0.65)
                or 0.65
            ),
            hard_health_score=float(
                getattr(cfg, "retire_severe_threshold", 30.0) or 30.0
            ),
            hard_health_min_n_obs=1,
            hard_model_min_samples=int(
                getattr(cfg, "factor_governance_model_min_samples", 3) or 3
            ),
            hard_model_min_weak_samples=int(
                getattr(cfg, "factor_governance_model_min_samples", 3) or 3
            ),
            hard_model_weakness=float(
                getattr(cfg, "factor_governance_model_disable_threshold", 0.85)
                or 0.85
            ),
            hard_model_health_ceiling=100.0,
            hard_disable_streak_cycles=1,
            health_max_age_seconds=180.0,
        )

    @staticmethod
    def _autonomy_posture() -> str:
        try:
            from backend.services.autonomy_health import AutonomyHealthService

            return str(AutonomyHealthService().latest_snapshot().get("posture") or "unknown")
        except Exception:
            return "unknown"

    def _expansion_preflight(
        self,
        catalog: list[dict[str, Any]],
        *,
        cfg: Any,
        profile: FactorGovernanceProfile,
        redundancy_report: dict[str, Any],
    ) -> dict[str, Any]:
        """Find concrete expansion work before claiming a V16 command."""
        now = time.time()
        signal_cfg = dict(getattr(cfg, "factor_signal_config", {}) or {})
        weights = dict(getattr(cfg, "factor_portfolio_weights", {}) or {})
        activation_ids: list[str] = []
        restore_ids: list[str] = []
        promotion_ids: list[str] = []

        activation_enabled = bool(
            getattr(cfg, "factor_governance_builtin_activation_enabled", True)
        )
        activation_weight = min(
            0.50,
            float(
                getattr(
                    cfg,
                    "factor_governance_builtin_activation_weight",
                    0.0,
                )
                or 0.0
            ),
        )
        if profile.balanced_demo:
            activation_weight = max(profile.min_live_weight, activation_weight)
        if activation_enabled and activation_weight > 0.0:
            for item in catalog:
                factor_id = str(item.get("factor_id") or "")
                entry = signal_cfg.get(factor_id)
                if (
                    not isinstance(entry, dict)
                    or item.get("source") != "builtin"
                    or item.get("role") != "alpha"
                    or str(item.get("lifecycle_status") or "").upper()
                    not in {
                        FactorLifecycleStage.SHADOW.value,
                        FactorLifecycleStage.PROMOTION_PREPARED.value,
                    }
                    or not bool(entry.get("autonomous_activation"))
                    or not bool(item.get("enabled"))
                ):
                    continue
                health_age = now - float(item.get("health_updated_at") or 0.0)
                if (
                    float(item.get("health_score") or 0.0)
                    < profile.builtin_activation_min_health_score
                    or int(item.get("health_n_obs") or 0)
                    < profile.builtin_activation_min_n_obs
                    or str(item.get("health_status") or "").upper()
                    not in {"HEALTHY", "WATCH"}
                    or health_age < -5.0
                    or health_age > profile.health_max_age_seconds
                    or self._factor_has_pending_effect(factor_id)
                ):
                    continue
                model = self._model_governance_evidence(item, cfg)
                if (
                    int(model.get("sample_count") or 0)
                    or int(model.get("weak_sample_count") or 0)
                ) and float(model.get("avg_weakness_score") or 0.0) >= float(
                    getattr(
                        cfg,
                        "factor_governance_builtin_activation_max_weakness",
                        0.65,
                    )
                    or 0.65
                ):
                    continue
                activation_ids.append(factor_id)

        from backend.services.governance_control_plans import (
            governance_coordinator_mode,
        )

        mode = governance_coordinator_mode()
        restore_enabled = bool(
            getattr(cfg, "factor_governance_auto_restore_enabled", True)
        )
        if restore_enabled and (mode == "off" or profile.balanced_demo):
            for item in catalog:
                factor_id = str(item.get("factor_id") or "")
                entry = signal_cfg.get(factor_id)
                if (
                    not factor_id
                    or item.get("source") != "builtin"
                    or item.get("role") != "alpha"
                    or not isinstance(entry, dict)
                    or entry.get("enabled", True) is not False
                    or str(entry.get("lifecycle_status") or "").upper()
                    not in {"QUARANTINE", "QUARANTINED"}
                    or item.get("governance_action") != "disable_factor_live"
                ):
                    continue
                disabled_at = float(
                    entry.get("disabled_at")
                    or item.get("last_action_ts")
                    or 0.0
                )
                health_updated_at = float(item.get("health_updated_at") or 0.0)
                health_age = now - health_updated_at
                if (
                    disabled_at <= 0.0
                    or now - disabled_at < profile.restore_cooldown_seconds
                    or health_updated_at <= disabled_at
                    or health_age < -5.0
                    or health_age > profile.health_max_age_seconds
                    or str(item.get("health_status") or "").upper()
                    not in {"HEALTHY", "WATCH"}
                    or float(item.get("health_score") or 0.0)
                    < profile.restore_min_health_score
                    or int(item.get("health_n_obs") or 0)
                    < profile.restore_min_n_obs
                    or (mode != "off" and self._factor_has_pending_effect(factor_id))
                ):
                    continue
                model = self._model_governance_evidence(item, cfg)
                has_model_evidence = (
                    int(model.get("sample_count") or 0)
                    >= profile.restore_model_min_samples
                    or int(model.get("weak_sample_count") or 0)
                    >= profile.restore_model_min_samples
                )
                observed_weakness = max(
                    float(model.get("avg_weakness_score") or 0.0),
                    float(model.get("latest_weakness_score") or 0.0),
                )
                if (
                    has_model_evidence
                    and observed_weakness >= profile.restore_max_weakness
                ):
                    continue
                restore_ids.append(factor_id)

        for item in catalog:
            factor_id = str(item.get("factor_id") or "")
            if (
                item.get("source") != "shadow"
                or not self._has_durable_shadow_lifecycle_identity(item)
                or not self._promotion_evidence(item, cfg).get("eligible")
                or self._factor_has_pending_effect(factor_id)
            ):
                continue
            promotion_ids.append(factor_id)

        reasons = {
            "builtin_activation": activation_ids,
            "builtin_restore": restore_ids,
            "shadow_promotion": promotion_ids,
            "redundancy_groups": int(
                redundancy_report.get("group_count") or 0
            ),
        }
        return {
            "required": bool(
                activation_ids
                or restore_ids
                or promotion_ids
                or reasons["redundancy_groups"]
            ),
            "reasons": reasons,
            "candidate_count": (
                len(activation_ids)
                + len(restore_ids)
                + len(promotion_ids)
                + int(reasons["redundancy_groups"])
            ),
            "current_positive_weights": sum(
                1
                for value in weights.values()
                if (
                    float(value.get("weight") or 0.0)
                    if isinstance(value, dict)
                    else float(value or 0.0)
                )
                > 0.0
            ),
        }

    def _factor_has_pending_effect(self, factor_id: str) -> bool:
        if not factor_id:
            return False
        db_path = self.overlay.db_path
        production_state = is_state_db_path(db_path)
        if not production_state and not Path(db_path).exists():
            return False
        try:
            conn = (
                get_state_pg_conn(read_only=True)
                if production_state
                else connect_sqlite(db_path, read_only=True)
            )
            if not production_state:
                conn.row_factory = sqlite3.Row
            try:
                if not state_table_exists(conn, "learning_application_log"):
                    return False
                row = conn.execute(
                    _p("""
                    SELECT l.status AS application_status, e.status AS effect_status
                    FROM learning_application_log l
                    LEFT JOIN learning_application_effect e ON e.application_id=l.application_id
                    WHERE l.scope_type='factor' AND l.scope_key=?
                    ORDER BY l.cycle_ts DESC, l.created_at DESC
                    LIMIT 1
                    """) if production_state else """
                    SELECT l.status AS application_status, e.status AS effect_status
                    FROM learning_application_log l
                    LEFT JOIN learning_application_effect e ON e.application_id=l.application_id
                    WHERE l.scope_type='factor' AND l.scope_key=?
                    ORDER BY l.cycle_ts DESC, l.created_at DESC
                    LIMIT 1
                    """,
                    (factor_id,),
                ).fetchone()
                return LearningExperimentAdmissionService.row_is_active(row)
            finally:
                conn.close()
        except Exception:
            # Production state uncertainty must block another mutation.  An
            # isolated test/research store has no live authority and may treat
            # a missing ledger as no pending experiment.
            return bool(production_state)

    def _advance_disable_evidence_streaks(
        self,
        candidates: dict[str, dict[str, Any]],
        *,
        now: float,
    ) -> dict[str, dict[str, Any]]:
        """Persist consecutive Demo hard-disable evidence; uncertainty resets it."""

        db_path = self.overlay.db_path
        production_state = is_state_db_path(db_path)
        conn = None
        try:
            conn = (
                get_state_pg_conn()
                if production_state
                else connect_sqlite(db_path)
            )
            if not production_state:
                conn.row_factory = sqlite3.Row
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS runtime_kv (
                       key TEXT PRIMARY KEY,
                       value_json TEXT NOT NULL DEFAULT '{}',
                       updated_at REAL NOT NULL DEFAULT 0.0
                    )"""
                )
            row = conn.execute(
                _p("SELECT value_json FROM runtime_kv WHERE key=?")
                if production_state
                else "SELECT value_json FROM runtime_kv WHERE key=?",
                (_EVIDENCE_STREAK_KEY,),
            ).fetchone()
            previous = _loads(row["value_json"], {}) if row else {}
            previous_factors = dict(previous.get("factors") or {})
            factors: dict[str, dict[str, Any]] = {}
            for factor_id, evidence in sorted(candidates.items()):
                previous_item = dict(previous_factors.get(factor_id) or {})
                same_reason = (
                    str(previous_item.get("reason") or "")
                    == str(evidence.get("reason") or "")
                )
                same_evidence_cycle = (
                    same_reason
                    and str(previous_item.get("evidence_cycle_id") or "")
                    == str(evidence.get("evidence_cycle_id") or "")
                )
                factors[factor_id] = {
                    **dict(evidence),
                    "streak": (
                        int(previous_item.get("streak") or 0)
                        if same_evidence_cycle
                        else int(previous_item.get("streak") or 0) + 1
                        if same_reason
                        else 1
                    ),
                    "last_new_evidence_at": (
                        float(previous_item.get("last_new_evidence_at") or now)
                        if same_evidence_cycle
                        else now
                    ),
                    "updated_at": now,
                }
            payload = {
                "schema_version": _EVIDENCE_STREAK_KEY,
                "factors": factors,
                "updated_at": now,
            }
            conn.execute(
                _p(
                    """INSERT INTO runtime_kv (key, value_json, updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                         value_json=excluded.value_json,
                         updated_at=excluded.updated_at"""
                )
                if production_state
                else """INSERT INTO runtime_kv (key, value_json, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                          value_json=excluded.value_json,
                          updated_at=excluded.updated_at""",
                (_EVIDENCE_STREAK_KEY, _dumps(payload), now),
            )
            conn.commit()
            return factors
        except Exception:
            logger.exception(
                "[factor_governance] evidence streak persistence unavailable"
            )
            # Never convert an unpersisted observation into a hard quarantine.
            return {}
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _scoped_factor_rollback_patch(
        factor_id: str,
        rollback_cfg: dict[str, Any],
        current_cfg: dict[str, Any],
    ) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        if "factor_signal_config" in rollback_cfg:
            current_signal = dict(current_cfg.get("factor_signal_config") or {})
            rollback_signal = dict(rollback_cfg.get("factor_signal_config") or {})
            if factor_id in rollback_signal:
                factor_signal = dict(rollback_signal[factor_id] or {})
            else:
                # Overlay patches are merge-only.  A factor absent from the
                # rollback snapshot is represented as explicitly disabled.
                factor_signal = dict(current_signal.get(factor_id, {}) or {})
                factor_signal["enabled"] = False
                factor_signal.setdefault("lifecycle_status", "QUARANTINE")
            patch["factor_signal_config"] = {factor_id: factor_signal}
        if "factor_portfolio_weights" in rollback_cfg:
            rollback_weights = dict(rollback_cfg.get("factor_portfolio_weights") or {})
            patch["factor_portfolio_weights"] = {
                factor_id: float(rollback_weights.get(factor_id, 0.0) or 0.0)
            }
        return patch

    def _apply_runtime_patch(self, patch: dict[str, Any], *, source: str, run_id: str) -> dict[str, Any]:
        factor_keys = list((patch.get("factor_signal_config") or {}).keys())
        risk_reduction = any(
            token in str(source or "").lower()
            for token in ("rollback", "disable", "downweight", "retire", "quarantine")
        )
        return RuntimeConfigMutationService(overlay=self.overlay).apply_patch(
            patch,
            source=source,
            run_id=run_id,
            actor="system:factor_governance",
            action=source,
            audit=False,
            require_v16_command=is_state_db_path(self.overlay.db_path),
            v16_target_agent="factor_governance",
            v16_scope_type="factor_weight",
            v16_scope_key=str(factor_keys[0]) if len(factor_keys) == 1 else "alpha_weight_policy",
            v16_action=source,
            risk_reduction=risk_reduction,
        )

    @staticmethod
    def _mutation_commit_state(result: dict[str, Any] | None) -> tuple[bool, bool, str]:
        """Return durable-commit, projection-ready and normalized status.

        A coordinator transaction can be committed while its in-process
        projection is degraded.  Callers must not report a blocked mutation
        as applied, and must not confuse durable commit with live projection.
        """
        payload = dict(result or {})
        status = str(payload.get("status") or "")
        committed = bool(payload.get("ok")) or status in {
            "applied",
            "committed",
            "committed_projection_degraded",
        }
        projection_ready = bool(payload.get("ok")) and status != "committed_projection_degraded"
        return committed, projection_ready, status or "mutation_blocked"

    def _rollback_failed_actions(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        cfg = runtime_config.shared()
        min_trades = int(getattr(cfg, "factor_governance_rollback_min_trades", 3) or 3)
        delta_threshold = float(getattr(cfg, "factor_governance_rollback_delta_threshold", -0.15) or -0.15)
        conn = get_state_pg_conn()
        try:
            rows = conn.execute(
                _p("""
                SELECT l.application_id, l.scope_key, l.action, l.suggestion_ids_json,
                       l.details_json, e.observed_trade_count, e.delta_avg_reward
                FROM learning_application_log l
                JOIN learning_application_effect e ON e.application_id = l.application_id
                WHERE l.scope_type='factor'
                  AND l.status IN ('applied','observing','ineffective')
                  AND e.status IN ('observing','applied','ineffective')
                  AND e.observed_trade_count >= ?
                  AND e.delta_avg_reward <= ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM learning_application_log newer
                      WHERE newer.scope_type='factor'
                        AND newer.scope_key=l.scope_key
                        AND newer.created_at > l.created_at
                        AND newer.status NOT IN ('rolled_back','superseded')
                  )
                ORDER BY e.updated_at DESC, l.created_at DESC
                LIMIT 5
                """),
                (min_trades, delta_threshold),
            ).fetchall()
            for row in rows:
                factor_id = str(row["scope_key"] or "")
                decision_id = self._decision_id_from_application(row)
                rollback_payload = self._rollback_payload_for_decision(decision_id)
                rollback_cfg = rollback_payload.get("runtime_config") if isinstance(rollback_payload, dict) else None
                item = {"factor_id": factor_id, "role": "alpha", "source": "autonomous"}
                evidence = {
                    "application_id": str(row["application_id"] or ""),
                    "decision_id": decision_id,
                    "observed_trade_count": int(row["observed_trade_count"] or 0),
                    "delta_avg_reward": float(row["delta_avg_reward"] or 0.0),
                    "action": str(row["action"] or ""),
                }
                verdict = self._risk("rollback_factor_action", item, evidence)
                if not verdict.allowed or not isinstance(rollback_cfg, dict):
                    actions.append(self._audit_action(
                        run,
                        item,
                        "rollback_factor_action",
                        "blocked_by_risk" if not verdict.allowed else "superseded",
                        evidence,
                        verdict,
                        result={"reason": "risk_blocked" if not verdict.allowed else "missing_rollback_config"},
                    ))
                    continue
                before_cfg = runtime_config.shared().to_dict()
                rollback_patch = self._scoped_factor_rollback_patch(factor_id, rollback_cfg, before_cfg)
                if not rollback_patch:
                    actions.append(self._audit_action(
                        run,
                        item,
                        "rollback_factor_action",
                        "superseded",
                        evidence,
                        verdict,
                        result={"reason": "missing_factor_scoped_rollback_config"},
                    ))
                    continue
                rollback_run_id = str(run.get("run_id") or "")
                rollback_committed = True
                rollback_projection_ready = True
                rollback_mutation: dict[str, Any] = {
                    "status": "no_runtime_change_required"
                }
                if "factor_portfolio_weights" in rollback_patch:
                    current_weights = dict(before_cfg.get("factor_portfolio_weights") or {})
                    rollback_weights = dict(rollback_patch.get("factor_portfolio_weights") or {})
                    target_weight = float(rollback_weights.get(factor_id, 0.0) or 0.0)
                    signal_patch = dict(rollback_patch.get("factor_signal_config") or {})
                    factor_configs = self._portfolio_configs(
                        runtime_config.shared(),
                        signal_cfg=signal_patch or dict(before_cfg.get("factor_signal_config") or {}),
                    )
                    weight_result = FactorWeightChangeService(self.overlay.db_path).execute(
                        source="factor_governance_auto_rollback",
                        producer="factor_governance_posterior_rollback",
                        run_id=rollback_run_id,
                        actor="system:factor_governance",
                        reason=f"posterior rollback for {factor_id}",
                        factor_configs=factor_configs,
                        current_weights=current_weights,
                        weight_policy_weights={factor_id: target_weight},
                        fast=True,
                        bypass_for_risk_reduction=True,
                        risk_check=lambda _plan, _verdict=verdict: _verdict,
                        evidence_by_factor={factor_id: evidence},
                        suggestion_ids_by_factor={
                            factor_id: [
                                *self._loads_list(row["suggestion_ids_json"]),
                                f"{rollback_run_id}:rollback:{factor_id}",
                            ]
                        },
                        source_agent="factor_governance",
                        additional_patch=(
                            {"factor_signal_config": signal_patch} if signal_patch else None
                        ),
                    )
                    if weight_result.get("status") not in {"applied", "no_admitted_change"}:
                        actions.append(self._audit_action(
                            run,
                            item,
                            "rollback_factor_action",
                            "blocked_by_risk",
                            evidence,
                            verdict,
                            result={"reason": weight_result.get("status"), "weight_result": weight_result},
                        ))
                        continue
                    rollback_mutation = dict(weight_result)
                    rollback_projection_ready = bool(
                        weight_result.get("projection_ready", True)
                    )
                    if weight_result.get("status") == "no_admitted_change" and signal_patch:
                        rollback_mutation = self._apply_runtime_patch(
                            {"factor_signal_config": signal_patch},
                            source="factor_governance_auto_rollback_config_only",
                            run_id=rollback_run_id,
                        )
                        (
                            rollback_committed,
                            rollback_projection_ready,
                            _rollback_status,
                        ) = self._mutation_commit_state(rollback_mutation)
                else:
                    rollback_mutation = self._apply_runtime_patch(
                        rollback_patch,
                        source="factor_governance_auto_rollback_config_only",
                        run_id=rollback_run_id,
                    )
                    (
                        rollback_committed,
                        rollback_projection_ready,
                        _rollback_status,
                    ) = self._mutation_commit_state(rollback_mutation)
                if not rollback_committed:
                    actions.append(self._audit_action(
                        run,
                        item,
                        "rollback_factor_action",
                        "blocked_by_evidence",
                        evidence,
                        verdict,
                        before={"runtime_config": before_cfg},
                        after={"runtime_config": runtime_config.shared().to_dict()},
                        rollback=rollback_payload,
                        result={
                            "reason": str(
                                rollback_mutation.get("status")
                                or "rollback_mutation_not_committed"
                            ),
                            "mutation": rollback_mutation,
                        },
                    ))
                    continue
                self._mark_application_rolled_back(
                    application_id=str(row["application_id"] or ""),
                    suggestion_ids=self._loads_list(row["suggestion_ids_json"]),
                    decision={"decision_id": decision_id, **evidence},
                )
                actions.append(self._audit_action(
                    run,
                    item,
                    "rollback_factor_action",
                    "rolled_back" if rollback_projection_ready else "projection_degraded",
                    evidence,
                    verdict,
                    before={"runtime_config": before_cfg},
                    after={"runtime_config": runtime_config.shared().to_dict()},
                    rollback=rollback_payload,
                    result={
                        "restored": rollback_projection_ready,
                        "durably_committed": True,
                        "projection_ready": rollback_projection_ready,
                        "mutation": rollback_mutation,
                    },
                ))
        except Exception as exc:
            logger.debug("[factor_governance] rollback scan skipped: %s", exc)
        finally:
            conn.close()
        return actions

    def _decision_id_from_application(self, row: Any) -> str:
        details = self._loads_dict(row["details_json"])
        decision_id = str(details.get("decision_id") or "")
        if decision_id:
            return decision_id
        suggestion_ids = self._loads_list(row["suggestion_ids_json"])
        if not suggestion_ids:
            return ""
        conn = get_state_pg_conn(read_only=True)
        try:
            record = conn.execute(
                _p("SELECT evidence_json FROM policy_suggestion WHERE suggestion_id=? LIMIT 1"),
                (suggestion_ids[0],),
            ).fetchone()
            evidence = self._loads_dict(record["evidence_json"] if record else "{}")
            return str(evidence.get("decision_id") or "")
        finally:
            conn.close()

    def _rollback_payload_for_decision(self, decision_id: str) -> dict[str, Any]:
        if not decision_id:
            return {}
        conn = get_state_pg_conn(read_only=True)
        try:
            row = conn.execute(
                _p("SELECT rollback_json FROM evolution_decision WHERE decision_id=? LIMIT 1"),
                (decision_id,),
            ).fetchone()
            return self._loads_dict(row["rollback_json"] if row else "{}")
        finally:
            conn.close()

    def _mark_application_rolled_back(self, *, application_id: str, suggestion_ids: list[str], decision: dict[str, Any]) -> None:
        now = time.time()
        conn = get_state_pg_conn()
        try:
            conn.execute(
                _p("UPDATE learning_application_log SET status='rolled_back' WHERE application_id=?"),
                (application_id,),
            )
            conn.execute(
                _p("""
                UPDATE learning_application_effect
                SET status='rolled_back', decision_json=?, updated_at=?
                WHERE application_id=?
                """),
                (_dumps(decision), now, application_id),
            )
            for suggestion_id in suggestion_ids:
                conn.execute(
                    _p("""
                    UPDATE policy_suggestion
                    SET status='rolled_back', reviewed_at=?, review_note=?
                    WHERE suggestion_id=?
                    """),
                    (now, "auto rollback by factor governance posterior effect", suggestion_id),
                )
            conn.commit()
        finally:
            conn.close()

    def _apply_redundancy_report(
        self,
        catalog: list[dict[str, Any]],
        report: dict[str, Any],
        run: dict[str, Any],
    ) -> list[dict[str, Any]]:
        groups = list(report.get("groups") or [])
        if not groups:
            return []
        before_cfg = runtime_config.shared().to_dict()
        signal_cfg = dict(runtime_config.shared().factor_signal_config or {})
        signal_patch: dict[str, dict[str, Any]] = {}
        for group in groups:
            group_id = str(group.get("group_id") or "")
            leader = str(group.get("leader") or "")
            for member in group.get("members") or []:
                entry = dict(signal_cfg.get(member, {}) or {})
                entry["redundancy_group"] = group_id
                entry["redundancy_leader"] = leader
                signal_cfg[member] = entry
                signal_patch[member] = entry
        result = self._apply_runtime_patch(
            {"factor_signal_config": signal_patch},
            source="factor_governance_redundancy",
            run_id=str(run.get("run_id") or ""),
        )
        committed, projection_ready, mutation_status = self._mutation_commit_state(result)
        item = {"factor_id": "redundancy", "role": "alpha", "source": "catalog"}
        verdict = RiskVerdict(allowed=True, reason="ok", audit_payload={"action": "update_weight"})
        return [self._audit_action(
            run,
            item,
            "update_redundancy_groups",
            (
                "applied"
                if projection_ready
                else "projection_degraded"
                if committed
                else "blocked_by_evidence"
            ),
            report,
            verdict,
            before={"runtime_config": before_cfg},
            after={"runtime_config": runtime_config.shared().to_dict()},
            rollback={"runtime_config": before_cfg},
            result={
                **dict(result or {}),
                "mutation_status": mutation_status,
                "durably_committed": committed,
                "projection_ready": projection_ready,
            },
        )]

    def _apply_parameter_template_actions(
        self,
        catalog: list[dict[str, Any]],
        run: dict[str, Any],
        *,
        v16_command_id: str = "",
    ) -> list[dict[str, Any]]:
        """Publish template evidence and hand execution to autonomous learning.

        Factor governance keeps its factor lifecycle/weight responsibility;
        parameter-template activation is owned by ``autonomous_learning``.
        Keeping this read path here is intentional: factor evidence can still
        surface a template mismatch, but two agents must not race to activate
        the same template or write the same runtime overlay.
        """
        actions: list[dict[str, Any]] = []
        try:
            from backend.services.parameter_templates import ParameterTemplateService

            service = ParameterTemplateService()
            recommendations = service.list_recommendations(limit=200)
        except Exception as exc:
            logger.debug("[factor_governance] parameter template recommendations unavailable: %s", exc)
            return actions

        by_factor = {str(item.get("factor_id") or ""): item for item in catalog}
        for rec in recommendations:
            factor_id = str(rec.get("factor_id") or "")
            if not factor_id or factor_id not in by_factor:
                continue
            boundary = rec.get("boundary") or {}
            scope = str(boundary.get("recommended_scope") or "")
            target_template_id = str(rec.get("target_template_id") or "")
            if not target_template_id:
                continue
            evidence = {
                "recommendation_id": rec.get("recommendation_id"),
                "factor_id": factor_id,
                "target_template_id": target_template_id,
                "boundary": boundary,
                "execution_owner": "autonomous_learning",
                "handoff_reason": "factor_governance_is_not_a_parameter_template_executor",
            }
            item = by_factor[factor_id]
            if scope not in {"online_light", "offline_deep"}:
                continue
            current = service.get_active_template(
                factor_id=factor_id,
                regime_key=str(rec.get("regime_key") or ""),
            ) or {}
            if scope == "online_light" and str(current.get("template_id") or "") == target_template_id:
                continue
            action = (
                "handoff_parameter_template_switch"
                if scope == "online_light"
                else "handoff_parameter_template_validation"
            )
            verdict = RiskVerdict(allowed=True, reason="handoff_only_no_mutation")
            actions.append(self._audit_action(
                run,
                item,
                action,
                "delegated_to_autonomous_learning",
                evidence,
                verdict,
                result={
                    "target_template_id": target_template_id,
                    "regime_key": str(rec.get("regime_key") or ""),
                    "scope": scope,
                    "v16_command_id": v16_command_id,
                    "execution_owner": "autonomous_learning",
                    "applied": False,
                },
            ))
        return actions

    def _submit_offline_template_validation(self, rec: dict[str, Any]) -> dict[str, Any]:
        try:
            from backend.jobs.manager import get_job_manager
            from backend.services.parameter_template_validation import run_parameter_template_offline_validation

            params = {
                "factor_id": str(rec.get("factor_id") or ""),
                "template_id": str(rec.get("target_template_id") or ""),
                "recommendation_context": {
                    "source": "factor_governance_orchestrator",
                    "recommendation_id": str(rec.get("recommendation_id") or ""),
                },
            }
            fn = lambda cb, _params=params: run_parameter_template_offline_validation(_params, cb)
            job = get_job_manager().submit("parameter_template_validation", params, fn)
            return {"job_id": getattr(job, "job_id", "") or getattr(job, "id", ""), "params": params}
        except Exception as exc:
            return {"blocked": True, "reason": f"offline_validation_submit_failed:{exc}"}

    def _promote_shadow_candidates(
        self,
        catalog: list[dict[str, Any]],
        run: dict[str, Any],
        *,
        v16_authority: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        cfg = runtime_config.shared()
        max_actions = int(getattr(cfg, "factor_governance_max_promotions_per_cycle", 1) or 1)
        actions: list[dict[str, Any]] = []
        candidates = [
            item
            for item in catalog
            if item.get("source") == "shadow"
            and self._has_durable_shadow_lifecycle_identity(item)
        ]
        candidates.sort(key=lambda item: self._shadow_score(item), reverse=True)
        for item in candidates:
            if len(actions) >= max_actions:
                break
            if self._factor_has_pending_effect(str(item.get("factor_id") or "")):
                continue
            evidence = self._promotion_evidence(item, cfg)
            if not evidence["eligible"]:
                continue
            verdict = self._risk("promote_factor", item, evidence)
            if not verdict.allowed:
                actions.append(self._audit_action(run, item, "promote_factor", "blocked_by_risk", evidence, verdict))
                continue
            factor_name = str(item.get("factor_id") or "")
            authority = dict(v16_authority or {})
            binding = FactorV16Binding(
                command_id=str(authority.get("command_id") or ""),
                target_agent=str(authority.get("target_agent") or "factor_governance"),
                candidate_id=str(authority.get("candidate_id") or ""),
                posterior_fingerprint=str(authority.get("posterior_fingerprint") or ""),
                evidence_fingerprint=str(authority.get("evidence_fingerprint") or ""),
            )
            try:
                adapter = RegistryAdapter.shared()
                lifecycle = FactorLifecycleService(self.overlay.db_path, adapter=adapter)
                state = lifecycle.get_state(factor_name=factor_name)
                stage = str(state.get("lifecycle_stage") or FactorLifecycleStage.SHADOW.value)
                if stage == FactorLifecycleStage.SHADOW.value:
                    meta = adapter.get_meta(factor_name)
                    expression = str(
                        item.get("lifecycle_expression")
                        or meta.get("description")
                        or ""
                    )
                    result = lifecycle.prepare_promotion(
                        name=factor_name,
                        expression=expression,
                        artifact_hash=str(
                            item.get("lifecycle_artifact_hash")
                            or meta.get("artifact_hash")
                            or ""
                        ),
                        actor="system:factor_governance",
                        reason="autonomous governance promotion preparation",
                        evidence_refs=evidence,
                        idempotency_key=f"factor_prepare:{factor_name}:{run.get('run_id', '')}",
                        v16=binding,
                    )
                    committed, projection_ready, _mutation_status = (
                        self._mutation_commit_state(result)
                    )
                    status = (
                        "promotion_prepared"
                        if projection_ready
                        else "projection_degraded"
                        if committed
                        else "blocked_by_evidence"
                    )
                elif stage in {
                    FactorLifecycleStage.PROMOTION_PREPARED.value,
                    FactorLifecycleStage.ACTIVE.value,
                }:
                    target_weight = float(
                        getattr(cfg, "factor_governance_new_factor_weight", 0.0) or 0.0
                    )
                    if target_weight <= 0.0:
                        result = {
                            "ok": False,
                            "status": "explicit_positive_weight_required",
                            "lifecycle_stage": stage,
                        }
                        status = "blocked_by_evidence"
                        actions.append(self._audit_action(
                            run,
                            item,
                            "promote_factor",
                            status,
                            {**evidence, "activation_weight": target_weight},
                            verdict,
                            before={"lifecycle_stage": stage},
                            after={"lifecycle_stage": stage},
                            rollback={"target_stage": FactorLifecycleStage.QUARANTINED.value},
                            result=result,
                        ))
                        continue
                    result = lifecycle.activate(
                        name=factor_name,
                        weight=target_weight,
                        actor="system:factor_governance",
                        reason="autonomous governance factor activation",
                        evidence_refs=evidence,
                        idempotency_key=f"factor_activate:{factor_name}:{run.get('run_id', '')}",
                        v16=binding,
                    )
                    committed, projection_ready, _mutation_status = (
                        self._mutation_commit_state(result)
                    )
                    active_stage = (
                        str(result.get("lifecycle_stage") or stage)
                        == FactorLifecycleStage.ACTIVE.value
                    )
                    status = (
                        "applied"
                        if projection_ready and active_stage
                        else "projection_degraded"
                        if committed and active_stage
                        else "blocked_by_evidence"
                    )
                else:
                    result = {
                        "ok": False,
                        "status": "terminal_lifecycle_state",
                        "lifecycle_stage": stage,
                    }
                    status = "superseded"
                actions.append(self._audit_action(
                    run,
                    item,
                    "promote_factor",
                    status,
                    evidence,
                    verdict,
                    before={"lifecycle_stage": stage},
                    after={
                        "lifecycle_stage": str(result.get("lifecycle_stage") or stage),
                        "mutation_id": str(result.get("mutation_id") or ""),
                    },
                    rollback={"target_stage": FactorLifecycleStage.QUARANTINED.value},
                    result=result,
                ))
            except Exception as exc:
                actions.append(self._audit_action(
                    run,
                    item,
                    "promote_factor",
                    "failed",
                    {**evidence, "error": str(exc)},
                    verdict,
                    before={"lifecycle_stage": "unknown"},
                    after={"lifecycle_stage": "unknown"},
                    rollback={"target_stage": FactorLifecycleStage.QUARANTINED.value},
                    result={"error": str(exc)},
                ))
        return actions

    def _rollback_canary_regressions(
        self,
        catalog: list[dict[str, Any]],
        run: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Apply explicit Canary regressions through the sole lifecycle executor.

        Missing canary state is deliberately ignored so legacy discovered
        factors are not mass-demoted.  A persisted, non-ACTIVE stage is an
        explicit safety signal produced by the evolution evidence loop.
        """

        regression_stages = {
            "SHADOW", "CANARY_5", "CANARY_20", "CANARY_50", "PROBATION", "QUARANTINED"
        }
        candidates = [
            item
            for item in catalog
            if item.get("source") == "discovered"
            and str((item.get("canary") or {}).get("stage") or "").upper() in regression_stages
        ]
        actions: list[dict[str, Any]] = []
        for item in candidates:
            factor_id = str(item.get("factor_id") or "")
            stage = str((item.get("canary") or {}).get("stage") or "").upper()
            evidence = {
                "canary_stage": stage,
                "reason": "persisted_canary_regression",
                "source": item.get("source"),
            }
            verdict = self._risk("rollback_factor_action", item, evidence)
            if not verdict.allowed:
                actions.append(self._audit_action(
                    run, item, "rollback_factor_action", "blocked_by_risk", evidence, verdict
                ))
                continue
            before_cfg = runtime_config.shared().to_dict()
            try:
                from alpha.registry_adapter import RegistryAdapter

                adapter = RegistryAdapter.shared()
                meta = adapter.get_meta(factor_id) or {}
                lifecycle = FactorLifecycleService(self.overlay.db_path, adapter=adapter)
                result = lifecycle.quarantine(
                    name=factor_id,
                    expression=str(meta.get("description") or ""),
                    artifact_hash=str(meta.get("artifact_hash") or ""),
                    actor="system:factor_governance",
                    reason="persisted canary regression",
                    evidence_refs=evidence,
                    idempotency_key=f"factor_quarantine:{factor_id}:{run.get('run_id', '')}",
                )
                committed, projection_ready, mutation_status = (
                    self._mutation_commit_state(result)
                )
                actions.append(self._audit_action(
                    run,
                    item,
                    "rollback_factor_action",
                    (
                        "applied"
                        if projection_ready
                        else "projection_degraded"
                        if committed
                        else "blocked_by_evidence"
                    ),
                    evidence,
                    verdict,
                    before={"source": "discovered", "runtime_config": before_cfg},
                    after={
                        "lifecycle_stage": str(result.get("lifecycle_stage") or stage),
                        "mutation_id": str(result.get("mutation_id") or ""),
                        "runtime_config": runtime_config.shared().to_dict(),
                    },
                    rollback={"runtime_config": before_cfg},
                    result={
                        **dict(result or {}),
                        "mutation_status": mutation_status,
                        "durably_committed": committed,
                        "projection_ready": projection_ready,
                    },
                ))
            except Exception as exc:
                logger.exception("[factor_governance] canary rollback failed for %s", factor_id)
                actions.append(self._audit_action(
                    run,
                    item,
                    "rollback_factor_action",
                    "failed",
                    {**evidence, "error": str(exc)},
                    verdict,
                    before={"runtime_config": before_cfg},
                    after={"runtime_config": runtime_config.shared().to_dict()},
                    result={"error": str(exc)},
                ))
        return actions

    def _downweight_weak_alpha(
        self,
        catalog: list[dict[str, Any]],
        run: dict[str, Any],
        *,
        cfg: Any | None = None,
        profile: FactorGovernanceProfile | None = None,
    ) -> list[dict[str, Any]]:
        cfg = cfg or runtime_config.shared()
        profile = profile or self._governance_profile(cfg)
        watch = float(getattr(cfg, "factor_health_watch_threshold", 40.0) or 40.0)
        max_delta = float(getattr(cfg, "awe_max_single_change", 0.15) or 0.15)
        current_weights = dict(getattr(cfg, "factor_portfolio_weights", {}) or {})
        factor_configs = self._portfolio_configs(cfg)
        patches: dict[str, dict[str, Any]] = {}
        evidence_by_factor: dict[str, dict[str, Any]] = {}
        for item in catalog:
            if not item.get("used_in_score"):
                continue
            score = float(item.get("health_score") or 0.0)
            status = str(item.get("health_status") or "")
            model_evidence = self._model_governance_evidence(item, cfg)
            health_weak = score > 0 and (score < watch or status == "DECAYING")
            model_weak = bool(model_evidence.get("weak_for_downweight"))
            if not health_weak and not model_weak:
                continue
            name = str(item["factor_id"])
            if self._factor_has_pending_effect(name):
                continue
            old_w = float(current_weights.get(name, item.get("weight", 0.0)) or 0.0)
            if old_w <= 0:
                continue
            target = max(
                profile.min_live_weight,
                old_w * (1.0 - max_delta),
            )
            if target >= old_w:
                continue
            reason = (
                "autonomous_model_weakness_downweight"
                if model_weak and not health_weak
                else "autonomous_health_downweight"
            )
            patches[name] = {"weight": target, "reason": reason}
            evidence_by_factor[name] = {
                "health_score": score,
                "health_status": status,
                "health_weak": health_weak,
                "model_governance": model_evidence,
                "old_weight": old_w,
                "target_weight": target,
                "max_single_change": max_delta,
                "governance_profile": profile.name,
                "minimum_live_weight": profile.min_live_weight,
            }
        if not patches:
            return []

        allowed_patches: dict[str, dict[str, Any]] = {}
        actions: list[dict[str, Any]] = []
        verdicts: dict[str, RiskVerdict] = {}
        for name, patch in patches.items():
            item = next(item for item in catalog if item["factor_id"] == name)
            verdict = self._risk("update_weight", item, evidence_by_factor[name])
            verdicts[name] = verdict
            if verdict.allowed:
                allowed_patches[name] = patch
            else:
                actions.append(self._audit_action(run, item, "update_weight", "blocked_by_risk", evidence_by_factor[name], verdict))
        if not allowed_patches:
            return actions

        before_cfg = runtime_config.shared().to_dict()
        dp = DecisionPolicy(
            redundancy_max_group_weight=float(getattr(cfg, "factor_redundancy_max_group_weight", 0.35) or 0.35)
        )
        weight_result = FactorWeightChangeService(self.overlay.db_path).execute(
            source="factor_governance_update_weight",
            producer="factor_governance",
            run_id=str(run.get("run_id") or ""),
            actor="system:factor_governance",
            reason="health/model evidence governed downweight",
            awe_patches=allowed_patches,
            weight_policy_weights=None,
            factor_configs=factor_configs,
            current_weights=current_weights,
            fast=True,
            decision_policy=dp,
            risk_check=lambda _plan: {
                "allowed": True,
                "reason": "producer_factor_risk_verdicts_allowed",
                "producer_verdicts": {name: verdict.to_dict() for name, verdict in verdicts.items()},
            },
            evidence_by_factor=evidence_by_factor,
        )
        decisions = dict(weight_result.get("admitted_decisions") or {})
        for name, admission in (weight_result.get("admissions") or {}).items():
            evidence_by_factor.setdefault(name, {})["experiment_admission"] = admission
            if admission.get("allowed"):
                continue
            decision = (weight_result.get("decisions") or {}).get(name)
            if decision is None:
                continue
            evidence_by_factor[name]["experiment_admission"] = admission
            item = next(item for item in catalog if item["factor_id"] == name)
            actions.append(self._audit_action(
                run,
                item,
                "update_weight",
                "blocked_by_evidence",
                evidence_by_factor[name],
                verdicts[name],
                before={"weight": decision.old_weight},
                after={"weight": decision.old_weight},
                result={"admission": admission},
            ))
        partial = DecisionPolicy.to_weights(decisions)
        if weight_result.get("status") != "applied" or not partial:
            return actions
        projection_ready = bool(weight_result.get("projection_ready", True))
        after_cfg = runtime_config.shared().to_dict()
        for name, decision in decisions.items():
            item = next(item for item in catalog if item["factor_id"] == name)
            actions.append(self._audit_action(
                run,
                item,
                "update_weight",
                "applied" if projection_ready else "projection_degraded",
                {**evidence_by_factor.get(name, {}), "decision": decision.to_api()},
                verdicts[name],
                before={"runtime_config": before_cfg, "weight": decision.old_weight},
                after={"runtime_config": after_cfg, "weight": decision.new_weight},
                rollback={"runtime_config": before_cfg},
                result={
                    "application_id": (weight_result.get("applications") or {}).get(name, ""),
                    "weight_change_status": weight_result.get("status"),
                    "projection_ready": projection_ready,
                    "mutation_id": str(
                        (weight_result.get("mutation") or {}).get("mutation_id")
                        or ""
                    ),
                },
            ))
        return actions

    def _disable_weak_live_alpha(
        self,
        catalog: list[dict[str, Any]],
        run: dict[str, Any],
        *,
        cfg: Any | None = None,
        profile: FactorGovernanceProfile | None = None,
    ) -> list[dict[str, Any]]:
        cfg = cfg or runtime_config.shared()
        profile = profile or self._governance_profile(cfg)
        severe = float(getattr(cfg, "retire_severe_threshold", 30.0) or 30.0)
        max_actions = int(getattr(cfg, "factor_governance_max_disables_per_cycle", 1) or 1)
        weak: list[dict[str, Any]] = []
        streak_candidates: dict[str, dict[str, Any]] = {}
        now = time.time()
        for item in catalog:
            if not item.get("eligible_for_live") or item.get("role") != "alpha":
                continue
            score = float(item.get("health_score") or 0.0)
            model_evidence = self._model_governance_evidence(item, cfg)
            if not profile.balanced_demo:
                if (score > 0.0 and score < severe) or bool(
                    model_evidence.get("weak_for_disable")
                ):
                    weak.append({**item, "_disable_streak": 1})
                continue

            model_weakness = max(
                float(model_evidence.get("avg_weakness_score") or 0.0),
                float(model_evidence.get("latest_weakness_score") or 0.0),
            )
            numeric_values = (
                score,
                model_weakness,
                float(item.get("weight") or 0.0),
            )
            integrity_failure = not all(math.isfinite(value) for value in numeric_values)
            if integrity_failure:
                weak.append(
                    {
                        **item,
                        "_disable_streak": profile.hard_disable_streak_cycles,
                        "_disable_reason": "non_finite_factor_evidence",
                    }
                )
                continue

            health_status = str(item.get("health_status") or "UNKNOWN").upper()
            health_updated_at = float(item.get("health_updated_at") or 0.0)
            health_age = (
                max(0.0, now - health_updated_at)
                if health_updated_at > 0.0
                else float("inf")
            )
            health_fresh = (
                health_status not in {"", "UNKNOWN"}
                and health_age <= profile.health_max_age_seconds
            )
            health_severe = (
                health_fresh
                and health_status == "DECAYING"
                and score < profile.hard_health_score
                and int(item.get("health_n_obs") or 0)
                >= profile.hard_health_min_n_obs
            )
            model_severe = (
                health_fresh
                and score < profile.hard_model_health_ceiling
                and int(model_evidence.get("sample_count") or 0)
                >= profile.hard_model_min_samples
                and int(model_evidence.get("weak_sample_count") or 0)
                >= profile.hard_model_min_weak_samples
                and model_weakness >= profile.hard_model_weakness
            )
            reason = (
                "persistent_severe_health"
                if health_severe
                else "persistent_mature_model_weakness"
                if model_severe
                else ""
            )
            if reason:
                evidence_cycle_id = (
                    f"{health_updated_at:.6f}:"
                    f"{int(model_evidence.get('sample_count') or 0)}:"
                    f"{int(model_evidence.get('weak_sample_count') or 0)}:"
                    f"{model_weakness:.8f}"
                )
                streak_candidates[str(item.get("factor_id") or "")] = {
                    "reason": reason,
                    "evidence_cycle_id": evidence_cycle_id,
                    "health_updated_at": health_updated_at,
                    "health_score": score,
                    "health_status": health_status,
                    "health_n_obs": int(item.get("health_n_obs") or 0),
                    "health_age_seconds": health_age,
                    "model_governance": model_evidence,
                }

        streaks = (
            self._advance_disable_evidence_streaks(streak_candidates, now=now)
            if profile.balanced_demo
            else {}
        )
        if profile.balanced_demo:
            for item in catalog:
                factor_id = str(item.get("factor_id") or "")
                streak = int((streaks.get(factor_id) or {}).get("streak") or 0)
                if streak >= profile.hard_disable_streak_cycles:
                    weak.append(
                        {
                            **item,
                            "_disable_streak": streak,
                            "_disable_reason": str(
                                (streaks.get(factor_id) or {}).get("reason") or ""
                            ),
                        }
                    )
        weak.sort(key=lambda item: (
            float(item.get("health_score") or 999.0),
            -float((item.get("factor_governance_shadow") or {}).get("avg_weakness_score") or item.get("model_weakness_score") or 0.0),
        ))
        actions: list[dict[str, Any]] = []
        for item in weak[:max_actions]:
            model_evidence = self._model_governance_evidence(item, cfg)
            evidence = {
                "health_score": float(item.get("health_score") or 0.0),
                "health_status": item.get("health_status"),
                "threshold": severe,
                "model_governance": model_evidence,
                "governance_profile": profile.name,
                "disable_reason": str(item.get("_disable_reason") or "strict_weakness"),
                "evidence_streak": int(item.get("_disable_streak") or 1),
                "required_evidence_streak": profile.hard_disable_streak_cycles,
            }
            verdict = self._risk("disable_factor_live", item, evidence)
            if not verdict.allowed:
                actions.append(self._audit_action(run, item, "disable_factor_live", "blocked_by_risk", evidence, verdict))
                continue
            before_cfg = runtime_config.shared().to_dict()
            name = str(item["factor_id"])
            entry = dict(runtime_config.shared().factor_signal_config.get(name, {}) or {})
            entry["enabled"] = False
            # Kept only for the one-release coordinator-off compatibility
            # path. Typed governance uses the terminal QUARANTINED state.
            entry["lifecycle_status"] = "QUARANTINE"
            entry["disabled_at"] = time.time()
            try:
                from backend.services.governance_control_plans import (
                    governance_coordinator_mode,
                )

                mode = governance_coordinator_mode()
                if mode != "off":
                    # Native and discovered factors share one durable state
                    # machine. Builtin code stays registered, while its
                    # RuntimeConfig admission and weight become terminal.
                    adapter = RegistryAdapter.shared()
                    meta = adapter.get_meta(name) or {}
                    builtin = str(item.get("source") or "") == "builtin"
                    result = FactorLifecycleService(
                        self.overlay.db_path,
                        adapter=adapter,
                    ).quarantine(
                        name=name,
                        expression=(
                            name if builtin else str(meta.get("description") or "")
                        ),
                        artifact_hash=str(meta.get("artifact_hash") or ""),
                        actor="system:factor_governance",
                        reason="weak factor removed from live alpha",
                        evidence_refs=evidence,
                        idempotency_key=(
                            f"factor_weak_quarantine:{name}:"
                            f"{run.get('run_id', '')}"
                        ),
                    )
                else:
                    # One-release legacy path only.
                    result = self._apply_runtime_patch(
                        {"factor_signal_config": {name: entry}},
                        source="factor_governance_disable_live",
                        run_id=str(run.get("run_id") or ""),
                    )
            except Exception as exc:
                result = {
                    "ok": False,
                    "status": "governance_mutation_failed",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            committed, projection_ready, mutation_status = self._mutation_commit_state(result)
            after_cfg = runtime_config.shared().to_dict()
            after_entry = dict(
                (after_cfg.get("factor_signal_config") or {}).get(name) or {}
            )
            actions.append(self._audit_action(
                run,
                item,
                "disable_factor_live",
                (
                    "applied"
                    if projection_ready
                    else "projection_degraded"
                    if committed
                    else "blocked_by_evidence"
                ),
                evidence,
                verdict,
                before={"runtime_config": before_cfg, "enabled": True},
                after={
                    "runtime_config": after_cfg,
                    "enabled": after_entry.get("enabled"),
                    "lifecycle_status": after_entry.get("lifecycle_status"),
                },
                rollback={"runtime_config": before_cfg},
                result={
                    **dict(result or {}),
                    "mutation_status": mutation_status,
                    "durably_committed": committed,
                    "projection_ready": projection_ready,
                },
            ))
        return actions

    def _activate_healthy_builtin_shadow(
        self,
        catalog: list[dict[str, Any]],
        run: dict[str, Any],
        *,
        v16_authority: dict[str, Any] | None = None,
        cfg: Any | None = None,
        profile: FactorGovernanceProfile | None = None,
    ) -> list[dict[str, Any]]:
        """Autonomously activate explicitly enrolled builtin shadow factors.

        Builtins are already executable code, so they do not need a registry
        source promotion.  They do need the same evidence boundary as a
        discovered factor: enough out-of-sample health observations, no
        current decay, and a governed initial weight.  ``weight == 0`` plus
        ``lifecycle_status == SHADOW`` is the observation-only state.
        """
        cfg = cfg or runtime_config.shared()
        profile = profile or self._governance_profile(cfg)
        if not bool(getattr(cfg, "factor_governance_builtin_activation_enabled", True)):
            return []

        min_score = profile.builtin_activation_min_health_score
        min_n_obs = profile.builtin_activation_min_n_obs
        max_activations = int(getattr(cfg, "factor_governance_max_builtin_activations_per_cycle", 1) or 1)
        initial_weight = min(
            0.50,
            float(getattr(cfg, "factor_governance_builtin_activation_weight", 0.0) or 0.0),
        )
        if profile.balanced_demo:
            initial_weight = max(profile.min_live_weight, initial_weight)
        if initial_weight <= 0.0:
            logger.warning(
                "[factor_governance] builtin activation disabled: explicit positive weight required"
            )
            return []
        signal_cfg = dict(getattr(cfg, "factor_signal_config", {}) or {})
        candidates: list[dict[str, Any]] = []
        for item in catalog:
            factor_id = str(item.get("factor_id") or "")
            entry = signal_cfg.get(factor_id)
            if not isinstance(entry, dict):
                continue
            if self._factor_has_pending_effect(factor_id):
                continue
            if item.get("source") != "builtin" or item.get("role") != "alpha":
                continue
            if str(item.get("lifecycle_status") or "").upper() not in {
                FactorLifecycleStage.SHADOW.value,
                FactorLifecycleStage.PROMOTION_PREPARED.value,
            }:
                continue
            if not bool(entry.get("autonomous_activation")):
                continue
            if not bool(item.get("enabled")) or item.get("lifecycle_status") == "DEAD":
                continue
            score = float(item.get("health_score") or 0.0)
            n_obs = int(item.get("health_n_obs") or 0)
            if score < min_score or n_obs < min_n_obs:
                continue
            health_status = str(item.get("health_status") or "").upper()
            health_age = time.time() - float(item.get("health_updated_at") or 0.0)
            if (
                health_status not in {"HEALTHY", "WATCH"}
                or health_age < -5.0
                or health_age > profile.health_max_age_seconds
            ):
                continue
            model_evidence = self._model_governance_evidence(item, cfg)
            model_samples = int(model_evidence.get("sample_count") or 0)
            model_weak_samples = int(model_evidence.get("weak_sample_count") or 0)
            if (model_samples or model_weak_samples) and (
                float(model_evidence.get("avg_weakness_score") or 0.0)
                >= float(getattr(cfg, "factor_governance_builtin_activation_max_weakness", 0.65) or 0.65)
            ):
                continue
            candidates.append({**item, "_model_governance": model_evidence})

        candidates.sort(key=lambda item: (
            -float(item.get("health_score") or 0.0),
            -int(item.get("health_n_obs") or 0),
            str(item.get("factor_id") or ""),
        ))

        actions: list[dict[str, Any]] = []
        activated = 0
        for item in candidates:
            if activated >= max_activations:
                break
            factor_id = str(item.get("factor_id") or "")
            entry = dict(signal_cfg.get(factor_id, {}) or {})
            evidence = {
                "activation_mode": "builtin_shadow_to_live",
                "health_score": float(item.get("health_score") or 0.0),
                "health_status": str(item.get("health_status") or "UNKNOWN"),
                "health_n_obs": int(item.get("health_n_obs") or 0),
                "current_weight": float((cfg.factor_portfolio_weights or {}).get(factor_id, 0.0) or 0.0),
                "target_weight": initial_weight,
                "model_governance": item.get("_model_governance") or {},
                "thresholds": {
                    "min_health_score": min_score,
                    "min_n_obs": min_n_obs,
                    "max_activations_per_cycle": max_activations,
                    "governance_profile": profile.name,
                },
            }
            verdict = self._risk("promote_factor", item, evidence)
            if not verdict.allowed:
                actions.append(self._audit_action(run, item, "promote_factor", "blocked_by_risk", evidence, verdict))
                continue

            before_cfg = runtime_config.shared().to_dict()
            try:
                authority = dict(v16_authority or {})
                binding = FactorV16Binding(
                    command_id=str(authority.get("command_id") or ""),
                    claim_token=str(authority.get("claim_token") or ""),
                    target_agent=str(
                        authority.get("target_agent") or "factor_governance"
                    ),
                    candidate_id=str(authority.get("candidate_id") or ""),
                    posterior_fingerprint=str(
                        authority.get("posterior_fingerprint") or ""
                    ),
                    evidence_fingerprint=str(
                        authority.get("evidence_fingerprint") or ""
                    ),
                )
                adapter = RegistryAdapter.shared()
                lifecycle = FactorLifecycleService(
                    self.overlay.db_path,
                    adapter=adapter,
                )
                state = lifecycle.get_state(factor_name=factor_id)
                stage = str(state.get("lifecycle_stage") or "")
                if not stage:
                    result = lifecycle.register_shadow(
                        name=factor_id,
                        expression=factor_id,
                        actor="system:factor_governance",
                        reason="enroll builtin shadow in durable lifecycle",
                        evidence_refs=evidence,
                        idempotency_key=(
                            f"builtin_shadow:{factor_id}:{run.get('run_id', '')}"
                        ),
                        v16=binding,
                    )
                    transition_status = "shadow_registered"
                elif stage == FactorLifecycleStage.SHADOW.value:
                    result = lifecycle.prepare_promotion(
                        name=factor_id,
                        expression=factor_id,
                        actor="system:factor_governance",
                        reason="healthy builtin shadow promotion preparation",
                        evidence_refs=evidence,
                        idempotency_key=(
                            f"builtin_prepare:{factor_id}:{run.get('run_id', '')}"
                        ),
                        v16=binding,
                    )
                    transition_status = "promotion_prepared"
                elif stage == FactorLifecycleStage.PROMOTION_PREPARED.value:
                    result = lifecycle.activate(
                        name=factor_id,
                        weight=initial_weight,
                        actor="system:factor_governance",
                        reason="healthy builtin shadow factor activation",
                        evidence_refs=evidence,
                        idempotency_key=(
                            f"builtin_activate:{factor_id}:{run.get('run_id', '')}"
                        ),
                        v16=binding,
                    )
                    transition_status = "applied"
                else:
                    continue
                committed, projection_ready, mutation_status = (
                    self._mutation_commit_state(result)
                )
                action_status = (
                    transition_status
                    if projection_ready
                    else "projection_degraded"
                    if committed
                    else "blocked_by_evidence"
                )
                if projection_ready:
                    activated += 1
                actions.append(self._audit_action(
                    run,
                    item,
                    "promote_factor",
                    action_status,
                    evidence,
                    verdict,
                    before={"runtime_config": before_cfg, "lifecycle_status": "SHADOW"},
                    after={
                        "runtime_config": runtime_config.shared().to_dict(),
                        "lifecycle_status": str(
                            result.get("lifecycle_stage") or stage or "SHADOW"
                        ),
                    },
                    rollback={"runtime_config": before_cfg},
                    result={
                        **dict(result or {}),
                        "mutation_status": mutation_status,
                        "durably_committed": committed,
                        "projection_ready": projection_ready,
                    },
                ))
            except Exception as exc:
                logger.exception("[factor_governance] builtin activation failed for %s", factor_id)
                actions.append(self._audit_action(
                    run,
                    item,
                    "promote_factor",
                    "failed",
                    {**evidence, "error": str(exc)},
                    verdict,
                    before={"runtime_config": before_cfg, "lifecycle_status": "SHADOW"},
                    after={"runtime_config": runtime_config.shared().to_dict()},
                    rollback={"runtime_config": before_cfg},
                    result={"error": str(exc)},
                ))
        return actions

    def _restore_quarantined_builtin_alpha(
        self,
        catalog: list[dict[str, Any]],
        run: dict[str, Any],
        *,
        cfg: Any | None = None,
        profile: FactorGovernanceProfile | None = None,
        v16_authority: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Keep one-release restore compatibility outside typed governance.

        Governance previously had a one-way path: a weak factor was marked
        ``enabled=false``/``QUARANTINE`` and AWE could only resurrect its
        weight.  That left the factor permanently outside the live selector.
        Recovery is deliberately conservative and only restores builtin
        factors after a fresh health evaluation, a cooldown, and (when model
        evidence exists) a cleared weakness verdict.  Discovered factors keep
        their separate Canary lifecycle and are not enabled by this path.
        Typed lifecycle treats QUARANTINED as terminal. A healthy native
        implementation must re-enter through a newly code-bound lifecycle,
        never by rewriting the terminal row back to ACTIVE.
        """
        from backend.services.governance_control_plans import (
            governance_coordinator_mode,
        )

        mode = governance_coordinator_mode()
        cfg = cfg or runtime_config.shared()
        profile = profile or self._governance_profile(cfg)
        if mode != "off" and not profile.balanced_demo:
            return []
        if not bool(getattr(cfg, "factor_governance_auto_restore_enabled", True)):
            return []

        max_actions = max(
            0,
            int(getattr(cfg, "factor_governance_max_restores_per_cycle", 1) or 1),
        )
        if max_actions <= 0:
            return []
        cooldown_seconds = max(0.0, profile.restore_cooldown_seconds)
        health_threshold = profile.restore_min_health_score
        max_weakness = profile.restore_max_weakness
        min_obs = profile.restore_min_n_obs
        now = time.time()
        candidates: list[tuple[dict[str, Any], dict[str, Any], float]] = []

        for item in catalog:
            factor_id = str(item.get("factor_id") or "")
            if not factor_id or item.get("source") != "builtin":
                continue
            if item.get("role") != "alpha" or item.get("lifecycle_status") == "DEAD":
                continue
            if item.get("governance_action") != "disable_factor_live":
                continue
            signal_entry = dict(cfg.factor_signal_config.get(factor_id, {}) or {})
            lifecycle = str(signal_entry.get("lifecycle_status") or "").upper()
            if signal_entry.get("enabled", True) is not False:
                continue
            if lifecycle not in {"QUARANTINE", "QUARANTINED"}:
                # Do not override an explicit/unknown disable reason.
                continue

            disabled_at = float(signal_entry.get("disabled_at") or item.get("last_action_ts") or 0.0)
            if disabled_at <= 0.0 or now - disabled_at < cooldown_seconds:
                continue
            health_updated_at = float(item.get("health_updated_at") or 0.0)
            if health_updated_at <= disabled_at:
                continue
            health_age = now - health_updated_at
            if health_age < -5.0 or health_age > profile.health_max_age_seconds:
                continue
            if str(item.get("health_status") or "").upper() not in {
                "HEALTHY",
                "WATCH",
            }:
                continue
            health_score = float(item.get("health_score") or 0.0)
            health_n_obs = int(item.get("health_n_obs") or 0)
            if health_score < health_threshold or health_n_obs < min_obs:
                continue

            model_evidence = self._model_governance_evidence(item, cfg)
            model_samples = int(model_evidence.get("sample_count") or 0)
            weak_samples = int(model_evidence.get("weak_sample_count") or 0)
            has_model_evidence = (
                model_samples >= profile.restore_model_min_samples
                or weak_samples >= profile.restore_model_min_samples
            )
            observed_weakness = max(
                float(model_evidence.get("avg_weakness_score") or 0.0),
                float(model_evidence.get("latest_weakness_score") or 0.0),
            )
            if has_model_evidence and observed_weakness >= max_weakness:
                continue

            if mode != "off" and self._factor_has_pending_effect(factor_id):
                continue
            candidates.append((item, signal_entry, disabled_at))

        candidates.sort(key=lambda row: (float(row[0].get("health_score") or 0.0), row[0].get("factor_id", "")), reverse=True)
        actions: list[dict[str, Any]] = []
        for item, entry, disabled_at in candidates[:max_actions]:
            factor_id = str(item["factor_id"])
            evidence = {
                "reason": "autonomous_quarantine_recovery",
                "source": item.get("source"),
                "health_score": float(item.get("health_score") or 0.0),
                "health_status": item.get("health_status"),
                "health_n_obs": int(item.get("health_n_obs") or 0),
                "health_updated_at": float(item.get("health_updated_at") or 0.0),
                "disabled_at": disabled_at,
                "cooldown_seconds": cooldown_seconds,
                "restore_health_threshold": health_threshold,
                "model_governance": self._model_governance_evidence(item, cfg),
                "governance_profile": profile.name,
                "target_stage": (
                    FactorLifecycleStage.SHADOW.value
                    if mode != "off"
                    else FactorLifecycleStage.ACTIVE.value
                ),
            }
            verdict = self._risk("restore_factor_live", item, evidence)
            if not verdict.allowed:
                actions.append(self._audit_action(run, item, "restore_factor_live", "blocked_by_risk", evidence, verdict))
                continue

            before_cfg = runtime_config.shared().to_dict()
            restored_entry = dict(entry)
            restored_entry["enabled"] = True
            restored_entry["lifecycle_status"] = "ACTIVE"
            restored_entry["restored_at"] = now
            restored_entry["restored_from"] = "QUARANTINE"
            try:
                if mode != "off":
                    authority = dict(v16_authority or {})
                    result = FactorLifecycleService(
                        self.overlay.db_path,
                        adapter=RegistryAdapter.shared(),
                    ).reenroll_quarantined_builtin(
                        name=factor_id,
                        actor="system:factor_governance",
                        reason="healthy builtin starts a new Demo shadow generation",
                        evidence_refs=evidence,
                        idempotency_key=(
                            f"builtin_reenroll:{factor_id}:{run.get('run_id', '')}"
                        ),
                        v16=FactorV16Binding(
                            command_id=str(authority.get("command_id") or ""),
                            claim_token=str(authority.get("claim_token") or ""),
                            target_agent=str(
                                authority.get("target_agent")
                                or "factor_governance"
                            ),
                            candidate_id=str(authority.get("candidate_id") or ""),
                            posterior_fingerprint=str(
                                authority.get("posterior_fingerprint") or ""
                            ),
                            evidence_fingerprint=str(
                                authority.get("evidence_fingerprint") or ""
                            ),
                        ),
                    )
                else:
                    result = self._apply_runtime_patch(
                        {"factor_signal_config": {factor_id: restored_entry}},
                        source="factor_governance_restore_live",
                        run_id=str(run.get("run_id") or ""),
                    )
                committed, projection_ready, mutation_status = self._mutation_commit_state(result)
                after_cfg = runtime_config.shared().to_dict()
                after_entry = dict(
                    (after_cfg.get("factor_signal_config") or {}).get(factor_id) or {}
                )
                actions.append(self._audit_action(
                    run,
                    item,
                    "restore_factor_live",
                    (
                        "applied"
                        if projection_ready
                        else "projection_degraded"
                        if committed
                        else "blocked_by_evidence"
                    ),
                    evidence,
                    verdict,
                    before={"runtime_config": before_cfg, "enabled": False},
                    after={
                        "runtime_config": after_cfg,
                        "enabled": after_entry.get("enabled"),
                        "lifecycle_status": after_entry.get("lifecycle_status"),
                        "weight": float((runtime_config.shared().factor_portfolio_weights or {}).get(factor_id, 0.0) or 0.0),
                    },
                    rollback={"runtime_config": before_cfg},
                    result={
                        **dict(result or {}),
                        "mutation_status": mutation_status,
                        "durably_committed": committed,
                        "projection_ready": projection_ready,
                    },
                ))
            except Exception as exc:
                logger.exception("[factor_governance] restore failed for %s", factor_id)
                actions.append(self._audit_action(
                    run,
                    item,
                    "restore_factor_live",
                    "failed",
                    {**evidence, "error": str(exc)},
                    verdict,
                    before={"runtime_config": before_cfg, "enabled": False},
                    after={"runtime_config": runtime_config.shared().to_dict(), "enabled": False},
                    rollback={"runtime_config": before_cfg},
                    result={"error": str(exc)},
                ))
        return actions

    def _retire_quarantined_discovered(self, catalog: list[dict[str, Any]], run: dict[str, Any]) -> list[dict[str, Any]]:
        cfg = runtime_config.shared()
        severe = float(getattr(cfg, "retire_severe_threshold", 30.0) or 30.0)
        max_actions = int(getattr(cfg, "factor_governance_max_retires_per_cycle", 1) or 1)
        candidates = []
        for item in catalog:
            if item.get("source") != "discovered" or item.get("enabled"):
                continue
            if self._factor_has_pending_effect(str(item.get("factor_id") or "")):
                continue
            score = float(item.get("health_score") or 0.0)
            model_evidence = self._model_governance_evidence(item, cfg)
            if (score > 0.0 and score < severe) or bool(model_evidence.get("weak_for_disable")):
                candidates.append(item)
        candidates.sort(key=lambda item: (
            float(item.get("health_score") or 999.0),
            -float((item.get("factor_governance_shadow") or {}).get("avg_weakness_score") or item.get("model_weakness_score") or 0.0),
        ))
        actions: list[dict[str, Any]] = []
        for item in candidates[:max_actions]:
            model_evidence = self._model_governance_evidence(item, cfg)
            evidence = {
                "health_score": float(item.get("health_score") or 0.0),
                "health_status": item.get("health_status"),
                "source": item.get("source"),
                "enabled": item.get("enabled"),
                "model_governance": model_evidence,
            }
            verdict = self._risk("retire_factor", item, evidence)
            if not verdict.allowed:
                actions.append(self._audit_action(run, item, "retire_factor", "blocked_by_risk", evidence, verdict))
                continue
            before_cfg = runtime_config.shared().to_dict()
            from alpha.registry_adapter import RegistryAdapter

            adapter = RegistryAdapter.shared()
            factor_name = str(item["factor_id"])
            meta = adapter.get_meta(factor_name) or {}
            lifecycle = FactorLifecycleService(self.overlay.db_path, adapter=adapter)
            result = lifecycle.retire(
                name=factor_name,
                expression=str(meta.get("description") or ""),
                artifact_hash=str(meta.get("artifact_hash") or ""),
                actor="system:factor_governance",
                reason="autonomous governance severe factor retirement",
                evidence_refs=evidence,
                idempotency_key=f"factor_retire:{factor_name}:{run.get('run_id', '')}",
            )
            actions.append(self._audit_action(
                run,
                item,
                "retire_factor",
                "applied" if result.get("ok") else "blocked_by_evidence",
                evidence,
                verdict,
                before={"runtime_config": before_cfg, "lifecycle_status": item.get("lifecycle_status")},
                after={
                    "lifecycle_status": str(result.get("lifecycle_stage") or ""),
                    "mutation_id": str(result.get("mutation_id") or ""),
                },
                rollback={"runtime_config": before_cfg},
                result=result,
            ))
        return actions

    # ── Evidence and mutation helpers ───────────────────────────────

    @staticmethod
    def _has_durable_shadow_lifecycle_identity(item: dict[str, Any]) -> bool:
        factor_id = str(item.get("factor_id") or "")
        return bool(
            factor_id
            and str(item.get("lifecycle_factor_id") or "") == factor_id
            and str(item.get("lifecycle_expression") or "")
            and str(item.get("lifecycle_artifact_hash") or "")
        )

    def _promotion_evidence(self, item: dict[str, Any], cfg: Any) -> dict[str, Any]:
        perf = item.get("shadow_perf") or {}
        oos_bars = int(perf.get("oos_bars") or 0)
        n_valid = int(perf.get("n_valid") or 0)
        cumulative_pnl = float(perf.get("cumulative_pnl") or 0.0)
        hit_rate = float(perf.get("hit_rate") or 0.0)
        max_drawdown = abs(float(perf.get("max_drawdown") or 0.0))
        health_score = float(item.get("health_score") or 0.0)
        health_status = str(item.get("health_status") or "UNKNOWN")
        canary_stage = str((item.get("canary") or {}).get("stage") or "").upper()
        min_oos = int(getattr(cfg, "factor_governance_shadow_min_oos_bars", 100) or 100)
        min_valid = int(getattr(cfg, "factor_governance_shadow_min_valid", 80) or 80)
        min_hit = float(getattr(cfg, "factor_governance_shadow_min_hit_rate", 0.5) or 0.5)
        max_dd = float(getattr(cfg, "factor_governance_shadow_max_drawdown", 0.05) or 0.05)
        watch = float(getattr(cfg, "factor_health_watch_threshold", 40.0) or 40.0)
        health_ok = health_status in {"UNKNOWN", "HEALTHY", "WATCH"} or health_score >= watch
        eligible = (
            canary_stage == "ACTIVE"
            and oos_bars >= min_oos
            and n_valid >= min_valid
            and cumulative_pnl > 0.0
            and hit_rate >= min_hit
            and max_drawdown <= max_dd
            and health_ok
        )
        return {
            "eligible": eligible,
            "oos_bars": oos_bars,
            "n_valid": n_valid,
            "cumulative_pnl": cumulative_pnl,
            "hit_rate": hit_rate,
            "max_drawdown": max_drawdown,
            "health_score": health_score,
            "health_status": health_status,
            "canary_stage": canary_stage,
            "thresholds": {
                "min_oos_bars": min_oos,
                "min_valid": min_valid,
                "min_hit_rate": min_hit,
                "max_drawdown": max_dd,
                "watch_health": watch,
            },
        }

    def _shadow_score(self, item: dict[str, Any]) -> float:
        perf = item.get("shadow_perf") or {}
        return (
            float(perf.get("cumulative_pnl") or 0.0)
            + 0.01 * float(perf.get("hit_rate") or 0.0)
            - abs(float(perf.get("max_drawdown") or 0.0))
        )

    def _model_governance_evidence(self, item: dict[str, Any], cfg: Any) -> dict[str, Any]:
        shadow = item.get("factor_governance_shadow") or {}
        sample_count = int(shadow.get("sample_count") or 0)
        weak_sample_count = int(shadow.get("weak_sample_count") or 0)
        latest_weakness = float(shadow.get("weakness_score") or item.get("model_weakness_score") or 0.0)
        avg_weakness = float(shadow.get("avg_weakness_score") or latest_weakness or 0.0)
        latest_positive = float(shadow.get("positive_score") or item.get("model_positive_score") or 0.0)
        avg_positive = float(shadow.get("avg_positive_score") or latest_positive or 0.0)
        min_samples = int(getattr(cfg, "factor_governance_model_min_samples", 3) or 3)
        down_th = float(getattr(cfg, "factor_governance_model_weakness_threshold", 0.65) or 0.65)
        disable_th = float(getattr(cfg, "factor_governance_model_disable_threshold", 0.85) or 0.85)
        enough = sample_count >= min_samples or weak_sample_count >= min_samples
        weak_for_downweight = enough and max(avg_weakness, latest_weakness) >= down_th
        weak_for_disable = enough and max(avg_weakness, latest_weakness) >= disable_th
        return {
            "sample_count": sample_count,
            "weak_sample_count": weak_sample_count,
            "latest_weakness_score": latest_weakness,
            "avg_weakness_score": avg_weakness,
            "latest_positive_score": latest_positive,
            "avg_positive_score": avg_positive,
            "min_samples": min_samples,
            "downweight_threshold": down_th,
            "disable_threshold": disable_th,
            "weak_for_downweight": weak_for_downweight,
            "weak_for_disable": weak_for_disable,
            "latest_inference_id": str(shadow.get("latest_inference_id") or ""),
            "model_type": str(shadow.get("model_type") or ""),
        }

    def _portfolio_configs(self, cfg: Any, *, signal_cfg: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        signal_cfg = dict(signal_cfg if signal_cfg is not None else (getattr(cfg, "factor_signal_config", {}) or {}))
        weights = dict(getattr(cfg, "factor_portfolio_weights", {}) or {})
        merged: dict[str, dict[str, Any]] = {}
        for name in set(signal_cfg) | set(weights):
            entry = dict(signal_cfg.get(name, {}) or {})
            entry["weight"] = float(weights.get(name, entry.get("weight", 0.0)) or 0.0)
            entry.setdefault("role", resolve_factor_role(name, entry))
            merged[name] = entry
        return merged

    def _risk(self, action: str, item: dict[str, Any], evidence: dict[str, Any]) -> RiskVerdict:
        return self.risk_policy.evaluate(
            action,
            {
                "factor": item.get("factor_id"),
                "source": item.get("source"),
                "role": item.get("role"),
                "required_mode": "autonomous_governance",
                "evidence": evidence,
            },
        )

    def _audit_action(
        self,
        run: dict[str, Any],
        item: dict[str, Any],
        action: str,
        status: str,
        evidence: dict[str, Any],
        verdict: RiskVerdict,
        *,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        rollback: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from backend.services.evolution_ledger import persist_runtime_config_snapshot, record_evolution_decision

        snapshot = persist_runtime_config_snapshot(
            runtime_config.shared(),
            source=f"factor_governance:{action}:{status}",
            run_id=str(run.get("run_id") or ""),
        )
        factor_id = str(item.get("factor_id") or "")
        decision_id = record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="factor_governance_autonomous",
            scope_type="factor",
            scope_key=factor_id,
            action=action,
            status=status,
            evidence=evidence,
            risk_verdict=verdict.to_dict(),
            before=before or {},
            after=after or {},
            result=result or {},
            rollback=rollback or {},
            config_version=int(snapshot.get("config_version") or 0),
            config_hash=str(snapshot.get("config_hash") or ""),
        )
        suggestion_id = self._record_policy_suggestion(factor_id, action, status, evidence, decision_id)
        self._record_learning_application(factor_id, action, status, suggestion_id, before, after, result, decision_id=decision_id)
        record_api_mutation(
            user="system:factor_governance",
            endpoint="backend.runtime.factor_governance_orchestrator",
            action=action,
            status=status,
            before=before or {},
            after=after or {},
            result={"decision_id": decision_id, **(result or {})},
            reason=_dumps(evidence),
            required_confirm="autonomous-risk-policy",
            confirm_ok=verdict.allowed,
            source_agent="factor_governance",
            decision_type="autonomous_mutation",
        )
        return {
            "factor_id": factor_id,
            "action": action,
            "status": status,
            "decision_id": decision_id,
            "suggestion_id": suggestion_id,
            "risk": verdict.to_dict(),
        }

    def _record_policy_suggestion(
        self,
        factor_id: str,
        action: str,
        status: str,
        evidence: dict[str, Any],
        decision_id: str,
    ) -> str:
        from backend.services.governance_control_plans import (
            governance_coordinator_mode,
        )

        try:
            coordinator_mode = governance_coordinator_mode()
        except Exception:
            # Invalid static authority must never fall through to a legacy
            # executable suggestion write.
            return ""
        if coordinator_mode != "off" and status in {
            "applied",
            "rolled_back",
            "projection_degraded",
            "promotion_prepared",
        }:
            # The coordinator intent/effect row is the committed fact.  This
            # old audit helper would otherwise create a second post-commit
            # `policy_suggestion` with no atomic applied_mutation_id binding.
            return ""
        suggestion_id = f"fgv3_{uuid.uuid4().hex[:16]}"
        now = time.time()
        evidence_payload = {
            "source_agent": "factor_governance",
            "decision_id": decision_id,
            **evidence,
        }
        evidence_payload.setdefault(
            "authority_verdict",
            AgentAuthorityRegistryService().evaluate_scope_write(
                "factor_governance",
                "factor",
                action,
                requested_writes=["policy_suggestion"],
                status=status,
                impact_level="medium",
            ),
        )
        evidence_payload = attach_policy_suggestion_agent_context(
            evidence_payload,
            source_agent="factor_governance",
            scope_type="factor",
            scope_key=factor_id,
            action=action,
            requested_writes=["policy_suggestion"],
            status=status,
            impact_level="medium",
        )
        conn = get_state_pg_conn()
        try:
            conn.execute(
                _p("""
                INSERT INTO policy_suggestion
                (suggestion_id, scope_type, scope_key, action, confidence, reason,
                 evidence_json, status, reviewed_at, review_note, created_at)
                VALUES (?, 'factor', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """),
                (
                    suggestion_id,
                    factor_id,
                    action,
                    1.0 if status == "applied" else 0.0,
                    "autonomous_factor_governance_v3",
                    _dumps(evidence_payload),
                    status,
                    now if status in {"auto_approved", "applied", "rolled_back", "blocked_by_risk", "superseded"} else 0.0,
                    "autonomous_execution_no_human_approval",
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return suggestion_id

    def _record_learning_application(
        self,
        factor_id: str,
        action: str,
        status: str,
        suggestion_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        result: dict[str, Any] | None,
        decision_id: str = "",
    ) -> None:
        from backend.services.governance_control_plans import (
            governance_coordinator_mode,
        )

        try:
            if governance_coordinator_mode() != "off":
                # In dual/enforce, applications/effects are written only by
                # the domain transaction owned by the coordinator.  Audit
                # callbacks must not synthesize a second application after
                # commit (or after a degraded projection).
                return
        except Exception:
            return
        if action in STRUCTURAL_AUDIT_ACTIONS or str((result or {}).get("application_id") or ""):
            return
        before = before or {}
        after = after or {}
        old_weight = float(before.get("weight") or 0.0)
        new_weight = float(after.get("weight") or old_weight)
        now = time.time()
        details = {
            "source_agent": "factor_governance",
            "decision_id": decision_id,
            "before": before,
            "after": after,
            "result": result or {},
            "authority_verdict": AgentAuthorityRegistryService().evaluate_scope_write(
                "factor_governance",
                "factor",
                action,
                requested_writes=["learning_application_log"],
                status=status,
                impact_level="medium",
            ),
        }
        conn = get_state_pg_conn()
        try:
            conn.execute(
                _p("""
                INSERT INTO learning_application_log
                (application_id, cycle_ts, scope_type, scope_key, action,
                 bias_multiplier, old_weight, new_weight, suggestion_ids_json,
                 status, details_json, created_at)
                VALUES (?, ?, 'factor', ?, ?, 1.0, ?, ?, ?, ?, ?, ?)
                """),
                (
                    f"fgv3_app_{uuid.uuid4().hex[:16]}",
                    now,
                    factor_id,
                    action,
                    old_weight,
                    new_weight,
                    _dumps([suggestion_id] if suggestion_id else []),
                    status,
                    _dumps(details),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _redundancy_report(self, catalog: list[dict[str, Any]]) -> dict[str, Any]:
        groups: dict[str, dict[str, Any]] = {}
        for item in catalog:
            group = str(item.get("redundancy_group") or "").strip()
            if not group or item.get("role") != "alpha":
                continue
            entry = groups.setdefault(group, {"members": [], "total_weight": 0.0, "leader": ""})
            entry["members"].append(item["factor_id"])
            entry["total_weight"] += float(item.get("weight") or 0.0)
            leader = entry.get("leader") or ""
            if not leader or float(item.get("health_score") or 0.0) > float(
                next((x.get("health_score") for x in catalog if x.get("factor_id") == leader), 0.0) or 0.0
            ):
                entry["leader"] = item["factor_id"]
        cap = float(getattr(runtime_config.shared(), "factor_redundancy_max_group_weight", 0.35) or 0.35)
        for entry in groups.values():
            entry["members"] = sorted(entry["members"])
            entry["total_weight"] = round(float(entry["total_weight"]), 6)
            entry["cap"] = cap
            entry["over_cap"] = entry["total_weight"] > cap
        return groups

    @staticmethod
    def _loads_dict(raw: Any) -> dict[str, Any]:
        value = _loads(raw, {})
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _loads_list(raw: Any) -> list[str]:
        value = _loads(raw, [])
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item or "")]


def run_autonomous_factor_governance_cycle() -> dict[str, Any]:
    return FactorGovernanceOrchestrator.shared().run_cycle(trigger_source="scheduler")
