from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

from backend.core.db import STATE_DB, is_state_db_path
from backend.services.autonomous_evolution_cycle import AutonomousEvolutionCycleService


class AutonomousEvolutionNurseryRunner:
    """Small coordinator for the demo-nursery self-evolution loop.

    The runner deliberately reuses the existing replay, release, candidate
    review, effect tracker, proposal registry, and autonomous learning services.
    It does not place orders or bypass RiskPolicyService/DecisionPolicy.
    """

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "autonomous_evolution_nursery_runner_boundary.v1",
            "does_not_submit_orders": True,
            "does_not_bypass_risk_policy": True,
            "does_not_bypass_decision_policy": True,
            "uses_existing_replay_harness": True,
            "uses_existing_release_control": True,
            "uses_existing_candidate_review": True,
            "uses_existing_effect_reconcile": True,
            "demo_apply_uses_existing_autonomous_learning_cycle": True,
            "recommended_step_consumption_supported": True,
            "recommended_step_max_per_cycle": 1,
        }

    def run_once(self, **kwargs: Any) -> dict[str, Any]:
        lock_handle = self._acquire_lock(self._lock_path())
        if lock_handle is None:
            now = time.time()
            return {
                "ok": True,
                "schema_version": "autonomous_evolution_nursery_run.v1",
                "run_id": f"nursery_cycle_{int(now)}",
                "status": "skipped_lock_held",
                "started_at": now,
                "finished_at": now,
                "initial_cycle": {},
                "repaired_cycle": {},
                "final_cycle": {},
                "actions": [],
                "release_run": {},
                "boundary": self.boundary(),
            }
        try:
            return self._run_once_locked(**kwargs)
        finally:
            self._release_lock(lock_handle)

    def _run_once_locked(
        self,
        *,
        replay_if_stale: bool = True,
        reconcile_effects: bool = True,
        refresh_proposals: bool = True,
        review_candidates: bool = True,
        create_release_evidence: bool = True,
        apply_when_ready: bool = False,
        full_learning_cycle: bool = False,
        replay_lookback_days: float = 7.0,
        replay_limit: int = 80,
        review_limit: int = 50,
        effect_limit: int = 50,
        sample_limit: int = 500,
        recommendation_limit: int = 20,
        suggestion_limit: int = 20,
        consume_recommended_step: bool = False,
        recommended_step_limit: int = 1,
        recommended_step_allowlist: list[str] | tuple[str, ...] | None = None,
        readiness: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started_at = time.time()
        run_id = f"nursery_cycle_{int(started_at)}"
        actions: list[dict[str, Any]] = []

        initial_readiness = readiness or self._build_readiness()
        initial_cycle = AutonomousEvolutionCycleService(self.db_path).status(
            readiness=initial_readiness,
            include_chain_health=False,
        )
        release_cleanup = self._close_stale_release()
        if str(release_cleanup.get("status") or "") == "cancelled":
            actions.append({
                "action": "close_stale_release",
                "ok": bool(release_cleanup.get("ok")),
                "result": release_cleanup,
            })
            initial_readiness = self._build_readiness()
            initial_cycle = AutonomousEvolutionCycleService(self.db_path).status(
                readiness=initial_readiness,
                include_chain_health=False,
            )
        if str(initial_cycle.get("status") or "") == "outside_demo_nursery_scope":
            return self._result(
                run_id=run_id,
                started_at=started_at,
                initial_cycle=initial_cycle,
                repaired_cycle=initial_cycle,
                final_cycle=initial_cycle,
                actions=actions,
                status="skipped_outside_demo_nursery",
            )

        components = {str(item.get("component") or "") for item in initial_cycle.get("blockers") or []}
        if replay_if_stale and ("evidence" in components or "replay" in components):
            actions.append(
                self._record(
                    "run_bar_replay_evidence",
                    lambda: self._run_bar_replay(
                        lookback_days=replay_lookback_days,
                        limit=replay_limit,
                    ),
                )
            )

        if reconcile_effects and "effect_monitor" in components:
            actions.append(
                self._record(
                    "reconcile_application_effects",
                    lambda: self._reconcile_effects(limit=effect_limit),
                )
            )

        proposal_status = dict(initial_cycle.get("proposal_registry") or {})
        proposal_empty = int(proposal_status.get("active_count") or proposal_status.get("proposal_count") or 0) <= 0
        if refresh_proposals and ("proposal_registry" in components or proposal_empty):
            actions.append(self._record("refresh_proposal_registry", self._refresh_proposals))

        candidate_count = int((initial_cycle.get("candidate_lane") or {}).get("candidate_count") or 0)
        review_count = int((initial_cycle.get("candidate_review") or {}).get("review_count") or 0)
        if review_candidates and candidate_count > 0 and review_count <= 0:
            actions.append(
                self._record(
                    "review_governance_candidates",
                    lambda: self._review_candidates(limit=review_limit),
                )
            )

        repaired_readiness = self._build_readiness()
        repaired_cycle = AutonomousEvolutionCycleService(self.db_path).status(
            readiness=repaired_readiness,
            refresh_proposals=False,
            include_chain_health=False,
        )

        release_run: dict[str, Any] = {}
        if create_release_evidence and self._release_missing(repaired_cycle):
            release_run = self._create_release_evidence(
                run_id=run_id,
                readiness=repaired_readiness,
                cycle=repaired_cycle,
                actions=actions,
            )
            actions.append({"action": "record_release_evidence", "ok": bool(release_run.get("ok")), "result": release_run})
            repaired_readiness = self._build_readiness()
            repaired_cycle = AutonomousEvolutionCycleService(self.db_path).status(
                readiness=repaired_readiness,
                include_chain_health=False,
            )

        if (
            consume_recommended_step
            and not apply_when_ready
            and bool(repaired_cycle.get("stable_demo_nursery_ready"))
        ):
            actions.append(
                self._record(
                    "consume_recommended_demo_apply_step",
                    lambda: self._consume_recommended_step(
                        limit=recommended_step_limit,
                        allowlist=recommended_step_allowlist,
                    ),
                )
            )

        if apply_when_ready and bool(repaired_cycle.get("stable_demo_nursery_ready")):
            if full_learning_cycle:
                actions.append(
                    self._record(
                        "run_autonomous_learning_cycle",
                        lambda: self._run_learning_cycle(
                            sample_limit=sample_limit,
                            recommendation_limit=recommendation_limit,
                        ),
                    )
                )
            else:
                actions.append(
                    self._record(
                        "run_demo_autonomy_apply",
                        lambda: self._run_demo_apply(suggestion_limit=suggestion_limit),
                    )
                )

        final_readiness = self._build_readiness()
        final_cycle = AutonomousEvolutionCycleService(self.db_path).status(
            readiness=final_readiness,
            include_chain_health=False,
        )
        errored = any(not bool(item.get("ok")) and str(item.get("action")) != "record_release_evidence" for item in actions)
        status = "completed_with_errors" if errored else "completed"
        if apply_when_ready and not bool(repaired_cycle.get("stable_demo_nursery_ready")):
            status = "repaired_waiting_for_ready"
        return self._result(
            run_id=run_id,
            started_at=started_at,
            initial_cycle=initial_cycle,
            repaired_cycle=repaired_cycle,
            final_cycle=final_cycle,
            actions=actions,
            status=status,
            release_run=release_run,
        )

    def _result(
        self,
        *,
        run_id: str,
        started_at: float,
        initial_cycle: dict[str, Any],
        repaired_cycle: dict[str, Any],
        final_cycle: dict[str, Any],
        actions: list[dict[str, Any]],
        status: str,
        release_run: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": status not in {"completed_with_errors"},
            "schema_version": "autonomous_evolution_nursery_run.v1",
            "run_id": run_id,
            "status": status,
            "started_at": started_at,
            "finished_at": time.time(),
            "initial_cycle": self._cycle_summary(initial_cycle),
            "repaired_cycle": self._cycle_summary(repaired_cycle),
            "final_cycle": self._cycle_summary(final_cycle),
            "actions": actions,
            "release_run": release_run or {},
            "boundary": self.boundary(),
        }

    @staticmethod
    def _cycle_summary(cycle: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": cycle.get("schema_version", ""),
            "status": cycle.get("status", ""),
            "stable_demo_nursery_ready": bool(cycle.get("stable_demo_nursery_ready")),
            "autonomy_mode": cycle.get("autonomy_mode", ""),
            "autonomy_posture": cycle.get("autonomy_posture", ""),
            "blocker_count": len(cycle.get("blockers") or []),
            "blockers": cycle.get("blockers") or [],
            "next_actions": cycle.get("next_actions") or [],
        }

    def _record(self, action: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        started_at = time.time()
        try:
            result = fn()
            return {
                "action": action,
                "ok": bool(result.get("ok", True)),
                "status": str(result.get("status") or result.get("schema_version") or "completed"),
                "started_at": started_at,
                "finished_at": time.time(),
                "result": result,
            }
        except Exception as exc:
            return {
                "action": action,
                "ok": False,
                "status": "error",
                "started_at": started_at,
                "finished_at": time.time(),
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _build_readiness(self) -> dict[str, Any]:
        return self.build_light_readiness()

    def build_light_readiness(self) -> dict[str, Any]:
        from backend.services.autonomy_health import AutonomyHealthService
        from backend.services.release_control import ReleaseControlService
        from backend.services.replay_harness import ReplayHarnessService
        from config.runtime_config import shared as runtime_config

        cfg = runtime_config()
        replay = self._replay_status(ReplayHarnessService(self.db_path).latest_report())
        release = self._release_status(ReleaseControlService(self.db_path).latest_release())
        health = AutonomyHealthService(self.db_path).latest_snapshot()
        return {
            "schema_version": "autonomous_evolution_runner_light_readiness.v1",
            "governance": {"autonomy_mode": str(getattr(cfg, "autonomy_mode", "") or "manual")},
            "autonomy_health": {
                "posture": str(health.get("posture") or "unknown"),
                "snapshot_id": str(health.get("snapshot_id") or ""),
                "status": str(health.get("status") or ""),
            },
            "replay": replay,
            "release": release,
            "live": {"loop": {"status": "unknown"}},
            "v16": {
                "control_plane_boundaries": {
                    "risk_policy_service_required_for_future_actions": True,
                    "decision_policy_required_for_future_weight_writes": True,
                    "runtime_overlay_snapshot_required_for_future_mutations": True,
                    "proposal_registry_review_only": True,
                }
            },
        }

    @staticmethod
    def _replay_status(report: dict[str, Any]) -> dict[str, Any]:
        created_at = float(report.get("created_at") or 0.0)
        age = max(0.0, time.time() - created_at) if created_at > 0 else 0.0
        if not report.get("replay_run_id"):
            status = "missing"
        elif report.get("replay_error"):
            status = "error"
        elif age > 24 * 3600:
            status = "stale"
        else:
            status = "fresh"
        return {
            "schema_version": "replay_latest_status.v1",
            "status": status,
            "replay_run_id": str(report.get("replay_run_id") or ""),
            "created_at": created_at,
            "age_seconds": age,
        }

    @staticmethod
    def _release_status(release: dict[str, Any]) -> dict[str, Any]:
        if not release.get("run_id"):
            status = "missing"
        else:
            status = str(release.get("status") or "unknown")
        return {
            "schema_version": "release_latest_status.v1",
            "status": status,
            "run_id": str(release.get("run_id") or ""),
            "created_at": float(release.get("created_at") or 0.0),
        }

    def _lock_path(self) -> Path:
        if is_state_db_path(self.db_path):
            return Path("logs/autonomous_evolution_nursery.lock")
        return Path(self.db_path).with_suffix(".autonomous_evolution_nursery.lock")

    @staticmethod
    def _acquire_lock(path: Path):
        import fcntl
        import os

        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "w", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        handle.write(str(os.getpid()))
        handle.truncate()
        handle.flush()
        return handle

    @staticmethod
    def _release_lock(handle: Any) -> None:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _run_bar_replay(self, *, lookback_days: float, limit: int) -> dict[str, Any]:
        from backend.services.replay_harness import ReplayHarnessService

        report = ReplayHarnessService(self.db_path).run_bar_replay_freshness(
            lookback_days=max(0.0, min(float(lookback_days), 30.0)),
            limit=max(1, min(int(limit), 1000)),
        )
        return {
            "ok": not bool(report.get("replay_error")),
            "schema_version": "autonomous_evolution_runner_replay_freshness.v1",
            "status": "completed" if not report.get("replay_error") else "failed",
            "report": report,
        }

    def _reconcile_effects(self, *, limit: int) -> dict[str, Any]:
        from backend.services.factor_governance_effect_tracker import FactorGovernanceEffectTrackerService

        return FactorGovernanceEffectTrackerService(self.db_path).reconcile(limit=max(1, min(int(limit), 200)))

    def _refresh_proposals(self) -> dict[str, Any]:
        from backend.services.proposal_registry import ProposalRegistryService

        return ProposalRegistryService(self.db_path).status(refresh=True)

    def _close_stale_release(self) -> dict[str, Any]:
        from backend.services.release_control import ReleaseControlService

        max_age = float(os.getenv("QUANT_RELEASE_STARTED_MAX_AGE_SEC", "3600") or 3600)
        return ReleaseControlService(self.db_path).close_stale_started_release(
            max_age_seconds=max_age,
            actor="system:autonomous_evolution_nursery_runner",
        )

    def _review_candidates(self, *, limit: int) -> dict[str, Any]:
        from backend.services.brain_governance_candidate_review import BrainGovernanceCandidateReviewService

        return BrainGovernanceCandidateReviewService(self.db_path).review_latest(
            limit=max(1, min(int(limit), 200)),
            run_llm=False,
            llm_dry_run=True,
            persist=True,
        )

    def _run_learning_cycle(self, *, sample_limit: int, recommendation_limit: int) -> dict[str, Any]:
        from backend.services.autonomous_learning import run_autonomous_learning_cycle

        return run_autonomous_learning_cycle(
            db_path=self.db_path,
            sample_limit=max(1, min(int(sample_limit), 2000)),
            recommendation_limit=max(1, min(int(recommendation_limit), 100)),
            apply_demo=True,
        )

    def _run_demo_apply(self, *, suggestion_limit: int) -> dict[str, Any]:
        from backend.services.autonomous_learning import apply_demo_autonomy

        return apply_demo_autonomy(
            db_path=self.db_path,
            suggestion_limit=max(1, min(int(suggestion_limit), 100)),
        )

    def _consume_recommended_step(
        self,
        *,
        limit: int,
        allowlist: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        from backend.services.autonomous_demo_apply_stepper import AutonomousDemoApplyStepper

        stepper = AutonomousDemoApplyStepper(self.db_path)
        plan = stepper.plan()
        selected = self._select_recommended_step(plan, allowlist=allowlist)
        if not selected:
            return {
                "ok": True,
                "schema_version": "autonomous_evolution_recommended_step_consumption.v1",
                "status": "skipped_no_recommended_step",
                "plan_generated_at": plan.get("generated_at"),
                "boundary": {
                    "single_step_only": True,
                    "does_not_submit_orders": True,
                    "uses_autonomous_demo_apply_stepper": True,
                },
            }
        step = str(selected.get("step") or "")
        result = stepper.run_step(
            step,
            limit=max(1, min(int(limit or 1), 20)),
            confirm_step=True,
            actor="system:autonomous_evolution_nursery_runner.recommended_step",
        )
        return {
            "ok": bool(result.get("ok")),
            "schema_version": "autonomous_evolution_recommended_step_consumption.v1",
            "status": str(result.get("status") or "unknown"),
            "selected_step": {
                "step": step,
                "pending_count": int(selected.get("pending_count") or 0),
                "execution_profile": str(selected.get("execution_profile") or ""),
            },
            "step_result": result,
            "boundary": {
                "single_step_only": True,
                "does_not_submit_orders": True,
                "uses_autonomous_demo_apply_stepper": True,
                "mutating_step_confirmed_by_runner": True,
            },
        }

    @staticmethod
    def _select_recommended_step(
        plan: dict[str, Any],
        *,
        allowlist: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        priority = tuple(allowlist or (
            "governor_review",
            "resolve_conflicts",
            "rollback_supervisor_templates",
            "factor_pruning_bridge",
        ))
        recommended_by_step = {
            str(item.get("step") or ""): dict(item)
            for item in plan.get("steps") or []
            if bool(item.get("recommended")) and int(item.get("pending_count") or 0) > 0
        }
        for step in priority:
            item = recommended_by_step.get(str(step))
            if item:
                return item
        allowed = set(priority)
        for item in recommended_by_step.values():
            step = str(item.get("step") or "")
            if step in allowed:
                return dict(item)
        return {}

    @staticmethod
    def _release_missing(cycle: dict[str, Any]) -> bool:
        return any(str(item.get("component") or "") == "release" for item in cycle.get("blockers") or [])

    def _create_release_evidence(
        self,
        *,
        run_id: str,
        readiness: dict[str, Any],
        cycle: dict[str, Any],
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        from backend.services.release_control import ReleaseControlService

        service = ReleaseControlService(self.db_path)
        tests = [
            {
                "name": str(item.get("action") or ""),
                "ok": bool(item.get("ok")),
                "status": str(item.get("status") or ""),
            }
            for item in actions
        ]
        release = service.start_release(
            release_class="demo_nursery_autonomous_evolution_cycle",
            summary={
                "run_id": run_id,
                "cycle_status": cycle.get("status", ""),
                "blocker_count": len(cycle.get("blockers") or []),
            },
            tests=tests,
            rollback_ref={"source": "runtime_config_snapshot", "runner": run_id},
            created_by="system:autonomous_evolution_nursery_runner",
            readiness=readiness,
            run_id=f"release_{run_id}",
        )
        return service.finish_release(
            str(release.get("run_id") or f"release_{run_id}"),
            status="completed",
            summary={
                "run_id": run_id,
                "cycle_status": cycle.get("status", ""),
                "blockers": cycle.get("blockers") or [],
            },
            tests=tests,
            rollback_ref={"source": "runtime_config_snapshot", "runner": run_id},
            readiness=readiness,
        )
