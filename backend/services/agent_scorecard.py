from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backend.core.ttl_cache import TTLCache
from backend.core.db import STATE_DB, state_table_exists
from backend.core.db_helpers import load_json as _loads
from backend.services.agent_authority import (
    AgentAuthorityRegistryService,
    infer_policy_suggestion_source_agent,
    policy_suggestion_requested_writes,
)
from backend.services._brain_helpers import connect as _connect, execute as _execute
from backend.services.governance_eligibility import GOVERNANCE_ELIGIBILITY_VERSION
from backend.services.canonical_v2_reader import canonical_ready, iter_counterfactual_rows, iter_review_rows, read_review
from backend.services.proposal_registry import ProposalRegistryService
from backend.services.review_contract import review_has_system_contamination
from backend.services.learning_application_store import LearningApplicationStore


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


def _review_payload(row: Any, *, inline_key: str = "review_json") -> dict[str, Any]:
    try:
        keys = row.keys() if hasattr(row, "keys") else ()
        inline_json = row[inline_key] if inline_key in keys else "{}"
    except Exception:
        inline_json = "{}"
    payload = inline_json if isinstance(inline_json, dict) else _loads(inline_json, {})
    return payload if isinstance(payload, dict) else {}


def _status_inc(bucket: dict[str, int], status: str) -> None:
    key = _text(status, "unknown")
    bucket[key] = bucket.get(key, 0) + 1


# The scorecard and trade-attribution builders scan the full recent-review
# set (blob gzip/JSON decode per review) on every call; the governance cycle
# calls them once per audited action (~58x/cycle) for the same data.  A short
# TTL cache collapses those repeats; staleness is bounded at 60s, which is
# fine for briefing/audit context (freshness lives in generated_at anyway).
_SCORECARD_CACHE = TTLCache(maxsize=8, ttl_seconds=60.0)
_ATTRIBUTIONS_CACHE = TTLCache(maxsize=16, ttl_seconds=60.0)


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
        cache_key = ("agent_scorecard.v1", str(self.db_path), int(limit))
        cached = _SCORECARD_CACHE.get(cache_key)
        if cached is not None:
            return cached
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
            # Keep the lifecycle distinction private so the v1 response
            # remains backward-compatible for readiness/API consumers.
            metric.pop("_posterior_not_selected_count", None)
            items.append(metric)
        items.sort(key=lambda item: (item["quality_score"], item["proposal_count"], item["application_count"]), reverse=True)
        result = {
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
        _SCORECARD_CACHE.put(cache_key, result)
        return result

    def latest_trade_attributions(self, *, limit: int = 50, include_external_links: bool = True) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        cache_key = (
            "agent_trade_attribution.v1",
            str(self.db_path),
            int(limit),
            bool(include_external_links),
        )
        cached = _ATTRIBUTIONS_CACHE.get(cache_key)
        if cached is not None:
            return cached
        conn = _connect(self.db_path, read_only=True)
        try:
            if not canonical_ready(conn):
                return {
                    "ok": False,
                    "schema_version": "agent_trade_attribution.v1",
                    "status": "missing_canonical_v2_trade_review",
                    "items": [],
                    "boundary": self.boundary(),
                }
            rows = sorted(
                iter_review_rows(conn, limit=0),
                key=lambda row: _safe_float(row.get("created_at")),
                reverse=True,
            )
            items = []
            for row in rows:
                if review_has_system_contamination(_review_payload(row)):
                    continue
                items.append(
                    self._trade_attribution(
                        conn, row, include_external_links=include_external_links
                    )
                )
                if len(items) >= limit:
                    break
        finally:
            conn.close()
        linked = [item for item in items if item["participants"]]
        lesson_count = sum(1 for item in items if item.get("lesson"))
        result = {
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
        _ATTRIBUTIONS_CACHE.put(cache_key, result)
        return result

    def chain_health(self, *, limit: int = 300) -> dict[str, Any]:
        authority = self.registry.status(db_path=self.db_path, limit=limit)
        proposals = ProposalRegistryService(self.db_path).status(refresh=False)
        scorecard = self.scorecard(limit=limit)
        attribution = self.latest_trade_attributions(limit=min(20, limit), include_external_links=False)
        try:
            from backend.services.entry_quality_governance import EntryQualityGovernanceService
            from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService

            entry_quality = EntryQualityGovernanceService(self.db_path).status()
            v16_commands = V16BrainOrchestratorService(self.db_path).status(limit=50)
        except Exception as exc:
            entry_quality = {"ok": False, "status": "error", "error": str(exc)}
            v16_commands = {"ok": False, "status": "error", "error": str(exc)}
        v16_status = str(v16_commands.get("status") or "unknown")
        v16_idle = v16_status == "no_actionable_command"
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
            {
                "component": "entry_quality_governance",
                "status": entry_quality.get("status", "unknown"),
                "ok": bool(entry_quality.get("ok")),
                "funnel": entry_quality.get("funnel") or {},
                "application_post_trade_progress": (
                    entry_quality.get("application_post_trade_progress") or {}
                ),
            },
            {
                "component": "v16_actionable_commands",
                "status": "idle" if v16_idle else v16_status,
                "ok": bool(v16_commands.get("ok")) or v16_idle,
                "actionable_count": int(v16_commands.get("actionable_command_count") or 0),
                "cancelled_count": int(v16_commands.get("cancelled_command_count") or 0),
                "oldest_actionable_age_seconds": float(
                    v16_commands.get("oldest_actionable_age_seconds") or 0.0
                ),
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
            "entry_quality_governance": entry_quality,
            "v16_commands": {
                "actionable_count": int(v16_commands.get("actionable_command_count") or 0),
                "cancelled_count": int(v16_commands.get("cancelled_command_count") or 0),
                "oldest_actionable_age_seconds": float(
                    v16_commands.get("oldest_actionable_age_seconds") or 0.0
                ),
            },
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
            # Internal-only lifecycle detail. Normal V16 posterior rotation
            # is not a failed candidate lifecycle and is not public output.
            "_posterior_not_selected_count": 0,
            "proposal_count": 0,
            "candidate_count": 0,
            "policy_suggestion_count": 0,
            "application_count": 0,
            "applied_application_count": 0,
            "rolled_back_application_count": 0,
            "positive_effect_count": 0,
            "negative_effect_count": 0,
            "terminal_effect_count": 0,
            "inconclusive_effect_count": 0,
            "observing_effect_count": 0,
            "effect_evidence_trade_count": 0,
            "trade_lesson_feedback_count": 0,
            "advisory_shadow_count": 0,
            "low_reliability_count": 0,
            "stale_evidence_count": 0,
            "conflict_count": 0,
            "contract_violation_count": 0,
            "historical_contract_violation_count": 0,
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
                   proposal_stage,
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
            if (
                _text(row["status"]) == "superseded"
                and _text(row["proposal_stage"]) == "posterior_not_selected"
            ):
                metric["_posterior_not_selected_count"] += 1
            lineage = _loads(row["lineage_json"], {})
            verdict = lineage.get("authority_verdict") if isinstance(lineage, dict) else {}
            if isinstance(verdict, dict) and verdict.get("violations"):
                key = "contract_violation_count" if _text(row["status"]) == "active" else "historical_contract_violation_count"
                metric[key] += len(verdict.get("violations") or [])

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
                   status, reviewed_at, governance_eligible,
                   governance_eligibility_version,
                   governance_eligibility_fingerprint, created_at
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
                current_executable = bool(
                    _text(row["status"]) in {"proposed", "approved", "applied"}
                    and int(row["governance_eligible"] or 0) == 1
                    and _text(row["governance_eligibility_version"]) == GOVERNANCE_ELIGIBILITY_VERSION
                    and _text(row["governance_eligibility_fingerprint"])
                )
                key = "contract_violation_count" if current_executable else "historical_contract_violation_count"
                metric[key] += len(verdict.get("violations") or [])
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
        store = LearningApplicationStore(self.db_path)
        apps = list(store.iter_applications(limit=limit))
        effect_by_app: dict[str, dict[str, Any]] = {}
        if has_effect:
            effect_by_app = {
                str(eff.get("application_id") or ""): eff
                for eff in store.iter_effects()
                if str(eff.get("application_id") or "")
            }
        for app in apps:
            details = app
            ids = [str(item) for item in (app.get("suggestion_ids") or [])]
            source_agent = _text(details.get("source_agent"), "")
            if not source_agent:
                source_agent = next((suggestion_sources.get(item, "") for item in ids if suggestion_sources.get(item)), "autonomous_learning")
            metric = self._metric(metrics, source_agent)
            metric["application_count"] += 1
            if _text(app.get("status")) in {"applied", "observing", "effective"}:
                metric["applied_application_count"] += 1
            if _text(app.get("status")) == "rolled_back":
                metric["rolled_back_application_count"] += 1
            _status_inc(metric["status_counts"], app.get("status"))
            self._touch(metric, app.get("created_at"))
            verdict = details.get("authority_verdict") if isinstance(details, dict) else {}
            if isinstance(verdict, dict) and verdict.get("violations"):
                metric["contract_violation_count"] += len(verdict.get("violations") or [])
            if has_effect:
                effect = effect_by_app.get(str(app.get("application_id") or ""))
                if effect:
                    delta = _safe_float(effect.get("delta_avg_reward"))
                    effect_status = _text(effect.get("status"))
                    if effect_status in {"effective", "reinforced", "ineffective", "rolled_back"}:
                        metric["terminal_effect_count"] += 1
                    elif effect_status == "inconclusive":
                        metric["inconclusive_effect_count"] += 1
                    elif effect_status in {"observing", "mixed", "applied", ""}:
                        metric["observing_effect_count"] += 1
                    if effect_status in {"effective", "reinforced"} and delta > 0:
                        metric["positive_effect_count"] += 1
                    if effect_status in {"ineffective", "rolled_back"} and delta < 0:
                        metric["negative_effect_count"] += 1

    def _experience_metrics(self, conn: Any, metrics: dict[str, dict[str, Any]], limit: int, gaps: list[str]) -> None:
        if not state_table_exists(conn, "experience_memory"):
            gaps.append("experience_memory")
            return
        rows = _execute(
            conn,
            f"""
            SELECT e.experience_id, e.source_table, e.source_id, e.append_source,
                   e.decision_context_json, e.recommended_action, e.created_at,
                   e.source_id AS source_review_id, '' AS source_review_json
            FROM experience_memory e
            ORDER BY e.created_at DESC
            """,
        ).fetchall()
        accepted = 0
        for row in rows:
            if _text(row["append_source"]) == "trade_lesson_memory.v1":
                source_review = self._canonical_review(conn, str(row["source_id"] or ""))
                if source_review is None or review_has_system_contamination(source_review):
                    continue
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
            accepted += 1
            if accepted >= limit:
                break

    def _advisory_shadow_metrics(self, conn: Any, metrics: dict[str, dict[str, Any]], limit: int, gaps: list[str]) -> None:
        specs = [
            ("llm_advisory_audit", "audit_id", "llm_advisory", "created_at", "result_json"),
            ("open_quality_shadow_audit", "inference_id", "lightgbm_shadow_models", "created_at", "result_json"),
            ("position_quality_shadow_audit", "inference_id", "lightgbm_shadow_models", "created_at", "result_json"),
            ("factor_governance_shadow_audit", "inference_id", "lightgbm_shadow_models", "created_at", "result_json"),
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

    def _trade_attribution(self, conn: Any, row: Any, *, include_external_links: bool = True) -> dict[str, Any]:
        review_id = _text(row["review_id"])
        trade_id = _text(row["trade_id"])
        position_id = _text(row["position_id"])
        review = _review_payload(row)
        failure_tags = _loads(row["failure_tags_json"], [])
        participants: list[dict[str, Any]] = []
        participants.extend(self._review_declared_agents(review))
        if include_external_links:
            participants.extend(self._shadow_links(conn, review_id=review_id, trade_id=trade_id, position_id=position_id))
            participants.extend(self._llm_links(conn, review_id=review_id, trade_id=trade_id, position_id=position_id))
            participants.extend(self._proposal_links(conn, review_id=review_id, trade_id=trade_id, position_id=position_id))
            participants.extend(self._counterfactual_links(conn, review_id=review_id, position_id=position_id))
        participants = self._dedupe_participants(participants)
        lesson = self._trade_lesson(conn, review_id)
        lesson_attribution = ((lesson or {}).get("context") or {}).get("agent_attribution") or {}
        if isinstance(lesson_attribution, dict):
            participants = self._dedupe_participants(
                participants + list(lesson_attribution.get("participants") or [])
            )
        feedback_targets = sorted({item["source_agent"] for item in participants} | set((lesson or {}).get("feedback_agents") or []))
        posterior_arbitration = self._posterior_arbitration(conn, review_id=review_id, position_id=position_id, review=row)
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
                "posterior_arbitration": posterior_arbitration,
            },
            "participants": participants,
            "feedback_targets": feedback_targets,
            "lesson": lesson,
            "counterfactuals": posterior_arbitration.get("counterfactuals") or [],
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
                items.append({"source_agent": source, "source_ref_type": "canonical_v2.trade_review", "source_ref_id": "", "role": role})
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

    def _counterfactual_links(self, conn: Any, *, review_id: str, position_id: str) -> list[dict[str, Any]]:
        if not canonical_ready(conn):
            return []
        rows = iter_counterfactual_rows(
            conn,
            limit=0,
            review_id=review_id,
            position_id=str(position_id),
            reverse=True,
        )
        links: list[dict[str, Any]] = []
        for row in rows:
            evidence = row.get("evidence") or _loads(row.get("evidence_json"), {})
            source_review = self._canonical_review(conn, str(row.get("review_id") or ""))
            if (
                source_review is None
                or review_has_system_contamination(source_review)
                or bool(evidence.get("evidence_invalidated"))
            ):
                continue
            links.append({
                "source_agent": "autonomous_learning",
                "source_ref_type": "canonical_v2.counterfactual_review",
                "source_ref_id": _text(row.get("counterfactual_id")),
                "role": "posterior_counterfactual",
                "created_at": _safe_float(row.get("updated_at") or row.get("observed_at")),
            })
            if len(links) >= 10:
                break
        return links

    def _posterior_arbitration(self, conn: Any, *, review_id: str, position_id: str, review: Any) -> dict[str, Any]:
        try:
            from backend.services.v16_brain_snapshot import build_posterior_arbitration

            if review_has_system_contamination(_review_payload(review)):
                return {
                    "schema_version": "posterior_arbitration.v1",
                    "status": "excluded_system_contamination",
                    "counterfactuals": [],
                }
            counterfactuals = []
            for row in iter_counterfactual_rows(
                conn,
                limit=10,
                review_id=review_id,
                position_id=str(position_id),
                reverse=True,
            ):
                evidence = row.get("evidence") or _loads(row.get("evidence_json"), {})
                source_review = self._canonical_review(conn, str(row.get("review_id") or ""))
                if (
                    source_review is None
                    or review_has_system_contamination(source_review)
                    or bool(evidence.get("evidence_invalidated"))
                ):
                    continue
                counterfactuals.append(
                    {
                        "counterfactual_id": _text(row.get("counterfactual_id")),
                        "review_id": _text(row.get("review_id")),
                        "trade_id": _text(row.get("trade_id")),
                        "position_id": _text(row.get("position_id")),
                        "label": _text(row.get("label")),
                        "confidence": _safe_float(row.get("confidence")),
                        "horizons": row.get("horizons") or _loads(row.get("horizons_json"), []),
                        "evidence": evidence,
                    }
                )
            review_dict = dict(review)
            review_dict["review_json"] = _review_payload(review)
            return build_posterior_arbitration(trade_reviews=[review_dict], counterfactuals=counterfactuals) | {
                "counterfactuals": counterfactuals,
            }
        except Exception as exc:
            return {"schema_version": "posterior_arbitration.v1", "status": "error", "error": f"{type(exc).__name__}: {exc}"}

    def _canonical_review(self, conn: Any, review_id: str) -> dict[str, Any] | None:
        """Resolve the full review payload through canonical mapping first."""
        if not review_id:
            return None
        record = read_review(conn, review_id)
        if record is None:
            return None
        if record.get("source") == "canonical":
            payload = record.get("payload") or {}
            nested = payload.get("review") if isinstance(payload, dict) else None
            return nested if isinstance(nested, dict) else {}
        return _review_payload(record.get("payload") or {})

    def _trade_lesson(self, conn: Any, review_id: str) -> dict[str, Any]:
        if not review_id or not state_table_exists(conn, "experience_memory"):
            return {}
        row = _execute(
            conn,
            """
            SELECT experience_id, decision_context_json, failure_tags_json,
                   recommended_action, evidence_strength, created_at
            FROM experience_memory
            WHERE experience_id=? OR (source_table='canonical_v2.trade_review' AND source_id=?)
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
        proposal_count = int(metric.get("proposal_count") or 0)
        candidate_count = int(metric.get("candidate_count") or 0)
        policy_suggestion_count = int(metric.get("policy_suggestion_count") or 0)
        originated_count = max(1, proposal_count, candidate_count, policy_suggestion_count)
        application_count = int(metric.get("application_count") or 0)
        positive = int(metric.get("positive_effect_count") or 0)
        negative = int(metric.get("negative_effect_count") or 0)
        terminal = int(metric.get("terminal_effect_count") or 0)
        inconclusive = int(metric.get("inconclusive_effect_count") or 0)
        superseded = int((metric.get("status_counts") or {}).get("superseded") or 0)
        posterior_not_selected = min(
            superseded,
            int(metric.get("_posterior_not_selected_count") or 0),
        )
        failed_superseded = max(0, superseded - posterior_not_selected)
        submitted = int((metric.get("status_counts") or {}).get("submitted") or 0)
        outcome_count = positive + negative
        score = 0.55
        # Reward verified outcomes, not proposal volume. A prolific agent with
        # mostly superseded/observing proposals must not look more reliable.
        if outcome_count:
            score += 0.16 * (positive / outcome_count) * min(1.0, outcome_count / 5.0)
        if application_count:
            score += 0.08 * min(1.0, terminal / application_count)
            score -= 0.08 * min(1.0, inconclusive / application_count)
        # V16 marks candidates that lost posterior arbitration as
        # ``posterior_not_selected``. They are ordinary candidate rotation,
        # not evidence of a failed lifecycle. Preserve the penalty for every
        # other superseded candidate.
        score -= 0.10 * min(1.0, failed_superseded / originated_count)
        # Candidate-only agents hand work to the policy lane instead of owning
        # applications.  A successful submit is positive lifecycle evidence;
        # using proposal_count=1 as their denominator used to mark healthy
        # candidate lanes as maximally superseded.
        if proposal_count == 0 and candidate_count > 0:
            score += 0.04 * min(1.0, submitted / originated_count)
        score += min(0.08, 0.01 * int(metric.get("trade_lesson_feedback_count") or 0))
        score -= min(0.18, 0.04 * negative)
        score -= min(0.18, 0.03 * int(metric.get("contract_violation_count") or 0))
        score -= min(0.12, 0.02 * int(metric.get("conflict_count") or 0))
        score -= min(0.10, 0.02 * int(metric.get("low_reliability_count") or 0))
        if application_count == 0 and proposal_count > 5:
            score -= 0.04
        return round(max(0.0, min(1.0, score)), 6)
