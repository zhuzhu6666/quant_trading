from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, state_table_exists
from backend.services.agent_authority_registry import AgentAuthorityRegistryService
from backend.services.brain_action_planner import _connect, _dumps, _execute, _loads, _safe_float


BRIDGE_READY_STAGES = {"governance_ready", "applyable"}


def ensure_brain_governance_candidate_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS brain_governance_candidate (
                candidate_id TEXT PRIMARY KEY,
                source_agent TEXT DEFAULT '',
                source_kind TEXT DEFAULT '',
                source_ref_type TEXT DEFAULT '',
                source_ref_id TEXT DEFAULT '',
                proposal_stage TEXT DEFAULT 'brain_candidate',
                capability_scope TEXT DEFAULT '',
                scope_type TEXT DEFAULT '',
                scope_key TEXT DEFAULT '',
                action TEXT DEFAULT '',
                confidence REAL DEFAULT 0.0,
                evidence_score REAL DEFAULT 0.0,
                risk_class TEXT DEFAULT '',
                max_impact TEXT DEFAULT '',
                expected_effect_json TEXT NOT NULL DEFAULT '{}',
                evidence_refs_json TEXT NOT NULL DEFAULT '{}',
                counter_evidence_refs_json TEXT NOT NULL DEFAULT '{}',
                risk_verdict_json TEXT NOT NULL DEFAULT '{}',
                decision_policy_json TEXT NOT NULL DEFAULT '{}',
                rollback_plan_json TEXT NOT NULL DEFAULT '{}',
                lineage_json TEXT NOT NULL DEFAULT '{}',
                status TEXT DEFAULT 'active',
                submitted_suggestion_id TEXT DEFAULT '',
                submitted_at REAL DEFAULT 0.0,
                expires_at REAL DEFAULT 0.0,
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_governance_candidate_created ON brain_governance_candidate(created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_governance_candidate_stage ON brain_governance_candidate(proposal_stage, status, created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_governance_candidate_scope ON brain_governance_candidate(scope_type, scope_key, action)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_governance_candidate_source ON brain_governance_candidate(source_agent, source_kind, created_at)")
        conn.commit()
    finally:
        conn.close()


def ensure_policy_suggestion_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS policy_suggestion (
                suggestion_id TEXT PRIMARY KEY,
                scope_type TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                action TEXT NOT NULL,
                confidence REAL DEFAULT 0.0,
                reason TEXT DEFAULT '',
                evidence_json TEXT DEFAULT '{}',
                status TEXT DEFAULT 'proposed',
                reviewed_at REAL DEFAULT 0.0,
                review_note TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_policy_suggestion_scope ON policy_suggestion(scope_type, scope_key, status)")
        conn.commit()
    finally:
        conn.close()


class BrainGovernanceCandidateService:
    """Isolated V16 candidate lane before legacy policy_suggestion submission."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "brain_governance_candidate_boundary.v1",
            "candidate_lane_isolated": True,
            "does_not_submit_orders": True,
            "does_not_apply_factor_weights": True,
            "does_not_switch_templates": True,
            "does_not_mutate_runtime_overlay": True,
            "does_not_write_learning_samples": True,
            "does_not_write_policy_suggestion_directly": True,
            "policy_suggestion_bridge_manual_only": True,
            "bridge_requires_existing_governor_compatible_payload": True,
            "bridge_ready_stages": sorted(BRIDGE_READY_STAGES),
        }

    @staticmethod
    def source_registry() -> dict[str, Any]:
        registry = AgentAuthorityRegistryService().list_agents()
        return {
            "schema_version": "brain_source_registry.v2",
            "registry_version": registry["registry_version"],
            "sources": registry["sources"],
            "system_sources": registry["system_sources"],
            "aliases": registry["aliases"],
            "boundary": registry["boundary"],
        }

    def create_candidate(
        self,
        *,
        candidate_id: str | None = None,
        source_agent: str,
        source_kind: str,
        source_ref_type: str,
        source_ref_id: str,
        proposal_stage: str,
        capability_scope: str,
        scope_type: str,
        scope_key: str,
        action: str,
        confidence: float,
        evidence_score: float,
        risk_class: str,
        max_impact: str,
        expected_effect: dict[str, Any] | None = None,
        evidence_refs: dict[str, Any] | None = None,
        counter_evidence_refs: dict[str, Any] | None = None,
        risk_verdict: dict[str, Any] | None = None,
        decision_policy: dict[str, Any] | None = None,
        rollback_plan: dict[str, Any] | None = None,
        lineage: dict[str, Any] | None = None,
        status: str = "active",
        expires_at: float = 0.0,
        now: float | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        ensure_brain_governance_candidate_table(self.db_path)
        created_at = _safe_float(now if now is not None else time.time())
        agent_context = self._agent_generation_context(
            source_agent=source_agent,
            scope_type=scope_type,
            action=action,
            requested_writes=["brain_governance_candidate"],
            status=status,
            impact_level=max_impact,
        )
        authority_verdict = dict(agent_context.get("authority_verdict") or {}) or AgentAuthorityRegistryService().evaluate_scope_write(
            source_agent,
            scope_type,
            action,
            requested_writes=["brain_governance_candidate"],
            status=status,
            impact_level=max_impact,
        )
        lineage_payload = dict(lineage or {})
        lineage_payload.setdefault("authority_verdict", authority_verdict)
        lineage_payload.setdefault("agent_context", agent_context)
        lineage_payload.setdefault("agent_context_required", True)
        item = {
            "candidate_id": candidate_id or f"brain_candidate_{uuid.uuid4().hex[:16]}",
            "schema_version": "brain_governance_candidate.v1",
            "source_agent": source_agent,
            "source_kind": source_kind,
            "source_ref_type": source_ref_type,
            "source_ref_id": source_ref_id,
            "proposal_stage": proposal_stage,
            "capability_scope": capability_scope,
            "scope_type": scope_type,
            "scope_key": scope_key,
            "action": action,
            "confidence": max(0.0, min(1.0, _safe_float(confidence))),
            "evidence_score": max(0.0, min(1.0, _safe_float(evidence_score))),
            "risk_class": risk_class,
            "max_impact": max_impact,
            "expected_effect": expected_effect or {},
            "evidence_refs": evidence_refs or {},
            "counter_evidence_refs": counter_evidence_refs or {},
            "risk_verdict": risk_verdict or {},
            "decision_policy": decision_policy or {},
            "rollback_plan": rollback_plan or {},
            "lineage": lineage_payload,
            "status": status,
            "submitted_suggestion_id": "",
            "submitted_at": 0.0,
            "expires_at": _safe_float(expires_at),
            "created_at": created_at,
            "updated_at": created_at,
            "boundary": self.boundary(),
            "authority_verdict": authority_verdict,
        }
        if persist:
            self._insert_candidate(item)
        return item

    def _agent_generation_context(
        self,
        *,
        source_agent: str,
        scope_type: str,
        action: str,
        requested_writes: list[str],
        status: str,
        impact_level: str,
    ) -> dict[str, Any]:
        try:
            from backend.services.agent_briefing import AgentBriefingContextService

            return AgentBriefingContextService(self.db_path).agent_context(
                source_agent,
                scope_type=scope_type,
                action=action,
                requested_writes=requested_writes,
                status=status,
                impact_level=impact_level,
                limit=20,
            )
        except Exception as exc:
            authority_verdict = AgentAuthorityRegistryService().evaluate_scope_write(
                source_agent,
                scope_type,
                action,
                requested_writes=requested_writes,
                status=status,
                impact_level=impact_level,
            )
            return {
                "ok": False,
                "schema_version": "agent_generation_context.v1",
                "source_agent": source_agent,
                "scope_type": scope_type,
                "action": action,
                "authority_verdict": authority_verdict,
                "error": f"{type(exc).__name__}: {exc}",
                "boundary": {"pre_generation_context_only": True},
            }

    def latest_candidates(self, *, limit: int = 50, status: str = "") -> dict[str, Any]:
        ensure_brain_governance_candidate_table(self.db_path)
        limit = max(1, min(int(limit), 200))
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_governance_candidate"):
                return self._missing_status("missing_table")
            params: tuple[Any, ...]
            where = ""
            if status:
                where = "WHERE status = ?"
                params = (status, limit)
            else:
                params = (limit,)
            rows = _execute(
                conn,
                f"""
                SELECT candidate_id, source_agent, source_kind, source_ref_type,
                       source_ref_id, proposal_stage, capability_scope, scope_type,
                       scope_key, action, confidence, evidence_score, risk_class,
                       max_impact, expected_effect_json, evidence_refs_json,
                       counter_evidence_refs_json, risk_verdict_json,
                       decision_policy_json, rollback_plan_json, lineage_json,
                       status, submitted_suggestion_id, submitted_at, expires_at,
                       created_at, updated_at
                FROM brain_governance_candidate
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return {
                "ok": bool(rows),
                "schema_version": "brain_governance_candidate_list.v1",
                "status": "available" if rows else "missing_candidates",
                "items": [self._row_to_candidate(row) for row in rows],
                "boundary": self.boundary(),
                "source_registry": self.source_registry(),
            }
        finally:
            conn.close()

    def generation_context_coverage(self, *, limit: int = 200) -> dict[str, Any]:
        ensure_brain_governance_candidate_table(self.db_path)
        limit = max(1, min(int(limit), 1000))
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_governance_candidate"):
                return {
                    "ok": False,
                    "schema_version": "candidate_generation_context_coverage.v1",
                    "status": "missing_table",
                    "items": [],
                    "boundary": self.boundary(),
                }
            rows = _execute(
                conn,
                """
                SELECT candidate_id, source_agent, proposal_stage, status,
                       lineage_json, created_at, updated_at
                FROM brain_governance_candidate
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        items = [self._generation_context_coverage_item(row) for row in rows]
        required_missing = [item for item in items if item.get("coverage_status") == "missing_required_agent_context"]
        legacy_missing = [item for item in items if item.get("coverage_status") == "legacy_missing_agent_context"]
        covered = [item for item in items if item.get("coverage_status") == "covered"]
        status = "ok" if not required_missing else "degraded"
        return {
            "ok": status == "ok",
            "schema_version": "candidate_generation_context_coverage.v1",
            "status": status,
            "candidate_count": len(items),
            "covered_count": len(covered),
            "missing_required_context_count": len(required_missing),
            "legacy_missing_context_count": len(legacy_missing),
            "coverage_ratio": round(len(covered) / len(items), 4) if items else 1.0,
            "violations": required_missing[:25],
            "items": items[: min(limit, 100)],
            "boundary": {
                **self.boundary(),
                "read_only_generation_context_audit": True,
                "does_not_create_candidates": True,
                "does_not_modify_candidates": True,
            },
        }

    @staticmethod
    def _generation_context_coverage_item(row: Any) -> dict[str, Any]:
        lineage = _loads(row["lineage_json"], {})
        if not isinstance(lineage, dict):
            lineage = {}
        context = lineage.get("agent_context") if isinstance(lineage.get("agent_context"), dict) else {}
        required = bool(lineage.get("agent_context_required"))
        covered = str(context.get("schema_version") or "") == "agent_generation_context.v1"
        if covered:
            coverage_status = "covered"
        elif required:
            coverage_status = "missing_required_agent_context"
        else:
            coverage_status = "legacy_missing_agent_context"
        return {
            "candidate_id": str(row["candidate_id"] or ""),
            "source_agent": str(row["source_agent"] or ""),
            "proposal_stage": str(row["proposal_stage"] or ""),
            "status": str(row["status"] or ""),
            "agent_context_required": required,
            "coverage_status": coverage_status,
            "agent_context_schema": str(context.get("schema_version") or ""),
            "created_at": _safe_float(row["created_at"]),
            "updated_at": _safe_float(row["updated_at"]),
        }

    def status(self, *, limit: int = 50) -> dict[str, Any]:
        latest = self.latest_candidates(limit=limit)
        items = list(latest.get("items") or [])
        if not items:
            return {
                "ok": False,
                "schema_version": "brain_governance_candidate_readiness.v1",
                "status": latest.get("status", "missing_candidates"),
                "candidate_count": 0,
                "candidate_lane_isolated": True,
                "policy_suggestion_bridge_manual_only": True,
                "source_registry": self.source_registry(),
            }
        stages: dict[str, int] = {}
        statuses: dict[str, int] = {}
        for item in items:
            stage = str(item.get("proposal_stage") or "unknown")
            item_status = str(item.get("status") or "unknown")
            stages[stage] = stages.get(stage, 0) + 1
            statuses[item_status] = statuses.get(item_status, 0) + 1
        return {
            "ok": True,
            "schema_version": "brain_governance_candidate_readiness.v1",
            "status": "available",
            "candidate_count": len(items),
            "latest_created_at": max(_safe_float(item.get("created_at")) for item in items),
            "stages": dict(sorted(stages.items())),
            "statuses": dict(sorted(statuses.items())),
            "candidate_lane_isolated": True,
            "policy_suggestion_bridge_manual_only": True,
            "source_registry": self.source_registry(),
        }

    def submit_candidate_to_policy_suggestion(self, candidate_id: str, *, actor: str = "api:ops.brain.governance_candidate") -> dict[str, Any]:
        ensure_brain_governance_candidate_table(self.db_path)
        ensure_policy_suggestion_table(self.db_path)
        candidate = self.load_candidate(candidate_id)
        if not candidate:
            return {
                "ok": False,
                "schema_version": "brain_governance_candidate_submit.v1",
                "status": "missing_candidate",
                "candidate_id": candidate_id,
            }
        if candidate.get("submitted_suggestion_id"):
            return {
                "ok": True,
                "schema_version": "brain_governance_candidate_submit.v1",
                "status": "already_submitted",
                "candidate": candidate,
                "suggestion_id": candidate.get("submitted_suggestion_id", ""),
                "boundary": self.boundary(),
            }
        if str(candidate.get("status") or "") != "active":
            return self._blocked_submit(candidate, "candidate_not_active")
        if str(candidate.get("proposal_stage") or "") not in BRIDGE_READY_STAGES:
            return self._blocked_submit(candidate, "proposal_stage_not_bridge_ready")
        risk_verdict = dict(candidate.get("risk_verdict") or {})
        if not bool(risk_verdict.get("allowed")):
            return self._blocked_submit(candidate, "risk_policy_not_allowed")
        candidate_review = self._latest_bridge_ready_review(candidate_id)

        payload = self._policy_suggestion_payload(candidate, actor=actor, candidate_review=candidate_review)
        if not payload.get("ok"):
            return self._blocked_submit(candidate, str(payload.get("reason") or "not_governor_compatible"), payload=payload)
        if not candidate_review.get("bridge_ready"):
            return self._blocked_submit(
                candidate,
                "missing_bridge_ready_candidate_review",
                payload={"latest_review": candidate_review, "candidate_review_required_before_submit": True},
            )

        suggestion_id = str(payload["suggestion_id"])
        now = time.time()
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                INSERT INTO policy_suggestion
                (suggestion_id, scope_type, scope_key, action, confidence,
                 reason, evidence_json, status, reviewed_at, review_note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', 0, '', ?)
                """,
                (
                    suggestion_id,
                    payload["scope_type"],
                    payload["scope_key"],
                    payload["action"],
                    _safe_float(payload.get("confidence")),
                    payload["reason"],
                    _dumps(payload["evidence"]),
                    now,
                ),
            )
            _execute(
                conn,
                """
                UPDATE brain_governance_candidate
                SET proposal_stage='submitted_to_policy_suggestion',
                    status='submitted',
                    submitted_suggestion_id=?,
                    submitted_at=?,
                    updated_at=?
                WHERE candidate_id=?
                """,
                (suggestion_id, now, now, candidate_id),
            )
            conn.commit()
        finally:
            conn.close()

        submitted = self.load_candidate(candidate_id) or candidate
        return {
            "ok": True,
            "schema_version": "brain_governance_candidate_submit.v1",
            "status": "submitted_to_policy_suggestion",
            "candidate": submitted,
            "suggestion_id": suggestion_id,
            "policy_suggestion": {key: payload[key] for key in ("scope_type", "scope_key", "action", "confidence", "reason")},
            "boundary": self.boundary(),
        }

    def preview_policy_suggestion_bridge(self, candidate_id: str, *, actor: str = "api:ops.brain.governance_candidate_review") -> dict[str, Any]:
        ensure_brain_governance_candidate_table(self.db_path)
        candidate = self.load_candidate(candidate_id)
        if not candidate:
            return {
                "ok": False,
                "schema_version": "brain_governance_candidate_bridge_preview.v1",
                "status": "missing_candidate",
                "candidate_id": candidate_id,
                "boundary": self.boundary(),
            }
        if candidate.get("submitted_suggestion_id"):
            return {
                "ok": True,
                "schema_version": "brain_governance_candidate_bridge_preview.v1",
                "status": "already_submitted",
                "candidate_id": candidate_id,
                "suggestion_id": candidate.get("submitted_suggestion_id", ""),
                "bridge_ready": False,
                "reason": "already_submitted",
                "boundary": self.boundary(),
            }
        if str(candidate.get("status") or "") != "active":
            return self._blocked_preview(candidate, "candidate_not_active")
        if str(candidate.get("proposal_stage") or "") not in BRIDGE_READY_STAGES:
            return self._blocked_preview(candidate, "proposal_stage_not_bridge_ready")
        risk_verdict = dict(candidate.get("risk_verdict") or {})
        if not bool(risk_verdict.get("allowed")):
            return self._blocked_preview(candidate, "risk_policy_not_allowed")
        payload = self._policy_suggestion_payload(candidate, actor=actor)
        if not payload.get("ok"):
            return self._blocked_preview(candidate, str(payload.get("reason") or "not_governor_compatible"), payload=payload)
        return {
            "ok": True,
            "schema_version": "brain_governance_candidate_bridge_preview.v1",
            "status": "bridge_ready",
            "candidate_id": candidate_id,
            "bridge_ready": True,
            "reason": str(payload.get("reason") or "bridge_payload_ready"),
            "policy_suggestion": {key: payload[key] for key in ("scope_type", "scope_key", "action", "confidence", "reason")},
            "evidence_contract": {
                "schema_version": (payload.get("evidence") or {}).get("schema_version", ""),
                "has_risk_verdict": bool((payload.get("evidence") or {}).get("risk_verdict")),
                "has_rollback_plan": bool((payload.get("evidence") or {}).get("rollback_plan")),
                "manual_only": True,
            },
            "boundary": self.boundary(),
        }

    def _insert_candidate(self, item: dict[str, Any]) -> None:
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                INSERT INTO brain_governance_candidate
                (candidate_id, source_agent, source_kind, source_ref_type, source_ref_id,
                 proposal_stage, capability_scope, scope_type, scope_key, action,
                 confidence, evidence_score, risk_class, max_impact, expected_effect_json,
                 evidence_refs_json, counter_evidence_refs_json, risk_verdict_json,
                 decision_policy_json, rollback_plan_json, lineage_json, status,
                 submitted_suggestion_id, submitted_at, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, ?, ?, ?)
                """,
                (
                    item["candidate_id"],
                    item.get("source_agent", ""),
                    item.get("source_kind", ""),
                    item.get("source_ref_type", ""),
                    item.get("source_ref_id", ""),
                    item.get("proposal_stage", "brain_candidate"),
                    item.get("capability_scope", ""),
                    item.get("scope_type", ""),
                    item.get("scope_key", ""),
                    item.get("action", ""),
                    _safe_float(item.get("confidence")),
                    _safe_float(item.get("evidence_score")),
                    item.get("risk_class", ""),
                    item.get("max_impact", ""),
                    _dumps(item.get("expected_effect", {})),
                    _dumps(item.get("evidence_refs", {})),
                    _dumps(item.get("counter_evidence_refs", {})),
                    _dumps(item.get("risk_verdict", {})),
                    _dumps(item.get("decision_policy", {})),
                    _dumps(item.get("rollback_plan", {})),
                    _dumps(item.get("lineage", {})),
                    item.get("status", "active"),
                    _safe_float(item.get("expires_at")),
                    _safe_float(item.get("created_at")),
                    _safe_float(item.get("updated_at")),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def load_candidate(self, candidate_id: str) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            row = _execute(
                conn,
                """
                SELECT candidate_id, source_agent, source_kind, source_ref_type,
                       source_ref_id, proposal_stage, capability_scope, scope_type,
                       scope_key, action, confidence, evidence_score, risk_class,
                       max_impact, expected_effect_json, evidence_refs_json,
                       counter_evidence_refs_json, risk_verdict_json,
                       decision_policy_json, rollback_plan_json, lineage_json,
                       status, submitted_suggestion_id, submitted_at, expires_at,
                       created_at, updated_at
                FROM brain_governance_candidate
                WHERE candidate_id=?
                LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
            return self._row_to_candidate(row) if row else {}
        finally:
            conn.close()

    def _policy_suggestion_payload(
        self,
        candidate: dict[str, Any],
        *,
        actor: str,
        candidate_review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scope_type = str(candidate.get("scope_type") or "")
        scope_key = str(candidate.get("scope_key") or "")
        action = str(candidate.get("action") or "")
        lineage = dict(candidate.get("lineage") or {})
        mapped = dict(lineage.get("mapped_action") or {})
        expected_effect = dict(candidate.get("expected_effect") or {})
        evidence_refs = dict(candidate.get("evidence_refs") or {})
        source_agent = str(candidate.get("source_agent") or "")
        impact_level = str(candidate.get("max_impact") or "")
        agent_generation_context = dict(lineage.get("agent_generation_context") or lineage.get("agent_context") or {})
        if not agent_generation_context:
            agent_generation_context = self._agent_generation_context(
                source_agent=source_agent,
                scope_type=scope_type,
                action=action,
                requested_writes=["policy_suggestion"],
                status="proposed",
                impact_level=impact_level,
            )
            lineage = {**lineage, "agent_generation_context": agent_generation_context}
        elif not isinstance(lineage.get("agent_generation_context"), dict):
            lineage = {**lineage, "agent_generation_context": agent_generation_context}
        base_evidence = {
            "schema_version": "brain_governance_candidate_policy_suggestion_evidence.v1",
            "candidate_id": candidate.get("candidate_id", ""),
            "source_agent": source_agent,
            "source_kind": candidate.get("source_kind", ""),
            "source_ref_type": candidate.get("source_ref_type", ""),
            "source_ref_id": candidate.get("source_ref_id", ""),
            "proposal_stage": candidate.get("proposal_stage", ""),
            "expected_effect": expected_effect,
            "evidence_refs": evidence_refs,
            "counter_evidence_refs": candidate.get("counter_evidence_refs", {}),
            "risk_verdict": candidate.get("risk_verdict", {}),
            "decision_policy_preview": candidate.get("decision_policy", {}),
            "rollback_plan": candidate.get("rollback_plan", {}),
            "lineage": lineage,
            "agent_context_required": True,
            "agent_generation_context": agent_generation_context,
            "authority_verdict": AgentAuthorityRegistryService().evaluate_scope_write(
                source_agent,
                scope_type,
                action,
                requested_writes=["policy_suggestion"],
                status="proposed",
                impact_level=impact_level,
            ),
            "bridge": {
                "actor": actor,
                "manual_only": True,
                "candidate_review_required": True,
                "candidate_review_required_before_submit": True,
                "candidate_review": candidate_review or {},
                "requires_rule_evolution_governor_review": True,
            },
            "boundary": self.boundary(),
        }

        if scope_type == "supervisor_template" and action == "switch_position_supervisor_template":
            replay_summary = dict(expected_effect.get("replay") or {})
            supervisor_summary = dict(expected_effect.get("supervisor") or {})
            target_template_id = str(mapped.get("target_template_id") or scope_key or "position_supervisor:conservative.v1")
            if not replay_summary.get("replay_run_id") or _safe_float(supervisor_summary.get("trace_count")) <= 0:
                return {"ok": False, "reason": "missing_replay_or_supervisor_evidence"}
            evidence = {
                **base_evidence,
                "target_template_id": target_template_id,
                "candidate_template": {"template_id": target_template_id},
                "replay_summary": replay_summary,
                "counterfactual_summary": supervisor_summary,
            }
            return {
                "ok": True,
                "suggestion_id": f"brain_bridge_{uuid.uuid4().hex[:16]}",
                "scope_type": "position_supervisor_template",
                "scope_key": target_template_id,
                "action": "switch_position_supervisor_template",
                "confidence": candidate.get("confidence", 0.0),
                "reason": "v16_brain_candidate_bridge: supervisor template evidence ready",
                "evidence": evidence,
            }

        if scope_type == "parameter_template" and action == "switch_parameter_template":
            target_template_id = str(mapped.get("target_template_id") or "")
            factor_id = str(mapped.get("factor_id") or scope_key.split(":", 1)[0])
            recommended_scope = str(mapped.get("recommended_scope") or "")
            if not target_template_id or recommended_scope != "online_light":
                return {"ok": False, "reason": "parameter_template_candidate_missing_governor_fields"}
            evidence = {
                **base_evidence,
                "target_template_id": target_template_id,
                "factor_id": factor_id,
                "recommended_scope": recommended_scope,
            }
            return {
                "ok": True,
                "suggestion_id": f"brain_bridge_{uuid.uuid4().hex[:16]}",
                "scope_type": "parameter_template",
                "scope_key": scope_key,
                "action": "switch_parameter_template",
                "confidence": candidate.get("confidence", 0.0),
                "reason": "v16_brain_candidate_bridge: parameter template evidence ready",
                "evidence": evidence,
            }

        if scope_type == "factor" and action in {"downweight", "boost_small"}:
            return {
                "ok": True,
                "suggestion_id": f"brain_bridge_{uuid.uuid4().hex[:16]}",
                "scope_type": scope_type,
                "scope_key": scope_key,
                "action": action,
                "confidence": candidate.get("confidence", 0.0),
                "reason": f"v16_brain_candidate_bridge: factor {action} evidence ready",
                "evidence": base_evidence,
            }

        return {
            "ok": False,
            "reason": f"unsupported_legacy_governor_surface:{scope_type}/{action}",
        }

    def _latest_bridge_ready_review(self, candidate_id: str) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_governance_candidate_review"):
                return {}
            row = _execute(
                conn,
                """
                SELECT review_id, review_status, bridge_ready,
                       evidence_gaps_json, created_at
                FROM brain_governance_candidate_review
                WHERE candidate_id=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
            if not row:
                return {}
            return {
                "schema_version": "brain_governance_candidate_review_ref.v1",
                "review_id": str(row["review_id"] or ""),
                "review_status": str(row["review_status"] or ""),
                "bridge_ready": bool(row["bridge_ready"]),
                "evidence_gaps": _loads(row["evidence_gaps_json"], []),
                "created_at": _safe_float(row["created_at"]),
            }
        finally:
            conn.close()

    @staticmethod
    def _row_to_candidate(row: Any) -> dict[str, Any]:
        return {
            "candidate_id": str(row["candidate_id"] or ""),
            "schema_version": "brain_governance_candidate.v1",
            "source_agent": str(row["source_agent"] or ""),
            "source_kind": str(row["source_kind"] or ""),
            "source_ref_type": str(row["source_ref_type"] or ""),
            "source_ref_id": str(row["source_ref_id"] or ""),
            "proposal_stage": str(row["proposal_stage"] or ""),
            "capability_scope": str(row["capability_scope"] or ""),
            "scope_type": str(row["scope_type"] or ""),
            "scope_key": str(row["scope_key"] or ""),
            "action": str(row["action"] or ""),
            "confidence": _safe_float(row["confidence"]),
            "evidence_score": _safe_float(row["evidence_score"]),
            "risk_class": str(row["risk_class"] or ""),
            "max_impact": str(row["max_impact"] or ""),
            "expected_effect": _loads(row["expected_effect_json"], {}),
            "evidence_refs": _loads(row["evidence_refs_json"], {}),
            "counter_evidence_refs": _loads(row["counter_evidence_refs_json"], {}),
            "risk_verdict": _loads(row["risk_verdict_json"], {}),
            "decision_policy": _loads(row["decision_policy_json"], {}),
            "rollback_plan": _loads(row["rollback_plan_json"], {}),
            "lineage": _loads(row["lineage_json"], {}),
            "status": str(row["status"] or ""),
            "submitted_suggestion_id": str(row["submitted_suggestion_id"] or ""),
            "submitted_at": _safe_float(row["submitted_at"]),
            "expires_at": _safe_float(row["expires_at"]),
            "created_at": _safe_float(row["created_at"]),
            "updated_at": _safe_float(row["updated_at"]),
            "boundary": BrainGovernanceCandidateService.boundary(),
        }

    @staticmethod
    def _missing_status(status: str) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "brain_governance_candidate_list.v1",
            "status": status,
            "items": [],
            "boundary": BrainGovernanceCandidateService.boundary(),
            "source_registry": BrainGovernanceCandidateService.source_registry(),
        }

    def _blocked_submit(self, candidate: dict[str, Any], reason: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "brain_governance_candidate_submit.v1",
            "status": "blocked",
            "reason": reason,
            "candidate": candidate,
            "bridge_preview": payload or {},
            "boundary": self.boundary(),
        }

    def _blocked_preview(self, candidate: dict[str, Any], reason: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "brain_governance_candidate_bridge_preview.v1",
            "status": "blocked",
            "candidate_id": candidate.get("candidate_id", ""),
            "bridge_ready": False,
            "reason": reason,
            "bridge_preview": payload or {},
            "boundary": self.boundary(),
        }
