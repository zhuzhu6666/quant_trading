from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, is_state_db_path, state_table_exists
from backend.services.agent_authority import (
    AgentAuthorityRegistryService,
    infer_policy_suggestion_source_agent,
    policy_suggestion_requested_writes,
)
from backend.services._brain_helpers import connect as _connect, dumps as _dumps, execute as _execute, loads as _loads, safe_float as _safe_float


ACTIVE_STATUSES = {
    "active",
    "proposed",
    "reviewing",
    "reviewed",
    "shadow_recorded",
    "needs_evidence",
    "governance_ready",
    "applyable",
    "candidate_materialized",
    "auto_approved",
    "approved",
    "observing",
}
TERMINAL_STATUSES = {"applied", "rolled_back", "blocked", "blocked_by_risk", "superseded", "rejected"}
ACTIONABLE_STATUSES = {
    "active",
    "proposed",
    "reviewing",
    "governance_ready",
    "applyable",
    "candidate_materialized",
    "auto_approved",
    "approved",
}
INERT_ACTIVE_STATUSES = {
    "completed",
    "dry_run",
    "needs_evidence",
    "observing",
    "ok",
    "recorded",
    "reviewed",
    "shadow",
    "shadow_after_train",
    "shadow_recorded",
}
ACTIONABLE_ROUTES = {"request_review", "request_replay", "submit_governance", "tighten_incident"}
IGNORED_MAINTENANCE_ACTIONS = {
    "sync_runtime_parameter_templates",
    "upsert_samples",
    "rebuild_contract_json",
    "review_suggestion",
    "meta_model_shadow_audit",
}
HIGH_IMPACTS = {"high", "critical", "live", "live_trading", "high_impact"}
SOURCE_BASE_RELIABILITY = {
    "policy_suggestion": 0.62,
    "brain_governance_candidate": 0.58,
    "brain_action_plan": 0.42,
    "learning_application_log": 0.74,
    "evolution_decision": 0.66,
    "live_autonomy_unlock_event": 0.72,
    "llm_advisory_audit": 0.24,
    "open_quality_shadow_audit": 0.36,
    "position_quality_shadow_audit": 0.36,
    "factor_governance_shadow_audit": 0.36,
    "meta_model_shadow_audit": 0.36,
}
FRESHNESS_LIMIT_SECONDS = {
    "high": 2 * 3600.0,
    "critical": 2 * 3600.0,
    "live": 2 * 3600.0,
    "medium": 12 * 3600.0,
    "low": 24 * 3600.0,
    "shadow": 7 * 24 * 3600.0,
    "observe": 7 * 24 * 3600.0,
}


def ensure_proposal_registry_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS proposal_registry (
                proposal_id TEXT PRIMARY KEY,
                source_agent TEXT DEFAULT '',
                source_ref_type TEXT DEFAULT '',
                source_ref_id TEXT DEFAULT '',
                proposal_type TEXT DEFAULT '',
                control_surface TEXT DEFAULT '',
                target_scope TEXT DEFAULT '',
                impact_level TEXT DEFAULT '',
                confidence REAL DEFAULT 0.0,
                evidence_refs_json TEXT NOT NULL DEFAULT '{}',
                counter_evidence_refs_json TEXT NOT NULL DEFAULT '{}',
                required_gate_json TEXT NOT NULL DEFAULT '[]',
                risk_verdict_json TEXT NOT NULL DEFAULT '{}',
                decision_policy_preview_json TEXT NOT NULL DEFAULT '{}',
                expected_effect_json TEXT NOT NULL DEFAULT '{}',
                rollback_plan_json TEXT NOT NULL DEFAULT '{}',
                source_reliability_json TEXT NOT NULL DEFAULT '{}',
                evidence_freshness_json TEXT NOT NULL DEFAULT '{}',
                status TEXT DEFAULT '',
                authority_state TEXT DEFAULT '',
                route_recommendation TEXT DEFAULT 'observe',
                conflict_json TEXT NOT NULL DEFAULT '{}',
                review_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _ensure_column(conn, db_path, "proposal_registry", "source_reliability_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, db_path, "proposal_registry", "evidence_freshness_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, db_path, "proposal_registry", "proposal_action", "TEXT DEFAULT ''")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_proposal_registry_updated ON proposal_registry(updated_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_proposal_registry_surface ON proposal_registry(control_surface, target_scope, status)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_proposal_registry_source ON proposal_registry(source_agent, source_ref_type, updated_at)")
        _execute(
            conn,
            "CREATE INDEX IF NOT EXISTS idx_proposal_registry_source_ref_updated_v2 "
            "ON proposal_registry(source_ref_id, updated_at DESC)",
        )
        conn.commit()
    finally:
        conn.close()


def _table_columns(conn: Any, db_path: str | Path, table: str) -> set[str]:
    if is_state_db_path(db_path):
        rows = _execute(
            conn,
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name=?
            """,
            (table,),
        ).fetchall()
        return {str(row["column_name"] if hasattr(row, "keys") else row[0]) for row in rows}
    rows = _execute(conn, f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"] if hasattr(row, "keys") else row[1]) for row in rows}


def _ensure_column(conn: Any, db_path: str | Path, table: str, column: str, ddl: str) -> None:
    if column in _table_columns(conn, db_path, table):
        return
    _execute(conn, f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _scope(scope_type: str, scope_key: str) -> str:
    scope_type = _text(scope_type, "unknown")
    scope_key = _text(scope_key, "global")
    return f"{scope_type}:{scope_key}"


def _control_surface(scope_type: str, action: str) -> str:
    return AgentAuthorityRegistryService.control_surface(scope_type, action)


def _proposal_type(scope_type: str, action: str) -> str:
    surface = _control_surface(scope_type, action)
    if surface == "factor_weight":
        return "factor_weight"
    if surface == "parameter_template":
        return "parameter_template"
    if surface == "position_supervisor_template":
        return "supervisor_template"
    if surface == "context_policy":
        return "context_policy"
    if surface == "incident_control":
        return "incident"
    if surface == "model_stage":
        return "model_stage"
    if surface == "replay":
        return "replay"
    return _text(action, surface)


def _impact_level(surface: str, status: str = "", raw: str = "") -> str:
    raw = _text(raw).lower()
    if raw:
        if "shadow" in raw:
            return "shadow"
        if "low" in raw:
            return "low"
        if "medium" in raw:
            return "medium"
        if "high" in raw or "live" in raw:
            return "high"
    if surface in {"factor_weight", "parameter_template", "position_supervisor_template", "context_policy"}:
        return "medium"
    if surface in {"incident_control", "model_stage"}:
        return "high"
    if surface == "replay":
        return "low"
    if _text(status).lower() in {"applied", "rolled_back"}:
        return "medium"
    return "observe"


def _required_gate(surface: str, action: str, source_agent: str) -> list[str]:
    return AgentAuthorityRegistryService().evaluate(source_agent, surface, action).get("required_gate", ["review"])


def _authority_state(source_agent: str, status: str, gates: list[str], impact: str) -> str:
    return AgentAuthorityRegistryService().authority_state(
        source_agent=source_agent,
        status=status,
        required_gate=gates,
        impact_level=impact,
    )


def _route(status: str, impact: str, conflict: bool, gates: list[str]) -> str:
    normalized = _text(status).lower()
    if normalized in TERMINAL_STATUSES:
        return "observe"
    if conflict:
        return "request_review"
    if impact in {"observe", "shadow"}:
        return "observe"
    if impact == "low":
        return "request_replay"
    if "RiskPolicyService" in gates or "DecisionPolicy" in gates:
        return "submit_governance"
    return "observe"


def _is_actionable_proposal(item: dict[str, Any]) -> bool:
    status = _text(item.get("status")).lower()
    if status in TERMINAL_STATUSES or status in INERT_ACTIVE_STATUSES:
        return False
    reliability = item.get("source_reliability") or {}
    if bool(reliability.get("advisory_only")):
        return False
    route = _text(item.get("route_recommendation")).lower()
    if route in ACTIONABLE_ROUTES:
        return True
    return status in ACTIONABLE_STATUSES


class ProposalRegistryService:
    """Unified read model for autonomous governance proposals.

    The registry maps existing ledgers into one envelope. It does not approve,
    apply, mutate runtime config, write source rows, or submit broker orders.
    """

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "proposal_registry_boundary.v1",
            "read_model": True,
            "does_not_submit_orders": True,
            "does_not_apply_runtime_mutations": True,
            "does_not_approve_llm_advisory": True,
            "does_not_change_source_status": True,
            "routes_to_existing_control_gates": True,
            "projection_compaction_preserves_source_ledgers": True,
        }

    def refresh(self, *, limit: int = 500) -> dict[str, Any]:
        ensure_proposal_registry_table(self.db_path)
        limit = max(1, min(int(limit), 5000))
        now = time.time()
        proposals = self._collect_source_proposals(limit=limit, now=now)
        conflicts = self._conflicts(proposals)
        by_id = {item["proposal_id"]: item for item in proposals}
        agent_scores = self._agent_scorecard_context(limit=limit)
        for proposal_id, conflict in conflicts.items():
            if proposal_id in by_id:
                by_id[proposal_id]["conflict"] = conflict
                by_id[proposal_id]["route_recommendation"] = _route(
                    by_id[proposal_id].get("status", ""),
                    by_id[proposal_id].get("impact_level", ""),
                    bool(conflict.get("conflict")),
                    by_id[proposal_id].get("required_gate", []),
                )
        for item in by_id.values():
            item["source_reliability"] = self._source_reliability(item)
            item["evidence_freshness"] = self._evidence_freshness(item, now=now)
            reliability_gate = self._agent_reliability_gate(item, agent_scores)
            item["source_reliability"]["agent_reliability_gate"] = reliability_gate
            if reliability_gate.get("review_strictness") == "high":
                item["source_reliability"]["band"] = "low"
                item["route_recommendation"] = "request_review"
                if _text(item.get("status")).lower() not in TERMINAL_STATUSES:
                    item["status"] = "needs_evidence"
            elif reliability_gate.get("priority_boost"):
                item["source_reliability"]["priority_boost"] = reliability_gate["priority_boost"]
            if (
                item["evidence_freshness"].get("status") == "stale"
                and item.get("route_recommendation") not in {"request_review", "tighten_incident", "observe"}
            ):
                item["route_recommendation"] = "request_replay"
        compaction = self.compact_projection()
        self._upsert(list(by_id.values()))
        projection_compaction = self._compact_duplicate_projection()
        summary = self.status(refresh=False)
        return {
            "ok": True,
            "schema_version": "proposal_registry_refresh.v1",
            "refreshed_count": len(by_id),
            "conflict_count": int(summary.get("conflict_count", 0)),
            "high_unresolved_conflict_count": int(summary.get("high_unresolved_conflict_count", 0)),
            "summary": summary,
            "compaction": compaction,
            "projection_compaction": projection_compaction,
            "boundary": self.boundary(),
        }

    def compact_projection(
        self,
        *,
        retention_seconds: float = 30 * 86400.0,
        delete_limit: int = 1000,
    ) -> dict[str, Any]:
        """Trim old terminal/inert read-model rows; authoritative ledgers remain intact."""
        ensure_proposal_registry_table(self.db_path)
        cutoff = time.time() - max(7 * 86400.0, float(retention_seconds or 0.0))
        limit = max(1, min(int(delete_limit or 1000), 5000))
        conn = _connect(self.db_path)
        try:
            rows = _execute(
                conn,
                """
                SELECT proposal_id
                FROM proposal_registry
                WHERE updated_at<?
                  AND status IN (
                      'applied', 'rolled_back', 'blocked', 'blocked_by_risk',
                      'superseded', 'rejected', 'completed', 'dry_run',
                      'ok', 'recorded', 'shadow_after_train'
                  )
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
            proposal_ids = [str(row["proposal_id"] or "") for row in rows if str(row["proposal_id"] or "")]
            for proposal_id in proposal_ids:
                _execute(conn, "DELETE FROM proposal_registry WHERE proposal_id=?", (proposal_id,))
            conn.commit()
        finally:
            conn.close()
        ignored_deleted = 0
        ignored = sorted(IGNORED_MAINTENANCE_ACTIONS)
        if ignored:
            conn = _connect(self.db_path)
            try:
                placeholders = ",".join("?" for _ in ignored)
                row = _execute(
                    conn,
                    f"SELECT COUNT(*) AS n FROM proposal_registry WHERE proposal_action IN ({placeholders})",
                    tuple(ignored),
                ).fetchone()
                ignored_deleted = int(row["n"] or 0) if row else 0
                _execute(
                    conn,
                    f"DELETE FROM proposal_registry WHERE proposal_action IN ({placeholders})",
                    tuple(ignored),
                )
                conn.commit()
            finally:
                conn.close()
        return {
            "ok": True,
            "schema_version": "proposal_registry_projection_compaction.v1",
            "deleted_count": len(proposal_ids) + ignored_deleted,
            "maintenance_deleted_count": ignored_deleted,
            "retention_seconds": max(7 * 86400.0, float(retention_seconds or 0.0)),
            "source_ledgers_preserved": True,
        }

    def _agent_scorecard_context(self, *, limit: int) -> dict[str, dict[str, Any]]:
        try:
            from backend.services.agent_scorecard import AgentScorecardService

            scorecard = AgentScorecardService(self.db_path).scorecard(limit=max(100, min(int(limit), 1000)))
            return {
                str(item.get("source_agent") or ""): item
                for item in (scorecard.get("items") or [])
                if str(item.get("source_agent") or "")
            }
        except Exception:
            return {}

    @staticmethod
    def _agent_reliability_gate(item: dict[str, Any], scorecard: dict[str, dict[str, Any]]) -> dict[str, Any]:
        source_agent = _text(item.get("source_agent"), "unknown")
        metric = scorecard.get(source_agent) or {}
        score = _safe_float(metric.get("quality_score"), 0.55)
        contract_violations = int(metric.get("contract_violation_count") or 0)
        negative_effects = int(metric.get("negative_effect_count") or 0)
        positive_effects = int(metric.get("positive_effect_count") or 0)
        terminal_effects = int(metric.get("terminal_effect_count") or 0)
        low_reliability = int(metric.get("low_reliability_count") or 0)
        strictness = "normal"
        reasons: list[str] = []
        if contract_violations > 0:
            strictness = "high"
            reasons.append("agent_contract_violation_history")
        if score < 0.5:
            strictness = "high"
            reasons.append("agent_quality_score_below_0_50")
        elif score < 0.58:
            strictness = "elevated"
            reasons.append("agent_quality_score_below_0_58")
        if negative_effects > 0 and (positive_effects == 0 or negative_effects >= positive_effects):
            strictness = "high"
            reasons.append("agent_negative_effect_history")
        elif negative_effects > 0 and strictness == "normal":
            strictness = "elevated"
            reasons.append("agent_mixed_effect_history")
        if low_reliability >= 3 and strictness == "normal":
            strictness = "elevated"
            reasons.append("agent_low_reliability_history")
        priority_boost = score >= 0.7 and contract_violations == 0
        return {
            "schema_version": "agent_reliability_gate.v1",
            "source_agent": source_agent,
            "quality_score": round(score, 6),
            "verified_positive_effects": positive_effects,
            "verified_negative_effects": negative_effects,
            "terminal_effects": terminal_effects,
            "review_strictness": strictness,
            "reasons": sorted(set(reasons)),
            "required_evidence_level": "high" if strictness == "high" else ("normal_plus" if strictness == "elevated" else "normal"),
            "priority_boost": "review_first" if priority_boost else "",
            "does_not_expand_authority": True,
        }

    def latest(self, *, limit: int = 100, status: str = "", refresh: bool = False) -> dict[str, Any]:
        if refresh:
            self.refresh(limit=max(limit, 500))
        ensure_proposal_registry_table(self.db_path)
        limit = max(1, min(int(limit), 500))
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = _connect(self.db_path, read_only=True)
        try:
            rows = _execute(
                conn,
                f"""
                SELECT *
                FROM proposal_registry
                {where}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                tuple(params + [limit]),
            ).fetchall()
            items = [self._row_to_proposal(row) for row in rows]
            return {
                "ok": True,
                "schema_version": "proposal_registry_list.v1",
                "items": items,
                "summary": self.status(refresh=False),
                "boundary": self.boundary(),
            }
        finally:
            conn.close()

    def get(self, proposal_id: str) -> dict[str, Any]:
        ensure_proposal_registry_table(self.db_path)
        conn = _connect(self.db_path, read_only=True)
        try:
            row = _execute(conn, "SELECT * FROM proposal_registry WHERE proposal_id=? LIMIT 1", (proposal_id,)).fetchone()
            if not row:
                return {
                    "ok": False,
                    "schema_version": "proposal_registry_item.v1",
                    "status": "missing",
                    "proposal_id": proposal_id,
                    "boundary": self.boundary(),
                }
            return {
                "ok": True,
                "schema_version": "proposal_registry_item.v1",
                "proposal": self._row_to_proposal(row),
                "boundary": self.boundary(),
            }
        finally:
            conn.close()

    def review(
        self,
        proposal_id: str,
        *,
        actor: str = "api:ops.autonomy.proposals",
        decision: str = "reviewed",
        route: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        ensure_proposal_registry_table(self.db_path)
        proposal = self.get(proposal_id).get("proposal")
        if not proposal:
            return {"ok": False, "status": "missing_proposal", "proposal_id": proposal_id, "boundary": self.boundary()}
        decision = _text(decision, "reviewed").lower()
        route = _text(route, proposal.get("route_recommendation") or "observe")
        forbidden = {"approved", "applied", "auto_approved"}
        if decision in forbidden:
            return {
                "ok": False,
                "schema_version": "proposal_registry_review.v1",
                "status": "refused_authorizing_review",
                "proposal_id": proposal_id,
                "reason": "proposal_registry_review_cannot_authorize_or_apply",
                "boundary": self.boundary(),
            }
        review = {
            "schema_version": "proposal_registry_review.v1",
            "actor": actor,
            "decision": decision,
            "route": route,
            "notes": notes,
            "reviewed_at": time.time(),
            "llm_advisory_only": proposal.get("source_agent") == "llm_advisory",
        }
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                UPDATE proposal_registry
                SET status=?,
                    authority_state=?,
                    route_recommendation=?,
                    review_json=?,
                    updated_at=?
                WHERE proposal_id=?
                """,
                (
                    "reviewed",
                    "operator_reviewed_no_authorization",
                    route,
                    _dumps(review),
                    time.time(),
                    proposal_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "ok": True,
            "schema_version": "proposal_registry_review.v1",
            "status": "reviewed",
            "proposal_id": proposal_id,
            "review": review,
            "boundary": self.boundary(),
        }

    def _compact_duplicate_projection(self) -> dict[str, Any]:
        """Keep one current projection row per source/control/action surface."""
        conn = _connect(self.db_path)
        try:
            rows = _execute(
                conn,
                """SELECT proposal_id, source_agent, proposal_type, control_surface,
                          target_scope, proposal_action, updated_at
                   FROM proposal_registry
                   ORDER BY updated_at DESC, created_at DESC""",
            ).fetchall()
            keep: set[tuple[str, str, str, str, str]] = set()
            delete_ids: list[str] = []
            for row in rows:
                key = (
                    _text(row["source_agent"], "unknown"),
                    _text(row["proposal_type"], "unknown"),
                    _text(row["control_surface"], "unknown"),
                    _text(row["target_scope"], "unknown"),
                    _text(row["proposal_action"], "unknown"),
                )
                proposal_id = _text(row["proposal_id"])
                if key in keep:
                    delete_ids.append(proposal_id)
                else:
                    keep.add(key)
            for proposal_id in delete_ids:
                _execute(conn, "DELETE FROM proposal_registry WHERE proposal_id=?", (proposal_id,))
            conn.commit()
            return {
                "ok": True,
                "schema_version": "proposal_registry_duplicate_compaction.v1",
                "deleted_count": len(delete_ids),
                "source_ledgers_preserved": True,
            }
        finally:
            conn.close()

    def status(self, *, refresh: bool = False) -> dict[str, Any]:
        if refresh:
            self.refresh()
        ensure_proposal_registry_table(self.db_path)
        conn = _connect(self.db_path, read_only=True)
        try:
            rows = _execute(
                conn,
                """
                SELECT proposal_id, source_agent, source_ref_type, proposal_type,
                       proposal_action,
                       control_surface, target_scope, impact_level, source_reliability_json,
                       evidence_freshness_json, status, route_recommendation,
                       conflict_json
                FROM proposal_registry
                """,
            ).fetchall()
            items = [self._row_to_status_item(row) for row in rows]
        finally:
            conn.close()
        active = [item for item in items if _text(item.get("status")).lower() not in TERMINAL_STATUSES]
        actionable = [item for item in active if _is_actionable_proposal(item)]
        conflict_items = [item for item in actionable if bool((item.get("conflict") or {}).get("conflict"))]
        stale_items = [item for item in actionable if bool((item.get("evidence_freshness") or {}).get("stale"))]
        stale_replay_items = [
            item for item in stale_items if _text(item.get("route_recommendation")).lower() == "request_replay"
        ]
        stale_review_items = [
            item for item in stale_items if _text(item.get("route_recommendation")).lower() == "request_review"
        ]
        stale_tighten_items = [
            item for item in stale_items if _text(item.get("route_recommendation")).lower() == "tighten_incident"
        ]
        hard_stale_items = [
            item
            for item in stale_items
            if _text(item.get("route_recommendation")).lower()
            not in {"request_replay", "request_review", "tighten_incident", "observe"}
        ]
        low_reliability_items = [
            item
            for item in actionable
            if _text((item.get("source_reliability") or {}).get("band")).lower() == "low"
        ]
        raw_conflict_items = [item for item in active if bool((item.get("conflict") or {}).get("conflict"))]
        raw_stale_items = [item for item in active if bool((item.get("evidence_freshness") or {}).get("stale"))]
        raw_low_reliability_items = [
            item
            for item in active
            if _text((item.get("source_reliability") or {}).get("band")).lower() == "low"
        ]
        high_conflict = [
            item
            for item in conflict_items
            if _text(item.get("impact_level")).lower() in HIGH_IMPACTS
            or _text((item.get("conflict") or {}).get("severity")).lower() in {"high", "critical"}
        ]
        counts: dict[str, int] = {}
        for item in items:
            counts[_text(item.get("status"), "unknown")] = counts.get(_text(item.get("status"), "unknown"), 0) + 1
        duplicate_groups: dict[tuple[str, str, str, str, str], int] = {}
        for item in active:
            key = (
                _text(item.get("source_agent"), "unknown"),
                _text(item.get("proposal_type"), "unknown"),
                _text(item.get("control_surface"), "unknown"),
                _text(item.get("target_scope"), "unknown"),
                _text(item.get("proposal_action"), "unknown"),
            )
            duplicate_groups[key] = duplicate_groups.get(key, 0) + 1
        duplicate_group_items = [
            {
                "source_agent": key[0],
                "proposal_type": key[1],
                "control_surface": key[2],
                "target_scope": key[3],
                "proposal_action": key[4],
                "count": count,
            }
            for key, count in sorted(duplicate_groups.items(), key=lambda kv: kv[1], reverse=True)
            if count > 1
        ]
        conflict_groups: dict[str, int] = {}
        for item in conflict_items:
            conflict = item.get("conflict") or {}
            surface = _text(conflict.get("control_surface"), _text(item.get("control_surface"), "unknown"))
            conflict_groups[surface] = conflict_groups.get(surface, 0) + 1
        conflict_group_items = [
            {"control_surface": key, "count": count}
            for key, count in sorted(conflict_groups.items(), key=lambda kv: kv[1], reverse=True)
        ]
        return {
            "ok": True,
            "schema_version": "proposal_registry_status.v1",
            "proposal_count": len(items),
            "active_count": len(active),
            "actionable_count": len(actionable),
            "historical_noise_count": max(0, len(active) - len(actionable)),
            "needs_evidence_count": sum(1 for item in active if _text(item.get("status")).lower() == "needs_evidence"),
            "status_counts": counts,
            "conflict_count": len(conflict_items),
            "high_unresolved_conflict_count": len(high_conflict),
            "stale_evidence_count": len(stale_items),
            "stale_replay_required_count": len(stale_replay_items),
            "stale_review_required_count": len(stale_review_items),
            "stale_tighten_required_count": len(stale_tighten_items),
            "hard_stale_evidence_count": len(hard_stale_items),
            "low_reliability_count": len(low_reliability_items),
            "raw_conflict_count": len(raw_conflict_items),
            "raw_stale_evidence_count": len(raw_stale_items),
            "raw_low_reliability_count": len(raw_low_reliability_items),
            "duplicate_group_count": len(duplicate_group_items),
            "top_duplicate_groups": duplicate_group_items[:10],
            "conflict_group_count": len(conflict_group_items),
            "conflict_groups": conflict_group_items[:10],
            "boundary": self.boundary(),
        }

    def _row_to_status_item(self, row: Any) -> dict[str, Any]:
        return {
            "proposal_id": _text(row["proposal_id"]),
            "source_agent": _text(row["source_agent"]),
            "source_ref_type": _text(row["source_ref_type"]),
                "proposal_type": _text(row["proposal_type"]),
                "proposal_action": _text(row["proposal_action"]) if "proposal_action" in row.keys() else "",
            "control_surface": _text(row["control_surface"]),
            "target_scope": _text(row["target_scope"]),
            "impact_level": _text(row["impact_level"]),
            "source_reliability": _loads(row["source_reliability_json"], {}),
            "evidence_freshness": _loads(row["evidence_freshness_json"], {}),
            "status": _text(row["status"]),
            "route_recommendation": _text(row["route_recommendation"], "observe"),
            "conflict": _loads(row["conflict_json"], {}),
        }

    def generation_context_coverage(self, *, limit: int = 500) -> dict[str, Any]:
        """Audit whether source proposals carry the shared agent generation context.

        This is deliberately read-only. It treats explicit `agent_context_required`
        flags as the new contract and keeps older policy suggestions visible as
        legacy gaps instead of degrading runtime readiness.
        """
        limit = max(1, min(int(limit), 2000))
        boundary = {
            **self.boundary(),
            "schema_version": "proposal_generation_context_coverage_boundary.v1",
            "read_only_generation_context_audit": True,
            "does_not_modify_policy_suggestion": True,
            "does_not_refresh_proposal_registry": True,
            "does_not_apply_proposals": True,
        }
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "policy_suggestion"):
                return {
                    "ok": True,
                    "schema_version": "proposal_generation_context_coverage.v1",
                    "status": "ok",
                    "proposal_count": 0,
                    "covered_count": 0,
                    "missing_required_context_count": 0,
                    "legacy_missing_context_count": 0,
                    "coverage_ratio": 1.0,
                    "items": [],
                    "boundary": boundary,
                }
            rows = _execute(
                conn,
                """
                SELECT suggestion_id, scope_type, scope_key, action,
                       evidence_json, status, created_at
                FROM policy_suggestion
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()

        items = [self._proposal_generation_context_item(row) for row in rows]
        covered = [item for item in items if item.get("coverage_status") == "covered"]
        missing_required = [item for item in items if item.get("coverage_status") == "missing_required_agent_context"]
        legacy_missing = [item for item in items if item.get("coverage_status") == "legacy_missing_agent_context"]
        ratio = round(len(covered) / len(items), 6) if items else 1.0
        return {
            "ok": not missing_required,
            "schema_version": "proposal_generation_context_coverage.v1",
            "status": "degraded" if missing_required else "ok",
            "proposal_count": len(items),
            "covered_count": len(covered),
            "missing_required_context_count": len(missing_required),
            "legacy_missing_context_count": len(legacy_missing),
            "coverage_ratio": ratio,
            "items": items[:50],
            "boundary": boundary,
        }

    def repair_missing_generation_context(
        self,
        *,
        limit: int = 200,
        dry_run: bool = True,
        actor: str = "system:proposal_generation_context_repair",
    ) -> dict[str, Any]:
        """Attach current review context to required policy suggestions missing original context.

        The repair is explicit: it records that the original generation context was
        missing and stores a current AgentBriefing context for review/apply safety.
        """

        limit = max(1, min(int(limit), 1000))
        boundary = {
            **self.boundary(),
            "schema_version": "proposal_generation_context_repair_boundary.v1",
            "repairs_policy_suggestion_evidence_only": True,
            "does_not_approve_or_apply_proposals": True,
            "does_not_change_policy_suggestion_status": True,
            "repair_context_is_current_not_original": True,
        }
        conn = _connect(self.db_path, read_only=False)
        try:
            if not state_table_exists(conn, "policy_suggestion"):
                return {
                    "ok": True,
                    "schema_version": "proposal_generation_context_repair.v1",
                    "status": "missing_policy_suggestion",
                    "dry_run": dry_run,
                    "repaired_count": 0,
                    "items": [],
                    "boundary": boundary,
                }
            rows = _execute(
                conn,
                """
                SELECT suggestion_id, scope_type, scope_key, action,
                       confidence, evidence_json, status, created_at
                FROM policy_suggestion
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            items = []
            repaired = 0
            for row in rows:
                evidence = _loads(row["evidence_json"], {})
                if not isinstance(evidence, dict):
                    evidence = {}
                context = self._proposal_agent_context(evidence)
                required = self._proposal_agent_context_required(evidence)
                if not required or str(context.get("schema_version") or "") == "agent_generation_context.v1":
                    continue
                source_agent = infer_policy_suggestion_source_agent(
                    evidence,
                    scope_type=_text(row["scope_type"]),
                    action=_text(row["action"]),
                )
                repaired_evidence = self._repaired_generation_context_evidence(
                    evidence,
                    source_agent=source_agent,
                    scope_type=_text(row["scope_type"]),
                    action=_text(row["action"]),
                    status=_text(row["status"], "proposed"),
                    actor=actor,
                )
                item = {
                    "suggestion_id": _text(row["suggestion_id"]),
                    "source_agent": source_agent,
                    "scope_type": _text(row["scope_type"]),
                    "scope_key": _text(row["scope_key"]),
                    "action": _text(row["action"]),
                    "status": _text(row["status"]),
                    "candidate_id": _text(evidence.get("candidate_id")),
                    "repair_status": "would_repair" if dry_run else "repaired",
                }
                items.append(item)
                if dry_run:
                    continue
                _execute(
                    conn,
                    """
                    UPDATE policy_suggestion
                    SET evidence_json=?
                    WHERE suggestion_id=?
                    """,
                    (_dumps(repaired_evidence), _text(row["suggestion_id"])),
                )
                repaired += 1
            if not dry_run:
                conn.commit()
            return {
                "ok": True,
                "schema_version": "proposal_generation_context_repair.v1",
                "status": "dry_run" if dry_run else "repaired",
                "dry_run": dry_run,
                "candidate_count": len(items),
                "repaired_count": repaired,
                "items": items[:50],
                "boundary": boundary,
            }
        finally:
            conn.close()

    def _repaired_generation_context_evidence(
        self,
        evidence: dict[str, Any],
        *,
        source_agent: str,
        scope_type: str,
        action: str,
        status: str,
        actor: str,
    ) -> dict[str, Any]:
        from backend.services.agent_scorecard import AgentScorecardService

        impact_level = _impact_level(_control_surface(scope_type, action), status)
        requested_writes = policy_suggestion_requested_writes(source_agent, evidence)
        authority = AgentAuthorityRegistryService().evaluate_scope_write(
            source_agent,
            scope_type,
            action,
            requested_writes=requested_writes,
            status=status,
            impact_level=impact_level,
        )
        scorecard = AgentScorecardService(self.db_path).scorecard(limit=200)
        canonical_source = str(authority.get("canonical_source_agent") or source_agent)
        source_scorecard = next(
            (
                item
                for item in scorecard.get("items", [])
                if str(item.get("source_agent") or "") in {source_agent, canonical_source}
            ),
            {},
        )
        trade_feedback = AgentScorecardService(self.db_path).latest_trade_attributions(
            limit=20,
            include_external_links=False,
        )
        recent_loss_feedback = [
            item
            for item in trade_feedback.get("items", [])
            if source_agent in set(item.get("feedback_targets") or [])
            or canonical_source in set(item.get("feedback_targets") or [])
        ][:10]
        context = {
            "ok": True,
            "schema_version": "agent_generation_context.v1",
            "source_agent": source_agent,
            "canonical_source_agent": canonical_source,
            "scope_type": scope_type,
            "action": action,
            "requested_writes": requested_writes,
            "authority_verdict": authority,
            "scorecard": source_scorecard,
            "recent_loss_feedback": recent_loss_feedback,
            "review_rules": {
                "low_score_requires_extra_evidence": True,
                "contract_violation_blocks_auto_bridge": True,
                "negative_feedback_requires_counter_evidence": True,
                "high_score_changes_priority_only": True,
                "never_expands_execution_authority": True,
            },
            "context_status": "repair_current_context",
            "repair_notice": {
                "original_generation_context_missing": True,
                "repair_source": "ProposalRegistryService.repair_missing_generation_context",
                "actor": actor,
                "repaired_at": time.time(),
            },
            "generated_at": time.time(),
            "boundary": {
                "read_only_context": True,
                "does_not_apply_policy_suggestion": True,
                "does_not_expand_authority": True,
                "repair_context_is_current_not_original": True,
            },
        }
        repaired = dict(evidence)
        repaired["agent_context_required"] = True
        repaired["agent_context"] = context
        repaired["agent_generation_context"] = context
        lineage = dict(repaired.get("lineage") or {})
        lineage["agent_generation_context"] = context
        repaired["lineage"] = lineage
        repaired["agent_generation_context_repair"] = {
            "schema_version": "proposal_generation_context_repair.v1",
            "actor": actor,
            "original_generation_context_missing": True,
            "repair_context_is_current_not_original": True,
            "repaired_at": context["repair_notice"]["repaired_at"],
        }
        return repaired

    def _collect_source_proposals(self, *, limit: int, now: float) -> list[dict[str, Any]]:
        conn = _connect(self.db_path, read_only=True)
        try:
            items: list[dict[str, Any]] = []
            items.extend(self._from_policy_suggestions(conn, limit=limit, now=now))
            items.extend(self._from_brain_governance_candidates(conn, limit=limit, now=now))
            items.extend(self._from_brain_action_plans(conn, limit=limit, now=now))
            items.extend(self._from_learning_applications(conn, limit=limit, now=now))
            items.extend(self._from_evolution_decisions(conn, limit=limit, now=now))
            items.extend(self._from_live_autonomy_events(conn, limit=limit, now=now))
            items.extend(self._from_llm_advisory(conn, limit=limit, now=now))
            items.extend(self._from_shadow_audits(conn, limit=limit, now=now))
            return self._compact_source_proposals(items)
        finally:
            conn.close()

    @staticmethod
    def _compact_source_proposals(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collapse repeated source events into the newest read-model row."""
        latest: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for item in items:
            key = (
                _text(item.get("source_agent"), "unknown"),
                _text(item.get("proposal_type"), "unknown"),
                _text(item.get("control_surface"), "unknown"),
                _text(item.get("target_scope"), "unknown"),
                _text(item.get("proposal_action"), "unknown"),
            )
            current = latest.get(key)
            if current is None or (
                _safe_float(item.get("updated_at")), _safe_float(item.get("created_at"))
            ) >= (
                _safe_float(current.get("updated_at")), _safe_float(current.get("created_at"))
            ):
                latest[key] = item
        return list(latest.values())

    def _from_policy_suggestions(self, conn: Any, *, limit: int, now: float) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "policy_suggestion"):
            return []
        rows = _execute(
            conn,
            """
            SELECT suggestion_id, scope_type, scope_key, action, confidence, reason,
                   evidence_json, status, reviewed_at, review_note, created_at
            FROM policy_suggestion
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        items = []
        for row in rows:
            evidence = _loads(row["evidence_json"], {})
            scope_type = _text(row["scope_type"])
            scope_key = _text(row["scope_key"])
            action = _text(row["action"])
            if action in IGNORED_MAINTENANCE_ACTIONS:
                continue
            surface = _control_surface(scope_type, action)
            status = _text(row["status"], "proposed")
            source_agent = infer_policy_suggestion_source_agent(evidence, scope_type=scope_type, action=action)
            authority = AgentAuthorityRegistryService().evaluate(
                source_agent,
                surface,
                action,
                requested_writes=policy_suggestion_requested_writes(source_agent, evidence),
                status=status,
                impact_level=_impact_level(surface, status),
            )
            gates = authority["required_gate"]
            impact = _impact_level(surface, status)
            items.append(self._proposal(
                proposal_id=f"policy_suggestion:{row['suggestion_id']}",
                source_agent=source_agent,
                source_ref_type="policy_suggestion",
                source_ref_id=_text(row["suggestion_id"]),
                proposal_type=_proposal_type(scope_type, action),
                proposal_action=action,
                control_surface=surface,
                target_scope=_scope(scope_type, scope_key),
                impact_level=impact,
                confidence=_safe_float(row["confidence"]),
                evidence_refs={"policy_suggestion": row["suggestion_id"], "evidence": evidence, "reason": row["reason"]},
                counter_evidence_refs={},
                required_gate=gates,
                risk_verdict=evidence.get("risk_verdict") or {},
                decision_policy_preview=evidence.get("decision_policy") or evidence.get("decision_policy_preview") or {},
                expected_effect=evidence.get("expected_effect") or {},
                rollback_plan=evidence.get("rollback_plan") or evidence.get("rollback") or {},
                status=status,
                authority_state=authority["authority_state"],
                route_recommendation=_route(status, impact, False, gates),
                created_at=_safe_float(row["created_at"], now),
                updated_at=max(_safe_float(row["reviewed_at"]), _safe_float(row["created_at"], now)),
            ))
        return items

    @staticmethod
    def _proposal_agent_context(evidence: dict[str, Any]) -> dict[str, Any]:
        for key in ("agent_generation_context", "agent_context"):
            value = evidence.get(key)
            if isinstance(value, dict):
                return value
        lineage = evidence.get("lineage")
        if isinstance(lineage, dict):
            for key in ("agent_generation_context", "agent_context"):
                value = lineage.get(key)
                if isinstance(value, dict):
                    return value
        return {}

    @staticmethod
    def _proposal_agent_context_required(evidence: dict[str, Any]) -> bool:
        if bool(evidence.get("agent_context_required")):
            return True
        lineage = evidence.get("lineage")
        if isinstance(lineage, dict) and bool(lineage.get("agent_context_required")):
            return True
        bridge = evidence.get("bridge")
        if isinstance(bridge, dict) and bool(bridge.get("candidate_review_required")):
            return True
        return False

    @staticmethod
    def _proposal_generation_context_item(row: Any) -> dict[str, Any]:
        evidence = _loads(row["evidence_json"], {})
        if not isinstance(evidence, dict):
            evidence = {}
        source_agent = infer_policy_suggestion_source_agent(
            evidence,
            scope_type=_text(row["scope_type"]),
            action=_text(row["action"]),
        )
        canonical_source = AgentAuthorityRegistryService.canonical_source(source_agent)
        registered = bool(AgentAuthorityRegistryService._source_contract(canonical_source))
        context = ProposalRegistryService._proposal_agent_context(evidence)
        required = ProposalRegistryService._proposal_agent_context_required(evidence)
        covered = str(context.get("schema_version") or "") == "agent_generation_context.v1"
        if covered:
            coverage_status = "covered"
        elif required:
            coverage_status = "missing_required_agent_context"
        elif registered:
            coverage_status = "legacy_missing_agent_context"
        else:
            coverage_status = "unknown_source_no_agent_context"
        return {
            "suggestion_id": _text(row["suggestion_id"]),
            "source_agent": source_agent,
            "canonical_source_agent": canonical_source,
            "registered_source": registered,
            "scope_type": _text(row["scope_type"]),
            "scope_key": _text(row["scope_key"]),
            "action": _text(row["action"]),
            "status": _text(row["status"]),
            "created_at": _safe_float(row["created_at"]),
            "agent_context_required": required,
            "agent_context_present": covered,
            "agent_context_schema": str(context.get("schema_version") or ""),
            "coverage_status": coverage_status,
            "evidence_schema": str(evidence.get("schema_version") or ""),
            "candidate_id": _text(evidence.get("candidate_id")),
        }

    def _from_brain_governance_candidates(self, conn: Any, *, limit: int, now: float) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "brain_governance_candidate"):
            return []
        rows = _execute(
            conn,
            """
            SELECT candidate_id, source_agent, source_kind, source_ref_type, source_ref_id,
                   proposal_stage, capability_scope, scope_type, scope_key, action,
                   confidence, evidence_score, risk_class, max_impact, expected_effect_json,
                   evidence_refs_json, counter_evidence_refs_json, risk_verdict_json,
                   decision_policy_json, rollback_plan_json, status, submitted_suggestion_id,
                   created_at, updated_at
            FROM brain_governance_candidate
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        items = []
        for row in rows:
            scope_type = _text(row["scope_type"])
            scope_key = _text(row["scope_key"])
            action = _text(row["action"])
            surface = _control_surface(scope_type, action)
            status = _text(row["status"], "active")
            impact = _impact_level(surface, status, row["max_impact"])
            source_agent = _text(row["source_agent"], "v16_brain")
            authority = AgentAuthorityRegistryService().evaluate(
                source_agent,
                surface,
                action,
                requested_writes=["brain_governance_candidate"],
                status=status,
                impact_level=impact,
            )
            gates = authority["required_gate"]
            items.append(self._proposal(
                proposal_id=f"brain_governance_candidate:{row['candidate_id']}",
                source_agent=source_agent,
                source_ref_type="brain_governance_candidate",
                source_ref_id=_text(row["candidate_id"]),
                proposal_type=_proposal_type(scope_type, action),
                proposal_action=action,
                control_surface=surface,
                target_scope=_scope(scope_type, scope_key),
                impact_level=impact,
                confidence=_safe_float(row["confidence"]),
                evidence_refs=_loads(row["evidence_refs_json"], {}),
                counter_evidence_refs=_loads(row["counter_evidence_refs_json"], {}),
                required_gate=gates,
                risk_verdict=_loads(row["risk_verdict_json"], {}),
                decision_policy_preview=_loads(row["decision_policy_json"], {}),
                expected_effect=_loads(row["expected_effect_json"], {}),
                rollback_plan=_loads(row["rollback_plan_json"], {}),
                status=status,
                authority_state=authority["authority_state"],
                route_recommendation=_route(status, impact, False, gates),
                created_at=_safe_float(row["created_at"], now),
                updated_at=_safe_float(row["updated_at"], now),
            ))
        return items

    def _from_brain_action_plans(self, conn: Any, *, limit: int, now: float) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "brain_action_plan"):
            return []
        rows = _execute(
            conn,
            """
            SELECT plan_id, action_type, status, scope_json, max_impact, risk_class,
                   validation_refs_json, rollback_plan_json, required_services_json,
                   shadow_eval_json, boundary_json, created_at
            FROM brain_action_plan
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        items = []
        for row in rows:
            scope = _loads(row["scope_json"], {})
            scope_type = _text(scope.get("scope_type") or scope.get("type"), _text(row["action_type"]))
            scope_key = _text(scope.get("scope_key") or scope.get("key"), "shadow")
            action = _text(row["action_type"])
            surface = _control_surface(scope_type, action)
            status = _text(row["status"], "shadow_recorded")
            impact = _impact_level(surface, status, row["max_impact"])
            gates = _list(_loads(row["required_services_json"], []))
            authority = AgentAuthorityRegistryService().evaluate(
                "v16_brain",
                surface,
                action,
                requested_writes=["brain_action_plan"],
                status=status,
                impact_level=impact,
            )
            gates = [str(item) for item in gates] or authority["required_gate"]
            items.append(self._proposal(
                proposal_id=f"brain_action_plan:{row['plan_id']}",
                source_agent="v16_brain",
                source_ref_type="brain_action_plan",
                source_ref_id=_text(row["plan_id"]),
                proposal_type=_proposal_type(scope_type, action),
                proposal_action=action,
                control_surface=surface,
                target_scope=_scope(scope_type, scope_key),
                impact_level=impact,
                confidence=0.0,
                evidence_refs={"validation_refs": _loads(row["validation_refs_json"], {}), "shadow_eval": _loads(row["shadow_eval_json"], {})},
                counter_evidence_refs={},
                required_gate=gates,
                risk_verdict={},
                decision_policy_preview={},
                expected_effect={},
                rollback_plan=_loads(row["rollback_plan_json"], {}),
                status=status,
                authority_state=authority["authority_state"],
                route_recommendation=_route(status, impact, False, gates),
                created_at=_safe_float(row["created_at"], now),
                updated_at=_safe_float(row["created_at"], now),
            ))
        return items

    def _from_learning_applications(self, conn: Any, *, limit: int, now: float) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "learning_application_log"):
            return []
        rows = _execute(
            conn,
            """
            SELECT application_id, cycle_ts, scope_type, scope_key, action,
                   suggestion_ids_json, status, details_json, created_at
            FROM learning_application_log
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        items = []
        for row in rows:
            scope_type = _text(row["scope_type"])
            scope_key = _text(row["scope_key"])
            action = _text(row["action"])
            surface = _control_surface(scope_type, action)
            status = _text(row["status"], "applied")
            impact = _impact_level(surface, status)
            authority = AgentAuthorityRegistryService().evaluate(
                "autonomous_learning",
                surface,
                action,
                requested_writes=["learning_application_log"],
                status=status,
                impact_level=impact,
            )
            gates = authority["required_gate"]
            details = _loads(row["details_json"], {})
            items.append(self._proposal(
                proposal_id=f"learning_application_log:{row['application_id']}",
                source_agent="autonomous_learning",
                source_ref_type="learning_application_log",
                source_ref_id=_text(row["application_id"]),
                proposal_type=_proposal_type(scope_type, action),
                proposal_action=action,
                control_surface=surface,
                target_scope=_scope(scope_type, scope_key),
                impact_level=impact,
                confidence=_safe_float(details.get("confidence")),
                evidence_refs={"suggestion_ids": _loads(row["suggestion_ids_json"], []), "details": details},
                counter_evidence_refs={},
                required_gate=gates,
                risk_verdict=details.get("risk_verdict") or {},
                decision_policy_preview=details.get("decision_policy") or {},
                expected_effect=details.get("expected_effect") or {},
                rollback_plan=details.get("rollback_json") or details.get("rollback_plan") or {},
                status=status,
                authority_state=authority["authority_state"],
                route_recommendation=_route(status, impact, False, gates),
                created_at=_safe_float(row["created_at"], _safe_float(row["cycle_ts"], now)),
                updated_at=_safe_float(row["created_at"], _safe_float(row["cycle_ts"], now)),
            ))
        return items

    def _from_evolution_decisions(self, conn: Any, *, limit: int, now: float) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "evolution_decision"):
            return []
        rows = _execute(
            conn,
            """
            SELECT decision_id, run_id, decision_type, scope_type, scope_key, action,
                   status, evidence_json, risk_verdict_json, result_json, rollback_json, created_at
            FROM evolution_decision
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        items = []
        for row in rows:
            if _text(row["decision_type"]) in {"manual_api_mutation", "autonomous_mutation"}:
                continue
            scope_type = _text(row["scope_type"])
            scope_key = _text(row["scope_key"])
            action = _text(row["action"] or row["decision_type"])
            if action in IGNORED_MAINTENANCE_ACTIONS:
                continue
            decision_evidence = _loads(row["evidence_json"], {})
            if not isinstance(decision_evidence, dict):
                decision_evidence = {}
            surface = _control_surface(scope_type, action)
            status = _text(row["status"], "recorded")
            impact = _impact_level(surface, status)
            authority = AgentAuthorityRegistryService().evaluate(
                "factor_governance",
                surface,
                action,
                requested_writes=["evolution_decision"],
                status=status,
                impact_level=impact,
            )
            gates = authority["required_gate"]
            items.append(self._proposal(
                proposal_id=f"evolution_decision:{row['decision_id']}",
                source_agent=_text(decision_evidence.get("source_agent"), "factor_governance"),
                source_ref_type="evolution_decision",
                source_ref_id=_text(row["decision_id"]),
                proposal_type=_proposal_type(scope_type, action),
                proposal_action=action,
                control_surface=surface,
                target_scope=_scope(scope_type, scope_key),
                impact_level=impact,
                confidence=0.0,
                evidence_refs={"run_id": row["run_id"], "evidence": decision_evidence, "result": _loads(row["result_json"], {})},
                counter_evidence_refs={},
                required_gate=gates,
                risk_verdict=_loads(row["risk_verdict_json"], {}),
                decision_policy_preview={},
                expected_effect={},
                rollback_plan=_loads(row["rollback_json"], {}),
                status=status,
                authority_state=authority["authority_state"],
                route_recommendation=_route(status, impact, False, gates),
                created_at=_safe_float(row["created_at"], now),
                updated_at=_safe_float(row["created_at"], now),
            ))
        return items

    def _from_live_autonomy_events(self, conn: Any, *, limit: int, now: float) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "live_autonomy_unlock_event"):
            return []
        rows = _execute(
            conn,
            """
            SELECT event_id, action, status, actor, reason, autonomy_mode_before,
                   autonomy_mode_after, readiness_json, proposal_registry_json,
                   risk_verdict_json, blockers_json, mutation_json, created_at
            FROM live_autonomy_unlock_event
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        items = []
        for row in rows:
            blockers = _loads(row["blockers_json"], [])
            if not isinstance(blockers, list):
                blockers = []
            risk_verdict = _loads(row["risk_verdict_json"], {})
            if not isinstance(risk_verdict, dict):
                risk_verdict = {}
            reason = _text(risk_verdict.get("reason"))
            blocker_components = {str(item.get("component") or "") for item in blockers if isinstance(item, dict)}
            budget_breach = reason == "live_autonomy_budget_breach" or "risk_policy_budget" in blocker_components
            if not budget_breach and _text(row["status"]) not in {"blocked", "degraded"}:
                continue
            action = "tighten_to_no_new_risk" if budget_breach else "review_live_autonomy_blocker"
            impact = "high" if budget_breach else "medium"
            authority = AgentAuthorityRegistryService().evaluate(
                "live_autonomy",
                "incident_control",
                action,
                requested_writes=["live_autonomy_unlock_event"],
                status="proposed",
                impact_level=impact,
            )
            gates = authority["required_gate"]
            items.append(self._proposal(
                proposal_id=f"live_autonomy_unlock_event:{row['event_id']}",
                source_agent="live_autonomy",
                source_ref_type="live_autonomy_unlock_event",
                source_ref_id=_text(row["event_id"]),
                proposal_type="incident",
                proposal_action=action,
                control_surface="incident_control",
                target_scope="runtime_incident_mode:no_new_risk" if budget_breach else "runtime_incident_mode:review",
                impact_level=impact,
                confidence=0.9 if budget_breach else 0.65,
                evidence_refs={
                    "event_id": row["event_id"],
                    "action": row["action"],
                    "status": row["status"],
                    "reason": row["reason"],
                    "readiness": _loads(row["readiness_json"], {}),
                    "proposal_registry": _loads(row["proposal_registry_json"], {}),
                    "risk_verdict": risk_verdict,
                    "blockers": blockers,
                },
                counter_evidence_refs={},
                required_gate=gates,
                risk_verdict=risk_verdict,
                decision_policy_preview={},
                expected_effect={
                    "incident_mode": "no_new_risk",
                    "blocks_new_risk": True,
                    "allows_risk_reducing_actions": True,
                } if budget_breach else {"requires_operator_review": True},
                rollback_plan={
                    "rollback_mode": "operator_confirmed_thaw",
                    "required_gate": "RiskPolicyService.evaluate('set_incident_control')",
                },
                status="proposed",
                authority_state=authority["authority_state"],
                route_recommendation="tighten_incident" if budget_breach else "request_review",
                created_at=_safe_float(row["created_at"], now),
                updated_at=_safe_float(row["created_at"], now),
            ))
        return items

    def _from_llm_advisory(self, conn: Any, *, limit: int, now: float) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "llm_advisory_audit"):
            return []
        rows = _execute(
            conn,
            """
            SELECT audit_id, task_type, target_type, target_id, status,
                   result_json, error, created_at
            FROM llm_advisory_audit
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        items = []
        for row in rows:
            scope_type = _text(row["target_type"], "llm_advisory")
            scope_key = _text(row["target_id"], row["audit_id"])
            action = _text(row["task_type"], "advisory_review")
            surface = _control_surface(scope_type, action)
            result = _loads(row["result_json"], {})
            authority = AgentAuthorityRegistryService().evaluate(
                "llm_advisory",
                surface,
                action,
                requested_writes=["llm_advisory_audit"],
                status=_text(row["status"], "recorded"),
                impact_level="observe",
            )
            gates = authority["required_gate"]
            items.append(self._proposal(
                proposal_id=f"llm_advisory_audit:{row['audit_id']}",
                source_agent="llm_advisory",
                source_ref_type="llm_advisory_audit",
                source_ref_id=_text(row["audit_id"]),
                proposal_type="llm_advisory",
                proposal_action=action,
                control_surface=surface,
                target_scope=_scope(scope_type, scope_key),
                impact_level="observe",
                confidence=0.0,
                evidence_refs={"result": result, "error": row["error"]},
                counter_evidence_refs={},
                required_gate=gates,
                risk_verdict={},
                decision_policy_preview={},
                expected_effect={},
                rollback_plan={},
                status=_text(row["status"], "recorded"),
                authority_state=authority["authority_state"],
                route_recommendation="observe",
                created_at=_safe_float(row["created_at"], now),
                updated_at=_safe_float(row["created_at"], now),
            ))
        return items

    def _from_shadow_audits(self, conn: Any, *, limit: int, now: float) -> list[dict[str, Any]]:
        specs = [
            ("open_quality_shadow_audit", "inference_id", "open_quality_model", "decision_id", "quality_score", "risk_score"),
            ("position_quality_shadow_audit", "inference_id", "position_quality_model", "position_id", "quality_score", "risk_score"),
            ("factor_governance_shadow_audit", "inference_id", "factor_governance_model", "factor", "weakness_score", "positive_score"),
            ("meta_model_shadow_audit", "audit_id", "meta_model", "model_type", "posture_score", "risk_score"),
        ]
        items: list[dict[str, Any]] = []
        for table, id_col, agent, target_col, primary_score, secondary_score in specs:
            if not state_table_exists(conn, table):
                continue
            try:
                rows = _execute(
                    conn,
                    f"""
                    SELECT *
                    FROM {table}
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (max(1, min(limit, 100)),),
                ).fetchall()
            except Exception:
                continue
            for row in rows:
                row_dict = dict(row)
                ref_id = _text(row_dict.get(id_col), _text(row_dict.get("inference_id"), _text(row_dict.get("audit_id"))))
                target = _text(row_dict.get(target_col), ref_id)
                score = _safe_float(row_dict.get(primary_score), _safe_float(row_dict.get(secondary_score)))
                status = _text(row_dict.get("mode"), "shadow")
                authority = AgentAuthorityRegistryService().evaluate(
                    "lightgbm_shadow_models",
                    "model_stage",
                    "shadow_model_audit",
                    requested_writes=[table],
                    status=status,
                    impact_level="shadow",
                )
                items.append(self._proposal(
                    proposal_id=f"{table}:{ref_id}",
                    source_agent="lightgbm_shadow_models",
                    source_ref_type=table,
                    source_ref_id=ref_id,
                    proposal_type="model_advisory",
                    proposal_action="shadow_model_audit",
                    control_surface="model_stage",
                    target_scope=_scope(table, target),
                    impact_level="shadow",
                    confidence=score,
                    evidence_refs={"shadow_audit": row_dict, "model_source": agent},
                    counter_evidence_refs={},
                    required_gate=authority["required_gate"],
                    risk_verdict={},
                    decision_policy_preview={},
                    expected_effect={},
                    rollback_plan={},
                    status=status,
                    authority_state=authority["authority_state"],
                    route_recommendation="observe",
                    created_at=_safe_float(row_dict.get("created_at"), now),
                    updated_at=_safe_float(row_dict.get("created_at"), now),
                ))
        return items

    def _proposal(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("proposal_action", kwargs.get("proposal_type") or kwargs.get("source_ref_type") or "unknown")
        kwargs.setdefault("source_reliability", {})
        kwargs.setdefault("evidence_freshness", {})
        kwargs.setdefault("conflict", {})
        kwargs.setdefault("review", {})
        return kwargs

    def _source_reliability(self, item: dict[str, Any]) -> dict[str, Any]:
        source_ref_type = _text(item.get("source_ref_type"), "unknown")
        source_agent = _text(item.get("source_agent"), source_ref_type)
        base = SOURCE_BASE_RELIABILITY.get(source_ref_type, SOURCE_BASE_RELIABILITY.get(source_agent, 0.5))
        score = float(base)
        drivers = [{"name": "base_source", "value": round(base, 3)}]
        confidence = _safe_float(item.get("confidence"))
        if confidence > 0:
            delta = (max(0.0, min(1.0, confidence)) - 0.5) * 0.2
            score += delta
            drivers.append({"name": "confidence", "value": round(delta, 3)})
        if item.get("risk_verdict"):
            score += 0.05
            drivers.append({"name": "risk_verdict_present", "value": 0.05})
        if item.get("decision_policy_preview"):
            score += 0.04
            drivers.append({"name": "decision_policy_preview_present", "value": 0.04})
        if item.get("rollback_plan"):
            score += 0.04
            drivers.append({"name": "rollback_plan_present", "value": 0.04})
        if item.get("counter_evidence_refs"):
            score -= 0.08
            drivers.append({"name": "counter_evidence_present", "value": -0.08})
        status = _text(item.get("status")).lower()
        if status in {"applied", "rolled_back"}:
            score += 0.06
            drivers.append({"name": "observed_application", "value": 0.06})
        advisory_only = (
            source_agent in {"llm_advisory", "lightgbm_shadow_models"}
            or source_ref_type == "llm_advisory_audit"
            or bool(((item.get("evidence_refs") or {}).get("evidence") or {}).get("advisory_only"))
        )
        if advisory_only:
            score = min(score, 0.35)
            drivers.append({"name": "advisory_only_cap", "value": 0.35})
        score = max(0.0, min(1.0, score))
        if score >= 0.7:
            band = "high"
        elif score >= 0.45:
            band = "medium"
        else:
            band = "low"
        return {
            "schema_version": "proposal_source_reliability.v1",
            "score": round(score, 3),
            "band": band,
            "source_agent": source_agent,
            "source_ref_type": source_ref_type,
            "drivers": drivers,
            "advisory_only": advisory_only,
        }

    def _evidence_freshness(self, item: dict[str, Any], *, now: float) -> dict[str, Any]:
        impact = _text(item.get("impact_level"), "observe").lower()
        stale_after = FRESHNESS_LIMIT_SECONDS.get(impact, FRESHNESS_LIMIT_SECONDS["observe"])
        timestamp = _safe_float(item.get("updated_at")) or _safe_float(item.get("created_at"))
        if timestamp <= 0:
            return {
                "schema_version": "proposal_evidence_freshness.v1",
                "status": "unknown",
                "age_seconds": None,
                "stale": True,
                "stale_after_seconds": stale_after,
                "reason": "missing_timestamp",
            }
        age = max(0.0, now - timestamp)
        stale = age > stale_after
        return {
            "schema_version": "proposal_evidence_freshness.v1",
            "status": "stale" if stale else "fresh",
            "age_seconds": round(age, 3),
            "stale": stale,
            "stale_after_seconds": stale_after,
            "timestamp": timestamp,
        }

    def _conflicts(self, proposals: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in proposals:
            status = _text(item.get("status")).lower()
            if status in TERMINAL_STATUSES:
                continue
            key = (_text(item.get("control_surface"), "unknown"), _text(item.get("target_scope"), "unknown"))
            grouped.setdefault(key, []).append(item)
        conflicts: dict[str, dict[str, Any]] = {}
        for (surface, target), items in grouped.items():
            sources = sorted({_text(item.get("source_agent"), "unknown") for item in items})
            actions = sorted({_text(item.get("proposal_action") or item.get("proposal_type") or item.get("source_ref_type"), "unknown") for item in items})
            if len(items) < 2 or (len(sources) < 2 and len(actions) < 2):
                continue
            severity = "high" if any(_text(item.get("impact_level")).lower() in HIGH_IMPACTS for item in items) else "medium"
            payload = {
                "schema_version": "proposal_conflict.v1",
                "conflict": True,
                "severity": severity,
                "control_surface": surface,
                "target_scope": target,
                "proposal_ids": [item["proposal_id"] for item in items],
                "sources": sources,
                "actions": actions,
            }
            for item in items:
                conflicts[item["proposal_id"]] = payload
        return conflicts

    def _upsert(self, proposals: list[dict[str, Any]]) -> None:
        if not proposals:
            return
        conn = _connect(self.db_path)
        try:
            for item in proposals:
                _execute(
                    conn,
                    """
                    INSERT INTO proposal_registry
                    (proposal_id, source_agent, source_ref_type, source_ref_id, proposal_type,
                     proposal_action, control_surface, target_scope, impact_level, confidence, evidence_refs_json,
                     counter_evidence_refs_json, required_gate_json, risk_verdict_json,
                     decision_policy_preview_json, expected_effect_json, rollback_plan_json,
                     source_reliability_json, evidence_freshness_json, status, authority_state,
                     route_recommendation, conflict_json, review_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (proposal_id) DO UPDATE SET
                        source_agent=excluded.source_agent,
                        source_ref_type=excluded.source_ref_type,
                        source_ref_id=excluded.source_ref_id,
                        proposal_type=excluded.proposal_type,
                        proposal_action=excluded.proposal_action,
                        control_surface=excluded.control_surface,
                        target_scope=excluded.target_scope,
                        impact_level=excluded.impact_level,
                        confidence=excluded.confidence,
                        evidence_refs_json=excluded.evidence_refs_json,
                        counter_evidence_refs_json=excluded.counter_evidence_refs_json,
                        required_gate_json=excluded.required_gate_json,
                        risk_verdict_json=excluded.risk_verdict_json,
                        decision_policy_preview_json=excluded.decision_policy_preview_json,
                        expected_effect_json=excluded.expected_effect_json,
                        rollback_plan_json=excluded.rollback_plan_json,
                        source_reliability_json=excluded.source_reliability_json,
                        evidence_freshness_json=excluded.evidence_freshness_json,
                        status=excluded.status,
                        authority_state=excluded.authority_state,
                        route_recommendation=excluded.route_recommendation,
                        conflict_json=excluded.conflict_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        item["proposal_id"],
                        item.get("source_agent", ""),
                        item.get("source_ref_type", ""),
                        item.get("source_ref_id", ""),
                        item.get("proposal_type", ""),
                        item.get("proposal_action", ""),
                        item.get("control_surface", ""),
                        item.get("target_scope", ""),
                        item.get("impact_level", ""),
                        _safe_float(item.get("confidence")),
                        _dumps(item.get("evidence_refs", {})),
                        _dumps(item.get("counter_evidence_refs", {})),
                        _dumps(item.get("required_gate", [])),
                        _dumps(item.get("risk_verdict", {})),
                        _dumps(item.get("decision_policy_preview", {})),
                        _dumps(item.get("expected_effect", {})),
                        _dumps(item.get("rollback_plan", {})),
                        _dumps(item.get("source_reliability", {})),
                        _dumps(item.get("evidence_freshness", {})),
                        item.get("status", ""),
                        item.get("authority_state", ""),
                        item.get("route_recommendation", "observe"),
                        _dumps(item.get("conflict", {})),
                        _dumps(item.get("review", {})),
                        _safe_float(item.get("created_at")),
                        _safe_float(item.get("updated_at")),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def _row_to_proposal(self, row: Any) -> dict[str, Any]:
        return {
            "proposal_id": _text(row["proposal_id"]),
            "source_agent": _text(row["source_agent"]),
            "source_ref_type": _text(row["source_ref_type"]),
            "source_ref_id": _text(row["source_ref_id"]),
            "proposal_type": _text(row["proposal_type"]),
            "proposal_action": _text(row["proposal_action"]) if "proposal_action" in row.keys() else "",
            "control_surface": _text(row["control_surface"]),
            "target_scope": _text(row["target_scope"]),
            "impact_level": _text(row["impact_level"]),
            "confidence": _safe_float(row["confidence"]),
            "evidence_refs": _loads(row["evidence_refs_json"], {}),
            "counter_evidence_refs": _loads(row["counter_evidence_refs_json"], {}),
            "required_gate": _loads(row["required_gate_json"], []),
            "risk_verdict": _loads(row["risk_verdict_json"], {}),
            "decision_policy_preview": _loads(row["decision_policy_preview_json"], {}),
            "expected_effect": _loads(row["expected_effect_json"], {}),
            "rollback_plan": _loads(row["rollback_plan_json"], {}),
            "source_reliability": _loads(row["source_reliability_json"], {}),
            "evidence_freshness": _loads(row["evidence_freshness_json"], {}),
            "status": _text(row["status"]),
            "authority_state": _text(row["authority_state"]),
            "route_recommendation": _text(row["route_recommendation"], "observe"),
            "conflict": _loads(row["conflict_json"], {}),
            "review": _loads(row["review_json"], {}),
            "created_at": _safe_float(row["created_at"]),
            "updated_at": _safe_float(row["updated_at"]),
        }
