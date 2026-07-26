from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from backend.core.db import STATE_DB
from backend.services.autonomous_learning import (
    _approve_demo_policy_suggestions,
    _auto_apply_parameter_template_suggestions,
    _auto_apply_position_supervisor_template_suggestions,
    _auto_release_parameter_template_candidates,
    _auto_rollback_position_supervisor_template,
    _connect,
    _demo_autonomous_enabled,
    _execute,
    _new_experiment_id,
    _sync_factor_weights_for_demo,
    ensure_autonomous_learning_tables,
    materialize_entry_quality_governance_suggestions,
)
from backend.services.entry_quality_governance import EntryQualityGovernanceService
from backend.services.evolution_ledger import finish_evolution_run, start_evolution_run
from backend.services.governance_eligibility import GOVERNANCE_ELIGIBILITY_VERSION
from backend.services.v16_command_gate import V16CommandGate


class AutonomousDemoApplyStepper:
    """Explicit single-step wrapper around the existing demo apply chain."""

    STEP_ORDER = [
        "entry_quality_materialize",
        "factor_pruning_materialize",
        "factor_pruning_promote",
        "factor_pruning_bridge",
        "factor_pruning_governance",
        "dispatch_v16_delegation",
        "governor_review",
        "resolve_conflicts",
        "apply_entry_quality_control",
        "sync_factor_weights",
        "apply_parameter_templates",
        "release_parameter_candidates",
        "apply_supervisor_templates",
        "rollback_supervisor_templates",
    ]

    DEFAULT_LIMITS = {
        "entry_quality_materialize": 1,
        "factor_pruning_materialize": 1,
        "factor_pruning_promote": 1,
        "factor_pruning_bridge": 1,
        "factor_pruning_governance": 2,
        "dispatch_v16_delegation": 1,
        "governor_review": 5,
        "resolve_conflicts": 20,
        "apply_entry_quality_control": 1,
        "sync_factor_weights": 1,
        "apply_parameter_templates": 2,
        "release_parameter_candidates": 1,
        "apply_supervisor_templates": 1,
        "rollback_supervisor_templates": 1,
    }

    MUTATING_STEPS = set(STEP_ORDER)

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "autonomous_demo_apply_stepper_boundary.v1",
            "wraps_existing_demo_apply_steps": True,
            "single_step_only": True,
            "background_step_supported": True,
            "does_not_submit_orders": True,
            "does_not_bypass_risk_policy": True,
            "does_not_bypass_decision_policy": True,
            "mutating_steps_require_confirm_step": True,
            "system_demo_actor_auto_confirms": True,
            "full_learning_cycle_not_run_here": True,
        }

    def plan(self) -> dict[str, Any]:
        mode = self._autonomy_mode()
        pending = self._pending_counts()
        steps = []
        for name in self.STEP_ORDER:
            steps.append(
                {
                    "step": name,
                    "default_limit": self.DEFAULT_LIMITS[name],
                    "mutating": name in self.MUTATING_STEPS,
                    "requires_confirm_step": name in self.MUTATING_STEPS,
                    "pending_count": int(pending.get(name, 0) or 0),
                    "execution_profile": self._execution_profile(name),
                    "recommended": self._recommended(name, pending),
                }
            )
        return {
            "ok": True,
            "schema_version": "autonomous_demo_apply_plan.v1",
            "mode": mode,
            "enabled": bool(_demo_autonomous_enabled()),
            "steps": steps,
            "pending": pending,
            "entry_quality": EntryQualityGovernanceService(self.db_path).status(),
            "v16_commands": self._v16_command_status(),
            "generated_at": time.time(),
            "boundary": self.boundary(),
        }

    def _v16_command_status(self) -> dict[str, Any]:
        try:
            from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService

            status = V16BrainOrchestratorService(self.db_path).status(limit=50)
            return {
                "actionable_count": int(status.get("actionable_command_count") or 0),
                "cancelled_count": int(status.get("cancelled_command_count") or 0),
                "oldest_actionable_age_seconds": float(
                    status.get("oldest_actionable_age_seconds") or 0.0
                ),
            }
        except Exception as exc:
            return {
                "actionable_count": 0,
                "cancelled_count": 0,
                "oldest_actionable_age_seconds": 0.0,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def run_step(
        self,
        step: str,
        *,
        limit: int | None = None,
        confirm_step: bool = False,
        actor: str = "api:ops.autonomous_demo_apply_stepper",
    ) -> dict[str, Any]:
        step = str(step or "").strip()
        if step not in self.STEP_ORDER:
            return {
                "ok": False,
                "schema_version": "autonomous_demo_apply_step.v1",
                "status": "rejected",
                "error": "unknown_step",
                "known_steps": self.STEP_ORDER,
                "boundary": self.boundary(),
            }
        if step in self.MUTATING_STEPS and not confirm_step and not self._is_system_demo_actor(actor):
            return {
                "ok": False,
                "schema_version": "autonomous_demo_apply_step.v1",
                "status": "confirmation_required",
                "step": step,
                "error": "confirm_step_required",
                "boundary": self.boundary(),
            }
        if not _demo_autonomous_enabled():
            return {
                "ok": True,
                "schema_version": "autonomous_demo_apply_step.v1",
                "status": "skipped",
                "step": step,
                "enabled": False,
                "mode": self._autonomy_mode(),
                "boundary": self.boundary(),
            }

        limit_value = max(1, min(int(limit or self.DEFAULT_LIMITS[step]), 100))
        ensure_autonomous_learning_tables(self.db_path)
        experiment_id = _new_experiment_id()
        execution_context = self._step_execution_context(
            step,
            limit=limit_value,
            actor=actor,
            experiment_id=experiment_id,
            run_id=experiment_id,
            background=False,
        )
        run = start_evolution_run(
            run_type=f"demo_autonomy_apply_step:{step}",
            trigger_source=actor,
            db_path=self.db_path,
            run_id=experiment_id,
            summary={"step": step, "limit": limit_value, "actor": actor, "execution_context": execution_context},
        )
        run_id = str(run.get("run_id") or experiment_id)
        execution_context = self._with_run_refs(execution_context, run_id=run_id)
        started_at = time.time()
        try:
            result = self._execute_step(step, experiment_id=experiment_id, run_id=run_id, limit=limit_value)
            payload = {
                "ok": True,
                "schema_version": "autonomous_demo_apply_step.v1",
                "status": "completed",
                "step": step,
                "limit": limit_value,
                "experiment_id": experiment_id,
                "run_id": run_id,
                "execution_context": self._with_result_refs(execution_context, result),
                "result": result,
                "started_at": started_at,
                "finished_at": time.time(),
                "boundary": self.boundary(),
            }
            finish_evolution_run(run_id, status="completed", summary=payload, db_path=self.db_path)
            return payload
        except Exception as exc:
            payload = {
                "ok": False,
                "schema_version": "autonomous_demo_apply_step.v1",
                "status": "error",
                "step": step,
                "limit": limit_value,
                "experiment_id": experiment_id,
                "run_id": run_id,
                "execution_context": execution_context,
                "error": f"{type(exc).__name__}: {exc}",
                "started_at": started_at,
                "finished_at": time.time(),
                "boundary": self.boundary(),
            }
            finish_evolution_run(run_id, status="error", summary=payload, db_path=self.db_path)
            return payload

    def start_background_step(
        self,
        step: str,
        *,
        limit: int | None = None,
        confirm_step: bool = False,
        actor: str = "api:ops.autonomous_demo_apply_step",
    ) -> dict[str, Any]:
        step = str(step or "").strip()
        if step not in self.STEP_ORDER:
            return {
                "ok": False,
                "schema_version": "autonomous_demo_apply_step.v1",
                "status": "rejected",
                "error": "unknown_step",
                "known_steps": self.STEP_ORDER,
                "boundary": self.boundary(),
            }
        if step in self.MUTATING_STEPS and not confirm_step and not self._is_system_demo_actor(actor):
            return {
                "ok": False,
                "schema_version": "autonomous_demo_apply_step.v1",
                "status": "confirmation_required",
                "step": step,
                "error": "confirm_step_required",
                "boundary": self.boundary(),
            }
        if not _demo_autonomous_enabled():
            return {
                "ok": True,
                "schema_version": "autonomous_demo_apply_step.v1",
                "status": "skipped",
                "step": step,
                "enabled": False,
                "mode": self._autonomy_mode(),
                "boundary": self.boundary(),
            }

        limit_value = max(1, min(int(limit or self.DEFAULT_LIMITS[step]), 100))
        ensure_autonomous_learning_tables(self.db_path)
        experiment_id = _new_experiment_id()
        execution_context = self._step_execution_context(
            step,
            limit=limit_value,
            actor=actor,
            experiment_id=experiment_id,
            run_id=experiment_id,
            background=True,
        )
        run = start_evolution_run(
            run_type=f"demo_autonomy_apply_step:{step}",
            trigger_source=actor,
            db_path=self.db_path,
            run_id=experiment_id,
            summary={
                "step": step,
                "limit": limit_value,
                "actor": actor,
                "status": "accepted",
                "background": True,
                "execution_context": execution_context,
            },
        )
        run_id = str(run.get("run_id") or experiment_id)
        execution_context = self._with_run_refs(execution_context, run_id=run_id)
        return {
            "ok": True,
            "schema_version": "autonomous_demo_apply_step.v1",
            "status": "accepted",
            "step": step,
            "limit": limit_value,
            "experiment_id": experiment_id,
            "run_id": run_id,
            "actor": actor,
            "background": True,
            "execution_context": execution_context,
            "boundary": self.boundary(),
        }

    def run_accepted_step(
        self,
        *,
        step: str,
        experiment_id: str,
        run_id: str,
        limit: int,
    ) -> dict[str, Any]:
        started_at = time.time()
        execution_context = self._step_execution_context(
            step,
            limit=limit,
            actor="system:autonomous_demo_apply_worker",
            experiment_id=experiment_id,
            run_id=run_id,
            background=True,
        )
        try:
            result = self._execute_step(step, experiment_id=experiment_id, run_id=run_id, limit=limit)
            payload = {
                "ok": True,
                "schema_version": "autonomous_demo_apply_step.v1",
                "status": "completed",
                "step": step,
                "limit": limit,
                "experiment_id": experiment_id,
                "run_id": run_id,
                "background": True,
                "execution_context": self._with_result_refs(execution_context, result),
                "result": result,
                "started_at": started_at,
                "finished_at": time.time(),
                "boundary": self.boundary(),
            }
            finish_evolution_run(run_id, status="completed", summary=payload, db_path=self.db_path)
            return payload
        except Exception as exc:
            payload = {
                "ok": False,
                "schema_version": "autonomous_demo_apply_step.v1",
                "status": "error",
                "step": step,
                "limit": limit,
                "experiment_id": experiment_id,
                "run_id": run_id,
                "background": True,
                "execution_context": execution_context,
                "error": f"{type(exc).__name__}: {exc}",
                "started_at": started_at,
                "finished_at": time.time(),
                "boundary": self.boundary(),
            }
            finish_evolution_run(run_id, status="error", summary=payload, db_path=self.db_path)
            return payload

    def _step_execution_context(
        self,
        step: str,
        *,
        limit: int,
        actor: str,
        experiment_id: str,
        run_id: str,
        background: bool,
    ) -> dict[str, Any]:
        try:
            plan = self.plan()
            step_info = next((item for item in plan.get("steps", []) if item.get("step") == step), {})
            pending = dict(plan.get("pending") or {})
        except Exception as exc:
            step_info = {}
            pending = {}
            plan = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        pending_count = int(step_info.get("pending_count") or pending.get(step, 0) or 0)
        recommended = bool(step_info.get("recommended"))
        selected_reason = "recommended_pending_step" if recommended else "system_or_worker_selected_step"
        if pending_count <= 0:
            selected_reason = "system_or_worker_selected_no_pending_snapshot"

        return {
            "schema_version": "autonomous_demo_apply_step_execution_context.v1",
            "step": step,
            "actor": actor,
            "limit": max(1, min(int(limit or self.DEFAULT_LIMITS.get(step, 1)), 100)),
            "background": bool(background),
            "experiment_id": experiment_id,
            "run_id": run_id,
            "selected_reason": selected_reason,
            "recommended": recommended,
            "pending_count": pending_count,
            "execution_profile": step_info.get("execution_profile") or self._execution_profile(step),
            "plan_schema_version": plan.get("schema_version", "autonomous_demo_apply_plan.v1"),
            "posterior_monitor": {
                "schema_version": "autonomous_demo_apply_step_posterior_monitor.v1",
                "primary_reader": "RuleEvolutionGovernor.reconcile_application_effects",
                "scorecard_reader": "AgentScorecardService.latest_trade_attributions",
                "proposal_reader": "ProposalRegistryService.status",
                "watch_tables": [
                    "learning_application_log",
                    "learning_application_effect",
                    "trade_outcome_review",
                    "experience_memory",
                    "proposal_registry",
                    "evolution_run",
                ],
            },
            "rollback_refs": {
                "schema_version": "autonomous_demo_apply_step_rollback_refs.v1",
                "run_id": run_id,
                "experiment_id": experiment_id,
                "decision_sources": [
                    "evolution_run.summary",
                    "learning_application_log.details_json",
                    "learning_application_effect",
                    "policy_suggestion.review_note",
                    "runtime_config_snapshot",
                ],
            },
        }

    @staticmethod
    def _with_run_refs(execution_context: dict[str, Any], *, run_id: str) -> dict[str, Any]:
        context = dict(execution_context)
        context["run_id"] = run_id
        rollback = dict(context.get("rollback_refs") or {})
        rollback["run_id"] = run_id
        context["rollback_refs"] = rollback
        return context

    @staticmethod
    def _with_result_refs(execution_context: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        context = dict(execution_context)
        refs: dict[str, Any] = {}
        for key in (
            "application_id",
            "application_ids",
            "suggestion_id",
            "suggestion_ids",
            "candidate_id",
            "candidate_ids",
            "proposal_id",
            "proposal_ids",
            "superseded",
            "remaining_superseded",
        ):
            if key in result:
                refs[key] = result.get(key)
        if refs:
            context["result_refs"] = refs
        return context

    def _execute_step(self, step: str, *, experiment_id: str, run_id: str, limit: int) -> dict[str, Any]:
        handlers: dict[str, Callable[[], dict[str, Any]]] = {
            "entry_quality_materialize": lambda: materialize_entry_quality_governance_suggestions(
                db_path=self.db_path,
                limit=max(100, min(int(limit) * 1000, 5000)),
            ),
            "factor_pruning_materialize": lambda: self._run_factor_pruning_materialize(limit=limit),
            "factor_pruning_promote": lambda: self._run_factor_pruning_promote(limit=limit),
            "factor_pruning_bridge": lambda: self._run_factor_pruning_bridge(limit=limit),
            "factor_pruning_governance": lambda: self._run_factor_pruning_governance(limit=limit),
            "dispatch_v16_delegation": lambda: self._run_dispatch_v16_delegation(),
            "governor_review": lambda: self._run_governor_review(experiment_id=experiment_id, run_id=run_id, limit=limit),
            "resolve_conflicts": lambda: self._run_resolve_conflicts(limit=limit),
            "apply_entry_quality_control": lambda: self._run_apply_entry_quality_control(
                run_id=run_id
            ),
            "sync_factor_weights": lambda: _sync_factor_weights_for_demo(experiment_id=experiment_id),
            "apply_parameter_templates": lambda: _auto_apply_parameter_template_suggestions(
                db_path=self.db_path,
                experiment_id=experiment_id,
                limit=limit,
            ),
            "release_parameter_candidates": lambda: _auto_release_parameter_template_candidates(
                db_path=self.db_path,
                experiment_id=experiment_id,
                limit=limit,
            ),
            "apply_supervisor_templates": lambda: _auto_apply_position_supervisor_template_suggestions(
                db_path=self.db_path,
                experiment_id=experiment_id,
                limit=limit,
                run_id=run_id,
            ),
            "rollback_supervisor_templates": lambda: _auto_rollback_position_supervisor_template(
                db_path=self.db_path,
                experiment_id=experiment_id,
                run_id=run_id,
            ),
        }
        return handlers[step]()

    def _run_apply_entry_quality_control(self, *, run_id: str) -> dict[str, Any]:
        service = EntryQualityGovernanceService(self.db_path)
        suggestion = dict(service.status().get("current_suggestion") or {})
        if suggestion:
            suggestion["governance_eligible"] = bool(
                str(suggestion.get("status") or "") == "approved"
                and str(
                    suggestion.get("governance_eligibility_fingerprint") or ""
                )
            )
            from backend.services.v16_brain_orchestrator import (
                V16BrainOrchestratorService,
            )

            delegated = V16BrainOrchestratorService(
                self.db_path
            ).delegate_entry_quality_control(suggestion, persist=True)
            if not delegated.get("ok"):
                return {
                    "ok": False,
                    "status": "blocked_by_v16_entry_quality_gate",
                    "v16_delegation": delegated,
                }
        else:
            delegated = {
                "ok": True,
                "status": "skipped_no_current_entry_quality_suggestion",
            }
        result = service.apply_next_weak_signal(
            run_id=run_id,
            actor="system:autonomous_demo_apply_stepper.entry_quality",
        )
        return {**result, "v16_delegation": delegated}

    def _factor_pruning_service(self):
        from backend.services.factor_pruning_governance import FactorPruningGovernanceService

        return FactorPruningGovernanceService(self.db_path)

    def _run_factor_pruning_materialize(self, *, limit: int) -> dict[str, Any]:
        service = self._factor_pruning_service()
        step_limit = max(1, min(int(limit), 20))
        materialize = service.materialize_latest(limit=step_limit, min_priority=0.75, persist=True)
        return {
            "schema_version": "demo_nursery_factor_pruning_materialize_step.v1",
            "limit": step_limit,
            "materialize": materialize,
        }

    def _run_factor_pruning_promote(self, *, limit: int) -> dict[str, Any]:
        service = self._factor_pruning_service()
        step_limit = max(1, min(int(limit), 20))
        promote = service.promote_ready(limit=step_limit, min_evidence_score=0.9, require_weak_health=True)
        return {
            "schema_version": "demo_nursery_factor_pruning_promote_step.v1",
            "limit": step_limit,
            "promote": promote,
        }

    def _run_factor_pruning_bridge(self, *, limit: int) -> dict[str, Any]:
        service = self._factor_pruning_service()
        step_limit = max(1, min(int(limit), 20))
        bridge = service.bridge_ready_candidates(
            limit=step_limit,
            require_demo_nursery=True,
            actor="system:autonomous_demo_apply_stepper.factor_pruning",
            review_missing=False,
            preview_before_submit=False,
        )
        return {
            "schema_version": "demo_nursery_factor_pruning_bridge_step.v1",
            "limit": step_limit,
            "bridge": bridge,
        }

    def _run_factor_pruning_governance(self, *, limit: int) -> dict[str, Any]:
        step_limit = max(1, min(int(limit), 20))
        promote = self._run_factor_pruning_promote(limit=step_limit)
        bridge = self._run_factor_pruning_bridge(limit=step_limit)
        return {
            "schema_version": "demo_nursery_factor_pruning_governance_step.v2",
            "limit": step_limit,
            "materialize": {"status": "skipped", "reason": "materialize_is_explicit_factor_pruning_materialize_step"},
            "promote": promote.get("promote", promote),
            "bridge": bridge.get("bridge", bridge),
        }

    def _run_dispatch_v16_delegation(self) -> dict[str, Any]:
        from backend.services.brain_governance_candidate_review import (
            BrainGovernanceCandidateReviewService,
        )
        from backend.services.brain_governance_candidates import (
            BrainGovernanceCandidateService,
        )

        conn = _connect(self.db_path, read_only=True)
        try:
            rows = _execute(
                conn,
                """
                SELECT command.command_id, command.candidate_id,
                       command.target_agent, command.scope_type,
                       command.scope_key, command.action, command.decision,
                       command.claim_status, command.apply_count,
                       command.max_apply_count, command.authority_issued_at
                FROM v16_brain_command command
                JOIN brain_governance_candidate candidate
                  ON candidate.candidate_id=command.candidate_id
                WHERE command.decision='delegate'
                  AND command.claim_status='available'
                  AND candidate.status='active'
                ORDER BY command.created_at ASC
                """,
            ).fetchall()
            command = next(
                (
                    dict(row)
                    for row in rows
                    if V16CommandGate.is_actionable(dict(row))
                ),
                {},
            )
        finally:
            conn.close()
        if not command:
            return {
                "ok": True,
                "status": "skipped_no_actionable_v16_delegation",
            }

        candidate_id = str(command["candidate_id"])
        review_result = BrainGovernanceCandidateReviewService(
            self.db_path
        ).review_candidate(
            candidate_id,
            run_llm=False,
            llm_dry_run=True,
            persist=True,
        )
        review = dict(review_result.get("review") or {})
        if not bool(review.get("bridge_ready")):
            return {
                "ok": True,
                "status": "waiting_for_candidate_evidence",
                "command_id": command["command_id"],
                "candidate_id": candidate_id,
                "target_agent": command["target_agent"],
                "review": review,
            }
        submitted = BrainGovernanceCandidateService(
            self.db_path
        ).submit_candidate_to_policy_suggestion(
            candidate_id,
            actor="system:autonomous_demo_apply_stepper.v16_dispatch",
        )
        return {
            "ok": bool(submitted.get("ok", True)),
            "status": str(submitted.get("status") or "routed_to_specialist"),
            "command_id": command["command_id"],
            "candidate_id": candidate_id,
            "target_agent": command["target_agent"],
            "scope_type": command["scope_type"],
            "scope_key": command["scope_key"],
            "action": command["action"],
            "suggestion_id": submitted.get("suggestion_id", ""),
            "claim_deferred_to_atomic_mutation": True,
        }

    def _run_governor_review(self, *, experiment_id: str, run_id: str, limit: int) -> dict[str, Any]:
        conn = _connect(self.db_path)
        try:
            result = _approve_demo_policy_suggestions(
                conn,
                experiment_id=experiment_id,
                limit=limit,
                db_path=self.db_path,
                run_id=run_id,
            )
            conn.commit()
            return result
        finally:
            conn.close()

    def _run_resolve_conflicts(self, *, limit: int) -> dict[str, Any]:
        from research.learning.governance_conflicts import GovernanceConflictResolver

        conn = _connect(self.db_path)
        try:
            rows = _execute(
                conn,
                """
                SELECT suggestion_id, scope_type, scope_key, action, confidence,
                       evidence_json, status, reviewed_at, created_at
                FROM policy_suggestion
                WHERE status IN ('proposed', 'approved', 'applied')
                  AND governance_eligible=1
                  AND governance_eligibility_version=?
                  AND COALESCE(governance_eligibility_fingerprint, '') <> ''
                ORDER BY created_at ASC
                """,
                (GOVERNANCE_ELIGIBILITY_VERSION,),
            ).fetchall()
            result = GovernanceConflictResolver().resolve([dict(row) for row in rows])
            superseded = list(result.get("superseded") or [])
            limited = superseded[: max(1, min(int(limit), 100))]
            now = time.time()
            for item in limited:
                _execute(
                    conn,
                    """
                    UPDATE policy_suggestion
                    SET status='superseded', reviewed_at=?, review_note=?
                    WHERE suggestion_id=? AND status IN ('proposed', 'approved', 'applied')
                    """,
                    (
                        now,
                        str(item.get("reason") or "superseded by governance conflict resolver"),
                        str(item.get("suggestion_id") or ""),
                    ),
                )
            conn.commit()
            return {
                "winners": len(result.get("winners", [])),
                "superseded": len(limited),
                "remaining_superseded": max(0, len(superseded) - len(limited)),
                "items": limited,
                "limit": max(1, min(int(limit), 100)),
            }
        finally:
            conn.close()

    def _pending_counts(self) -> dict[str, int]:
        try:
            conn = _connect(self.db_path, read_only=True)
        except Exception:
            return {step: 0 for step in self.STEP_ORDER}
        try:
            counts = {
                "entry_quality_materialize": self._pending_entry_quality_materialize(
                    conn
                ),
                "factor_pruning_materialize": 0,
                "factor_pruning_promote": self._count_table(
                    conn,
                    "brain_governance_candidate",
                    "source_agent='factor_pruning_governance' AND proposal_stage='brain_candidate' AND status='active' AND COALESCE(submitted_suggestion_id, '') = ''",
                ),
                "factor_pruning_bridge": self._count_table(
                    conn,
                    "brain_governance_candidate",
                    "source_agent='factor_pruning_governance' AND proposal_stage='governance_ready' AND status='active' AND action='downweight' AND COALESCE(submitted_suggestion_id, '') = ''",
                ),
                "factor_pruning_governance": self._count_table(
                    conn,
                    "brain_governance_candidate",
                    "source_agent='factor_pruning_governance' AND status='active' AND COALESCE(submitted_suggestion_id, '') = ''",
                ),
                "dispatch_v16_delegation": self._count_actionable_v16_delegations(
                    conn
                ),
                "governor_review": self._count_policy(conn, "status='proposed'"),
                "resolve_conflicts": self._count_conflict_superseded(conn),
                "apply_entry_quality_control": self._count_policy(
                    conn,
                    "status='approved' AND scope_type='entry_quality' "
                    "AND scope_key='weak_signal' "
                    "AND action='raise_weak_signal_threshold' "
                    "AND governance_eligible=1 "
                    f"AND governance_eligibility_version='{GOVERNANCE_ELIGIBILITY_VERSION}' "
                    "AND COALESCE(governance_eligibility_fingerprint, '') <> '' "
                    "AND COALESCE(applied_mutation_id, '') = ''",
                ),
                "sync_factor_weights": self._count_policy(
                    conn,
                    "status='approved' AND scope_type='factor' "
                    "AND governance_eligible=1 "
                    f"AND governance_eligibility_version='{GOVERNANCE_ELIGIBILITY_VERSION}' "
                    "AND COALESCE(governance_eligibility_fingerprint, '') <> ''",
                ),
                "apply_parameter_templates": self._count_policy(conn, "status='approved' AND scope_type='parameter_template' AND action='switch_parameter_template'"),
                "release_parameter_candidates": self._count_table(conn, "parameter_template_release_candidate", "status IN ('pending_review','approved')"),
                "apply_supervisor_templates": self._count_policy(conn, "status='approved' AND scope_type='position_supervisor_template'"),
                "rollback_supervisor_templates": self._count_supervisor_rollbacks(conn),
            }
            return counts
        finally:
            conn.close()

    def _count_policy(self, conn: Any, where: str) -> int:
        return self._count_table(conn, "policy_suggestion", where)

    def _count_actionable_v16_delegations(self, conn: Any) -> int:
        if not self._table_exists(conn, "v16_brain_command") or not self._table_exists(
            conn, "brain_governance_candidate"
        ):
            return 0
        rows = _execute(
            conn,
            """
            SELECT command.command_id, command.decision, command.claim_status,
                   command.apply_count, command.max_apply_count,
                   command.authority_issued_at
            FROM v16_brain_command command
            JOIN brain_governance_candidate candidate
              ON candidate.candidate_id=command.candidate_id
            WHERE command.decision='delegate'
              AND command.claim_status='available'
              AND candidate.status='active'
            """,
        ).fetchall()
        return sum(
            1
            for row in rows
            if V16CommandGate.is_actionable(dict(row))
        )

    def _pending_entry_quality_materialize(self, conn: Any) -> int:
        active = self._count_policy(
            conn,
            "scope_type='entry_quality' AND scope_key='weak_signal' "
            "AND action='raise_weak_signal_threshold' "
            "AND status IN ('proposed','approved','applied') "
            "AND governance_eligible=1 "
            f"AND governance_eligibility_version='{GOVERNANCE_ELIGIBILITY_VERSION}' "
            "AND COALESCE(governance_eligibility_fingerprint, '') <> ''",
        )
        if active > 0:
            return 0
        return min(
            1,
            self._count_table(
                conn,
                "autonomous_learning_sample",
                "sample_type='trade_review_outcome' AND label_status='matured' "
                "AND governance_eligible=1 AND governance_effective_weight>0 "
                f"AND governance_eligibility_version='{GOVERNANCE_ELIGIBILITY_VERSION}'",
            ),
        )

    def _count_table(self, conn: Any, table: str, where: str) -> int:
        if not self._table_exists(conn, table):
            return 0
        row = _execute(conn, f"SELECT COUNT(*) AS n FROM {table} WHERE {where}").fetchone()
        return int((row["n"] if hasattr(row, "keys") else row[0]) or 0)

    def _count_supervisor_rollbacks(self, conn: Any) -> int:
        if not self._table_exists(conn, "learning_application_log") or not self._table_exists(conn, "learning_application_effect"):
            return 0
        try:
            from config.runtime_config import shared as runtime_config

            current_template_id = str(getattr(runtime_config(), "position_supervisor_template_id", "") or "")
            rows = _execute(
                conn,
                """
                SELECT l.application_id, l.scope_key, l.details_json,
                       e.observed_trade_count, e.delta_avg_reward
                FROM learning_application_log l
                JOIN learning_application_effect e ON e.application_id = l.application_id
                WHERE l.scope_type='position_supervisor_template'
                  AND l.action='switch_position_supervisor_template'
                  AND l.status IN ('applied', 'observing', 'ineffective')
                  AND e.status IN ('observing', 'ineffective')
                  AND COALESCE(e.observed_trade_count, 0) >= 3
                  AND COALESCE(e.delta_avg_reward, 0.0) <= -0.005
                ORDER BY l.created_at DESC
                LIMIT 50
                """,
            ).fetchall()
            actionable = 0
            for row in rows:
                details = self._loads(row["details_json"] if hasattr(row, "keys") else row[2], {})
                previous_template_id = str(details.get("previous_template_id") or "")
                target_template_id = str(details.get("target_template_id") or (row["scope_key"] if hasattr(row, "keys") else row[1]) or "")
                if previous_template_id and current_template_id and current_template_id == target_template_id:
                    actionable += 1
            return actionable
        except Exception:
            return 0

    def _count_conflict_superseded(self, conn: Any) -> int:
        if not self._table_exists(conn, "policy_suggestion"):
            return 0
        try:
            from research.learning.governance_conflicts import GovernanceConflictResolver

            rows = _execute(
                conn,
                """
                SELECT suggestion_id, scope_type, scope_key, action, confidence,
                       evidence_json, status, reviewed_at, created_at
                FROM policy_suggestion
                WHERE status IN ('proposed', 'approved', 'applied')
                  AND governance_eligible=1
                  AND governance_eligibility_version=?
                  AND COALESCE(governance_eligibility_fingerprint, '') <> ''
                ORDER BY created_at ASC
                """,
                (GOVERNANCE_ELIGIBILITY_VERSION,),
            ).fetchall()
            result = GovernanceConflictResolver().resolve([dict(row) for row in rows])
            return len(result.get("superseded") or [])
        except Exception:
            return 0

    @staticmethod
    def _table_exists(conn: Any, table: str) -> bool:
        try:
            row = _execute(
                conn,
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'state_v1' AND table_name = ?
                LIMIT 1
                """,
                (table,),
            ).fetchone()
            if row:
                return True
        except Exception:
            pass
        try:
            row = _execute(conn, f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            return row is not None or True
        except Exception:
            return False

    @staticmethod
    def _recommended(step: str, pending: dict[str, int]) -> bool:
        if step == "factor_pruning_materialize":
            return False
        if step == "factor_pruning_promote":
            return int(pending.get("factor_pruning_bridge", 0) or 0) <= 0 and int(pending.get(step, 0) or 0) > 0
        if step == "factor_pruning_governance":
            return False
        if step == "apply_entry_quality_control":
            return (
                int(pending.get("entry_quality_materialize", 0) or 0) <= 0
                and int(pending.get(step, 0) or 0) > 0
            )
        if step == "resolve_conflicts":
            return int(pending.get("governor_review", 0) or 0) > 0 or int(pending.get("resolve_conflicts", 0) or 0) > 1
        return int(pending.get(step, 0) or 0) > 0

    @staticmethod
    def _execution_profile(step: str) -> str:
        if step == "factor_pruning_materialize":
            return "maintenance_heavy_rescan"
        if step == "factor_pruning_promote":
            return "maintenance_counter_evidence"
        if step == "factor_pruning_bridge":
            return "bounded_candidate_review_bridge"
        return "bounded_existing_apply_step"

    @staticmethod
    def _loads(raw: Any, default: dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        try:
            value = json.loads(str(raw or ""))
            return value if isinstance(value, dict) else default
        except Exception:
            return default

    @staticmethod
    def _autonomy_mode() -> str:
        try:
            from config.runtime_config import shared as runtime_config

            return str(getattr(runtime_config(), "autonomy_mode", "") or "manual")
        except Exception:
            return "manual"

    @staticmethod
    def _is_system_demo_actor(actor: str) -> bool:
        if str(actor or "").startswith("api:"):
            return False
        try:
            from config.runtime_config import shared as runtime_config

            mode = str(getattr(runtime_config(), "autonomy_mode", "") or "")
        except Exception:
            mode = ""
        return mode in {"demo_nursery", "demo_autonomous"} and str(actor or "").startswith("system:")
