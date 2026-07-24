"""V16 meta-brain command loop.

V16 owns prioritisation and delegation.  It may persist its own snapshots,
plans, evaluations, commands and governance candidates, but it never writes a
policy suggestion, runtime overlay, factor weight, order, or broker state.
Those mutations remain the responsibility of the existing downstream agent
and governor services.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, is_state_db_path, state_table_columns, state_table_exists
from backend.services._brain_helpers import connect, dumps, execute, loads, safe_float
from backend.services.v16_brain_planning import (
    BrainActionPlanEvaluatorService,
    BrainActionPlannerService,
    BrainMediumImpactGovernanceService,
)
from backend.services.v16_brain_snapshot import BrainStateService


def ensure_v16_brain_command_table(db_path: str | Path = STATE_DB) -> None:
    conn = connect(db_path)
    try:
        execute(
            conn,
            """CREATE TABLE IF NOT EXISTS v16_brain_command (
                command_id TEXT PRIMARY KEY,
                snapshot_id TEXT DEFAULT '',
                plan_id TEXT DEFAULT '',
                eval_id TEXT DEFAULT '',
                candidate_id TEXT DEFAULT '',
                target_agent TEXT DEFAULT '',
                scope_type TEXT DEFAULT '',
                scope_key TEXT DEFAULT '',
                action TEXT DEFAULT '',
                decision TEXT DEFAULT '',
                status TEXT DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                delegation_json TEXT NOT NULL DEFAULT '{}',
                claim_status TEXT NOT NULL DEFAULT 'available',
                claim_token TEXT DEFAULT '',
                claimed_at REAL NOT NULL DEFAULT 0.0,
                claim_expires_at REAL NOT NULL DEFAULT 0.0,
                apply_count INTEGER NOT NULL DEFAULT 0,
                max_apply_count INTEGER NOT NULL DEFAULT 1,
                consumed_at REAL NOT NULL DEFAULT 0.0,
                consumed_mutation_id TEXT DEFAULT '',
                posterior_fingerprint TEXT DEFAULT '',
                evidence_fingerprint TEXT DEFAULT '',
                last_release_reason TEXT DEFAULT '',
                finalized_at REAL NOT NULL DEFAULT 0.0,
                failure_reason TEXT DEFAULT '',
                authority_issued_at REAL NOT NULL DEFAULT 0.0,
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0
            )""",
        )
        columns = {
            "claim_status": "TEXT NOT NULL DEFAULT 'available'",
            "claim_token": "TEXT DEFAULT ''",
            "claimed_at": "REAL NOT NULL DEFAULT 0.0",
            "claim_expires_at": "REAL NOT NULL DEFAULT 0.0",
            "apply_count": "INTEGER NOT NULL DEFAULT 0",
            "max_apply_count": "INTEGER NOT NULL DEFAULT 1",
            "consumed_at": "REAL NOT NULL DEFAULT 0.0",
            "consumed_mutation_id": "TEXT DEFAULT ''",
            "posterior_fingerprint": "TEXT DEFAULT ''",
            "evidence_fingerprint": "TEXT DEFAULT ''",
            "last_release_reason": "TEXT DEFAULT ''",
            "finalized_at": "REAL NOT NULL DEFAULT 0.0",
            "failure_reason": "TEXT DEFAULT ''",
            "authority_issued_at": "REAL NOT NULL DEFAULT 0.0",
        }
        if is_state_db_path(db_path):
            for name, ddl in columns.items():
                execute(conn, f'ALTER TABLE v16_brain_command ADD COLUMN IF NOT EXISTS "{name}" {ddl}')
        else:
            existing = state_table_columns(conn, "v16_brain_command")
            for name, ddl in columns.items():
                if name not in existing:
                    execute(conn, f'ALTER TABLE v16_brain_command ADD COLUMN "{name}" {ddl}')
        if not is_state_db_path(db_path):
            execute(
                conn,
                """UPDATE v16_brain_command
                   SET authority_issued_at=CASE
                       WHEN created_at>0.0 THEN created_at ELSE updated_at END
                   WHERE authority_issued_at<=0.0""",
            )
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_v16_brain_command_created ON v16_brain_command(created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_v16_brain_command_target ON v16_brain_command(target_agent, status, created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_v16_brain_command_scope ON v16_brain_command(scope_type, scope_key, created_at)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_v16_brain_command_claim ON v16_brain_command(target_agent, scope_type, claim_status, claim_expires_at)")
        if not is_state_db_path(db_path):
            execute(conn, "CREATE INDEX IF NOT EXISTS idx_v16_brain_command_authority ON v16_brain_command(target_agent, decision, authority_issued_at)")
        conn.commit()
    finally:
        conn.close()


class V16BrainOrchestratorService:
    """Run the V16 perception -> judgement -> delegation loop."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "v16_brain_orchestrator_boundary.v1",
            "role": "meta_brain_command_and_delegation",
            "can_write": [
                "brain_state_snapshot",
                "brain_memory",
                "brain_action_plan",
                "brain_action_plan_eval",
                "brain_medium_impact_governance",
                "brain_governance_candidate",
                "v16_brain_command",
            ],
            "does_not_write": [
                "policy_suggestion",
                "runtime_config_overlay",
                "runtime_config_snapshot",
                "factor_weight",
                "learning_application_log",
                "order",
                "position",
                "broker_state",
            ],
            "command_owner": "v16_brain",
            "execution_owner": "downstream_specialist_agent_and_governor",
            "delegation_targets": [
                "autonomous_learning",
                "factor_governance",
                "position_supervisor_governance",
            ],
            "delegation_authority_source": "AgentAuthorityRegistryService.v16_brain.delegation_targets",
            "specialist_mutation_gate": "V16CommandGate",
            "specialist_mutations_require_recent_command": True,
            "risk_reduction_exception": "rollback_or_reduce_only",
            "requires_downstream_gates": ["RiskPolicyService", "DecisionPolicy", "RuntimeConfigMutationService"],
            "demo_bridge_owner": "AutonomousEvolutionNurseryRunner",
            "human_approval_required_in_demo": False,
            "human_approval_required_for_live_mutation": True,
        }

    def run_once(
        self,
        *,
        readiness: dict[str, Any] | None = None,
        limit: int = 20,
        source: str = "system:v16_brain_orchestrator",
        persist: bool = True,
    ) -> dict[str, Any]:
        ensure_v16_brain_command_table(self.db_path)
        limit = max(4, min(int(limit or 20), 50))
        if readiness is None:
            from backend.services.backend_readiness import BackendReadinessService

            readiness = BackendReadinessService(db_path=self.db_path).build()
        snapshot = BrainStateService(self.db_path).build(readiness=dict(readiness), persist=persist, source=source)
        plans_run = BrainActionPlannerService(self.db_path).build_plans(
            brain_state=snapshot,
            persist=persist,
            source=source,
        )
        evals_run = BrainActionPlanEvaluatorService(self.db_path).evaluate_latest_plans(
            limit=limit,
            persist=persist,
        )
        governance_run = BrainMediumImpactGovernanceService(self.db_path).materialize_latest(
            limit=limit,
            readiness=dict(readiness),
            persist=persist,
        )
        plans = {str(item.get("plan_id") or ""): item for item in plans_run.get("plans") or []}
        evals = list(evals_run.get("evals") or [])
        governance = {str(item.get("eval_id") or ""): item for item in governance_run.get("items") or []}
        evaluated_commands = [
            self._command_for_evaluation(
                snapshot=snapshot,
                plan=plans.get(str(evaluation.get("plan_id") or ""), {}),
                evaluation=evaluation,
                governance=governance.get(str(evaluation.get("eval_id") or ""), {}),
            )
            for evaluation in evals[:limit]
        ]
        raw_commands = [
            item for item in evaluated_commands if item.get("decision") == "delegate"
        ]
        if persist and raw_commands:
            active_candidate_ids = self._active_candidate_ids(
                [str(item.get("candidate_id") or "") for item in raw_commands]
            )
            raw_commands = [
                item
                for item in raw_commands
                if str(item.get("candidate_id") or "") in active_candidate_ids
            ]
        # Plan/eval tables are append-only audit ledgers.  Re-running the
        # coordinator therefore sees prior evaluations as well; the command
        # identity is deliberately posterior/scope based so the specialist
        # inbox remains idempotent.
        commands = self._dedupe_command_surfaces(raw_commands, limit=limit)
        if persist:
            self._persist_commands(commands)
        superseded = self._reconcile_stale_candidates(commands=commands, persist=persist)
        cancelled = self._cancel_non_actionable_commands(persist=persist)
        delegated = [item for item in commands if item.get("decision") == "delegate"]
        return {
            "ok": True,
            "schema_version": "v16_brain_orchestrator_run.v1",
            "status": "delegated" if delegated else "observing",
            "snapshot_id": snapshot.get("snapshot_id", ""),
            "plan_count": len(plans_run.get("plans") or []),
            "eval_count": len(evals),
            "governance_count": len(governance_run.get("items") or []),
            "command_count": len(commands),
            "delegated_count": len(delegated),
            "observation_count": len(evaluated_commands) - len(raw_commands),
            "cancelled_command_count": int(cancelled.get("cancelled_count") or 0),
            "cancelled_observation_count": int(
                cancelled.get("observation_count") or 0
            ),
            "cancelled_stale_delegate_count": int(
                cancelled.get("stale_delegate_count") or 0
            ),
            "superseded_candidate_count": len(superseded),
            "superseded_candidate_ids": superseded,
            "commands": commands,
            "posterior_arbitration": (snapshot.get("memory") or {}).get("posterior_arbitration") or {},
            "boundary": self.boundary(),
            "read_only_decision_layer": True,
            "direct_mutation": False,
            "created_at": time.time(),
        }

    def _active_candidate_ids(self, candidate_ids: list[str]) -> set[str]:
        candidate_ids = sorted({item for item in candidate_ids if item})
        if not candidate_ids:
            return set()
        conn = connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_governance_candidate"):
                return set()
            placeholders = ",".join("?" for _ in candidate_ids)
            rows = execute(
                conn,
                f"""
                SELECT candidate_id
                FROM brain_governance_candidate
                WHERE status='active' AND candidate_id IN ({placeholders})
                """,
                tuple(candidate_ids),
            ).fetchall()
            return {str(row["candidate_id"] or "") for row in rows}
        finally:
            conn.close()

    def _cancel_non_actionable_commands(self, *, persist: bool) -> dict[str, int]:
        """Terminalize observation records and delegates whose candidate is stale."""
        if not persist:
            return {
                "cancelled_count": 0,
                "observation_count": 0,
                "stale_delegate_count": 0,
            }
        conn = connect(self.db_path)
        try:
            if not state_table_exists(conn, "v16_brain_command"):
                return {
                    "cancelled_count": 0,
                    "observation_count": 0,
                    "stale_delegate_count": 0,
                }
            now = time.time()
            observation = execute(
                conn,
                """
                UPDATE v16_brain_command
                SET claim_status='cancelled',
                    failure_reason='observation_only_not_actionable',
                    finalized_at=?, updated_at=?
                WHERE decision='observe' AND claim_status='available'
                  AND COALESCE(apply_count, 0)=0
                """,
                (now, now),
            )
            stale = execute(
                conn,
                """
                UPDATE v16_brain_command
                SET claim_status='cancelled',
                    failure_reason='candidate_not_active',
                    finalized_at=?, updated_at=?
                WHERE decision='delegate' AND claim_status='available'
                  AND COALESCE(apply_count, 0)=0
                  AND EXISTS (
                      SELECT 1
                      FROM brain_governance_candidate candidate
                      WHERE candidate.candidate_id=v16_brain_command.candidate_id
                        AND candidate.status<>'active'
                  )
                """,
                (now, now),
            )
            conn.commit()
            observation_count = int(observation.rowcount or 0)
            stale_count = int(stale.rowcount or 0)
            return {
                "cancelled_count": observation_count + stale_count,
                "observation_count": observation_count,
                "stale_delegate_count": stale_count,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _reconcile_stale_candidates(self, *, commands: list[dict[str, Any]], persist: bool) -> list[str]:
        """Close old V16 candidates that no longer have a current delegate command.

        Candidate rows are an audit/governance surface, not runtime state.  A
        stale candidate must not remain bridgeable merely because an older
        review marked it ready.
        """
        allowed = {
            str(item.get("candidate_id") or "")
            for item in commands
            if item.get("decision") == "delegate" and item.get("candidate_id")
        }
        conn = connect(self.db_path)
        try:
            if not state_table_exists(conn, "brain_governance_candidate"):
                return []
            rows = execute(
                conn,
                """SELECT candidate_id, lineage_json
                   FROM brain_governance_candidate
                   WHERE source_agent='v16_brain' AND status='active'""",
            ).fetchall()
            superseded: list[str] = []
            now = time.time()
            if persist:
                for row in rows:
                    candidate_id = str(row["candidate_id"] or "")
                    if not candidate_id or candidate_id in allowed:
                        continue
                    lineage = loads(row["lineage_json"], {})
                    if not isinstance(lineage, dict):
                        lineage = {}
                    lineage["posterior_reconciliation"] = {
                        "status": "posterior_not_selected",
                        "command_owner": "v16_brain",
                        "reconciled_at": now,
                    }
                    execute(
                        conn,
                        """UPDATE brain_governance_candidate
                           SET status='superseded', proposal_stage='posterior_not_selected',
                               lineage_json=?, updated_at=?
                           WHERE candidate_id=? AND source_agent='v16_brain' AND status='active'""",
                        (dumps(lineage), now, candidate_id),
                    )
                    superseded.append(candidate_id)
                conn.commit()
            return superseded
        finally:
            conn.close()

    def status(self, *, limit: int = 50) -> dict[str, Any]:
        ensure_v16_brain_command_table(self.db_path)
        limit = max(1, min(int(limit or 50), 200))
        conn = connect(self.db_path, read_only=True)
        try:
            posterior_source_available = state_table_exists(conn, "supervisor_counterfactual_review")
            rows = execute(
                conn,
                """SELECT command_id, snapshot_id, plan_id, eval_id, candidate_id,
                   target_agent, scope_type, scope_key, action, decision, status,
                   evidence_json, delegation_json, claim_status, claim_token,
                   claim_expires_at, apply_count, max_apply_count, consumed_at,
                   consumed_mutation_id, posterior_fingerprint, evidence_fingerprint,
                   authority_issued_at, created_at, updated_at
                   FROM v16_brain_command ORDER BY created_at DESC LIMIT ?""",
                (min(1000, limit * 10),),
            ).fetchall()
            commands = self._dedupe_command_surfaces(
                [self._row_to_command(row) for row in rows],
                limit=limit,
            )
            latest_cf = 0.0
            latest_cf_updated = 0.0
            if state_table_exists(conn, "supervisor_counterfactual_review"):
                row = execute(
                    conn,
                    """
                    SELECT MAX(close_ts) AS latest_event,
                           MAX(updated_at) AS latest_updated
                    FROM supervisor_counterfactual_review
                    """,
                ).fetchone()
                latest_cf = safe_float(row["latest_event"] if row else 0.0)
                latest_cf_updated = safe_float(
                    row["latest_updated"] if row else 0.0
                )
            latest_brain_snapshot = 0.0
            if state_table_exists(conn, "brain_state_snapshot"):
                row = execute(
                    conn,
                    "SELECT MAX(created_at) AS latest FROM brain_state_snapshot",
                ).fetchone()
                latest_brain_snapshot = safe_float(
                    row["latest"] if row else 0.0
                )
            # Claim lifecycle timestamps are operational only. Closure uses
            # the evidence-bound V16 authority issuance time.
            latest_command = max(
                (
                    safe_float(item.get("authority_issued_at"))
                    or safe_float(item.get("created_at"))
                    for item in commands
                ),
                default=0.0,
            )
            posterior_closed = (
                latest_cf <= 0.0
                or max(latest_command, latest_brain_snapshot) >= latest_cf
            )
            candidate_commands = [item for item in commands if item.get("decision") == "delegate"]
            candidate_closed = all(bool(item.get("candidate_id")) for item in candidate_commands)
            lifecycle = execute(
                conn,
                """
                SELECT
                    SUM(CASE WHEN decision='delegate' AND claim_status IN ('available','claimed')
                             THEN 1 ELSE 0 END) AS actionable_count,
                    SUM(CASE WHEN claim_status='cancelled' THEN 1 ELSE 0 END) AS cancelled_count,
                    MIN(CASE WHEN decision='delegate' AND claim_status IN ('available','claimed')
                             THEN created_at ELSE NULL END) AS oldest_actionable_at
                FROM v16_brain_command
                """,
            ).fetchone()
            actionable_count = int((lifecycle["actionable_count"] if lifecycle else 0) or 0)
            cancelled_count = int((lifecycle["cancelled_count"] if lifecycle else 0) or 0)
            oldest_actionable_at = safe_float(
                lifecycle["oldest_actionable_at"] if lifecycle else 0.0
            )
            status = "healthy"
            if not posterior_source_available:
                status = "posterior_source_missing"
                posterior_closed = False
            elif latest_cf > 0 and not posterior_closed:
                status = "posterior_not_dispatched"
            elif candidate_commands and not candidate_closed:
                status = "command_candidate_gap"
            return {
                "ok": status == "healthy",
                "schema_version": "v16_brain_orchestrator_status.v1",
                "status": status,
                "command_count": len(commands),
                "delegated_count": len(candidate_commands),
                "actionable_command_count": actionable_count,
                "cancelled_command_count": cancelled_count,
                "oldest_actionable_at": oldest_actionable_at,
                "oldest_actionable_age_seconds": (
                    max(0.0, time.time() - oldest_actionable_at)
                    if oldest_actionable_at > 0.0
                    else 0.0
                ),
                "latest_command_created_at": latest_command,
                "latest_counterfactual_updated_at": latest_cf_updated,
                "latest_counterfactual_event_at": latest_cf,
                "latest_counterfactual_ledger_updated_at": latest_cf_updated,
                "latest_brain_snapshot_created_at": latest_brain_snapshot,
                "posterior_source_available": posterior_source_available,
                "posterior_to_brain_closed": posterior_closed,
                "command_to_candidate_closed": candidate_closed,
                "commands": commands,
                "boundary": self.boundary(),
            }
        finally:
            conn.close()

    def latest_commands(self, *, limit: int = 50) -> dict[str, Any]:
        status = self.status(limit=limit)
        return {
            "ok": bool(status.get("command_count")),
            "schema_version": "v16_brain_command_list.v1",
            "status": "available" if status.get("command_count") else "missing_commands",
            "commands": status.get("commands") or [],
            "boundary": self.boundary(),
        }

    def delegate_model_promotion(self, gate: dict[str, Any], *, persist: bool = True) -> dict[str, Any]:
        """Delegate one evidence-qualified model stage change to its specialist.

        This is a V16 command only: it does not change RuntimeConfig or grant
        the model broker/config permissions.  The specialist must still pass
        RiskPolicy and the runtime mutation gate.
        """
        model_type = str(gate.get("model_type") or "")
        if not bool(gate.get("passed")) or not model_type:
            return {
                "ok": False,
                "status": "model_promotion_evidence_not_ready",
                "model_type": model_type,
                "failed_checks": list(gate.get("failed_checks") or []),
                "boundary": self.boundary(),
            }
        evidence_fingerprint = hashlib.sha256(dumps({
            "model_type": model_type,
            "artifact_sha256": gate.get("artifact_sha256"),
            "feature_schema_version": gate.get("feature_schema_version"),
            "checks": gate.get("checks") or [],
        }).encode("utf-8")).hexdigest()
        now = time.time()
        command = {
            "command_id": f"v16cmd_{hashlib.sha1(('model|' + evidence_fingerprint).encode('utf-8')).hexdigest()[:20]}",
            "schema_version": "v16_brain_command.v1",
            "snapshot_id": "",
            "plan_id": "",
            "eval_id": "",
            "candidate_id": f"modelgate_{evidence_fingerprint[:16]}",
            "target_agent": "factor_governance",
            "scope_type": "model_stage",
            "scope_key": model_type,
            "action": "promote_model_influence",
            "decision": "delegate",
            "status": "delegated_to_specialist",
            "evidence": {
                "schema_version": "v16_model_promotion_evidence.v1",
                "promotion_gate": gate,
                "risk_reducing_or_veto_only": True,
            },
            "delegation": {
                "target_agent": "factor_governance",
                "delegated_by": "v16_brain",
                "specialist_must_use": ["RiskPolicyService", "RuntimeConfigMutationService"],
                "specialist_must_not": ["bypass_risk_policy", "write_broker_directly", "expand_hard_risk_limits"],
            },
            "posterior_fingerprint": "",
            "evidence_fingerprint": evidence_fingerprint,
            "max_apply_count": 1,
            "created_at": now,
            "updated_at": now,
            "boundary": self.boundary(),
        }
        if persist:
            ensure_v16_brain_command_table(self.db_path)
            self._persist_commands([command])
        return {"ok": True, "status": "delegated", "command": command, "boundary": self.boundary()}

    def delegate_entry_quality_control(
        self,
        gate: dict[str, Any],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Issue one evidence-bound Demo delegation for an unbiased v2 control."""

        evidence = dict(gate.get("evidence") or {})
        controls = dict(evidence.get("recommended_controls") or {})
        scan = dict(evidence.get("threshold_scan") or {})
        metrics = dict(scan.get("metrics") or {})
        threshold = safe_float(controls.get("min_abs_signal_score"))
        strong_override = safe_float(controls.get("strong_signal_override"))
        fingerprint = str(gate.get("governance_eligibility_fingerprint") or "")
        qualified = (
            str(gate.get("status") or "") == "approved"
            and bool(gate.get("governance_eligible"))
            and str(evidence.get("schema_version") or "")
            == "entry_quality_governance_evidence.v2"
            and bool(fingerprint)
            and 0.35 <= threshold <= 0.55
            and strong_override == 0.70
            and safe_float(scan.get("selected_threshold")) == threshold
            and int(metrics.get("sample_count") or 0) >= 20
            and int(metrics.get("bad_count") or 0) > 0
            and int(metrics.get("win_count") or 0) > 0
        )
        if not qualified:
            return {
                "ok": False,
                "status": "entry_quality_v2_evidence_not_ready",
                "failed_gate": {
                    "suggestion_status": gate.get("status"),
                    "governance_eligible": bool(gate.get("governance_eligible")),
                    "evidence_version": evidence.get("schema_version"),
                    "threshold": threshold,
                    "strong_signal_override": strong_override,
                    "sample_count": int(metrics.get("sample_count") or 0),
                    "bad_count": int(metrics.get("bad_count") or 0),
                    "win_count": int(metrics.get("win_count") or 0),
                },
                "boundary": self.boundary(),
            }
        now = time.time()
        suggestion_id = str(gate.get("suggestion_id") or "")
        command = {
            "command_id": (
                "v16cmd_"
                + hashlib.sha1(
                    f"entry_quality|weak_signal|{fingerprint}".encode("utf-8")
                ).hexdigest()[:20]
            ),
            "schema_version": "v16_brain_command.v1",
            "snapshot_id": "",
            "plan_id": "",
            "eval_id": "",
            "candidate_id": f"entryq_{fingerprint[:16]}",
            "target_agent": "autonomous_learning",
            "scope_type": "entry_quality",
            "scope_key": "weak_signal",
            "action": "activate_entry_quality_control",
            "decision": "delegate",
            "status": "delegated_to_specialist",
            "evidence": {
                "schema_version": "v16_entry_quality_control_evidence.v1",
                "suggestion_id": suggestion_id,
                "entry_quality_evidence": evidence,
                "qualified_v2_population": True,
            },
            "delegation": {
                "target_agent": "autonomous_learning",
                "delegated_by": "v16_brain",
                "specialist_must_use": [
                    "RiskPolicyService",
                    "GovernanceMutationCoordinator",
                ],
                "specialist_must_not": [
                    "bypass_risk_policy",
                    "write_runtime_overlay_directly",
                    "submit_order",
                ],
            },
            "posterior_fingerprint": "",
            "evidence_fingerprint": fingerprint,
            "max_apply_count": 1,
            "created_at": now,
            "updated_at": now,
            "boundary": self.boundary(),
        }
        if persist:
            ensure_v16_brain_command_table(self.db_path)
            self._persist_commands([command])
        return {
            "ok": True,
            "status": "delegated",
            "command": command,
            "boundary": self.boundary(),
        }

    def _command_for_evaluation(
        self,
        *,
        snapshot: dict[str, Any],
        plan: dict[str, Any],
        evaluation: dict[str, Any],
        governance: dict[str, Any],
    ) -> dict[str, Any]:
        scope = dict(plan.get("scope") or {})
        delegation = dict(scope.get("delegation") or {})
        target_agent = str(delegation.get("target_agent") or self._target_agent(str(evaluation.get("scope_type") or "")))
        posterior = dict((evaluation.get("comparison") or {}).get("posterior_arbitration") or {})
        candidate_id = str(governance.get("candidate_id") or "")
        decision = "delegate" if candidate_id and governance.get("status") == "candidate_materialized" else "observe"
        action = str(governance.get("governance_action") or evaluation.get("action_type") or "observe")
        status = "delegated_to_specialist" if decision == "delegate" else str(governance.get("status") or evaluation.get("comparison_verdict") or "observing")
        posterior_fingerprint = str(posterior.get("fingerprint") or "")
        command_scope_type = str(governance.get("scope_type") or evaluation.get("scope_type") or "")
        command_scope_key = str(governance.get("scope_key") or scope.get("scope_key") or "")
        # IDs and timestamps are audit coordinates, not new evidence. Hash only
        # the substantive verdict so a periodic rerun updates one command
        # instead of manufacturing a new command for the same posterior.
        evidence_fingerprint = hashlib.sha256(dumps({
            "posterior_fingerprint": posterior_fingerprint,
            "selected_scope": posterior.get("selected_scope"),
            "selected_conclusion": posterior.get("selected_conclusion"),
            "comparison_verdict": evaluation.get("comparison_verdict"),
            "coverage_score": round(safe_float(evaluation.get("coverage_score")), 6),
            "governance_status": governance.get("status"),
            "candidate_id": candidate_id,
            "scope_type": governance.get("scope_type") or evaluation.get("scope_type"),
            "scope_key": governance.get("scope_key") or scope.get("scope_key"),
            "action": action,
        }).encode("utf-8")).hexdigest()
        identity = "|".join(
            [
                posterior_fingerprint or str(evaluation.get("eval_id") or ""),
                evidence_fingerprint,
                command_scope_type,
                action,
                command_scope_key,
            ]
        )
        command_id = f"v16cmd_{hashlib.sha1(identity.encode('utf-8')).hexdigest()[:20]}"
        return {
            "command_id": command_id,
            "schema_version": "v16_brain_command.v1",
            "snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "plan_id": str(evaluation.get("plan_id") or ""),
            "eval_id": str(evaluation.get("eval_id") or ""),
            "candidate_id": candidate_id,
            "target_agent": target_agent,
            "scope_type": command_scope_type,
            "scope_key": command_scope_key,
            "action": action,
            "decision": decision,
            "status": status,
            "evidence": {
                "posterior_arbitration": posterior,
                "evaluation": {
                    "eval_id": evaluation.get("eval_id", ""),
                    "comparison_verdict": evaluation.get("comparison_verdict", ""),
                    "coverage_score": evaluation.get("coverage_score", 0.0),
                    "evidence_refs": evaluation.get("evidence_refs") or {},
                },
                "governance": {
                    "status": governance.get("status", ""),
                    "candidate_id": candidate_id,
                },
            },
            "delegation": {
                **delegation,
                "target_agent": target_agent,
                "delegated_by": "v16_brain",
                "specialist_must_use": ["RiskPolicyService", "DecisionPolicy", "RuntimeConfigMutationService"],
                "specialist_must_not": ["bypass_risk_policy", "bypass_decision_policy", "write_broker_directly"],
            },
            "posterior_fingerprint": posterior_fingerprint,
            "evidence_fingerprint": evidence_fingerprint,
            "max_apply_count": 1,
            "created_at": time.time(),
            "updated_at": time.time(),
            "boundary": self.boundary(),
        }

    @staticmethod
    def _target_agent(scope_type: str) -> str:
        return {
            "factor_weight": "factor_governance",
            "parameter_template": "autonomous_learning",
            "context_policy": "autonomous_learning",
            "supervisor_template": "position_supervisor_governance",
            "model_stage": "factor_governance",
        }.get(scope_type, "autonomous_learning")

    @staticmethod
    def _dedupe_command_surfaces(commands: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
        latest_by_surface: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for item in commands:
            surface = (
                str(item.get("target_agent") or ""),
                str(item.get("scope_type") or ""),
                str(item.get("scope_key") or ""),
                str(item.get("action") or ""),
            )
            current = latest_by_surface.get(surface)
            if current is None or safe_float(item.get("created_at")) >= safe_float(current.get("created_at")):
                latest_by_surface[surface] = item
        return sorted(
            latest_by_surface.values(),
            key=lambda item: safe_float(item.get("created_at")),
            reverse=True,
        )[:limit]

    def _persist_commands(self, commands: list[dict[str, Any]]) -> None:
        conn = connect(self.db_path)
        try:
            for item in commands:
                execute(
                    conn,
                    """INSERT INTO v16_brain_command
                    (command_id, snapshot_id, plan_id, eval_id, candidate_id, target_agent,
                     scope_type, scope_key, action, decision, status, evidence_json,
                     delegation_json, posterior_fingerprint, evidence_fingerprint,
                     max_apply_count, authority_issued_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(command_id) DO UPDATE SET
                      snapshot_id=excluded.snapshot_id,
                      plan_id=excluded.plan_id,
                      eval_id=excluded.eval_id,
                      candidate_id=excluded.candidate_id,
                      status=excluded.status,
                      evidence_json=excluded.evidence_json,
                      delegation_json=excluded.delegation_json,
                      posterior_fingerprint=excluded.posterior_fingerprint,
                      evidence_fingerprint=excluded.evidence_fingerprint,
                      max_apply_count=excluded.max_apply_count,
                      updated_at=excluded.updated_at""",
                    (
                        item["command_id"], item.get("snapshot_id", ""), item.get("plan_id", ""),
                        item.get("eval_id", ""), item.get("candidate_id", ""), item.get("target_agent", ""),
                        item.get("scope_type", ""), item.get("scope_key", ""), item.get("action", ""),
                        item.get("decision", ""), item.get("status", ""), dumps(item.get("evidence", {})),
                        dumps(item.get("delegation", {})),
                        str(item.get("posterior_fingerprint") or ""),
                        str(item.get("evidence_fingerprint") or ""),
                        max(1, int(item.get("max_apply_count") or 1)),
                        safe_float(item.get("authority_issued_at") or item.get("created_at")),
                        safe_float(item.get("created_at")),
                        safe_float(item.get("updated_at")),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_command(row: Any) -> dict[str, Any]:
        return {
            "command_id": str(row["command_id"] or ""),
            "schema_version": "v16_brain_command.v1",
            "snapshot_id": str(row["snapshot_id"] or ""),
            "plan_id": str(row["plan_id"] or ""),
            "eval_id": str(row["eval_id"] or ""),
            "candidate_id": str(row["candidate_id"] or ""),
            "target_agent": str(row["target_agent"] or ""),
            "scope_type": str(row["scope_type"] or ""),
            "scope_key": str(row["scope_key"] or ""),
            "action": str(row["action"] or ""),
                "decision": str(row["decision"] or ""),
                "status": str(row["status"] or ""),
                "claim_status": str(row["claim_status"] or "available"),
                "apply_count": int(row["apply_count"] or 0),
                "max_apply_count": int(row["max_apply_count"] or 1),
                "posterior_fingerprint": str(row["posterior_fingerprint"] or ""),
                "evidence_fingerprint": str(row["evidence_fingerprint"] or ""),
                "consumed_at": safe_float(row["consumed_at"]),
                "consumed_mutation_id": str(row["consumed_mutation_id"] or ""),
                "evidence": loads(row["evidence_json"], {}),
            "delegation": loads(row["delegation_json"], {}),
            "authority_issued_at": safe_float(row["authority_issued_at"]),
            "created_at": safe_float(row["created_at"]),
            "updated_at": safe_float(row["updated_at"]),
            "boundary": V16BrainOrchestratorService.boundary(),
        }
