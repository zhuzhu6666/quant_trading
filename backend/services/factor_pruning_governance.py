from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from alpha.decision_policy import DecisionPolicy
from backend.core.db import STATE_DB, state_table_exists
from backend.services._brain_helpers import connect as _connect, dumps as _dumps, execute as _execute, loads as _loads, safe_float as _safe_float
from backend.services.brain_governance_candidates import BrainGovernanceCandidateService, ensure_brain_governance_candidate_table
from backend.services.factor_counter_evidence import FactorCounterEvidenceService
from backend.services.factor_pruning_candidates import DEFAULT_MAX_CANDIDATES, FactorPruningCandidateService
from risk.policy_service import RiskPolicyService


DEFAULT_MIN_PRIORITY = 0.75


class FactorPruningGovernanceService:
    """Materialize factor pruning advice into the isolated governance lane."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "factor_pruning_governance_boundary.v1",
            "materializes_governance_candidates_only": True,
            "does_not_write_policy_suggestion_directly": True,
            "policy_suggestion_bridge_manual_only": True,
            "demo_nursery_auto_bridge_allowed_after_governance_ready": True,
            "demo_nursery_system_bridge_enabled": True,
            "demo_nursery_human_approval_required": False,
            "does_not_apply_factor_weights": True,
            "does_not_disable_factors": True,
            "does_not_submit_orders": True,
            "risk_policy_service_required": True,
            "decision_policy_preview_required": True,
            "candidate_review_required_before_auto_bridge": True,
            "proposal_stage": "brain_candidate",
            "bridge_ready": False,
        }

    def materialize_latest(
        self,
        *,
        limit: int = DEFAULT_MAX_CANDIDATES,
        min_priority: float = DEFAULT_MIN_PRIORITY,
        persist: bool = True,
    ) -> dict[str, Any]:
        ensure_brain_governance_candidate_table(self.db_path)
        limit = max(1, min(int(limit or DEFAULT_MAX_CANDIDATES), 100))
        min_priority = max(0.0, min(1.0, _safe_float(min_priority, DEFAULT_MIN_PRIORITY)))
        source = FactorPruningCandidateService(self.db_path).build(limit=limit)
        now = time.time()
        candidates = [
            item
            for item in list(source.get("candidates") or [])
            if _safe_float(item.get("priority_score")) >= min_priority
        ][:limit]
        items = [self._materialize_candidate(item, now=now, persist=persist) for item in candidates]
        return {
            "ok": any(item.get("status") in {"candidate_materialized", "candidate_updated", "already_materialized"} for item in items),
            "schema_version": "factor_pruning_governance_run.v1",
            "status": "materialized" if items else "no_candidates",
            "source_status": source.get("status"),
            "source_generated_count": source.get("generated_count", 0),
            "candidate_count": len(candidates),
            "materialized_count": sum(1 for item in items if item.get("status") == "candidate_materialized"),
            "updated_count": sum(1 for item in items if item.get("status") == "candidate_updated"),
            "already_submitted_count": sum(1 for item in items if item.get("status") == "already_submitted"),
            "blocked_count": sum(1 for item in items if str(item.get("status") or "").startswith("blocked_")),
            "items": items,
            "boundary": self.boundary(),
            "created_at": now,
        }

    def status(self, *, limit: int = 50) -> dict[str, Any]:
        ensure_brain_governance_candidate_table(self.db_path)
        limit = max(1, min(int(limit or 50), 200))
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_governance_candidate"):
                return self._empty_status("missing_table")
            rows = _execute(
                conn,
                """
                SELECT candidate_id, proposal_stage, status, scope_key, action,
                       confidence, evidence_score, submitted_suggestion_id, created_at, updated_at
                FROM brain_governance_candidate
                WHERE source_agent = 'factor_pruning_governance'
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return self._empty_status("missing_candidates")
        stages: dict[str, int] = {}
        statuses: dict[str, int] = {}
        items = []
        for row in rows:
            stage = str(row["proposal_stage"] or "")
            item_status = str(row["status"] or "")
            stages[stage] = stages.get(stage, 0) + 1
            statuses[item_status] = statuses.get(item_status, 0) + 1
            items.append(
                {
                    "candidate_id": str(row["candidate_id"] or ""),
                    "scope_key": str(row["scope_key"] or ""),
                    "action": str(row["action"] or ""),
                    "proposal_stage": stage,
                    "status": item_status,
                    "confidence": _safe_float(row["confidence"]),
                    "evidence_score": _safe_float(row["evidence_score"]),
                    "submitted_suggestion_id": str(row["submitted_suggestion_id"] or ""),
                    "created_at": _safe_float(row["created_at"]),
                    "updated_at": _safe_float(row["updated_at"]),
                }
            )
        return {
            "ok": True,
            "schema_version": "factor_pruning_governance_status.v1",
            "status": "available",
            "candidate_count": len(items),
            "stages": dict(sorted(stages.items())),
            "statuses": dict(sorted(statuses.items())),
            "latest_updated_at": max(_safe_float(item.get("updated_at")) for item in items),
            "items": items[: min(limit, 25)],
            "boundary": self.boundary(),
        }

    def promote_ready(
        self,
        *,
        limit: int = 50,
        min_evidence_score: float = 0.9,
        require_weak_health: bool = True,
    ) -> dict[str, Any]:
        ensure_brain_governance_candidate_table(self.db_path)
        limit = max(1, min(int(limit or 50), 100))
        min_evidence_score = max(0.0, min(1.0, _safe_float(min_evidence_score, 0.9)))
        now = time.time()
        rows = self._load_pruning_candidates(limit=limit)
        items = [
            self._promote_candidate(
                row,
                now=now,
                min_evidence_score=min_evidence_score,
                require_weak_health=require_weak_health,
            )
            for row in rows
        ]
        return {
            "ok": any(item.get("status") in {"promoted", "already_governance_ready"} for item in items),
            "schema_version": "factor_pruning_governance_promote_run.v1",
            "status": "promoted" if items else "no_candidates",
            "candidate_count": len(items),
            "promoted_count": sum(1 for item in items if item.get("status") == "promoted"),
            "already_ready_count": sum(1 for item in items if item.get("status") == "already_governance_ready"),
            "blocked_count": sum(1 for item in items if str(item.get("status") or "").startswith("blocked_")),
            "items": items,
            "boundary": {**self.boundary(), "promotes_to_governance_ready_only": True, "does_not_submit_policy_suggestion": True},
            "created_at": now,
        }

    def bridge_ready_candidates(
        self,
        *,
        limit: int = 5,
        require_demo_nursery: bool = True,
        actor: str = "system:factor_pruning_governance.demo_nursery_auto_bridge",
        review_missing: bool = True,
        preview_before_submit: bool = True,
    ) -> dict[str, Any]:
        ensure_brain_governance_candidate_table(self.db_path)
        limit = max(1, min(int(limit or 5), 20))
        if require_demo_nursery:
            from config.runtime_config import shared as runtime_config

            mode = str(getattr(runtime_config(), "autonomy_mode", "") or "")
            if mode != "demo_nursery":
                return {
                    "ok": False,
                    "schema_version": "factor_pruning_governance_bridge_run.v1",
                    "status": "blocked_mode",
                    "mode": mode,
                    "items": [],
                    "boundary": self.boundary(),
                }
        rows = self._load_bridge_ready_candidates(
            limit=max(limit * 10, 50),
            require_existing_bridge_ready_review=not review_missing,
            sort_current_blend=review_missing,
        )
        candidate_service = BrainGovernanceCandidateService(self.db_path)
        review_service = None
        if review_missing:
            from backend.services.brain_governance_candidate_review import BrainGovernanceCandidateReviewService

            review_service = BrainGovernanceCandidateReviewService(self.db_path)
        items = []
        submitted_seen = 0
        for row in rows:
            if submitted_seen >= limit:
                break
            candidate_id = str(row["candidate_id"] or "")
            factor = str(row["scope_key"] or "")
            if self._has_policy_conflict(factor):
                items.append({"status": "blocked_conflict", "candidate_id": candidate_id, "factor": factor})
                continue
            expected = _loads(row["expected_effect_json"], {})
            reason_codes = self._reason_codes(expected)
            if "recent_live_decision_participation" not in reason_codes:
                items.append(
                    {
                        "status": "blocked_missing_recent_live_decision_participation",
                        "candidate_id": candidate_id,
                        "factor": factor,
                    }
                )
                continue
            if preview_before_submit:
                preview = candidate_service.preview_policy_suggestion_bridge(candidate_id, actor=actor)
                if not bool(preview.get("bridge_ready")):
                    items.append(
                        {
                            "status": "blocked_bridge_preview",
                            "candidate_id": candidate_id,
                            "factor": factor,
                            "reason": preview.get("reason", ""),
                        }
                    )
                    continue
            if review_missing and review_service is not None:
                review = review_service.review_candidate(candidate_id, run_llm=False, llm_dry_run=True, persist=True)
                review_item = dict(review.get("review") or {})
                if not bool(review_item.get("bridge_ready")):
                    items.append(
                        {
                            "status": "blocked_candidate_review",
                            "candidate_id": candidate_id,
                            "factor": factor,
                            "review_status": review_item.get("review_status", review.get("status", "")),
                            "evidence_gaps": review_item.get("evidence_gaps") or [],
                            "source_reliability": review_item.get("source_reliability") or {},
                            "review_id": review_item.get("review_id", ""),
                        }
                    )
                    continue
            submitted = candidate_service.submit_candidate_to_policy_suggestion(candidate_id, actor=actor)
            if str(submitted.get("status") or "") in {"submitted_to_policy_suggestion", "already_submitted"}:
                submitted_seen += 1
            items.append(
                {
                    "status": str(submitted.get("status") or ""),
                    "candidate_id": candidate_id,
                    "factor": factor,
                    "suggestion_id": submitted.get("suggestion_id", ""),
                    "policy_suggestion": submitted.get("policy_suggestion", {}),
                    "reason": submitted.get("reason", ""),
                }
            )
        return {
            "ok": any(item.get("status") in {"submitted_to_policy_suggestion", "already_submitted"} for item in items),
            "schema_version": "factor_pruning_governance_bridge_run.v1",
            "status": "bridged" if items else "no_candidates",
            "candidate_count": len(items),
            "submitted_count": sum(1 for item in items if item.get("status") == "submitted_to_policy_suggestion"),
            "already_submitted_count": sum(1 for item in items if item.get("status") == "already_submitted"),
            "blocked_count": sum(1 for item in items if str(item.get("status") or "").startswith("blocked_")),
            "items": items,
            "boundary": {
                **self.boundary(),
                "writes_policy_suggestion_through_existing_bridge": True,
                "candidate_review_required_before_bridge": True,
                "review_missing_before_bridge": bool(review_missing),
                "preview_before_submit": bool(preview_before_submit),
                "does_not_apply_factor_weights": True,
            },
        }

    def _materialize_candidate(self, source: dict[str, Any], *, now: float, persist: bool) -> dict[str, Any]:
        factor = str(source.get("factor") or "")
        if not factor:
            return {"status": "blocked_missing_factor", "source": source}
        risk_verdict = RiskPolicyService.shared().evaluate(
            "update_weight",
            {
                "required_mode": "autonomous_governance",
                "session": {"drawdown_pct": 0.0},
                "evidence": {"factor_pruning_candidate": source},
                "suggestion_status": "candidate",
                "autonomous_apply": False,
                "factor": factor,
                "current_weight": source.get("current_weight"),
                "target_weight": source.get("suggested_target_weight"),
            },
        ).to_dict()
        if not bool(risk_verdict.get("allowed")):
            return {
                "status": "blocked_by_risk",
                "factor": factor,
                "candidate_id": self._candidate_id(factor),
                "risk_verdict": risk_verdict,
                "source_candidate": source,
            }
        item = self._candidate_payload(source, risk_verdict=risk_verdict, now=now)
        if not persist:
            return {"status": "candidate_preview", "candidate": item, "boundary": self.boundary()}
        return self._upsert_candidate(item)

    def _load_pruning_candidates(self, *, limit: int) -> list[Any]:
        query_limit = max(int(limit or 50) * 20, 200)
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_governance_candidate"):
                return []
            rows = _execute(
                conn,
                """
                SELECT candidate_id, proposal_stage, status, scope_key, action,
                       confidence, evidence_score, expected_effect_json,
                       evidence_refs_json, risk_verdict_json, decision_policy_json,
                       submitted_suggestion_id
                FROM brain_governance_candidate
                WHERE source_agent='factor_pruning_governance'
                  AND status='active'
                  AND COALESCE(submitted_suggestion_id, '') = ''
                ORDER BY evidence_score DESC, updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (query_limit,),
            ).fetchall()
        finally:
            conn.close()
        return self._sort_current_blend_first(rows)[:limit]

    def _load_bridge_ready_candidates(
        self,
        *,
        limit: int,
        require_existing_bridge_ready_review: bool = False,
        sort_current_blend: bool = True,
    ) -> list[Any]:
        query_limit = max(int(limit or 50) * 50, 500)
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_governance_candidate"):
                return []
            if require_existing_bridge_ready_review and not state_table_exists(conn, "brain_governance_candidate_review"):
                return []
            review_filter = ""
            if require_existing_bridge_ready_review:
                review_filter = """
                  AND EXISTS (
                      SELECT 1
                      FROM brain_governance_candidate_review r
                      WHERE r.candidate_id = c.candidate_id
                        AND r.bridge_ready = 1
                  )
                """
            rows = _execute(
                conn,
                f"""
                SELECT c.candidate_id, c.scope_key, c.expected_effect_json
                FROM brain_governance_candidate c
                WHERE c.source_agent='factor_pruning_governance'
                  AND c.proposal_stage='governance_ready'
                  AND c.status='active'
                  AND c.action='downweight'
                  AND COALESCE(c.submitted_suggestion_id, '') = ''
                  {review_filter}
                ORDER BY evidence_score DESC, updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (query_limit,),
            ).fetchall()
        finally:
            conn.close()
        if not sort_current_blend:
            return rows[:limit]
        return self._sort_current_blend_first(rows)[:limit]

    def _sort_current_blend_first(self, rows: list[Any]) -> list[Any]:
        if not rows:
            return rows
        current_used: set[str] = set()
        runtime_weighted: set[str] = set()
        try:
            from backend.services.factor_catalog import build_factor_catalog

            for item in build_factor_catalog(self.db_path):
                factor = str(item.get("factor_id") or "")
                if bool(item.get("used_in_score")) and factor:
                    current_used.add(factor)
        except Exception:
            current_used = set()
        try:
            from config.runtime_config import shared as runtime_config

            weights = dict(getattr(runtime_config(), "factor_portfolio_weights", {}) or {})
            runtime_weighted = {str(name) for name, weight in weights.items() if _safe_float(weight) > 0.0}
        except Exception:
            runtime_weighted = set()

        def _rank(row: Any) -> tuple[int, float, str]:
            factor = str(row["scope_key"] or "")
            if factor in current_used:
                bucket = 0
            elif factor in runtime_weighted:
                bucket = 1
            else:
                bucket = 2
            return (bucket, -_safe_float(row["evidence_score"] if "evidence_score" in row.keys() else 0.0), factor)

        try:
            return sorted(rows, key=_rank)
        except Exception:
            return rows

    def _promote_candidate(
        self,
        row: Any,
        *,
        now: float,
        min_evidence_score: float,
        require_weak_health: bool,
    ) -> dict[str, Any]:
        candidate_id = str(row["candidate_id"] or "")
        stage = str(row["proposal_stage"] or "")
        if stage == "governance_ready":
            return {"status": "already_governance_ready", "candidate_id": candidate_id}
        if stage != "brain_candidate":
            return {"status": "blocked_stage", "candidate_id": candidate_id, "proposal_stage": stage}
        evidence_score = _safe_float(row["evidence_score"])
        expected = _loads(row["expected_effect_json"], {})
        reason_codes = self._reason_codes(expected)
        risk_verdict = _loads(row["risk_verdict_json"], {})
        decision_policy = _loads(row["decision_policy_json"], {})
        decision = dict(decision_policy.get("decision") or {})
        current_weight = _safe_float(expected.get("current_weight"))
        target_weight = _safe_float(expected.get("suggested_target_weight"))
        blockers = []
        if evidence_score < min_evidence_score:
            blockers.append("evidence_score_below_threshold")
        if "recent_live_decision_participation" not in reason_codes:
            blockers.append("missing_recent_live_decision_participation")
        has_live_loss_pressure = "recent_loss_contribution_pressure" in reason_codes and "recent_live_decision_participation" in reason_codes
        if require_weak_health and "weak_factor_health" not in reason_codes and not has_live_loss_pressure:
            blockers.append("missing_weak_health_or_live_loss_pressure")
        if not bool(risk_verdict.get("allowed")):
            blockers.append("risk_policy_not_allowed")
        if not bool(decision_policy.get("required")) or not decision:
            blockers.append("missing_decision_policy_preview")
        if _safe_float(decision.get("new_weight"), target_weight) > current_weight:
            blockers.append("decision_policy_not_risk_reducing")
        if self._has_policy_conflict(str(row["scope_key"] or "")):
            blockers.append("active_policy_suggestion_conflict")
        counter_evidence = FactorCounterEvidenceService(self.db_path).build_for_factor(
            str(row["scope_key"] or ""),
            candidate={
                "candidate_id": candidate_id,
                "evidence_score": evidence_score,
                "expected_effect": expected,
            },
        )
        self._write_counter_evidence(candidate_id, counter_evidence=counter_evidence, now=now)
        counter_stage = str(counter_evidence.get("recommended_stage") or "")
        if counter_stage == "block_pruning":
            blockers.append("counter_evidence_keep_signal")
        elif counter_stage == "regime_exception_review":
            blockers.append("counter_evidence_regime_exception")
        if blockers:
            return {
                "status": "blocked_evidence",
                "candidate_id": candidate_id,
                "factor": str(row["scope_key"] or ""),
                "blockers": blockers,
                "evidence_score": evidence_score,
                "counter_evidence": counter_evidence,
            }
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                UPDATE brain_governance_candidate
                SET proposal_stage='governance_ready',
                    updated_at=?
                WHERE candidate_id=?
                  AND proposal_stage='brain_candidate'
                  AND status='active'
                  AND COALESCE(submitted_suggestion_id, '') = ''
                """,
                (_safe_float(now), candidate_id),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "status": "promoted",
            "candidate_id": candidate_id,
            "factor": str(row["scope_key"] or ""),
            "proposal_stage": "governance_ready",
            "evidence_score": evidence_score,
            "reason_codes": sorted(reason_codes),
            "counter_evidence": {
                "recommended_stage": counter_evidence.get("recommended_stage"),
                "keep_score": counter_evidence.get("keep_score"),
                "prune_score": counter_evidence.get("prune_score"),
                "regime_exception": counter_evidence.get("regime_exception"),
            },
        }

    @staticmethod
    def _reason_codes(expected: dict[str, Any]) -> set[str]:
        reasons = list((expected or {}).get("reasons") or [])
        return {str(item.get("code") or "") for item in reasons if isinstance(item, dict)}

    def _write_counter_evidence(self, candidate_id: str, *, counter_evidence: dict[str, Any], now: float) -> None:
        if not candidate_id:
            return
        existing = BrainGovernanceCandidateService(self.db_path).load_candidate(candidate_id)
        if not existing:
            return
        counter_refs = dict(existing.get("counter_evidence_refs") or {})
        counter_refs["factor_counter_evidence"] = counter_evidence
        lineage = dict(existing.get("lineage") or {})
        lineage["counter_evidence_checked"] = {
            "schema_version": "factor_pruning_counter_evidence_check.v1",
            "recommended_stage": counter_evidence.get("recommended_stage"),
            "keep_score": counter_evidence.get("keep_score"),
            "prune_score": counter_evidence.get("prune_score"),
            "checked_at": now,
        }
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                UPDATE brain_governance_candidate
                SET counter_evidence_refs_json=?,
                    lineage_json=?,
                    updated_at=?
                WHERE candidate_id=?
                """,
                (_dumps(counter_refs), _dumps(lineage), _safe_float(now), candidate_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _has_policy_conflict(self, factor: str) -> bool:
        if not factor:
            return True
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "policy_suggestion"):
                return False
            row = _execute(
                conn,
                """
                SELECT 1
                FROM policy_suggestion
                WHERE scope_type='factor'
                  AND scope_key=?
                  AND status IN ('proposed', 'approved')
                LIMIT 1
                """,
                (factor,),
            ).fetchone()
            return bool(row)
        finally:
            conn.close()

    def _candidate_payload(self, source: dict[str, Any], *, risk_verdict: dict[str, Any], now: float) -> dict[str, Any]:
        factor = str(source.get("factor") or "")
        current_weight = _safe_float(source.get("current_weight"))
        target_weight = max(0.0, _safe_float(source.get("suggested_target_weight")))
        decision_policy = self._decision_policy_preview(factor=factor, current_weight=current_weight, target_weight=target_weight)
        mapped_action = {
            "schema_version": "factor_pruning_mapped_action.v1",
            "factor_id": factor,
            "policy_action": "downweight",
            "risk_action": "update_weight",
            "current_weight": current_weight,
            "target_weight": target_weight,
            "source_recommended_action": source.get("recommended_action", ""),
        }
        agent_context = self._agent_generation_context(
            scope_type="factor",
            action="downweight",
            requested_writes=["brain_governance_candidate"],
            status="active",
            impact_level="medium_impact",
        )
        return {
            "candidate_id": self._candidate_id(factor),
            "schema_version": "brain_governance_candidate.v1",
            "source_agent": "factor_pruning_governance",
            "source_kind": "factor_pruning_candidate_materializer",
            "source_ref_type": "factor_pruning_candidate",
            "source_ref_id": str(source.get("candidate_id") or ""),
            "proposal_stage": "brain_candidate",
            "capability_scope": "factor_catalog_runtime_governance",
            "scope_type": "factor",
            "scope_key": factor,
            "action": "downweight",
            "confidence": _safe_float(source.get("confidence")),
            "evidence_score": _safe_float(source.get("priority_score")),
            "risk_class": "medium",
            "max_impact": "medium_impact",
            "expected_effect": {
                "schema_version": "factor_pruning_expected_effect.v1",
                "candidate_only": True,
                "current_weight": current_weight,
                "suggested_target_weight": target_weight,
                "estimated_weight_delta": round(target_weight - current_weight, 8),
                "recommended_action": source.get("recommended_action", ""),
                "reasons": source.get("reasons") or [],
                "evidence": source.get("evidence") or {},
            },
            "evidence_refs": {
                "schema_version": "factor_pruning_evidence_refs.v1",
                "factor_pruning_candidate": source,
                "factor_blend_health": {
                    "active_alpha_count": (source.get("evidence") or {}).get("active_alpha_count"),
                    "family_count": (source.get("evidence") or {}).get("family_count"),
                },
            },
            "counter_evidence_refs": {
                "schema_version": "factor_pruning_counter_evidence_refs.v1",
                "required_before_bridge": [
                    "shadow_oos_no_recent_positive_contribution",
                    "no_unique_regime_edge",
                    "no_recent_trade_lesson_support",
                ],
            },
            "risk_verdict": risk_verdict,
            "decision_policy": decision_policy,
            "rollback_plan": {
                "schema_version": "factor_pruning_rollback_plan.v1",
                "candidate_lane_only": True,
                "runtime_mutation": False,
                "future_submit_requires_governed_bridge": True,
                "demo_nursery_system_bridge": True,
                "non_demo_explicit_bridge": True,
                "future_apply_requires_runtime_snapshot": True,
                "restore_weight": current_weight,
            },
            "lineage": {
                "schema_version": "factor_pruning_governance_lineage.v1",
                "phase": "factor_pruning_candidate_materialization",
                "mapped_action": mapped_action,
                "agent_context": agent_context,
                "agent_context_required": True,
                "authority_verdict": agent_context.get("authority_verdict") or {},
                "bridge": {
                    "policy_suggestion_direct_write": False,
                    "governed_bridge_required": True,
                    "demo_nursery_system_bridge": True,
                    "proposal_stage_not_bridge_ready": True,
                },
            },
            "status": "active",
            "submitted_suggestion_id": "",
            "submitted_at": 0.0,
            "expires_at": now + 7 * 86400,
            "created_at": now,
            "updated_at": now,
            "boundary": self.boundary(),
        }

    @staticmethod
    def _decision_policy_preview(*, factor: str, current_weight: float, target_weight: float) -> dict[str, Any]:
        decisions = DecisionPolicy().decide(
            awe_patches={factor: {"weight": target_weight, "reason": "factor_pruning_candidate"}},
            weight_policy_weights={factor: target_weight},
            shadow_perfs={},
            factor_configs={factor: {"enabled": True, "role": "alpha"}},
            current_weights={factor: current_weight},
        )
        decision = decisions.get(factor)
        return {
            "schema_version": "decision_policy_preview.v1",
            "required": True,
            "decision": decision.to_api() if decision else {},
            "applied": False,
        }

    def _agent_generation_context(
        self,
        *,
        scope_type: str,
        action: str,
        requested_writes: list[str],
        status: str,
        impact_level: str,
    ) -> dict[str, Any]:
        try:
            from backend.services.agent_briefing import AgentBriefingContextService

            return AgentBriefingContextService(self.db_path).agent_context(
                "factor_pruning_governance",
                scope_type=scope_type,
                action=action,
                requested_writes=requested_writes,
                status=status,
                impact_level=impact_level,
                limit=20,
            )
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": "agent_generation_context.v1",
                "source_agent": "factor_pruning_governance",
                "scope_type": scope_type,
                "action": action,
                "error": f"{type(exc).__name__}: {exc}",
                "boundary": {"pre_generation_context_only": True},
            }

    def _upsert_candidate(self, item: dict[str, Any]) -> dict[str, Any]:
        service = BrainGovernanceCandidateService(self.db_path)
        existing = service.load_candidate(str(item["candidate_id"]))
        if existing and existing.get("submitted_suggestion_id"):
            return {
                "status": "already_submitted",
                "candidate_id": item["candidate_id"],
                "suggestion_id": existing.get("submitted_suggestion_id", ""),
                "boundary": self.boundary(),
            }
        if not existing:
            service.create_candidate(
                candidate_id=item["candidate_id"],
                source_agent=item["source_agent"],
                source_kind=item["source_kind"],
                source_ref_type=item["source_ref_type"],
                source_ref_id=item["source_ref_id"],
                proposal_stage=item["proposal_stage"],
                capability_scope=item["capability_scope"],
                scope_type=item["scope_type"],
                scope_key=item["scope_key"],
                action=item["action"],
                confidence=item["confidence"],
                evidence_score=item["evidence_score"],
                risk_class=item["risk_class"],
                max_impact=item["max_impact"],
                expected_effect=item["expected_effect"],
                evidence_refs=item["evidence_refs"],
                counter_evidence_refs=item["counter_evidence_refs"],
                risk_verdict=item["risk_verdict"],
                decision_policy=item["decision_policy"],
                rollback_plan=item["rollback_plan"],
                lineage=item["lineage"],
                status=item["status"],
                expires_at=item["expires_at"],
                now=item["created_at"],
                persist=True,
            )
            return {
                "status": "candidate_materialized",
                "candidate_id": item["candidate_id"],
                "factor": item["scope_key"],
                "proposal_stage": item["proposal_stage"],
                "boundary": self.boundary(),
            }
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                UPDATE brain_governance_candidate
                SET source_ref_id=?,
                    confidence=?,
                    evidence_score=?,
                    expected_effect_json=?,
                    evidence_refs_json=?,
                    counter_evidence_refs_json=?,
                    risk_verdict_json=?,
                    decision_policy_json=?,
                    rollback_plan_json=?,
                    lineage_json=?,
                    status='active',
                    expires_at=?,
                    updated_at=?
                WHERE candidate_id=?
                """,
                (
                    item["source_ref_id"],
                    _safe_float(item["confidence"]),
                    _safe_float(item["evidence_score"]),
                    _dumps(item["expected_effect"]),
                    _dumps(item["evidence_refs"]),
                    _dumps(item["counter_evidence_refs"]),
                    _dumps(item["risk_verdict"]),
                    _dumps(item["decision_policy"]),
                    _dumps(item["rollback_plan"]),
                    _dumps(item["lineage"]),
                    _safe_float(item["expires_at"]),
                    _safe_float(item["updated_at"]),
                    item["candidate_id"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "status": "candidate_updated",
            "candidate_id": item["candidate_id"],
            "factor": item["scope_key"],
            "proposal_stage": item["proposal_stage"],
            "boundary": self.boundary(),
        }

    @staticmethod
    def _candidate_id(factor: str) -> str:
        return f"factor_pruning:{factor}"

    def _empty_status(self, status: str) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "factor_pruning_governance_status.v1",
            "status": status,
            "candidate_count": 0,
            "items": [],
            "boundary": self.boundary(),
        }
