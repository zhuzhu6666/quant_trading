from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_exists,
)
from backend.core.db_helpers import conn_is_pg as _conn_is_pg, execute as _execute, pg_sql as _sql
from backend.services.agent_scorecard import AgentScorecardService
from backend.services.brain_governance_candidate_review import BrainGovernanceCandidateReviewService
from backend.services.brain_governance_candidates import BrainGovernanceCandidateService
from backend.services.proposal_registry import ProposalRegistryService


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = True):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


class AutonomousEvolutionCycleService:
    """Read-only control map for the demo-nursery self-evolution loop.

    The service intentionally does not apply proposals, mutate runtime config,
    submit orders, or create candidates. It only assembles existing ledgers and
    readiness facts into one cycle state so the next safe action is obvious.
    """

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "autonomous_evolution_cycle_boundary.v1",
            "read_only_cycle_status": True,
            "does_not_submit_orders": True,
            "does_not_apply_proposals": True,
            "does_not_mutate_runtime_overlay": True,
            "does_not_write_policy_suggestion": True,
            "does_not_create_candidates": True,
            "reuses_existing_services": [
                "BackendReadinessService",
                "ProposalRegistryService",
                "BrainGovernanceCandidateService",
                "BrainGovernanceCandidateReviewService",
                "AgentScorecardService",
                "RiskPolicyService",
                "DecisionPolicy",
                "RuntimeConfigMutationService",
            ],
            "future_apply_must_use": [
                "RiskPolicyService",
                "DecisionPolicy",
                "RuntimeConfigMutationService",
                "runtime_config_overlay",
                "runtime_config_snapshot",
            ],
        }

    def status(
        self,
        *,
        readiness: dict[str, Any] | None = None,
        refresh_proposals: bool = False,
        include_chain_health: bool = True,
    ) -> dict[str, Any]:
        now = time.time()
        readiness = dict(readiness or {})
        governance = dict(readiness.get("governance") or {})
        autonomy_health = dict(readiness.get("autonomy_health") or {})
        replay = dict(readiness.get("replay") or {})
        release = dict(readiness.get("release") or {})
        live = dict(readiness.get("live") or {})
        v16 = dict(readiness.get("v16") or {})
        boundaries = dict(v16.get("control_plane_boundaries") or {})

        proposal_status = self._proposal_status(refresh=refresh_proposals)
        candidate_status = self._candidate_status()
        candidate_review_status = self._candidate_review_status()
        chain_health = self._chain_health() if include_chain_health else self._skipped_chain_health()
        evidence = self._evidence_status(now=now)
        effect = self._effect_status(now=now)

        autonomy_mode = str(governance.get("autonomy_mode") or "")
        replay_status = str(replay.get("status") or "")
        release_status = str(release.get("status") or "")
        posture = str(autonomy_health.get("posture") or "")
        blockers = self._blockers(
            autonomy_mode=autonomy_mode,
            posture=posture,
            replay=replay,
            release=release,
            proposal_status=proposal_status,
            candidate_review_status=candidate_review_status,
            chain_health=chain_health,
            evidence=evidence,
            effect=effect,
            boundaries=boundaries,
        )
        next_actions = self._next_actions(
            autonomy_mode=autonomy_mode,
            blockers=blockers,
            proposal_status=proposal_status,
            candidate_status=candidate_status,
            candidate_review_status=candidate_review_status,
            effect=effect,
        )
        steps = self._steps(
            live=live,
            evidence=evidence,
            proposal_status=proposal_status,
            candidate_status=candidate_status,
            candidate_review_status=candidate_review_status,
            replay=replay,
            release=release,
            chain_health=chain_health,
            effect=effect,
            boundaries=boundaries,
        )
        status = "ready_for_guarded_demo_apply" if not blockers else "needs_attention"
        if autonomy_mode not in {"demo_nursery", "demo_autonomous"}:
            status = "outside_demo_nursery_scope"

        return {
            "ok": True,
            "schema_version": "autonomous_evolution_cycle.v1",
            "phase": "stable_demo_nursery_self_evolution",
            "status": status,
            "autonomy_mode": autonomy_mode or "unknown",
            "autonomy_posture": posture or "unknown",
            "automatic_demo_governance": autonomy_mode in {"demo_nursery", "demo_autonomous"},
            "human_intervention_required": False if autonomy_mode in {"demo_nursery", "demo_autonomous"} else True,
            "system_decision_owner": "autonomous_evolution_nursery" if autonomy_mode in {"demo_nursery", "demo_autonomous"} else "operator",
            "replay_status": replay_status or "unknown",
            "release_status": release_status or "unknown",
            "stable_demo_nursery_ready": status == "ready_for_guarded_demo_apply",
            "blockers": blockers,
            "next_actions": next_actions,
            "steps": steps,
            "evidence": evidence,
            "proposal_registry": proposal_status,
            "candidate_lane": candidate_status,
            "candidate_review": candidate_review_status,
            "effect_monitor": effect,
            "agent_chain_health": chain_health,
            "generated_at": now,
            "boundary": self.boundary(),
        }

    def _proposal_status(self, *, refresh: bool) -> dict[str, Any]:
        try:
            return ProposalRegistryService(self.db_path).status(refresh=refresh)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "proposal_registry_status.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _candidate_status(self) -> dict[str, Any]:
        try:
            return BrainGovernanceCandidateService(self.db_path).status(limit=200)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "brain_governance_candidate_readiness.v1",
                "status": "error",
                "candidate_count": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _candidate_review_status(self) -> dict[str, Any]:
        try:
            return BrainGovernanceCandidateReviewService(self.db_path).status(limit=200)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "brain_governance_candidate_review_readiness.v1",
                "status": "error",
                "review_count": 0,
                "bridge_ready_count": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _chain_health(self) -> dict[str, Any]:
        try:
            return AgentScorecardService(self.db_path).chain_health(limit=80)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "agent_chain_health.v1",
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    @staticmethod
    def _skipped_chain_health() -> dict[str, Any]:
        return {
            "ok": True,
            "schema_version": "agent_chain_health.v1",
            "status": "skipped_for_nursery_runner",
            "reason": "runner_uses_lightweight_cycle_status",
        }

    def _evidence_status(self, *, now: float) -> dict[str, Any]:
        tables = [
            "decision_ledger",
            "trade_outcome_review",
            "experience_memory",
            "replay_report",
        ]
        items = [self._table_freshness(table, now=now) for table in tables]
        sample_item = self._sample_freshness(now=now)
        if sample_item is not None:
            items.append(sample_item)
        hard_missing = [
            item["table"]
            for item in items
            if item["table"] in {"decision_ledger", "canonical_v2.training_sample_row"} and item["count"] <= 0
        ]
        replay_item = next((item for item in items if item["table"] == "replay_report"), {})
        replay_stale = bool(replay_item.get("latest_age_seconds", 0) > 24 * 3600) or _safe_int(replay_item.get("count")) <= 0
        status = "ok"
        if hard_missing:
            status = "missing_core_evidence"
        elif replay_stale:
            status = "replay_stale"
        return {
            "ok": status == "ok",
            "schema_version": "autonomous_evolution_evidence_status.v1",
            "status": status,
            "hard_missing": hard_missing,
            "replay_stale": replay_stale,
            "tables": items,
        }

    def _effect_status(self, *, now: float) -> dict[str, Any]:
        items = [
            self._table_freshness("learning_application_log", now=now),
            self._table_freshness("learning_application_effect", now=now),
        ]
        effect_item = next((item for item in items if item["table"] == "learning_application_effect"), {})
        effect_count = _safe_int(effect_item.get("count"))
        status = "ok" if effect_count > 0 else "missing_effect_monitor"
        return {
            "ok": status == "ok",
            "schema_version": "autonomous_evolution_effect_status.v1",
            "status": status,
            "tables": items,
            "effect_count": effect_count,
            "requires_effect_monitor_before_repeated_apply": True,
        }

    def _table_freshness(self, table: str, *, now: float) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, table):
                return {
                    "table": table,
                    "exists": False,
                    "count": 0,
                    "latest_created_at": 0.0,
                    "latest_age_seconds": 0.0,
                    "status": "missing",
                }
            columns = self._columns(conn, table)
            timestamp_col = "created_at" if "created_at" in columns else ("updated_at" if "updated_at" in columns else "")
            if timestamp_col:
                row = _execute(conn, f"SELECT COUNT(*) AS n, MAX({timestamp_col}) AS latest FROM {table}").fetchone()
                latest = _safe_float(row["latest"] if hasattr(row, "keys") else row[1])
                count = _safe_int(row["n"] if hasattr(row, "keys") else row[0])
            else:
                row = _execute(conn, f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                latest = 0.0
                count = _safe_int(row["n"] if hasattr(row, "keys") else row[0])
            age = max(0.0, now - latest) if latest > 0 else 0.0
            return {
                "table": table,
                "exists": True,
                "count": count,
                "latest_created_at": latest,
                "latest_age_seconds": age,
                "status": "available" if count > 0 else "empty",
            }
        finally:
            conn.close()

    def _sample_freshness(self, *, now: float) -> dict[str, Any] | None:
        """Canonical training-sample freshness via the canonical reader."""
        conn = _connect(self.db_path, read_only=True)
        try:
            from backend.services.canonical_v2_reader import iter_training_sample_rows
            rows = iter_training_sample_rows(conn, order_by_event_ts=True)
            if not rows:
                return {
                    "table": "canonical_v2.training_sample_row",
                    "exists": True,
                    "count": 0,
                    "latest_created_at": 0.0,
                    "latest_age_seconds": 0.0,
                    "status": "empty",
                }
            latest = max(float(r.get("updated_at") or 0.0) for r in (rows[:1] or [{}]))
            age = max(0.0, now - latest) if latest > 0 else 0.0
            return {
                "table": "canonical_v2.training_sample_row",
                "exists": True,
                "count": len(rows),
                "latest_created_at": latest,
                "latest_age_seconds": age,
                "status": "available",
            }
        except Exception:
            return None
        finally:
            conn.close()

    def _columns(self, conn: Any, table: str) -> set[str]:
        if _conn_is_pg(conn):
            from backend.core.state_store import STATE_SCHEMA
            rows = _execute(
                conn,
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = ? AND table_name = ?
                """,
                (STATE_SCHEMA, table),
            ).fetchall()
            return {str(row["column_name"]) for row in rows}
        rows = _execute(conn, f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    @staticmethod
    def _blockers(
        *,
        autonomy_mode: str,
        posture: str,
        replay: dict[str, Any],
        release: dict[str, Any],
        proposal_status: dict[str, Any],
        candidate_review_status: dict[str, Any],
        chain_health: dict[str, Any],
        evidence: dict[str, Any],
        effect: dict[str, Any],
        boundaries: dict[str, Any],
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        if autonomy_mode not in {"demo_nursery", "demo_autonomous"}:
            blockers.append({"component": "autonomy_mode", "status": "blocked", "reason": "not_demo_nursery"})
        if posture in {"shadow_only", "frozen"}:
            blockers.append({"component": "autonomy_health", "status": posture, "reason": "posture_blocks_guarded_apply"})
        if not bool(evidence.get("ok")):
            blockers.append({"component": "evidence", "status": evidence.get("status"), "reason": "core_or_replay_evidence_not_ready"})
        if str(replay.get("status") or "") in {"missing", "stale", "error"}:
            blockers.append({"component": "replay", "status": replay.get("status"), "reason": "replay_freshness_required"})
        if str(release.get("status") or "") in {"missing", "error"}:
            blockers.append({"component": "release", "status": release.get("status"), "reason": "release_run_required_for_governed_apply"})
        if int(proposal_status.get("high_unresolved_conflict_count") or 0) > 0:
            blockers.append({"component": "proposal_registry", "status": "conflict", "reason": "high_unresolved_conflicts"})
        hard_stale_count = int(
            proposal_status.get("hard_stale_evidence_count", proposal_status.get("stale_evidence_count") or 0) or 0
        )
        if hard_stale_count > 0:
            blockers.append({"component": "proposal_registry", "status": "stale_evidence", "reason": "proposal_evidence_stale"})
        if not bool(candidate_review_status.get("ok")):
            blockers.append({"component": "candidate_review", "status": candidate_review_status.get("status"), "reason": "candidate_review_required"})
        if not bool(chain_health.get("ok")):
            blockers.append({"component": "agent_chain_health", "status": chain_health.get("status"), "reason": "agent_feedback_not_healthy"})
        if not bool(effect.get("ok")):
            blockers.append({"component": "effect_monitor", "status": effect.get("status"), "reason": "prevent_repeated_unmeasured_changes"})
        for key in [
            "risk_policy_service_required_for_future_actions",
            "decision_policy_required_for_future_weight_writes",
            "runtime_overlay_snapshot_required_for_future_mutations",
            "proposal_registry_review_only",
        ]:
            if key in boundaries and not bool(boundaries.get(key)):
                blockers.append({"component": "control_boundary", "status": "attention", "reason": key})
        return blockers

    @staticmethod
    def _next_actions(
        *,
        autonomy_mode: str,
        blockers: list[dict[str, Any]],
        proposal_status: dict[str, Any],
        candidate_status: dict[str, Any],
        candidate_review_status: dict[str, Any],
        effect: dict[str, Any],
    ) -> list[dict[str, Any]]:
        automatic_demo = autonomy_mode in {"demo_nursery", "demo_autonomous"}
        components = {str(item.get("component") or "") for item in blockers}
        actions: list[dict[str, Any]] = []
        if int(proposal_status.get("stale_replay_required_count") or 0) > 0:
            actions.append({
                "action": "auto_run_proposal_replay_refresh" if automatic_demo else "run_proposal_replay_refresh",
                "endpoint": "/api/ops/replay/bar-run",
                "executor": "autonomous_evolution_nursery" if automatic_demo else "operator",
                "reason": "proposal_registry_request_replay",
            })
        if int(proposal_status.get("stale_review_required_count") or 0) > 0:
            actions.append({
                "action": "auto_review_stale_proposals" if automatic_demo else "review_stale_proposals",
                "endpoint": "/api/ops/autonomy/proposals",
                "executor": "autonomous_evolution_nursery" if automatic_demo else "operator",
                "reason": "proposal_registry_request_review",
            })
        if not blockers:
            if int(candidate_review_status.get("bridge_ready_count") or 0) > 0:
                actions.append({
                    "action": "auto_bridge_reviewed_candidates" if automatic_demo else "bridge_reviewed_candidates",
                    "endpoint": "/api/ops/brain/governance-candidates/{candidate_id}/submit",
                    "executor": "autonomous_evolution_nursery" if automatic_demo else "operator",
                    "reason": "reviewed_candidate_ready_for_controlled_bridge",
                })
            actions.append({
                "action": "auto_inspect_demo_apply_plan" if automatic_demo else "inspect_demo_apply_plan",
                "endpoint": "/api/ops/autonomy/demo-apply-plan",
                "executor": "autonomous_evolution_nursery" if automatic_demo else "operator",
                "reason": "ready_for_system_owned_demo_apply_plan" if automatic_demo else "ready_for_single_step_demo_apply_plan",
            })
            actions.append({
                "action": "auto_guarded_demo_apply" if automatic_demo else "guarded_demo_apply_step",
                "endpoint": "/api/ops/autonomy/demo-apply-step",
                "executor": "autonomous_evolution_nursery" if automatic_demo else "operator",
                "reason": "system_will_apply_with_existing_gates" if automatic_demo else "ready_for_confirmed_single_step_demo_mutation",
            })
            return actions
        if "evidence" in components or "replay" in components:
            actions.append({"action": "run_replay", "endpoint": "/api/ops/replay/bar-run", "reason": "fresh_replay_required"})
        if "release" in components:
            actions.append({"action": "start_or_finish_release_run", "endpoint": "/api/ops/release/start", "reason": "release_evidence_required"})
        if int(proposal_status.get("active_count") or 0) == 0:
            actions.append({"action": "refresh_proposal_registry", "endpoint": "/api/ops/autonomy/proposals/refresh", "reason": "proposal_bus_empty"})
        if int(candidate_status.get("candidate_count") or 0) > 0 and int(candidate_review_status.get("review_count") or 0) == 0:
            actions.append({
                "action": "auto_review_candidates" if automatic_demo else "review_candidates",
                "endpoint": "/api/ops/brain/governance-candidates/review",
                "executor": "autonomous_evolution_nursery" if automatic_demo else "operator",
                "reason": "candidate_review_required",
            })
        if "proposal_registry" in components:
            actions.append({
                "action": "auto_resolve_proposal_conflicts" if automatic_demo else "resolve_proposal_conflicts",
                "endpoint": "/api/ops/autonomy/proposals",
                "executor": "autonomous_evolution_nursery" if automatic_demo else "operator",
                "reason": "conflict_or_stale_evidence",
            })
        if not bool(effect.get("ok")):
            actions.append({
                "action": "auto_reconcile_effects" if automatic_demo else "reconcile_effects",
                "endpoint": "/api/ops/factor/governance-effects/reconcile",
                "executor": "autonomous_evolution_nursery" if automatic_demo else "operator",
                "reason": "effect_monitor_required",
            })
        return actions

    @staticmethod
    def _steps(
        *,
        live: dict[str, Any],
        evidence: dict[str, Any],
        proposal_status: dict[str, Any],
        candidate_status: dict[str, Any],
        candidate_review_status: dict[str, Any],
        replay: dict[str, Any],
        release: dict[str, Any],
        chain_health: dict[str, Any],
        effect: dict[str, Any],
        boundaries: dict[str, Any],
    ) -> list[dict[str, Any]]:
        loop = dict(live.get("loop") or {})
        return [
            {"step": "observe_runtime", "status": loop.get("status", "unknown"), "ok": bool(live)},
            {"step": "collect_evidence", "status": evidence.get("status"), "ok": bool(evidence.get("ok"))},
            {"step": "refresh_proposals", "status": "available" if proposal_status.get("proposal_count") else "empty", "ok": bool(proposal_status.get("ok"))},
            {"step": "review_candidates", "status": candidate_review_status.get("status"), "ok": bool(candidate_review_status.get("ok")), "bridge_ready_count": candidate_review_status.get("bridge_ready_count", 0)},
            {"step": "candidate_lane", "status": candidate_status.get("status"), "ok": bool(candidate_status.get("ok")), "candidate_count": candidate_status.get("candidate_count", 0)},
            {"step": "check_replay", "status": replay.get("status", "unknown"), "ok": str(replay.get("status") or "") not in {"missing", "stale", "error"}},
            {"step": "release_control", "status": release.get("status", "unknown"), "ok": str(release.get("status") or "") not in {"missing", "error"}},
            {"step": "single_apply_boundary", "status": "ok" if boundaries else "unknown", "ok": bool(boundaries)},
            {"step": "monitor_effect", "status": effect.get("status"), "ok": bool(effect.get("ok"))},
            {"step": "memory_scorecard_feedback", "status": chain_health.get("status"), "ok": bool(chain_health.get("ok"))},
        ]
