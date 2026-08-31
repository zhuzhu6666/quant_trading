"""Autonomous factor governance V3.

This orchestrator is the single decision loop for factor lifecycle actions.  It
does not write directional signals and it does not bypass DecisionPolicy for
weights.  Every mutation is gated by RiskPolicyService and recorded in the
evolution ledger plus learning/policy audit tables.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Mapping

from alpha.decision_policy import DecisionPolicy
from alpha.portfolio_compositor import resolve_factor_role
from alpha.registry_adapter import RegistryAdapter
from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_exists,
)
from backend.core.db_helpers import load_json as _loads
from backend.services.learning_application_store import LearningApplicationStore
from backend.services.factor_catalog import build_factor_catalog, persist_factor_catalog_snapshot
from backend.services.factor_cards import build_factor_admission_evidence
from backend.services.factor_blend_health import FactorBlendHealthService
from backend.services.factor_identity import (
    canonical_factor_id,
    factor_definition_fingerprint,
)
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
from backend.services.policy_suggestion_context import attach_policy_suggestion_agent_context
from backend.services.policy_suggestion_identity import deterministic_policy_suggestion_id
from backend.services.runtime_config_mutation import RuntimeConfigMutationService
from backend.services.runtime_config_overlay import RuntimeConfigOverlayService
from config import runtime_config
from config.runtime_config import RuntimeConfig
from risk.policy_service import RiskPolicyService, RiskVerdict

logger = logging.getLogger(__name__)

_EVIDENCE_STREAK_KEY = "factor_governance_evidence_streak.v1"

# Only these audit outcomes mean that the effective catalog may have changed.
# Blocked, delegated and superseded observations must not make every later
# governance stage rebuild the expensive catalog projection.
_CATALOG_MUTATION_STATUSES = frozenset(
    {
        "applied",
        "promotion_prepared",
        "shadow_registered",
        "projection_degraded",
        "rolled_back",
        "demoted_to_shadow",
        "retired",
    }
)

_AUDIT_OCCURRENCE_KEYS = frozenset(
    {
        "run_id",
        "decision_id",
        "trace_id",
        "request_id",
        "correlation_id",
        "created_at",
        "updated_at",
        "timestamp",
        "started_at",
        "finished_at",
        "generated_at",
        "published_at",
        "heartbeat_at",
        "loaded_at",
        "catalog_ts",
        "write_timestamp",
        "observed_at",
        "cycle_ts",
    }
)


def _audit_semantic_value(value: Any) -> Any:
    """Strip only per-run occurrence metadata before coalescing observations."""

    if isinstance(value, Mapping):
        return {
            str(key): _audit_semantic_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _AUDIT_OCCURRENCE_KEYS
        }
    if isinstance(value, (list, tuple, set)):
        return [_audit_semantic_value(item) for item in value]
    return value


class _GovernanceCycleAuditWriter:
    """Run-scoped audit seam that keeps observations and facts distinct.

    Coordinator-owned mutation/effect facts remain one row per authoritative
    transition.  Repeated non-mutating observations with the same semantic
    evidence are represented by the first decision in this run; this avoids
    N identical ledger/policy writes without changing governance authority.
    """

    def __init__(
        self,
        run: Mapping[str, Any],
        cfg: RuntimeConfig | None = None,
        *,
        db_path: str | Path = STATE_DB,
    ):
        self.run_id = str(run.get("run_id") or "")
        self.cfg = cfg
        self.db_path = db_path
        # ``start_evolution_run`` already persisted and returned the
        # authoritative config snapshot.  Reuse it instead of creating a
        # second runtime_config_snapshot row for the audit writer.  Direct
        # diagnostic callers may provide only a run id; those callers lazily
        # create one snapshot on first use.
        self._snapshot: dict[str, Any] | None = (
            dict(run)
            if run.get("config_version") or run.get("config_hash")
            else None
        )
        self._seen: dict[str, str] = {}

    def snapshot(self) -> dict[str, Any]:
        if self._snapshot is None:
            from backend.services.evolution_ledger import persist_runtime_config_snapshot

            self._snapshot = persist_runtime_config_snapshot(
                self.cfg or runtime_config.shared(),
                source="factor_governance:cycle",
                run_id=self.run_id,
                db_path=self.db_path,
            )
        return dict(self._snapshot)

    @staticmethod
    def _authoritative(
        status: str,
        result: Mapping[str, Any] | None,
    ) -> bool:
        if str(status or "") in _CATALOG_MUTATION_STATUSES:
            return True
        payload = result or {}
        return any(
            bool(payload.get(key))
            for key in (
                "mutation_id",
                "applied_mutation_id",
                "application_id",
                "effect_id",
            )
        )

    def decision(
        self,
        *,
        factor_id: str,
        action: str,
        status: str,
        evidence: Mapping[str, Any],
        risk_verdict: Mapping[str, Any],
        before: Mapping[str, Any] | None,
        after: Mapping[str, Any] | None,
        rollback: Mapping[str, Any] | None,
        result: Mapping[str, Any] | None,
        config_snapshot: Mapping[str, Any],
    ) -> tuple[str, bool]:
        from backend.services.evolution_ledger import record_evolution_decision

        semantic = _audit_semantic_value(
            {
                "factor_id": factor_id,
                "action": action,
                "status": status,
                "evidence": evidence,
                "risk_verdict": risk_verdict,
                "before": before or {},
                "after": after or {},
                "rollback": rollback or {},
                "result": result or {},
            }
        )
        key = hashlib.sha256(_dumps(semantic).encode("utf-8")).hexdigest()
        if not self._authoritative(status, result) and key in self._seen:
            return self._seen[key], True
        decision_id = record_evolution_decision(
            run_id=self.run_id,
            decision_type="factor_governance_autonomous",
            scope_type="factor",
            scope_key=factor_id,
            action=action,
            status=status,
            evidence=dict(evidence),
            risk_verdict=dict(risk_verdict),
            before=dict(before or {}),
            after=dict(after or {}),
            result=dict(result or {}),
            rollback=dict(rollback or {}),
            config_version=int(config_snapshot.get("config_version") or 0),
            config_hash=str(config_snapshot.get("config_hash") or ""),
            db_path=self.db_path,
        )
        self._seen[key] = decision_id
        return decision_id, False


def _catalog_refresh_required(actions: list[dict[str, Any]]) -> bool:
    return any(
        str(item.get("status") or "") in _CATALOG_MUTATION_STATUSES
        for item in actions
    )


def _redundancy_signal_patch(
    report: Mapping[str, Any] | None,
    signal_cfg: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Return only redundancy metadata that differs from RuntimeConfig."""
    current = dict(signal_cfg or {})
    patch: dict[str, dict[str, Any]] = {}
    for group in list((report or {}).get("groups") or []):
        if not isinstance(group, Mapping):
            continue
        group_id = str(group.get("group_id") or "")
        leader = str(group.get("leader") or "")
        if not group_id or not leader:
            continue
        for member in list(group.get("members") or []):
            factor_id = str(member or "")
            if not factor_id:
                continue
            entry = dict(current.get(factor_id) or {})
            if (
                str(entry.get("redundancy_group") or "") == group_id
                and str(entry.get("redundancy_leader") or "") == leader
            ):
                continue
            entry["redundancy_group"] = group_id
            entry["redundancy_leader"] = leader
            patch[factor_id] = entry

    return patch


def factor_governance_health_max_age_seconds(
    cfg: RuntimeConfig | None = None,
) -> float:
    """Return the canonical freshness contract for factor governance."""

    current = cfg if cfg is not None else runtime_config.shared()
    if runtime_config.bounded_demo_mode_active(current):
        return float(
            getattr(
                current,
                "factor_governance_demo_health_max_age_seconds",
                900.0,
            )
            or 900.0
        )
    return 900.0


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


def posterior_expansion_verdict(
    *,
    delta_avg_reward: float | None,
    observed_trade_count: int,
    block_delta: float = -0.05,
    min_samples: int = 10,
) -> str:
    """Mixed-mode posterior guard verdict for factor expansion candidates.

    A previously-applied autonomous factor action whose measured posterior
    effect (delta_avg_reward in learning_application_effect) is negative must
    not be blindly repeated.  With enough observed trades the expansion is
    blocked; with thin evidence the candidate is kept but flagged degraded
    (apply path may use a reduced weight/scope).  No record or non-negative
    delta allows the expansion.

    Returns one of: blocked_by_posterior | posterior_degraded | posterior_ok.
    """
    if delta_avg_reward is None:
        return "posterior_ok"
    if delta_avg_reward < block_delta:
        if observed_trade_count >= min_samples:
            return "blocked_by_posterior"
        return "posterior_degraded"
    return "posterior_ok"


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


def factor_batch_manifest_verdict(
    authority: dict[str, Any],
    expansion_preflight: dict[str, Any],
) -> dict[str, Any]:
    """Ensure a factor run consumes the exact V16-issued batch manifest."""
    candidate_refs = [
        item
        for item in list((expansion_preflight or {}).get("candidate_refs") or [])
        if isinstance(item, dict)
    ]
    candidate_count = int((expansion_preflight or {}).get("candidate_count") or 0)
    if (
        not candidate_refs
        or len(candidate_refs) != 1
        or any(
            not bool(item.get("execution_ready"))
            or list(item.get("blocker_codes") or [])
            for item in candidate_refs
        )
    ):
        return {
            "allowed": False,
            "status": "factor_candidate_contract_not_ready",
            "reason": "candidate_refs_must_be_single_frozen_execution_ready_candidate",
            "candidate_count": candidate_count,
            "candidate_ref_count": len(candidate_refs),
        }
    evidence = dict(authority.get("evidence") or {})
    manifest = dict(evidence.get("batch_manifest") or {})
    expected_fingerprint = hashlib.sha256(
        _dumps(dict(expansion_preflight or {})).encode("utf-8")
    ).hexdigest()
    if str(manifest.get("schema_version") or "") != "factor_governance_batch_manifest.v1":
        return {
            "allowed": False,
            "status": "factor_batch_manifest_missing",
            "reason": "v16_factor_batch_manifest_required",
        }
    if str(manifest.get("preflight_fingerprint") or "") != expected_fingerprint:
        return {
            "allowed": False,
            "status": "factor_batch_manifest_mismatch",
            "reason": "v16_factor_preflight_fingerprint_mismatch",
            "expected_fingerprint": expected_fingerprint,
            "authority_fingerprint": str(manifest.get("preflight_fingerprint") or ""),
        }
    if int(manifest.get("candidate_count") or 0) != int(
        expansion_preflight.get("candidate_count") or 0
    ):
        return {
            "allowed": False,
            "status": "factor_batch_manifest_mismatch",
            "reason": "v16_factor_candidate_count_mismatch",
            "expected_candidate_count": int(expansion_preflight.get("candidate_count") or 0),
            "authority_candidate_count": int(manifest.get("candidate_count") or 0),
        }
    return {
        "allowed": True,
        "status": "factor_batch_manifest_bound",
        "manifest": manifest,
    }


class FactorGovernanceOrchestrator:
    """Conservative autonomous lifecycle and weight governance."""

    _instance: "FactorGovernanceOrchestrator | None" = None

    def __init__(self, risk_policy: RiskPolicyService | None = None):
        self.risk_policy = risk_policy or RiskPolicyService.shared()
        self.overlay = RuntimeConfigOverlayService()
        self._admission_evidence_count_cache: dict[str, dict[str, Any]] = {}
        self._active_audit_writer: _GovernanceCycleAuditWriter | None = None

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
        self._admission_evidence_count_cache = {}
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
            db_path=self.overlay.db_path,
            summary={"version": "factor_governance.v3"},
        )
        self._active_audit_writer = _GovernanceCycleAuditWriter(
            run,
            cfg,
            db_path=self.overlay.db_path,
        )
        actions: list[dict[str, Any]] = []
        status = "completed"
        try:
            shadow_refresh = self._refresh_shadow_model_evidence()
            if shadow_refresh.get("status") in ("ok", "failed"):
                logger.info(
                    "[governance] shadow evidence refresh %s count=%s error=%s",
                    shadow_refresh.get("status"),
                    shadow_refresh.get("count"),
                    shadow_refresh.get("error"),
                )
            catalog = build_factor_catalog(self.overlay.db_path)
            rollback_actions = self._rollback_failed_actions(run)
            actions.extend(rollback_actions)
            catalog_snapshot = persist_factor_catalog_snapshot(
                catalog,
                run_id=str(run.get("run_id") or ""),
                source="factor_governance_cycle",
                db_path=self.overlay.db_path,
            )
            canary_actions = self._rollback_canary_regressions(catalog, run)
            actions.extend(canary_actions)
            if _catalog_refresh_required([*rollback_actions, *canary_actions]):
                catalog = build_factor_catalog(self.overlay.db_path)
            demotion_actions = self._demote_invalid_candidate_evidence(
                catalog,
                run,
                cfg=cfg,
            )
            actions.extend(demotion_actions)
            if _catalog_refresh_required(demotion_actions):
                catalog = build_factor_catalog(self.overlay.db_path)
            # Tightening is always evaluated before any expansion posture or
            # V16 authorization gate. A freeze/missing delegate may stop
            # promotion, restore and template expansion, but must never defer
            # downweight, quarantine or terminal retirement.
            downweight_actions = self._downweight_weak_alpha(
                catalog, run, cfg=cfg, profile=profile
            )
            actions.extend(downweight_actions)
            if _catalog_refresh_required(downweight_actions):
                catalog = build_factor_catalog(self.overlay.db_path)
            disable_actions = self._disable_weak_live_alpha(
                catalog, run, cfg=cfg, profile=profile
            )
            actions.extend(disable_actions)
            if _catalog_refresh_required(disable_actions):
                catalog = build_factor_catalog(self.overlay.db_path)
            retire_actions = self._retire_quarantined_discovered(catalog, run)
            actions.extend(retire_actions)
            if _catalog_refresh_required(retire_actions):
                catalog = build_factor_catalog(self.overlay.db_path)
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
                finish_evolution_run(
                    run["run_id"],
                    status=status,
                    summary=summary,
                    db_path=self.overlay.db_path,
                )
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
                    db_path=self.overlay.db_path,
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
                        db_path=self.overlay.db_path,
                    )
                    return summary
                v16_manifest_verdict = factor_batch_manifest_verdict(
                    v16_authority,
                    expansion_preflight,
                )
                if not v16_manifest_verdict.get("allowed"):
                    summary = {
                        "status": "waiting_v16_command",
                        "reason": str(
                            v16_manifest_verdict.get("reason")
                            or "factor_v16_batch_manifest_mismatch"
                        ),
                        "catalog_count": len(catalog),
                        "actions": actions,
                        "catalog_snapshot": catalog_snapshot,
                        "redundancy_report": redundancy_report,
                        "expansion_preflight": expansion_preflight,
                        "v16_delegation": v16_delegation,
                        "v16_authority": v16_authority,
                        "v16_manifest_verdict": v16_manifest_verdict,
                    }
                    finish_evolution_run(
                        run["run_id"],
                        status="blocked_by_v16_command",
                        summary=summary,
                        db_path=self.overlay.db_path,
                    )
                    return summary
            v16_candidate_id = str(v16_authority.get("candidate_id") or "")
            # A V16 command is a fixed one-candidate manifest.  Do not let a
            # later fallback stage spend that authority on a different factor
            # when the originally selected candidate is unavailable or already
            # became a no-op.
            authorized_catalog = (
                catalog
                if not v16_candidate_id
                else []
                if v16_candidate_id == "redundancy"
                else [
                    item
                    for item in catalog
                    if str(item.get("factor_id") or "") == v16_candidate_id
                ]
            )
            # Expansion is single-mutation and ordered by the shortest safe
            # recovery path before longer lifecycle promotion work.
            expansion_actions = self._restore_active_zero_weight_alpha(
                authorized_catalog,
                run,
                v16_authority=v16_authority,
                cfg=cfg,
                profile=profile,
            )
            actions.extend(expansion_actions)
            expansion_committed = self._expansion_command_consumed(
                expansion_actions
            )
            if _catalog_refresh_required(expansion_actions):
                catalog = build_factor_catalog(self.overlay.db_path)
            if not expansion_committed:
                expansion_actions = self._restore_quarantined_builtin_alpha(
                    authorized_catalog,
                    run,
                    v16_authority=v16_authority,
                    cfg=cfg,
                    profile=profile,
                )
                actions.extend(expansion_actions)
                expansion_committed = self._expansion_command_consumed(
                    expansion_actions
                )
                if _catalog_refresh_required(expansion_actions):
                    catalog = build_factor_catalog(self.overlay.db_path)
            if not expansion_committed:
                expansion_actions = self._activate_healthy_builtin_shadow(
                    authorized_catalog,
                    run,
                    v16_authority=v16_authority,
                    cfg=cfg,
                    profile=profile,
                )
                actions.extend(expansion_actions)
                expansion_committed = self._expansion_command_consumed(
                    expansion_actions
                )
                if _catalog_refresh_required(expansion_actions):
                    catalog = build_factor_catalog(self.overlay.db_path)
            if not expansion_committed:
                expansion_actions = (
                    self._apply_redundancy_report(
                        catalog,
                        redundancy_report,
                        run,
                    )
                    if not v16_candidate_id or v16_candidate_id == "redundancy"
                    else []
                )
                actions.extend(expansion_actions)
                expansion_committed = self._expansion_command_consumed(
                    expansion_actions
                )
                if _catalog_refresh_required(expansion_actions):
                    catalog = build_factor_catalog(self.overlay.db_path)
            if not expansion_committed:
                expansion_actions = self._promote_shadow_candidates(
                    authorized_catalog,
                    run,
                    v16_authority=v16_authority,
                )
                actions.extend(expansion_actions)
                expansion_committed = self._expansion_command_consumed(
                    expansion_actions
                )
                if _catalog_refresh_required(expansion_actions):
                    catalog = build_factor_catalog(self.overlay.db_path)
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
                db_path=self.overlay.db_path,
            )
            return summary
        except Exception as exc:
            status = "failed"
            logger.exception("[factor_governance] cycle failed")
            finish_evolution_run(
                run["run_id"],
                status=status,
                summary={"status": status, "error": str(exc), "actions": actions},
                db_path=self.overlay.db_path,
            )
            return {"status": status, "error": str(exc), "actions": actions}
        finally:
            self._active_audit_writer = None

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
                health_max_age_seconds=factor_governance_health_max_age_seconds(cfg),
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
            health_max_age_seconds=factor_governance_health_max_age_seconds(cfg),
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
        # Batch D: single-point current-regime resolution consumed by every
        # restore candidate gate below (same fact owner as batch C).
        current_regime = self._current_market_regime_projection()
        current_regime_id = str(current_regime.get("regime_id") or "")
        regime_fit_ok = float(
            getattr(cfg, "factor_governance_regime_fit_ok_threshold", 0.5) or 0.5
        )
        activation_ids: list[str] = []
        active_zero_weight_ids: list[str] = []
        restore_ids: list[str] = []
        promotion_ids: list[str] = []
        posterior_blocked_ids: list[str] = []
        posterior_degraded_ids: list[str] = []

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
                    or str(
                        item.get("lifecycle_origin")
                        or item.get("source")
                        or ""
                    ).lower() != "builtin"
                    or item.get("role") != "alpha"
                    or str(item.get("lifecycle_status") or "").upper()
                    not in {
                        FactorLifecycleStage.SHADOW.value,
                        FactorLifecycleStage.PROMOTION_PREPARED.value,
                    }
                    or not (
                        bool(entry.get("autonomous_activation"))
                        or int(item.get("lifecycle_generation") or 1) > 1
                    )
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
                if bool(model.get("mutation_eligible")) and (
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
                if not self._activation_projection_ready(item):
                    continue
                posterior = self._posterior_expansion_guard(
                    factor_id,
                    cfg=cfg,
                )
                if posterior == "blocked_by_posterior":
                    posterior_blocked_ids.append(factor_id)
                    continue
                if posterior == "posterior_degraded":
                    posterior_degraded_ids.append(factor_id)
                activation_ids.append(factor_id)

        from backend.services.governance_control_plans import (
            governance_coordinator_mode,
        )

        mode = governance_coordinator_mode()
        restore_enabled = bool(
            getattr(cfg, "factor_governance_auto_restore_enabled", True)
        )
        if restore_enabled and profile.balanced_demo:
            for item in catalog:
                factor_id = str(item.get("factor_id") or "")
                entry = signal_cfg.get(factor_id)
                current_weight = float(weights.get(factor_id, 0.0) or 0.0)
                if (
                    not factor_id
                    or str(
                        item.get("lifecycle_origin")
                        or item.get("source")
                        or ""
                    ).lower() != "builtin"
                    or item.get("role") != "alpha"
                    or not isinstance(entry, dict)
                    or entry.get("enabled", True) is False
                    or str(item.get("lifecycle_status") or "").upper()
                    != FactorLifecycleStage.ACTIVE.value
                    or current_weight >= profile.min_live_weight
                ):
                    continue
                health_updated_at = float(item.get("health_updated_at") or 0.0)
                health_age = now - health_updated_at
                if (
                    health_updated_at <= 0.0
                    or health_age < -5.0
                    or health_age > profile.health_max_age_seconds
                    or str(item.get("health_status") or "").upper()
                    not in {"HEALTHY", "WATCH"}
                    or float(item.get("health_score") or 0.0)
                    < profile.restore_min_health_score
                    or int(item.get("health_n_obs") or 0)
                    < profile.restore_min_n_obs
                    or self._factor_has_pending_effect(factor_id)
                ):
                    continue
                model = self._model_governance_evidence(item, cfg)
                has_model_evidence = bool(model.get("mutation_eligible")) and (
                    int(model.get("sample_count") or 0)
                    >= profile.restore_model_min_samples
                    or int(model.get("weak_sample_count") or 0)
                    >= profile.restore_model_min_samples
                )
                observed_weakness = max(
                    float(model.get("avg_weakness_score") or 0.0),
                    float(model.get("latest_weakness_score") or 0.0),
                )
                if has_model_evidence and observed_weakness >= profile.restore_max_weakness:
                    continue
                posterior = self._posterior_expansion_guard(
                    factor_id,
                    cfg=cfg,
                )
                if posterior == "blocked_by_posterior":
                    posterior_blocked_ids.append(factor_id)
                    continue
                if posterior == "posterior_degraded":
                    posterior_degraded_ids.append(factor_id)
                regime_verdict = self._regime_suitable_for_restore(
                    current_regime_id=current_regime_id,
                    regime_fit_score=self._shadow_regime_fit_score(item),
                    regime_fit_ok_threshold=regime_fit_ok,
                )
                if not regime_verdict.get("suitable"):
                    continue
                active_zero_weight_ids.append(factor_id)
        if restore_enabled and (mode == "off" or profile.balanced_demo):
            for item in catalog:
                factor_id = str(item.get("factor_id") or "")
                entry = signal_cfg.get(factor_id)
                if (
                    not factor_id
                    or not self._is_quarantined_builtin_lifecycle(item)
                    or item.get("role") != "alpha"
                    or not isinstance(entry, dict)
                ):
                    continue
                disabled_at = float(
                    item.get("lifecycle_terminal_at")
                    or entry.get("disabled_at")
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
                has_model_evidence = bool(model.get("mutation_eligible")) and (
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
                posterior = self._posterior_expansion_guard(
                    factor_id,
                    cfg=cfg,
                )
                if posterior == "blocked_by_posterior":
                    posterior_blocked_ids.append(factor_id)
                    continue
                if posterior == "posterior_degraded":
                    posterior_degraded_ids.append(factor_id)
                regime_verdict = self._regime_suitable_for_restore(
                    current_regime_id=current_regime_id,
                    regime_fit_score=self._shadow_regime_fit_score(item),
                    regime_fit_ok_threshold=regime_fit_ok,
                )
                if not regime_verdict.get("suitable"):
                    continue
                restore_ids.append(factor_id)

        self._prime_admission_evidence_count_cache(
            [
                str(item.get("factor_id") or "")
                for item in catalog
                if self._is_dsl_promotion_lifecycle_candidate(item)
            ]
        )
        for item in catalog:
            factor_id = str(item.get("factor_id") or "")
            if (
                not self._is_dsl_promotion_lifecycle_candidate(item)
                or not self._promotion_evidence(item, cfg).get("eligible")
                or self._factor_has_pending_effect(factor_id)
            ):
                continue
            posterior = self._posterior_expansion_guard(
                factor_id,
                cfg=cfg,
            )
            if posterior == "blocked_by_posterior":
                posterior_blocked_ids.append(factor_id)
                continue
            if posterior == "posterior_degraded":
                posterior_degraded_ids.append(factor_id)
            promotion_ids.append(factor_id)

        candidate_actions: dict[str, str] = {}
        for factor_id in activation_ids:
            candidate_actions.setdefault(str(factor_id), "promote_factor")
        for factor_id in active_zero_weight_ids + restore_ids:
            candidate_actions.setdefault(str(factor_id), "restore_factor_live")
        for factor_id in promotion_ids:
            candidate_actions.setdefault(str(factor_id), "promote_factor")
        redundancy_patch = _redundancy_signal_patch(redundancy_report, signal_cfg)
        catalog_by_id = {
            str(item.get("factor_id") or ""): item
            for item in catalog
            if str(item.get("factor_id") or "")
        }
        candidate_refs: list[dict[str, Any]] = []
        for factor_id, candidate_action in sorted(candidate_actions.items()):
            item = catalog_by_id.get(factor_id, {})
            projection = dict(item.get("loaded_projection") or {})
            evidence_refs = {
                "lifecycle_factor_id": str(item.get("lifecycle_factor_id") or ""),
                "lifecycle_mutation_id": str(item.get("lifecycle_mutation_id") or ""),
                "lifecycle_generation": int(item.get("lifecycle_generation") or 0),
                "lifecycle_artifact_hash": str(item.get("lifecycle_artifact_hash") or ""),
                "runtime_admission": str(item.get("runtime_admission") or ""),
                "projection_id": str(projection.get("projection_id") or ""),
                "projection_generation": int(projection.get("generation") or 0),
                "projection_artifact_hash": str(projection.get("artifact_hash") or ""),
            }
            candidate_refs.append({
                "candidate_id": factor_id,
                "target_agent": "factor_governance",
                "scope_type": "factor_weight",
                "scope_key": factor_id,
                "action": candidate_action,
                "execution_ready": True,
                "governance_eligible": True,
                "bridge_ready": True,
                "blocker_codes": [],
                "evidence_refs": evidence_refs,
                "evidence_fingerprint": hashlib.sha256(
                    _dumps(evidence_refs).encode("utf-8")
                ).hexdigest(),
                "command_version": "factor_governance_candidate.v1",
            })
        if redundancy_patch:
            evidence_refs = {
                "groups": [
                    dict(group)
                    for group in list(redundancy_report.get("groups") or [])
                    if isinstance(group, Mapping)
                ],
                "patch_fingerprint": hashlib.sha256(
                    _dumps(redundancy_patch).encode("utf-8")
                ).hexdigest(),
            }
            candidate_refs.append({
                "candidate_id": "redundancy",
                "target_agent": "factor_governance",
                "scope_type": "factor_weight",
                "scope_key": "alpha_weight_policy",
                "action": "update_redundancy_groups",
                "execution_ready": True,
                "governance_eligible": True,
                "bridge_ready": True,
                "blocker_codes": [],
                "evidence_refs": evidence_refs,
                "evidence_fingerprint": hashlib.sha256(
                    _dumps(evidence_refs).encode("utf-8")
                ).hexdigest(),
                "command_version": "factor_governance_candidate.v1",
            })
        candidate_refs.sort(
            key=lambda item: (
                str(item.get("candidate_id") or ""),
                str(item.get("action") or ""),
            )
        )

        reasons = {
            "builtin_activation": activation_ids,
            "active_zero_weight_restore": active_zero_weight_ids,
            "builtin_restore": restore_ids,
            "shadow_promotion": promotion_ids,
            "redundancy_groups": int(
                redundancy_report.get("group_count") or 0
            ),
            "redundancy_mutation": bool(redundancy_patch),
        }
        return {
            "required": bool(candidate_refs),
            "reasons": reasons,
            "candidate_count": len(candidate_refs),
            "posterior_blocked_ids": posterior_blocked_ids,
            "posterior_degraded_ids": posterior_degraded_ids,
            "candidate_refs": candidate_refs,
            "directional_portfolio_guard": FactorWeightChangeService._directional_guard(
                factor_configs=self._portfolio_configs(cfg),
                weights=weights,
            ),
        }

    @staticmethod
    def _activation_projection_ready(item: Mapping[str, Any]) -> bool:
        """Require a fresh live projection before activating a prepared factor."""
        stage = str(item.get("lifecycle_status") or "").upper()
        if stage != FactorLifecycleStage.PROMOTION_PREPARED.value:
            return True
        if str(item.get("runtime_admission") or "").lower() != "projection_acknowledged":
            return False
        projection = item.get("loaded_projection") or {}
        if not bool(projection.get("loaded")):
            return False
        lifecycle_generation = int(item.get("lifecycle_generation") or 0)
        projection_generation = int(projection.get("generation") or 0)
        if lifecycle_generation and projection_generation and lifecycle_generation != projection_generation:
            return False
        lifecycle_artifact = str(item.get("lifecycle_artifact_hash") or "")
        projection_artifact = str(projection.get("artifact_hash") or "")
        if lifecycle_artifact and projection_artifact and lifecycle_artifact != projection_artifact:
            return False
        return True

    def _factor_has_pending_effect(self, factor_id: str) -> bool:
        if not factor_id:
            return False
        db_path = self.overlay.db_path
        production_state = is_state_db_path(db_path)
        if not production_state and not Path(db_path).exists():
            return False
        try:
            store = LearningApplicationStore(str(db_path))
            app = store.latest_application(scope_type="factor", scope_key=str(factor_id))
            eff = store.latest_effect(scope_key=str(factor_id), scope_type="factor")
            if app is None and eff is None:
                return False
            return LearningExperimentAdmissionService.row_is_active(
                {
                    "application_status": str((app or {}).get("status") or ""),
                    "effect_status": str((eff or {}).get("status") or ""),
                }
            )
        except Exception:
            # Production state uncertainty must block another mutation.  An
            # isolated test/research store has no live authority and may treat
            # a missing ledger as no pending experiment.
            return bool(production_state)

    def _latest_posterior_effect(
        self,
        factor_id: str,
    ) -> dict[str, Any] | None:
        """Latest measured posterior effect of the last autonomous factor action.

        Reuses the converged lean learning_application_effect store
        (scope=scope_key, posterior in effect_json) shared with the rollback
        path, so the expansion guard reads one fact source.  Returns None when
        there is no applicable record.
        """
        if not factor_id:
            return None
        db_path = self.overlay.db_path
        production_state = is_state_db_path(db_path)
        if not production_state and not Path(db_path).exists():
            return None
        try:
            eff = LearningApplicationStore(str(db_path)).latest_effect(
                scope_key=str(factor_id), scope_type="factor"
            )
            if eff is None:
                return None
            return {
                "observed_trade_count": int(eff.get("observed_trade_count") or 0),
                "delta_avg_reward": (
                    float(eff["delta_avg_reward"])
                    if eff.get("delta_avg_reward") is not None
                    else None
                ),
                "status": str(eff.get("status") or ""),
            }
        except Exception:
            # Production state uncertainty must block expansion; an isolated
            # test/research store has no live authority and may treat a
            # missing ledger as no posterior evidence.
            if not production_state:
                return None
            return {
                "observed_trade_count": 0,
                "delta_avg_reward": None,
                "unknown": True,
            }

    def _posterior_expansion_guard(
        self,
        factor_id: str,
        *,
        cfg: Any,
    ) -> str:
        """Verdict for one factor expansion candidate from posterior evidence.

        blocked_by_posterior  -> candidate must be removed
        posterior_degraded    -> candidate kept, but flagged for degraded apply
        posterior_ok          -> no posterior objection
        """
        effect = self._latest_posterior_effect(factor_id)
        if effect is None:
            return "posterior_ok"
        if bool(effect.get("unknown")):
            return "blocked_by_posterior"
        return posterior_expansion_verdict(
            delta_avg_reward=effect.get("delta_avg_reward"),
            observed_trade_count=int(effect.get("observed_trade_count") or 0),
            block_delta=float(
                getattr(
                    cfg,
                    "factor_governance_posterior_block_delta",
                    -0.05,
                )
                or -0.05
            ),
            min_samples=int(
                getattr(
                    cfg,
                    "factor_governance_posterior_min_samples",
                    10,
                )
                or 10
            ),
        )

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
            from backend.services.runtime_kv_store import set_on_conn as set_runtime_kv_on_conn

            set_runtime_kv_on_conn(
                conn,
                _EVIDENCE_STREAK_KEY,
                payload,
                updated_at=now,
                ensure=False,
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

    def _refresh_shadow_model_evidence(self) -> dict[str, Any]:
        """Refresh factor-governance shadow evidence on every cycle.

        Runs score_samples over the most recent review samples with the
        latest artifact (skip_existing dedupes by artifact+sample). This
        decouples governance evidence freshness from the weekend-only
        offmarket full-profile training window. Never blocks the cycle.
        """
        try:
            from research.factor_governance_lightgbm import (
                FactorGovernanceLightGBMService,
            )

            svc = FactorGovernanceLightGBMService(db_path=self.overlay.db_path)
            result = svc.score_samples(
                mode="shadow",
                skip_existing=True,
                limit=200,
            )
            return {
                "status": "ok" if result.get("ok") else "skipped",
                "error": str(result.get("error") or result.get("reason") or ""),
                "count": int(result.get("count") or 0),
                "skipped": bool(result.get("skipped")),
                "model_version": str(result.get("model_version") or ""),
            }
        except Exception as exc:  # noqa: BLE001 - never crash the governance cycle
            return {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "count": 0,
            }

    def _rollback_failed_actions(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        cfg = runtime_config.shared()
        min_trades = int(getattr(cfg, "factor_governance_rollback_min_trades", 3) or 3)
        delta_threshold = float(getattr(cfg, "factor_governance_rollback_delta_threshold", -0.15) or -0.15)
        try:
            store = LearningApplicationStore(str(self.overlay.db_path))
            _rows: list[dict[str, Any]] = []
            for eff in store.iter_effects(scope_type="factor"):
                if eff.get("status") not in ("observing", "applied", "ineffective"):
                    continue
                otc = int(eff.get("observed_trade_count") or 0)
                dar = eff.get("delta_avg_reward")
                if otc < min_trades:
                    continue
                if dar is None or float(dar) > delta_threshold:
                    continue
                app = store.get_application(str(eff.get("application_id") or ""))
                if not app:
                    continue
                if app.get("status") not in ("applied", "observing", "ineffective"):
                    continue
                factor_id = str(eff.get("scope_key") or app.get("scope_key") or "")
                app_ts = float(app.get("created_at") or 0)
                superseded = False
                for newer in store.iter_applications(scope_key=factor_id):
                    if (
                        float(newer.get("created_at") or 0) > app_ts
                        and str(newer.get("status")) not in ("rolled_back", "superseded")
                    ):
                        superseded = True
                        break
                if superseded:
                    continue
                _rows.append({
                    "application_id": str(app.get("application_id") or ""),
                    "scope_key": factor_id,
                    "action": str(eff.get("action") or app.get("action") or ""),
                    "observed_trade_count": otc,
                    "delta_avg_reward": float(dar),
                    "details_json": json.dumps({
                        "decision_id": app.get("decision_id") or "",
                        "scope_type": app.get("scope_type") or "factor",
                        "scope_key": factor_id,
                        "action": eff.get("action") or "",
                    }, ensure_ascii=False),
                    "suggestion_ids_json": json.dumps(
                        app.get("suggestion_ids") or [], ensure_ascii=False
                    ),
                    "_effect_updated_at": float(eff.get("updated_at") or 0),
                })
            _rows.sort(key=lambda r: r["_effect_updated_at"], reverse=True)
            rows = _rows[:5]
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
                        producer="factor_governance",
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
                _p(
                    """SELECT p.rollback_json AS rollback_json
                      FROM evolution_decision d
                      LEFT JOIN mutation_payload p ON p.payload_hash=d.payload_hash
                      WHERE d.decision_id=? LIMIT 1"""
                ),
                (decision_id,),
            ).fetchone()
            return self._loads_dict(row["rollback_json"] if row else "{}")
        finally:
            conn.close()

    def _mark_application_rolled_back(self, *, application_id: str, suggestion_ids: list[str], decision: dict[str, Any]) -> None:
        now = time.time()
        store = LearningApplicationStore(str(self.overlay.db_path))
        store.transition_application(application_id, status="rolled_back")
        store.update_effect(
            application_id,
            patch={"status": "rolled_back", "decision": decision},
        )
        use_pg = is_state_db_path(self.overlay.db_path)
        conn = get_state_pg_conn() if use_pg else connect_sqlite(self.overlay.db_path)
        if not use_pg:
            conn.row_factory = sqlite3.Row
        try:
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
        signal_patch = _redundancy_signal_patch(report, signal_cfg)
        if not signal_patch:
            return []
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

            params = {
                "factor_id": str(rec.get("factor_id") or ""),
                "template_id": str(rec.get("target_template_id") or ""),
                "recommendation_context": {
                    "source": "factor_governance_orchestrator",
                    "recommendation_id": str(rec.get("recommendation_id") or ""),
                },
            }
            job = get_job_manager().submit("parameter_template_validation", params)
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
        profile = self._governance_profile(cfg)
        max_actions = int(getattr(cfg, "factor_governance_max_promotions_per_cycle", 1) or 1)
        actions: list[dict[str, Any]] = []
        candidates = [
            item
            for item in catalog
            if self._is_dsl_promotion_lifecycle_candidate(item)
        ]
        candidates.sort(
            key=lambda item: (
                -self._shadow_score(item),
                str(item.get("factor_id") or ""),
            )
        )
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
                claim_token=str(authority.get("claim_token") or ""),
                target_agent=str(authority.get("target_agent") or "factor_governance"),
                candidate_id=str(authority.get("candidate_id") or ""),
                posterior_fingerprint=str(authority.get("posterior_fingerprint") or ""),
                evidence_fingerprint=str(authority.get("evidence_fingerprint") or ""),
            )
            try:
                adapter = RegistryAdapter.shared()
                lifecycle = FactorLifecycleService(
                    self.overlay.db_path,
                    adapter=adapter,
                    health_stale_after_sec=profile.health_max_age_seconds,
                )
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

    def _demote_invalid_candidate_evidence(
        self,
        catalog: list[dict[str, Any]],
        run: dict[str, Any],
        *,
        cfg: Any,
    ) -> list[dict[str, Any]]:
        """Cancel stale prepared candidates and quarantine legacy ACTIVE risk."""
        actions: list[dict[str, Any]] = []
        lifecycle = FactorLifecycleService(
            self.overlay.db_path,
            adapter=RegistryAdapter.shared(),
            health_stale_after_sec=factor_governance_health_max_age_seconds(cfg),
        )
        for item in catalog:
            factor_id = str(item.get("factor_id") or "")
            stage = str(item.get("lifecycle_status") or "").upper()
            if (
                not factor_id
                or str(item.get("lifecycle_origin") or "").lower()
                not in {"dsl", "shadow", "discovered"}
                or stage
                not in {
                    FactorLifecycleStage.PROMOTION_PREPARED.value,
                    FactorLifecycleStage.ACTIVE.value,
                }
            ):
                continue
            if stage == FactorLifecycleStage.PROMOTION_PREPARED.value:
                evidence = self._promotion_evidence(item, cfg)
                blockers = list(evidence.get("blocker_codes") or [])
                if evidence.get("eligible") is True:
                    continue
                reason_code = "prepared_evidence_invalidated"
            else:
                persisted = dict(
                    (item.get("lifecycle_evidence") or {}).get(
                        "admission_evidence"
                    )
                    or {}
                )
                config_complete = bool(
                    item.get("activation_canary")
                    and str(item.get("admission_evidence_version") or "")
                    == "factor_admission_evidence.v1"
                    and int(item.get("direction") or 0) in {-1, 1}
                )
                evidence_complete = bool(
                    str(persisted.get("schema_version") or "")
                    == "factor_admission_evidence.v1"
                    and persisted.get("eligible_for_activation") is True
                    and not list(persisted.get("activation_blocker_codes") or [])
                )
                if config_complete and evidence_complete:
                    continue
                blockers = ["legacy_evidence_incomplete"]
                evidence = {
                    "eligible": False,
                    "admission_evidence": persisted,
                    "blocker_codes": blockers,
                }
                reason_code = "legacy_evidence_incomplete"
            verdict = self._risk("retire_factor", item, evidence)
            if not verdict.allowed:
                actions.append(
                    self._audit_action(
                        run,
                        item,
                        "demote_to_shadow",
                        "blocked_by_risk",
                        evidence,
                        verdict,
                    )
                )
                continue
            result = lifecycle.demote_to_shadow(
                name=factor_id,
                actor="system:factor_governance",
                reason=reason_code,
                evidence_refs={
                    **evidence,
                    "blocker_codes": blockers,
                    "cancellation_command": "demote_to_shadow",
                },
                idempotency_key=(
                    f"factor_demote:{factor_id}:"
                    f"{item.get('lifecycle_mutation_id') or reason_code}"
                ),
            )
            committed, projection_ready, _status = self._mutation_commit_state(
                result
            )
            actions.append(
                self._audit_action(
                    run,
                    item,
                    "demote_to_shadow",
                    (
                        "demoted_to_shadow"
                        if projection_ready
                        else "projection_degraded"
                        if committed
                        else "blocked_by_evidence"
                    ),
                    evidence,
                    verdict,
                    before={"lifecycle_stage": stage},
                    after={
                        "lifecycle_stage": str(
                            result.get("lifecycle_stage") or stage
                        ),
                        "mutation_id": str(result.get("mutation_id") or ""),
                    },
                    rollback={"target_stage": stage},
                    result=result,
                )
            )
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
        regime_fit_ok = float(
            getattr(cfg, "factor_governance_regime_fit_ok_threshold", 0.5) or 0.5
        )
        # Batch C: resolve current market regime once (single-point fact owner),
        # then per-factor regime-fit decides whether global model weakness is a
        # true current-regime weakness or a regime mismatch.
        current_regime = self._current_market_regime_projection()
        current_regime_id = str(current_regime.get("regime_id") or "")
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
            # Batch C: when the model calls this factor globally weak, check
            # whether it actually fits the *current* regime.  If its recent
            # regime-fit is good, global weakness is concentrated elsewhere ->
            # regime_mismatch: keep the weight, record the reason, do not drop it.
            regime_fit_score = self._shadow_regime_fit_score(item)
            mismatch = self._regime_mismatch_verdict(
                model_weak,
                current_regime_id=current_regime_id,
                regime_fit_score=regime_fit_score,
                regime_fit_ok_threshold=regime_fit_ok,
            )
            if mismatch.get("regime_mismatch"):
                evidence_by_factor[name] = {
                    "health_score": score,
                    "health_status": status,
                    "health_weak": health_weak,
                    "model_governance": model_evidence,
                    "old_weight": old_w,
                    "regime_mismatch": mismatch,
                    "current_regime_projection": current_regime,
                    "governance_profile": profile.name,
                }
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
                bool(model_evidence.get("mutation_eligible"))
                and
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
            before_signal_cfg = dict(before_cfg.get("factor_signal_config") or {})
            after_signal_cfg = {**before_signal_cfg, name: entry}
            before_weights = dict(before_cfg.get("factor_portfolio_weights") or {})
            after_weights = {**before_weights, name: 0.0}
            before_guard = FactorWeightChangeService._directional_guard(
                factor_configs=self._portfolio_configs(
                    cfg,
                    signal_cfg=before_signal_cfg,
                ),
                weights=before_weights,
            )
            after_guard = FactorWeightChangeService._directional_guard(
                factor_configs=self._portfolio_configs(
                    cfg,
                    signal_cfg=after_signal_cfg,
                ),
                weights=after_weights,
            )
            evidence["directional_portfolio_guard_before"] = before_guard
            evidence["directional_portfolio_guard"] = after_guard
            if profile.balanced_demo and not FactorBlendHealthService.guard_allows_transition(
                before_guard,
                after_guard,
            ):
                actions.append(
                    self._audit_action(
                        run,
                        item,
                        "disable_factor_live",
                        "blocked_by_directional_portfolio_guard",
                        evidence,
                        verdict,
                        before={"runtime_config": before_cfg, "enabled": True},
                        after={"runtime_config": before_cfg, "enabled": True},
                        rollback={"runtime_config": before_cfg},
                        result={"reason": "directional_portfolio_degraded"},
                    )
                )
                continue
            try:
                from backend.services.governance_control_plans import (
                    governance_coordinator_mode,
                )

                mode = governance_coordinator_mode()
                if mode == "off":
                    # A production governance mutation must not fall back to
                    # a direct overlay write when the Coordinator is absent.
                    # Keep the tightening decision auditable and fail closed.
                    result = {
                        "ok": False,
                        "status": "governance_coordinator_required",
                        "reason": "factor_quarantine_requires_coordinator",
                    }
                else:
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
        configured_max_activations = int(
            getattr(cfg, "factor_governance_max_builtin_activations_per_cycle", 1)
            or 1
        )
        # Promotion is deliberately single-candidate. The existing config
        # remains the kill switch, but a cycle never batches activations.
        max_activations = min(1, max(0, configured_max_activations))
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
            if (
                str(
                    item.get("lifecycle_origin")
                    or item.get("source")
                    or ""
                ).lower()
                != "builtin"
                or item.get("role") != "alpha"
            ):
                continue
            if str(item.get("lifecycle_status") or "").upper() not in {
                FactorLifecycleStage.SHADOW.value,
                FactorLifecycleStage.PROMOTION_PREPARED.value,
            }:
                continue
            if not (
                bool(entry.get("autonomous_activation"))
                or int(item.get("lifecycle_generation") or 1) > 1
            ):
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
            if not self._activation_projection_ready(item):
                continue
            model_evidence = self._model_governance_evidence(item, cfg)
            model_samples = int(model_evidence.get("sample_count") or 0)
            model_weak_samples = int(model_evidence.get("weak_sample_count") or 0)
            if bool(model_evidence.get("mutation_eligible")) and (model_samples or model_weak_samples) and (
                float(model_evidence.get("avg_weakness_score") or 0.0)
                >= float(getattr(cfg, "factor_governance_builtin_activation_max_weakness", 0.65) or 0.65)
            ):
                continue
            candidates.append({**item, "_model_governance": model_evidence})

        candidates.sort(key=lambda item: (
            0 if str(item.get("factor_id") or "") == "morning_evening_star" else 1,
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
                    health_stale_after_sec=profile.health_max_age_seconds,
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

    def _restore_active_zero_weight_alpha(
        self,
        catalog: list[dict[str, Any]],
        run: dict[str, Any],
        *,
        cfg: Any | None = None,
        profile: FactorGovernanceProfile | None = None,
        v16_authority: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Restore one evidence-qualified ACTIVE builtin to the demo canary."""

        cfg = cfg or runtime_config.shared()
        profile = profile or self._governance_profile(cfg)
        if not profile.balanced_demo:
            return []
        now = time.time()
        weights = dict(getattr(cfg, "factor_portfolio_weights", {}) or {})
        candidates: list[dict[str, Any]] = []
        for item in catalog:
            factor_id = str(item.get("factor_id") or "")
            if (
                not factor_id
                or str(
                    item.get("lifecycle_origin")
                    or item.get("source")
                    or ""
                ).lower() != "builtin"
                or item.get("role") != "alpha"
                or not bool(item.get("enabled"))
                or str(item.get("lifecycle_status") or "").upper()
                != FactorLifecycleStage.ACTIVE.value
                or float(weights.get(factor_id, 0.0) or 0.0)
                >= profile.min_live_weight
                or self._factor_has_pending_effect(factor_id)
            ):
                continue
            health_updated_at = float(item.get("health_updated_at") or 0.0)
            health_age = now - health_updated_at
            if (
                health_updated_at <= 0.0
                or health_age < -5.0
                or health_age > profile.health_max_age_seconds
                or str(item.get("health_status") or "").upper()
                not in {"HEALTHY", "WATCH"}
                or float(item.get("health_score") or 0.0)
                < profile.restore_min_health_score
                or int(item.get("health_n_obs") or 0) < profile.restore_min_n_obs
            ):
                continue
            model = self._model_governance_evidence(item, cfg)
            has_model_evidence = bool(model.get("mutation_eligible")) and (
                int(model.get("sample_count") or 0) >= profile.restore_model_min_samples
                or int(model.get("weak_sample_count") or 0)
                >= profile.restore_model_min_samples
            )
            observed_weakness = max(
                float(model.get("avg_weakness_score") or 0.0),
                float(model.get("latest_weakness_score") or 0.0),
            )
            if has_model_evidence and observed_weakness >= profile.restore_max_weakness:
                continue
            candidates.append({**item, "_model_governance": model})
        candidates.sort(
            key=lambda item: (
                -float(item.get("health_score") or 0.0),
                -int(item.get("health_n_obs") or 0),
                str(item.get("factor_id") or ""),
            )
        )
        if not candidates:
            return []
        item = candidates[0]
        factor_id = str(item["factor_id"])
        evidence = {
            "recovery_mode": "active_zero_weight_to_demo_canary",
            "health_score": float(item.get("health_score") or 0.0),
            "health_status": str(item.get("health_status") or "UNKNOWN"),
            "health_n_obs": int(item.get("health_n_obs") or 0),
            "model_governance": item.get("_model_governance") or {},
            "old_weight": float(weights.get(factor_id, 0.0) or 0.0),
            "target_weight": profile.min_live_weight,
            "governance_profile": profile.name,
        }
        verdict = self._risk("promote_factor", item, evidence)
        if not verdict.allowed:
            return [
                self._audit_action(
                    run,
                    item,
                    "promote_factor",
                    "blocked_by_risk",
                    evidence,
                    verdict,
                )
            ]
        authority = dict(v16_authority or {})
        result = FactorWeightChangeService(self.overlay.db_path).execute(
            source="factor_governance_active_canary_restore",
            producer="factor_governance",
            run_id=str(run.get("run_id") or ""),
            actor="system:factor_governance",
            reason=f"restore evidence-qualified ACTIVE builtin canary: {factor_id}",
            factor_configs=self._portfolio_configs(cfg),
            current_weights=weights,
            weight_policy_weights={factor_id: profile.min_live_weight},
            fast=True,
            risk_check=lambda _plan, _verdict=verdict: _verdict,
            evidence_by_factor={factor_id: evidence},
            source_agent="factor_governance",
            v16_command_id=str(authority.get("command_id") or ""),
            v16_candidate_id=str(authority.get("candidate_id") or ""),
            v16_posterior_fingerprint=str(
                authority.get("posterior_fingerprint") or ""
            ),
            v16_evidence_fingerprint=str(
                authority.get("evidence_fingerprint") or ""
            ),
        )
        result_status = str(result.get("status") or "")
        applied = result_status == "applied"
        audit_status = (
            "applied"
            if applied
            else "blocked_by_directional_portfolio_guard"
            if result_status == "blocked_by_directional_portfolio_guard"
            else "mutation_failed"
            if result_status == "governance_error"
            else "blocked_by_evidence"
        )
        return [
            self._audit_action(
                run,
                item,
                "promote_factor",
                audit_status,
                evidence,
                verdict,
                before={"weight": evidence["old_weight"]},
                after={"weight": profile.min_live_weight if applied else evidence["old_weight"]},
                rollback={"weight": evidence["old_weight"]},
                result=result,
            )
        ]

    def _ensure_quarantine_review(
        self,
        item: dict[str, Any],
        *,
        disabled_at: float,
        cfg: Any,
    ) -> dict[str, Any]:
        """Guarantee a quarantined factor has a post-quarantine model verdict.

        The routine model sweep (score_samples) only covers factors that
        still produce trade reviews, so a quarantined factor's evidence stays
        frozen at the pre-quarantine verdict forever.  When no inference
        newer than the quarantine exists, run the latest model artifact over
        the factor's full historical review samples (mode=quarantine_review)
        and persist the fresh verdict.

        Returns {"reviewed": True, "weakness": ...} when a post-quarantine
        verdict exists (already in the audit trail or just written).
        Returns {"reviewed": False} when re-review is impossible (missing
        dependency, no historical samples) so callers keep the strict path
        and never widen risk on unverifiable evidence.
        """
        shadow = item.get("factor_governance_shadow") or {}
        latest_ts = float(shadow.get("latest_created_at") or 0.0)
        if latest_ts > 0.0 and latest_ts >= disabled_at:
            return {
                "reviewed": True,
                "weakness": float(shadow.get("weakness_score") or 0.0),
                "source": "latest_inference",
                "latest_created_at": latest_ts,
            }
        try:
            from research.factor_governance_lightgbm import (
                FactorGovernanceLightGBMService,
            )

            svc = FactorGovernanceLightGBMService(db_path=self.overlay.db_path)
            result = svc.re_review_quarantined_factor(
                factor=str(item.get("factor_id") or ""),
                mode="quarantine_review",
            )
        except Exception as exc:  # noqa: BLE001 - never crash the governance cycle
            return {
                "reviewed": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if not result.get("ok"):
            return {
                "reviewed": False,
                "error": str(result.get("error") or result.get("reason") or ""),
                "result": result,
            }
        return {
            "reviewed": True,
            "weakness": float(result.get("weakness") or 0.0),
            "source": "quarantine_review",
            "inference_id": str(result.get("inference_id") or ""),
            "count": int(result.get("count") or 0),
        }

    def _restore_quarantined_builtin_alpha(
        self,
        catalog: list[dict[str, Any]],
        run: dict[str, Any],
        *,
        cfg: Any | None = None,
        profile: FactorGovernanceProfile | None = None,
        v16_authority: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Create one new SHADOW generation for an evidence-qualified builtin."""

        cfg = cfg or runtime_config.shared()
        profile = profile or self._governance_profile(cfg)
        if not profile.balanced_demo:
            return []
        now = time.time()
        signal_cfg = dict(getattr(cfg, "factor_signal_config", {}) or {})
        candidates: list[dict[str, Any]] = []
        for item in catalog:
            factor_id = str(item.get("factor_id") or "")
            entry = signal_cfg.get(factor_id)
            if (
                not factor_id
                or not self._is_quarantined_builtin_lifecycle(item)
                or item.get("role") != "alpha"
                or not isinstance(entry, dict)
                or self._factor_has_pending_effect(factor_id)
            ):
                continue
            disabled_at = float(
                item.get("lifecycle_terminal_at")
                or entry.get("disabled_at")
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
            ):
                continue
            health_status = str(item.get("health_status") or "").upper()
            health_ok = (
                health_status in {"HEALTHY", "WATCH"}
                and float(item.get("health_score") or 0.0)
                >= profile.restore_min_health_score
                and int(item.get("health_n_obs") or 0) >= profile.restore_min_n_obs
            )
            # Batch re-review: a quarantined factor must be re-scored by a
            # model artifact newer than its quarantine before its evidence can
            # speak again.  The routine sweep never covers quarantined factors
            # (no new trade reviews), so stale pre-quarantine verdicts would
            # otherwise freeze the restore path forever.  This is the
            # automated replacement for a human re-evaluating the freeze.
            review = self._ensure_quarantine_review(
                item, disabled_at=disabled_at, cfg=cfg
            )
            fresh_weakness = (
                float(review.get("weakness") or 0.0)
                if review.get("reviewed")
                else None
            )
            model = self._model_governance_evidence(item, cfg)
            if fresh_weakness is not None:
                # New-model verdict wins: it can both acquit (weakness below
                # threshold -> eligible) and confirm (weakness above
                # threshold -> stay quarantined).
                if fresh_weakness >= profile.restore_max_weakness:
                    continue
                if float(item.get("health_score") or 0.0) < profile.hard_health_score:
                    # Re-entry still needs a live factor: health scored below
                    # the hard floor is not worth a new shadow generation.
                    continue
            else:
                # Re-review unavailable (dependency missing / no historical
                # samples): keep the strict path.  Only a healthy factor
                # whose old model evidence does not veto may come back; this
                # never widens risk on unverifiable evidence.
                if not health_ok:
                    continue
                # A strict model mutation gate is required to authorize a new
                # mutation, but an existing sufficiently sampled weak
                # observation remains a valid veto when fresh review is
                # unavailable.  Missing artifact metadata must not erase a
                # recorded strong-weakness quarantine signal.
                has_observation_evidence = (
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
                    has_observation_evidence
                    and observed_weakness >= profile.restore_max_weakness
                ):
                    continue
            candidates.append(
                {
                    **item,
                    "_model_governance": model,
                    "_quarantine_review": review,
                }
            )
        candidates.sort(
            key=lambda item: (
                -float(item.get("health_score") or 0.0),
                -int(item.get("health_n_obs") or 0),
                str(item.get("factor_id") or ""),
            )
        )
        if not candidates:
            return []
        item = candidates[0]
        factor_id = str(item["factor_id"])
        evidence = {
            "recovery_mode": "quarantined_builtin_new_shadow_generation",
            "health_score": float(item.get("health_score") or 0.0),
            "health_status": str(item.get("health_status") or "UNKNOWN"),
            "health_n_obs": int(item.get("health_n_obs") or 0),
            "model_governance": item.get("_model_governance") or {},
            "quarantine_review": item.get("_quarantine_review") or {},
            "governance_profile": profile.name,
        }
        verdict = self._risk("promote_factor", item, evidence)
        if not verdict.allowed:
            return [
                self._audit_action(
                    run,
                    item,
                    "promote_factor",
                    "blocked_by_risk",
                    evidence,
                    verdict,
                )
            ]
        authority = dict(v16_authority or {})
        binding = FactorV16Binding(
            command_id=str(authority.get("command_id") or ""),
            claim_token=str(authority.get("claim_token") or ""),
            target_agent=str(authority.get("target_agent") or "factor_governance"),
            candidate_id=str(authority.get("candidate_id") or ""),
            posterior_fingerprint=str(authority.get("posterior_fingerprint") or ""),
            evidence_fingerprint=str(authority.get("evidence_fingerprint") or ""),
        )
        result = FactorLifecycleService(
            self.overlay.db_path,
            adapter=RegistryAdapter.shared(),
        ).reenroll_quarantined_builtin(
            name=factor_id,
            actor="system:factor_governance",
            reason="evidence-qualified builtin re-enrollment",
            evidence_refs=evidence,
            idempotency_key=f"builtin_reenroll:{factor_id}:{run.get('run_id', '')}",
            v16=binding,
        )
        committed, projection_ready, mutation_status = self._mutation_commit_state(result)
        status = (
            "shadow_registered"
            if projection_ready
            else "projection_degraded"
            if committed
            else "blocked_by_evidence"
        )
        return [
            self._audit_action(
                run,
                item,
                "promote_factor",
                status,
                evidence,
                verdict,
                before={
                    "lifecycle_status": FactorLifecycleStage.QUARANTINED.value,
                    "generation": int(item.get("lifecycle_generation") or 1),
                },
                after={
                    "lifecycle_status": str(result.get("lifecycle_stage") or ""),
                    "generation": int(result.get("generation") or 0),
                },
                rollback={"target_stage": FactorLifecycleStage.QUARANTINED.value},
                result={
                    **dict(result or {}),
                    "mutation_status": mutation_status,
                    "durably_committed": committed,
                    "projection_ready": projection_ready,
                },
            )
        ]

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
        expression = str(item.get("lifecycle_expression") or "").strip()
        lifecycle_factor_id = str(item.get("lifecycle_factor_id") or "")
        definition_fingerprint = str(
            item.get("lifecycle_definition_fingerprint") or ""
        )
        artifact_hash = str(item.get("lifecycle_artifact_hash") or "").lower()
        if not expression or not lifecycle_factor_id or len(artifact_hash) != 64:
            return False
        try:
            return bool(
                lifecycle_factor_id == canonical_factor_id(expression)
                and definition_fingerprint
                == factor_definition_fingerprint(expression)
                and all(char in "0123456789abcdef" for char in artifact_hash)
            )
        except Exception:
            return False

    @classmethod
    def _is_dsl_promotion_lifecycle_candidate(
        cls,
        item: dict[str, Any],
    ) -> bool:
        """Use the durable lifecycle state, not the mutable catalog source."""

        stage = str(item.get("lifecycle_status") or "").upper()
        projection_ready = (
            stage == FactorLifecycleStage.SHADOW.value
            or str(item.get("runtime_admission") or "").lower()
            == "projection_acknowledged"
        )
        return bool(
            str(item.get("lifecycle_origin") or "").lower()
            in {"dsl", "shadow", "discovered"}
            and stage
            in {
                FactorLifecycleStage.SHADOW.value,
                FactorLifecycleStage.PROMOTION_PREPARED.value,
            }
            and projection_ready
            and cls._has_durable_shadow_lifecycle_identity(item)
        )

    @staticmethod
    def _is_quarantined_builtin_lifecycle(item: dict[str, Any]) -> bool:
        """Identify re-enrollment solely from the terminal lifecycle fact."""

        return bool(
            str(item.get("lifecycle_origin") or "").lower() == "builtin"
            and str(item.get("lifecycle_status") or "").upper()
            == FactorLifecycleStage.QUARANTINED.value
        )

    def _promotion_evidence(self, item: dict[str, Any], cfg: Any) -> dict[str, Any]:
        perf = item.get("shadow_perf") or {}
        oos_bars = int(perf.get("oos_bars") or 0)
        n_valid = int(perf.get("n_valid") or 0)
        cumulative_pnl = float(perf.get("cumulative_pnl") or 0.0)
        hit_rate = float(perf.get("hit_rate") or 0.0)
        max_drawdown = abs(float(perf.get("max_drawdown") or 0.0))
        health_score = float(item.get("health_score") or 0.0)
        health_status = str(item.get("health_status") or "UNKNOWN").upper()
        health_updated_at = float(item.get("health_updated_at") or 0.0)
        health_age_seconds = (
            time.time() - health_updated_at
            if health_updated_at > 0.0
            else float("inf")
        )
        canary_stage = str((item.get("canary") or {}).get("stage") or "").upper()
        min_oos = int(getattr(cfg, "factor_governance_shadow_min_oos_bars", 100) or 100)
        min_valid = int(getattr(cfg, "factor_governance_shadow_min_valid", 80) or 80)
        min_hit = float(getattr(cfg, "factor_governance_shadow_min_hit_rate", 0.5) or 0.5)
        max_dd = float(getattr(cfg, "factor_governance_shadow_max_drawdown", 0.05) or 0.05)
        watch = float(getattr(cfg, "factor_health_watch_threshold", 40.0) or 40.0)
        health_max_age_seconds = factor_governance_health_max_age_seconds(cfg)
        health_fresh = bool(
            health_updated_at > 0.0
            and health_age_seconds >= -5.0
            and health_age_seconds <= health_max_age_seconds
        )
        health_ok = bool(
            health_fresh
            and (
                health_status == "HEALTHY"
                or (
                    health_status == "WATCH"
                    and health_score >= watch
                )
            )
        )
        legacy_blockers = [
            code
            for code, blocked in (
                # Canary evidence ladder ends at PROBATION; the final
                # PROBATION -> ACTIVE hop requires committed ACTIVE backing in
                # factor_lifecycle_state (D1 gate).  Requiring canary == ACTIVE
                # here inverted the dependency: lifecycle activation IS the
                # backing producer, so preparation accepts PROBATION as
                # completed canary evidence.  Intermediate stages stay blocked.
                (
                    "bar_oos_canary_incomplete",
                    canary_stage not in {"ACTIVE", "PROBATION"},
                ),
                ("bar_oos_below_minimum", oos_bars < min_oos),
                ("bar_valid_samples_below_minimum", n_valid < min_valid),
                ("bar_oos_pnl_non_positive", cumulative_pnl <= 0.0),
                ("bar_oos_hit_rate_below_minimum", hit_rate < min_hit),
                ("bar_oos_drawdown_above_maximum", max_drawdown > max_dd),
                ("factor_health_invalid_or_stale", not health_ok),
            )
            if blocked
        ]
        legacy_blockers.extend(
            code
            for code, blocked in (
                ("factor_health_unknown", health_status == "UNKNOWN"),
                ("factor_health_decaying", health_status == "DECAYING"),
                ("factor_health_stale", not health_fresh),
                (
                    "factor_health_watch_below_threshold",
                    health_status == "WATCH" and health_score < watch,
                ),
            )
            if blocked
        )
        factor_id = str(item.get("factor_id") or "")
        admission = build_factor_admission_evidence(
            factor_id=factor_id,
            catalog_item=item,
            evidence_counts=self._factor_admission_evidence_counts(factor_id),
            governance={},
            health_max_age_seconds=health_max_age_seconds,
        )
        stage = str(item.get("lifecycle_status") or "").upper()
        eligibility_field = (
            "eligible_for_activation"
            if stage == FactorLifecycleStage.PROMOTION_PREPARED.value
            else "eligible_for_preparation"
        )
        admission_blocker_field = (
            "activation_blocker_codes"
            if eligibility_field == "eligible_for_activation"
            else "preflight_blocker_codes"
        )
        blockers = sorted(
            set(
                legacy_blockers
                + list(admission.get(admission_blocker_field) or [])
            )
        )
        eligible = bool(admission.get(eligibility_field) is True and not blockers)
        return {
            "eligible": eligible,
            "eligibility_field": eligibility_field,
            "admission_evidence": admission,
            "oos_bars": oos_bars,
            "n_valid": n_valid,
            "cumulative_pnl": cumulative_pnl,
            "hit_rate": hit_rate,
            "max_drawdown": max_drawdown,
            "health_score": health_score,
            "health_status": health_status,
            "health_updated_at": health_updated_at,
            "health_age_seconds": health_age_seconds,
            "health_fresh": health_fresh,
            "canary_stage": canary_stage,
            "blocker_codes": blockers,
            "thresholds": {
                "min_oos_bars": min_oos,
                "min_valid": min_valid,
                "min_hit_rate": min_hit,
                "max_drawdown": max_dd,
                "watch_health": watch,
                "health_max_age_seconds": health_max_age_seconds,
            },
        }

    @staticmethod
    def _unavailable_admission_evidence_counts() -> dict[str, Any]:
        return {
            "decision_observations": None,
            "factor_linked_trade_reviews": None,
            "governance_eligible_mature": None,
            "contaminated_or_ineligible": None,
            "effects_observed": None,
            "status": "unavailable",
        }

    def _prime_admission_evidence_count_cache(
        self,
        factor_ids: list[str],
    ) -> None:
        """Read admission counts in one projection for this governance cycle.

        ``factor_evidence_summary`` already returns an independent projection
        per factor.  Calling it in chunks caused the provider to rescan the
        complete canonical learning history once per chunk.  Keep the result
        run-scoped and keyed by factor ID; never persist or reuse it as a
        cross-cycle governance verdict.
        """
        ids = list(dict.fromkeys(str(item) for item in factor_ids if str(item)))
        if not ids:
            return
        unavailable = self._unavailable_admission_evidence_counts()
        try:
            from research.features.feature_provider import LearningFeatureProvider

            provider = LearningFeatureProvider(str(self.overlay.db_path))
        except Exception:
            for factor_id in ids:
                self._admission_evidence_count_cache[factor_id] = dict(unavailable)
            return

        try:
            summary = provider.factor_evidence_summary(ids)
        except Exception:
            summary = {}
        for factor_id in ids:
            value = summary.get(factor_id) if isinstance(summary, Mapping) else None
            self._admission_evidence_count_cache[factor_id] = (
                dict(value) if isinstance(value, Mapping) else dict(unavailable)
            )

    def _factor_admission_evidence_counts(
        self,
        factor_id: str,
    ) -> dict[str, Any]:
        if factor_id in self._admission_evidence_count_cache:
            return dict(self._admission_evidence_count_cache[factor_id])
        unavailable = self._unavailable_admission_evidence_counts()
        try:
            from research.features.feature_provider import LearningFeatureProvider

            summary = LearningFeatureProvider(
                str(self.overlay.db_path)
            ).factor_evidence_summary([factor_id])
            resolved = dict(summary.get(factor_id) or unavailable)
        except Exception:
            resolved = unavailable
        self._admission_evidence_count_cache[factor_id] = dict(resolved)
        return resolved

    def _shadow_score(self, item: dict[str, Any]) -> float:
        perf = item.get("shadow_perf") or {}
        return (
            float(perf.get("cumulative_pnl") or 0.0)
            + 0.01 * float(perf.get("hit_rate") or 0.0)
            - abs(float(perf.get("max_drawdown") or 0.0))
        )

    def _current_market_regime_projection(self) -> dict[str, Any]:
        """Read-only projection of the current market regime (batch B).

        Single fact owner: consumes `experience_memory.regime_id` (the only
        persisted regime label source) and resolves a low-cardinality regime
        via market_regime.project_current_market_regime().  No new writer,
        no new table.  Returns an `unavailable` projection when no data.
        """
        try:
            from backend.services.market_regime import project_current_market_regime

            db_path = self.overlay.db_path
            production_state = is_state_db_path(db_path)
            if not production_state and not Path(db_path).exists():
                return {
                    "regime_id": "",
                    "confidence": 0.0,
                    "source": "unavailable",
                    "dimensions": {},
                }
            conn = (
                get_state_pg_conn(read_only=True)
                if production_state
                else connect_sqlite(db_path, read_only=True)
            )
            if not production_state:
                conn.row_factory = sqlite3.Row
            try:
                if not state_table_exists(conn, "experience_memory"):
                    return {
                        "regime_id": "",
                        "confidence": 0.0,
                        "source": "unavailable",
                        "dimensions": {},
                    }
                rows = conn.execute(
                    """
                    SELECT regime_id, created_at, trade_id
                    FROM experience_memory
                    WHERE regime_id IS NOT NULL AND regime_id <> ''
                    ORDER BY created_at DESC
                    LIMIT 15
                    """
                ).fetchall()
                experience_rows = [
                    {
                        "regime_id": str(row["regime_id"] or ""),
                        "created_at": float(row["created_at"] or 0.0),
                        "trade_id": str(row["trade_id"] or ""),
                    }
                    for row in rows
                ]
                projection = project_current_market_regime(experience_rows)
                return projection
            finally:
                conn.close()
        except Exception:
            return {
                "regime_id": "",
                "confidence": 0.0,
                "source": "unavailable",
                "dimensions": {},
            }

    @staticmethod
    def _regime_mismatch_verdict(
        model_weak: bool,
        *,
        current_regime_id: str,
        regime_fit_score: float | None,
        regime_fit_ok_threshold: float = 0.5,
    ) -> dict[str, Any]:
        """Batch C: decide whether a globally-weak factor should pause downweight.

        A factor that the model calls weak globally may still fit the *current*
        market regime well (batch-A regime features make the model regime-aware;
        the factor's shadow payload carries `current_regime_fit_score`).  When
        the current-regime fit is good, global weakness is likely concentrated
        in other regimes -> mark `regime_mismatch` and leave the weight alone
        ("not suited to today's market" is not "never usable again").

        Fail-safe: missing current regime projection or missing fit evidence
        never erodes safety - both fall through to the existing global-weak
        downweight path.
        """
        if not model_weak:
            return {"regime_mismatch": False, "reason": "not_globally_weak"}
        if not current_regime_id:
            return {"regime_mismatch": False, "reason": "no_current_regime"}
        if regime_fit_score is None:
            return {"regime_mismatch": False, "reason": "no_regime_fit_evidence"}
        fit = float(regime_fit_score)
        if fit >= float(regime_fit_ok_threshold):
            return {
                "regime_mismatch": True,
                "reason": "current_regime_fit_ok",
                "current_regime_id": current_regime_id,
                "regime_fit_score": round(fit, 4),
            }
        return {
            "regime_mismatch": False,
            "reason": "current_regime_weak_too",
            "current_regime_id": current_regime_id,
            "regime_fit_score": round(fit, 4),
        }

    @staticmethod
    def _shadow_regime_fit_score(item: dict[str, Any]) -> float | None:
        """Extract the factor's current-regime conditional performance.

        Batch-F features are stored per-inference in
        `factor_governance_shadow_audit.payload_json.features`; the catalog
        projection keeps the newest inference per factor.

        Priority:
          1. `same_regime_positive_rate` — factor x regime conditional win rate
             (aggregated over the factor's own history in the current regime,
             from canonical_v2 factor events joined by decision lineage; distinguishes
             factors that fit today's market from those that don't).
          2. `current_regime_fit_score` — trade-level fallback (shared by all
             factors of the same trade, pre-Batch-F schema).

        Returns None when the evidence is missing or unparseable (callers
        fail open/fail safe as documented per gate).
        """
        try:
            shadow_payload = (item.get("factor_governance_shadow") or {}).get("payload") or {}
            shadow_features = shadow_payload.get("features") or {}
            if not isinstance(shadow_features, dict):
                return None
            candidate = shadow_features.get("same_regime_positive_rate")
            if candidate is None:
                candidate = shadow_features.get("current_regime_fit_score")
            if candidate is None:
                return None
            value = float(candidate)
            if math.isnan(value) or math.isinf(value):
                return None
            return value
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _regime_suitable_for_restore(
        *,
        current_regime_id: str,
        regime_fit_score: float | None,
        regime_fit_ok_threshold: float = 0.5,
    ) -> dict[str, Any]:
        """Batch D: second gate for zero-weight/quarantined restore candidates.

        A factor that was downweighted/frozen may become usable again when the
        market regime switches and the factor now fits the *current* regime.
        Restore is only allowed while the current-regime fit clears the
        threshold ("not suited to yesterday's market" is not "never usable").

        Fail-open on missing regime projection or missing fit evidence so the
        new gate never silently starves the existing restore path (the
        posterior guard already covers "this restore lost money last time" -
        these two gates are orthogonal).
        """
        if not current_regime_id:
            return {"suitable": True, "reason": "no_current_regime"}
        if regime_fit_score is None:
            return {"suitable": True, "reason": "no_regime_fit_evidence"}
        fit = float(regime_fit_score)
        if fit >= float(regime_fit_ok_threshold):
            return {
                "suitable": True,
                "reason": "current_regime_fit_ok",
                "current_regime_id": current_regime_id,
                "regime_fit_score": round(fit, 4),
            }
        return {
            "suitable": False,
            "reason": "current_regime_weak_too",
            "current_regime_id": current_regime_id,
            "regime_fit_score": round(fit, 4),
        }

    def _model_governance_evidence(self, item: dict[str, Any], cfg: Any) -> dict[str, Any]:
        shadow = item.get("factor_governance_shadow") or {}
        result = shadow.get("result") or {}
        promotion_gate = result.get("promotion_gate") or shadow.get("promotion_gate") or {}
        model_type = str(
            shadow.get("model_type") or result.get("model_type") or ""
        )
        sample_count = int(shadow.get("sample_count") or 0)
        weak_sample_count = int(shadow.get("weak_sample_count") or 0)
        latest_weakness = float(shadow.get("weakness_score") or item.get("model_weakness_score") or 0.0)
        avg_weakness = float(shadow.get("avg_weakness_score") or latest_weakness or 0.0)
        latest_positive = float(shadow.get("positive_score") or item.get("model_positive_score") or 0.0)
        avg_positive = float(shadow.get("avg_positive_score") or latest_positive or 0.0)
        min_samples = int(getattr(cfg, "factor_governance_model_min_samples", 3) or 3)
        min_factor_samples = int(
            getattr(cfg, "factor_governance_model_min_factor_samples", 20) or 20
        )
        down_th = float(getattr(cfg, "factor_governance_model_weakness_threshold", 0.65) or 0.65)
        disable_th = float(getattr(cfg, "factor_governance_model_disable_threshold", 0.85) or 0.85)
        promotion_gate_passed = bool(promotion_gate.get("passed"))
        factor_coverage_ready = sample_count >= min_factor_samples
        mutation_eligible = (
            model_type == "factor_governance_lightgbm"
            and promotion_gate_passed
            and result.get("mutation_eligible") is True
            and factor_coverage_ready
        )
        enough = mutation_eligible and (
            sample_count >= min_samples or weak_sample_count >= min_samples
        )
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
            "min_factor_samples": min_factor_samples,
            "factor_coverage_ready": factor_coverage_ready,
            "promotion_gate_passed": promotion_gate_passed,
            "promotion_gate_reason": str(
                promotion_gate.get("reason") or "promotion_gate_not_passed"
            ),
            "mutation_eligible": mutation_eligible,
            "artifact_sha256": str(
                result.get("artifact_sha256") or shadow.get("artifact_sha256") or ""
            ),
            "factor_generation": str(
                result.get("factor_generation") or shadow.get("factor_generation") or ""
            ),
            "lineage_hash": str(
                result.get("lineage_hash") or shadow.get("lineage_hash") or ""
            ),
            "downweight_threshold": down_th,
            "disable_threshold": disable_th,
            "weak_for_downweight": weak_for_downweight,
            "weak_for_disable": weak_for_disable,
            "latest_inference_id": str(shadow.get("latest_inference_id") or ""),
            "model_type": model_type,
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

    @staticmethod
    def _scope_audit_config(
        payload: dict[str, Any] | None,
        factor_id: str,
    ) -> dict[str, Any]:
        """Project an audit ``before/after/rollback`` config to factor scope.

        Historical behaviour embedded the full runtime configuration (a
        ~300 KB JSON copy of all 500+ factors) into every audit decision's
        before/after/rollback, tripling storage per action.  The only
        consumers of these audit configs are factor-scoped: the rollback
        reader (:meth:`_scoped_factor_rollback_patch`) reads
        ``factor_signal_config[factor_id]`` and the weight map entry for the
        same factor.  The complete configuration is already persisted once,
        content-addressed by hash, via ``persist_runtime_config_snapshot``
        below, so the full copy here was redundant.
        """

        if not isinstance(payload, dict) or not factor_id:
            return deepcopy(payload) if isinstance(payload, dict) else {}
        cfg = payload.get("runtime_config")
        if not isinstance(cfg, Mapping):
            return deepcopy(payload)
        scoped = {
            key: deepcopy(value) for key, value in payload.items() if key != "runtime_config"
        }
        runtime_scoped = {
            key: deepcopy(value)
            for key, value in cfg.items()
            if key not in {"factor_signal_config", "factor_portfolio_weights"}
        }
        signal_cfg = cfg.get("factor_signal_config")
        weights_cfg = cfg.get("factor_portfolio_weights")
        if isinstance(signal_cfg, Mapping):
            runtime_scoped["factor_signal_config"] = (
                {factor_id: deepcopy(signal_cfg.get(factor_id) or {})}
                if factor_id in signal_cfg
                else {}
            )
        if isinstance(weights_cfg, Mapping):
            runtime_scoped["factor_portfolio_weights"] = {
                factor_id: deepcopy(weights_cfg.get(factor_id))
            }
        if runtime_scoped or signal_cfg is not None or weights_cfg is not None:
            scoped["runtime_config"] = runtime_scoped
        return scoped

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
        writer = self._active_audit_writer
        if writer is None or writer.run_id != str(run.get("run_id") or ""):
            writer = _GovernanceCycleAuditWriter(
                run,
                runtime_config.shared(),
                db_path=self.overlay.db_path,
            )
            # Direct callers (including replay/diagnostic paths) still get the
            # same run-scoped writer; the normal cycle clears it in ``finally``.
            self._active_audit_writer = writer
        factor_id = str(item.get("factor_id") or "")
        before_scoped = self._scope_audit_config(before, factor_id)
        after_scoped = self._scope_audit_config(after, factor_id)
        rollback_scoped = self._scope_audit_config(rollback, factor_id)
        snapshot = writer.snapshot()
        decision_id, audit_reused = writer.decision(
            factor_id=factor_id,
            action=action,
            status=status,
            evidence=evidence,
            risk_verdict=verdict.to_dict(),
            before=before_scoped,
            after=after_scoped,
            rollback=rollback_scoped,
            result=result or {},
            config_snapshot=snapshot,
        )
        if audit_reused:
            return {
                "factor_id": factor_id,
                "action": action,
                "status": status,
                "decision_id": decision_id,
                "suggestion_id": "",
                "audit_reused": True,
                "risk": verdict.to_dict(),
            }
        suggestion_id = self._record_policy_suggestion(factor_id, action, status, evidence, decision_id)
        self._record_learning_application(
            factor_id,
            action,
            status,
            suggestion_id,
            before_scoped,
            after_scoped,
            result,
            decision_id=decision_id,
        )
        return {
            "factor_id": factor_id,
            "action": action,
            "status": status,
            "decision_id": decision_id,
            "suggestion_id": suggestion_id,
            "audit_reused": False,
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
        suggestion_id = deterministic_policy_suggestion_id(
            writer="factor_governance",
            scope_type="factor",
            scope_key=factor_id,
            action=action,
            evidence=evidence_payload,
            status=status,
            qualification_fingerprint="",
            prefix="fgv4",
        )
        conn = get_state_pg_conn()
        try:
            conn.execute(
                _p("""
                INSERT INTO policy_suggestion
                (suggestion_id, scope_type, scope_key, action, confidence, reason,
                 evidence_json, status, reviewed_at, review_note, created_at)
                VALUES (?, 'factor', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(suggestion_id) DO NOTHING
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
        LearningApplicationStore(str(self.overlay.db_path)).prepare_application(
            scope_type="factor",
            scope_key=str(factor_id),
            action=str(action),
            status=str(status or "applied"),
            bias_multiplier=1.0,
            old_weight=old_weight,
            new_weight=new_weight,
            suggestion_ids=[suggestion_id] if suggestion_id else [],
            cycle_ts=now,
            details=details,
        )

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
