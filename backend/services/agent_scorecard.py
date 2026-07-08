from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, state_table_exists
from backend.services.agent_authority_registry import (
    AgentAuthorityRegistryService,
    infer_policy_suggestion_source_agent,
    policy_suggestion_requested_writes,
)
from backend.services.brain_action_planner import _connect, _execute
from backend.services.proposal_registry import ProposalRegistryService


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        if hasattr(row, "keys") and key not in row.keys():
            return default
        value = row[key]
    except Exception:
        value = default
    return default if value is None else value


def _status_inc(bucket: dict[str, int], status: str) -> None:
    key = _text(status, "unknown")
    bucket[key] = bucket.get(key, 0) + 1


class AgentScorecardService:
    """Read-only scorecard and feedback map for autonomous agents."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path
        self.registry = AgentAuthorityRegistryService()

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "agent_scorecard_boundary.v1",
            "read_only": True,
            "does_not_submit_orders": True,
            "does_not_apply_runtime_mutations": True,
            "does_not_change_agent_authority": True,
            "uses_existing_ledgers_only": True,
        }

    def scorecard(self, *, limit: int = 500) -> dict[str, Any]:
        limit = max(1, min(int(limit), 2000))
        agents = self._initial_metrics()
        source_gaps: list[str] = []
        conn = _connect(self.db_path, read_only=True)
        try:
            self._proposal_metrics(conn, agents, limit, source_gaps)
            self._candidate_metrics(conn, agents, limit, source_gaps)
            suggestion_sources = self._policy_suggestion_metrics(conn, agents, limit, source_gaps)
            self._application_metrics(conn, agents, suggestion_sources, limit, source_gaps)
            self._experience_metrics(conn, agents, limit, source_gaps)
            self._advisory_shadow_metrics(conn, agents, limit, source_gaps)
        finally:
            conn.close()
        items = []
        for metric in agents.values():
            metric["quality_score"] = self._quality_score(metric)
            items.append(metric)
        items.sort(key=lambda item: (item["quality_score"], item["proposal_count"], item["application_count"]), reverse=True)
        return {
            "ok": True,
            "schema_version": "agent_scorecard.v1",
            "items": items,
            "summary": {
                "agent_count": len(items),
                "proposal_count": sum(item["proposal_count"] for item in items),
                "candidate_count": sum(item["candidate_count"] for item in items),
                "policy_suggestion_count": sum(item["policy_suggestion_count"] for item in items),
                "application_count": sum(item["application_count"] for item in items),
                "trade_lesson_feedback_count": sum(item["trade_lesson_feedback_count"] for item in items),
                "contract_violation_count": sum(item["contract_violation_count"] for item in items),
            },
            "source_gaps": sorted(set(source_gaps)),
            "generated_at": time.time(),
            "boundary": self.boundary(),
        }

    def latest_trade_attributions(self, *, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "trade_outcome_review"):
                return {
                    "ok": False,
                    "schema_version": "agent_trade_attribution.v1",
                    "status": "missing_trade_outcome_review",
                    "items": [],
                    "boundary": self.boundary(),
                }
            rows = _execute(
                conn,
                """
                SELECT review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
                       pnl, outcome_label, failure_tags_json, summary_text,
                       review_json, created_at
                FROM trade_outcome_review
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            items = [self._trade_attribution(conn, row) for row in rows]
        finally:
            conn.close()
        linked = [item for item in items if item["participants"]]
        lesson_count = sum(1 for item in items if item.get("lesson"))
        return {
            "ok": True,
            "schema_version": "agent_trade_attribution.v1",
            "status": "available" if items else "missing_reviews",
            "items": items,
            "summary": {
                "review_count": len(items),
                "linked_review_count": len(linked),
                "lesson_count": lesson_count,
                "unlinked_review_count": len(items) - len(linked),
            },
            "generated_at": time.time(),
            "boundary": self.boundary(),
        }

    def chain_health(self, *, limit: int = 300) -> dict[str, Any]:
        authority = self.registry.status(db_path=self.db_path, limit=limit)
        proposals = ProposalRegistryService(self.db_path).status(refresh=False)
        scorecard = self.scorecard(limit=limit)
        attribution = self.latest_trade_attributions(limit=min(50, limit))
        score_summary = scorecard.get("summary") or {}
        proposal_count = int(proposals.get("proposal_count") or 0)
        source_ledger_count = (
            int(score_summary.get("candidate_count") or 0)
            + int(score_summary.get("policy_suggestion_count") or 0)
            + int(score_summary.get("application_count") or 0)
        )
        proposal_flow_ok = proposal_count > 0 or source_ledger_count > 0
        checks = [
            {
                "component": "agent_authority",
                "status": authority.get("status", "unknown"),
                "ok": bool(authority.get("ok")),
                "unknown_sources": len(authority.get("unknown_sources") or []),
                "contract_violations": len(authority.get("contract_violations") or []),
            },
            {
                "component": "proposal_flow",
                "status": "available" if proposal_count > 0 else ("source_ledgers_available_registry_empty" if source_ledger_count > 0 else "empty"),
                "ok": proposal_flow_ok,
                "proposal_count": proposal_count,
                "source_ledger_count": source_ledger_count,
                "conflict_count": int(proposals.get("conflict_count") or 0),
            },
            {
                "component": "agent_scorecard",
                "status": "available" if scorecard.get("items") else "empty",
                "ok": bool(scorecard.get("items")),
                "summary": scorecard.get("summary") or {},
            },
            {
                "component": "trade_feedback",
                "status": (attribution.get("status") or "unknown"),
                "ok": bool(attribution.get("ok")) and int((attribution.get("summary") or {}).get("lesson_count") or 0) > 0,
                "summary": attribution.get("summary") or {},
            },
        ]
        blockers = [item for item in checks if not item.get("ok")]
        status = "ok" if not blockers and bool(authority.get("ok")) else "degraded"
        return {
            "ok": status == "ok",
            "schema_version": "agent_chain_health.v1",
            "status": status,
            "checks": checks,
            "blockers": blockers,
            "authority": authority,
            "proposal_registry": proposals,
            "scorecard_summary": scorecard.get("summary") or {},
            "trade_feedback_summary": attribution.get("summary") or {},
            "generated_at": time.time(),
            "boundary": self.boundary(),
        }

    def _initial_metrics(self) -> dict[str, dict[str, Any]]:
        registry = self.registry.list_agents()
        metrics: dict[str, dict[str, Any]] = {}
        for source in list(registry.get("sources") or []) + list(registry.get("system_sources") or []):
            source_agent = _text(source.get("source_agent"), "unknown")
            metrics[source_agent] = self._new_metric(source_agent, source)
        return metrics

    @staticmethod
    def _new_metric(source_agent: str, contract: dict[str, Any] | None = None) -> dict[str, Any]:
        contract = contract or {}
        return {
            "source_agent": source_agent,
            "source_kind": contract.get("source_kind", ""),
            "capability_scope": contract.get("capability_scope", ""),
            "authority_state": contract.get("authority_state", "review_only"),
            "proposal_count": 0,
            "candidate_count": 0,
            "policy_suggestion_count": 0,
            "application_count": 0,
            "applied_application_count": 0,
            "rolled_back_application_count": 0,
            "positive_effect_count": 0,
            "negative_effect_count": 0,
            "trade_lesson_feedback_count": 0,
            "advisory_shadow_count": 0,
            "low_reliability_count": 0,
            "stale_evidence_count": 0,
            "conflict_count": 0,
            "contract_violation_count": 0,
            "blocked_by_risk_count": 0,
            "status_counts": {},
            "required_gate_counts": {},
            "latest_activity_at": 0.0,
            "recommended_actions": {},
        }

    def _metric(self, metrics: dict[str, dict[str, Any]], source_agent: str) -> dict[str, Any]:
        source = self.registry.canonical_source(_text(source_agent, "unknown"))
        if source not in metrics:
            metrics[source] = self._new_metric(source, self.registry.get_agent(source))
        return metrics[source]

    @staticmethod
    def _touch(metric: dict[str, Any], ts: Any) -> None:
        metric["latest_activity_at"] = max(_safe_float(metric.get("latest_activity_at")), _safe_float(ts))

    def _proposal_metrics(self, conn: Any, metrics: dict[str, dict[str, Any]], limit: int, gaps: list[str]) -> None:
        if not state_table_exists(conn, "proposal_registry"):
            gaps.append("proposal_registry")
            return
        rows = _execute(
            conn,
            """
            SELECT proposal_id, source_agent, required_gate_json, source_reliability_json,
                   evidence_freshness_json, conflict_json, authority_state, status,
                   updated_at, created_at
            FROM proposal_registry
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            metric = self._metric(metrics, row["source_agent"])
            metric["proposal_count"] += 1
            _status_inc(metric["status_counts"], row["status"])
            self._touch(metric, row["updated_at"] or row["created_at"])
            for gate in _loads(row["required_gate_json"], []):
                _status_inc(metric["required_gate_counts"], str(gate))
            reliability = _loads(row["source_reliability_json"], {})
            if _text(reliability.get("band")).lower() == "low":
                metric["low_reliability_count"] += 1
            freshness = _loads(row["evidence_freshness_json"], {})
            if bool(freshness.get("stale")):
                metric["stale_evidence_count"] += 1
            conflict = _loads(row["conflict_json"], {})
            if bool(conflict.get("conflict")):
                metric["conflict_count"] += 1
            if _text(row["authority_state"]) == "blocked_by_agent_authority":
                metric["contract_violation_count"] += 1

    def _candidate_metrics(self, conn: Any, metrics: dict[str, dict[str, Any]], limit: int, gaps: list[str]) -> None:
        if not state_table_exists(conn, "brain_governance_candidate"):
            gaps.append("brain_governance_candidate")
            return
        rows = _execute(
            conn,
            """
            SELECT candidate_id, source_agent, scope_type, action, status,
                   lineage_json, created_at, updated_at
            FROM brain_governance_candidate
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            metric = self._metric(metrics, row["source_agent"])
            metric["candidate_count"] += 1
            _status_inc(metric["status_counts"], row["status"])
            self._touch(metric, row["updated_at"] or row["created_at"])
            lineage = _loads(row["lineage_json"], {})
            verdict = lineage.get("authority_verdict") if isinstance(lineage, dict) else {}
            if isinstance(verdict, dict) and verdict.get("violations"):
                metric["contract_violation_count"] += len(verdict.get("violations") or [])

    def _policy_suggestion_metrics(
        self,
        conn: Any,
        metrics: dict[str, dict[str, Any]],
        limit: int,
        gaps: list[str],
    ) -> dict[str, str]:
        sources: dict[str, str] = {}
        if not state_table_exists(conn, "policy_suggestion"):
            gaps.append("policy_suggestion")
            return sources
        rows = _execute(
            conn,
            """
            SELECT suggestion_id, scope_type, scope_key, action, evidence_json,
                   status, reviewed_at, created_at
            FROM policy_suggestion
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            evidence = _loads(row["evidence_json"], {})
            source_agent = infer_policy_suggestion_source_agent(
                evidence,
                scope_type=row["scope_type"],
                action=row["action"],
            )
            sources[_text(row["suggestion_id"])] = source_agent
            metric = self._metric(metrics, source_agent)
            metric["policy_suggestion_count"] += 1
            _status_inc(metric["status_counts"], row["status"])
            self._touch(metric, row["reviewed_at"] or row["created_at"])
            verdict = evidence.get("authority_verdict") if isinstance(evidence, dict) else {}
            if not isinstance(verdict, dict):
                verdict = self.registry.evaluate_scope_write(
                    source_agent,
                    row["scope_type"],
                    row["action"],
                    requested_writes=policy_suggestion_requested_writes(source_agent, evidence),
                    status=row["status"],
                    impact_level="medium",
                )
            if verdict.get("violations"):
                metric["contract_violation_count"] += len(verdict.get("violations") or [])
            if _text(row["status"]) == "blocked_by_risk":
                metric["blocked_by_risk_count"] += 1
        return sources

    def _application_metrics(
        self,
        conn: Any,
        metrics: dict[str, dict[str, Any]],
        suggestion_sources: dict[str, str],
        limit: int,
        gaps: list[str],
    ) -> None:
        if not state_table_exists(conn, "learning_application_log"):
            gaps.append("learning_application_log")
            return
        has_effect = state_table_exists(conn, "learning_application_effect")
        rows = _execute(
            conn,
            """
            SELECT application_id, scope_type, scope_key, action, suggestion_ids_json,
                   status, details_json, created_at
            FROM learning_application_log
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            details = _loads(row["details_json"], {})
            ids = [str(item) for item in _loads(row["suggestion_ids_json"], [])]
            source_agent = _text(details.get("source_agent"), "")
            if not source_agent:
                source_agent = next((suggestion_sources.get(item, "") for item in ids if suggestion_sources.get(item)), "autonomous_learning")
            metric = self._metric(metrics, source_agent)
            metric["application_count"] += 1
            if _text(row["status"]) in {"applied", "observing", "effective"}:
                metric["applied_application_count"] += 1
            if _text(row["status"]) == "rolled_back":
                metric["rolled_back_application_count"] += 1
            _status_inc(metric["status_counts"], row["status"])
            self._touch(metric, row["created_at"])
            verdict = details.get("authority_verdict") if isinstance(details, dict) else {}
            if isinstance(verdict, dict) and verdict.get("violations"):
                metric["contract_violation_count"] += len(verdict.get("violations") or [])
            if has_effect:
                effect = _execute(
                    conn,
                    "SELECT delta_avg_reward, status FROM learning_application_effect WHERE application_id=? LIMIT 1",
                    (row["application_id"],),
                ).fetchone()
                if effect:
                    delta = _safe_float(effect["delta_avg_reward"])
                    if delta > 0:
                        metric["positive_effect_count"] += 1
                    if delta < 0:
                        metric["negative_effect_count"] += 1

    def _experience_metrics(self, conn: Any, metrics: dict[str, dict[str, Any]], limit: int, gaps: list[str]) -> None:
        if not state_table_exists(conn, "experience_memory"):
            gaps.append("experience_memory")
            return
        rows = _execute(
            conn,
            """
            SELECT experience_id, source_table, source_id, append_source,
                   decision_context_json, recommended_action, created_at
            FROM experience_memory
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            context = _loads(row["decision_context_json"], {})
            agents = []
            if isinstance(context, dict):
                agents.extend(context.get("feedback_agents") or [])
                attribution = context.get("agent_attribution") or {}
                if isinstance(attribution, dict):
                    agents.extend(attribution.get("feedback_targets") or [])
            if not agents and _text(row["append_source"]) == "trade_lesson_memory.v1":
                agents = ["autonomous_learning"]
            for agent in sorted({str(item) for item in agents if str(item)}):
                metric = self._metric(metrics, agent)
                metric["trade_lesson_feedback_count"] += 1
                _status_inc(metric["recommended_actions"], row["recommended_action"])
                self._touch(metric, row["created_at"])

    def _advisory_shadow_metrics(self, conn: Any, metrics: dict[str, dict[str, Any]], limit: int, gaps: list[str]) -> None:
        specs = [
            ("llm_advisory_audit", "audit_id", "llm_advisory", "created_at", "result_json"),
            ("open_quality_shadow_audit", "inference_id", "lightgbm_shadow_models", "created_at", "result_json"),
            ("position_quality_shadow_audit", "inference_id", "lightgbm_shadow_models", "created_at", "result_json"),
            ("factor_governance_shadow_audit", "inference_id", "lightgbm_shadow_models", "created_at", "result_json"),
            ("meta_model_shadow_audit", "inference_id", "lightgbm_shadow_models", "created_at", "result_json"),
        ]
        for table, _id_col, source_agent, ts_col, result_col in specs:
            if not state_table_exists(conn, table):
                continue
            try:
                rows = _execute(conn, f"SELECT {ts_col}, {result_col} FROM {table} ORDER BY {ts_col} DESC LIMIT ?", (limit,)).fetchall()
            except Exception:
                continue
            metric = self._metric(metrics, source_agent)
            for row in rows:
                metric["advisory_shadow_count"] += 1
                self._touch(metric, row[ts_col])
                result = _loads(row[result_col], {})
                verdict = result.get("authority_verdict") if isinstance(result, dict) else {}
                if isinstance(verdict, dict) and verdict.get("violations"):
                    metric["contract_violation_count"] += len(verdict.get("violations") or [])

    def _trade_attribution(self, conn: Any, row: Any) -> dict[str, Any]:
        review_id = _text(row["review_id"])
        trade_id = _text(row["trade_id"])
        position_id = _text(row["position_id"])
        review = _loads(row["review_json"], {})
        failure_tags = _loads(row["failure_tags_json"], [])
        participants: list[dict[str, Any]] = []
        participants.extend(self._review_declared_agents(review))
        participants.extend(self._shadow_links(conn, review_id=review_id, trade_id=trade_id, position_id=position_id))
        participants.extend(self._llm_links(conn, review_id=review_id, trade_id=trade_id, position_id=position_id))
        participants.extend(self._proposal_links(conn, review_id=review_id, trade_id=trade_id, position_id=position_id))
        participants = self._dedupe_participants(participants)
        lesson = self._trade_lesson(conn, review_id)
        feedback_targets = sorted({item["source_agent"] for item in participants} | set((lesson or {}).get("feedback_agents") or []))
        return {
            "review_id": review_id,
            "trade_id": trade_id,
            "position_id": position_id,
            "pnl": _safe_float(row["pnl"]),
            "outcome_label": _text(row["outcome_label"]),
            "failure_tags": failure_tags if isinstance(failure_tags, list) else [],
            "system_judgement": {
                "primary_responsibility": review.get("primary_responsibility") or "",
                "failure_taxonomy": review.get("failure_taxonomy") or {},
                "system_issue_context": review.get("system_issue_context") or {},
                "summary_text": _text(row["summary_text"]),
            },
            "participants": participants,
            "feedback_targets": feedback_targets,
            "lesson": lesson,
            "created_at": _safe_float(row["created_at"]),
        }

    @staticmethod
    def _review_declared_agents(review: Any) -> list[dict[str, Any]]:
        if not isinstance(review, dict):
            return []
        attribution = review.get("agent_attribution") or {}
        if not isinstance(attribution, dict):
            return []
        items = []
        for agent in attribution.get("participants") or []:
            if isinstance(agent, dict):
                source = _text(agent.get("source_agent"))
                role = _text(agent.get("role"), "declared")
            else:
                source = _text(agent)
                role = "declared"
            if source:
                items.append({"source_agent": source, "source_ref_type": "trade_outcome_review", "source_ref_id": "", "role": role})
        return items

    def _shadow_links(self, conn: Any, *, review_id: str, trade_id: str, position_id: str) -> list[dict[str, Any]]:
        specs = [
            ("open_quality_shadow_audit", "inference_id", [("trade_id", trade_id), ("position_id", position_id)]),
            ("position_quality_shadow_audit", "inference_id", [("review_id", review_id), ("trade_id", trade_id), ("position_id", position_id)]),
            ("factor_governance_shadow_audit", "inference_id", [("review_id", review_id), ("trade_id", trade_id), ("position_id", position_id)]),
        ]
        links: list[dict[str, Any]] = []
        for table, id_col, keys in specs:
            if not state_table_exists(conn, table):
                continue
            clauses = []
            params: list[Any] = []
            for col, value in keys:
                if value:
                    clauses.append(f"{col}=?")
                    params.append(value)
            if not clauses:
                continue
            try:
                rows = _execute(
                    conn,
                    f"SELECT {id_col}, created_at FROM {table} WHERE {' OR '.join(clauses)} ORDER BY created_at DESC LIMIT 10",
                    tuple(params),
                ).fetchall()
            except Exception:
                continue
            for shadow in rows:
                links.append(
                    {
                        "source_agent": "lightgbm_shadow_models",
                        "source_ref_type": table,
                        "source_ref_id": _text(shadow[id_col]),
                        "role": "shadow_advisory",
                        "created_at": _safe_float(shadow["created_at"]),
                    }
                )
        return links

    def _llm_links(self, conn: Any, *, review_id: str, trade_id: str, position_id: str) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "llm_advisory_audit"):
            return []
        keys = [item for item in [review_id, trade_id, position_id] if item]
        if not keys:
            return []
        links = []
        for key in keys[:3]:
            rows = _execute(
                conn,
                """
                SELECT audit_id, created_at
                FROM llm_advisory_audit
                WHERE target_id=?
                ORDER BY created_at DESC
                LIMIT 5
                """,
                (key,),
            ).fetchall()
            for row in rows:
                links.append(
                    {
                        "source_agent": "llm_advisory",
                        "source_ref_type": "llm_advisory_audit",
                        "source_ref_id": _text(row["audit_id"]),
                        "role": "llm_review",
                        "created_at": _safe_float(row["created_at"]),
                    }
                )
        return links

    def _proposal_links(self, conn: Any, *, review_id: str, trade_id: str, position_id: str) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "proposal_registry"):
            return []
        keys = [item for item in [review_id, trade_id, position_id] if item]
        if not keys:
            return []
        links = []
        for key in keys[:3]:
            like = f"%{key}%"
            rows = _execute(
                conn,
                """
                SELECT proposal_id, source_agent, source_ref_type, source_ref_id, updated_at
                FROM proposal_registry
                WHERE source_ref_id=? OR evidence_refs_json LIKE ?
                ORDER BY updated_at DESC
                LIMIT 10
                """,
                (key, like),
            ).fetchall()
            for row in rows:
                links.append(
                    {
                        "source_agent": _text(row["source_agent"]),
                        "source_ref_type": _text(row["source_ref_type"]),
                        "source_ref_id": _text(row["source_ref_id"]) or _text(row["proposal_id"]),
                        "role": "proposal_or_evidence",
                        "created_at": _safe_float(row["updated_at"]),
                    }
                )
        return links

    def _trade_lesson(self, conn: Any, review_id: str) -> dict[str, Any]:
        if not review_id or not state_table_exists(conn, "experience_memory"):
            return {}
        row = _execute(
            conn,
            """
            SELECT experience_id, decision_context_json, failure_tags_json,
                   recommended_action, evidence_strength, created_at
            FROM experience_memory
            WHERE experience_id=? OR (source_table='trade_outcome_review' AND source_id=?)
            ORDER BY CASE WHEN experience_id=? THEN 0 ELSE 1 END, created_at DESC
            LIMIT 1
            """,
            (f"trade_lesson:{review_id}", review_id, f"trade_lesson:{review_id}"),
        ).fetchone()
        if not row:
            return {}
        context = _loads(row["decision_context_json"], {})
        attribution = context.get("agent_attribution") if isinstance(context, dict) else {}
        feedback_agents = (attribution or {}).get("feedback_targets") if isinstance(attribution, dict) else []
        if not feedback_agents and _text(row["experience_id"]).startswith("trade_lesson:"):
            feedback_agents = ["autonomous_learning"]
        return {
            "experience_id": _text(row["experience_id"]),
            "recommended_action": _text(row["recommended_action"]),
            "failure_tags": _loads(row["failure_tags_json"], []),
            "evidence_strength": _safe_float(row["evidence_strength"]),
            "feedback_agents": feedback_agents,
            "context": context,
        }

    @staticmethod
    def _dedupe_participants(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen = set()
        deduped = []
        for item in items:
            source = _text(item.get("source_agent"))
            ref_type = _text(item.get("source_ref_type"))
            ref_id = _text(item.get("source_ref_id"))
            key = (source, ref_type, ref_id, _text(item.get("role")))
            if not source or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    @staticmethod
    def _quality_score(metric: dict[str, Any]) -> float:
        proposal_count = max(1, int(metric.get("proposal_count") or 0))
        application_count = int(metric.get("application_count") or 0)
        score = 0.55
        score += min(0.15, 0.015 * int(metric.get("proposal_count") or 0))
        score += min(0.12, 0.03 * int(metric.get("positive_effect_count") or 0))
        score += min(0.08, 0.01 * int(metric.get("trade_lesson_feedback_count") or 0))
        score -= min(0.18, 0.04 * int(metric.get("negative_effect_count") or 0))
        score -= min(0.18, 0.03 * int(metric.get("contract_violation_count") or 0))
        score -= min(0.12, 0.02 * int(metric.get("conflict_count") or 0))
        score -= min(0.10, 0.02 * int(metric.get("low_reliability_count") or 0))
        if application_count == 0 and proposal_count > 5:
            score -= 0.04
        return round(max(0.0, min(1.0, score)), 6)
