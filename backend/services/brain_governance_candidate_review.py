from __future__ import annotations

import time
import uuid
import hashlib
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, state_table_columns, state_table_exists
from backend.services.agent_briefing import AgentBriefingContextService
from backend.services.agent_scorecard import AgentScorecardService
from backend.services._brain_helpers import connect as _connect, dumps as _dumps, execute as _execute, loads as _loads, safe_float as _safe_float
from backend.services.brain_governance_candidates import (
    BRIDGE_READY_STAGES,
    CANDIDATE_EXECUTION_PENDING_STATUSES,
    BrainGovernanceCandidateService,
    ensure_brain_governance_candidate_table,
    is_v16_candidate_bridge_evidence,
)
from backend.services.governance_eligibility import GOVERNANCE_ELIGIBILITY_VERSION
from research.learning.governance_conflicts import ACTIVE_CONFLICT_STATUSES, control_surface
from research.llm_advisory import LLMAdvisoryService


def _substantive_evidence(value: Any) -> Any:
    """Strip rotating audit coordinates while retaining measured evidence."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if (
                normalized in {"created_at", "updated_at", "expires_at", "timestamp", "ts"}
                or normalized.endswith("_id")
                or normalized.endswith("_ids")
            ):
                continue
            result[str(key)] = _substantive_evidence(item)
        return result
    if isinstance(value, list):
        return [_substantive_evidence(item) for item in value]
    return value


def ensure_brain_governance_candidate_review_table(db_path: str | Path = STATE_DB) -> None:
    ensure_brain_governance_candidate_table(db_path)
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS brain_governance_candidate_review (
                review_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                review_status TEXT DEFAULT '',
                bridge_ready INTEGER DEFAULT 0,
                bridge_reason TEXT DEFAULT '',
                evidence_gaps_json TEXT NOT NULL DEFAULT '[]',
                conflict_json TEXT NOT NULL DEFAULT '{}',
                bridge_preview_json TEXT NOT NULL DEFAULT '{}',
                source_reliability_json TEXT NOT NULL DEFAULT '{}',
                llm_advisory_json TEXT NOT NULL DEFAULT '{}',
                boundary_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        columns = state_table_columns(conn, "brain_governance_candidate_review")
        if "evidence_fingerprint" not in columns:
            _execute(
                conn,
                "ALTER TABLE brain_governance_candidate_review ADD COLUMN evidence_fingerprint TEXT NOT NULL DEFAULT ''",
            )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_candidate_review_candidate ON brain_governance_candidate_review(candidate_id, created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_candidate_review_status ON brain_governance_candidate_review(review_status, created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_candidate_review_fingerprint ON brain_governance_candidate_review(candidate_id, evidence_fingerprint, created_at)")
        conn.commit()
    finally:
        conn.close()


class BrainGovernanceCandidateReviewService:
    """Protocol review for isolated V16 governance candidates."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path
        self.candidates = BrainGovernanceCandidateService(db_path)

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "brain_governance_candidate_review_boundary.v1",
            "review_only": True,
            "does_not_submit_orders": True,
            "does_not_apply_factor_weights": True,
            "does_not_switch_templates": True,
            "does_not_mutate_runtime_overlay": True,
            "does_not_write_learning_samples": True,
            "does_not_submit_policy_suggestion": True,
            "bridge_preview_only": True,
            "demo_nursery_system_review_supported": True,
            "demo_nursery_review_does_not_require_operator": True,
            "llm_advisory_optional": True,
            "llm_advisory_only": True,
            "uses_existing_conflict_surface": True,
        }

    def review_latest(
        self,
        *,
        limit: int = 20,
        run_llm: bool = False,
        llm_dry_run: bool = True,
        persist: bool = True,
    ) -> dict[str, Any]:
        ensure_brain_governance_candidate_review_table(self.db_path)
        lifecycle = self.candidates.reconcile_expired_candidates()
        # Sweep the V16 command inbox on the same cadence.  The full
        # orchestration cycle is gated by posterior-evidence readiness and may
        # legitimately stay closed for a long time; without this sweep its
        # expired/observation-only commands would linger as 'available' in the
        # specialist inbox (they are already unactionable via the authority
        # age check, but the ledger would never be terminalized).
        if persist:
            try:
                from backend.services.v16_brain_orchestrator import (
                    cancel_expired_v16_commands,
                )

                cancel_expired_v16_commands(self.db_path)
            except Exception:
                # Command-hygiene is best-effort here; candidate review must
                # not fail because the command store is unavailable.
                pass
        limit = max(1, min(int(limit), 200))
        latest = self.candidates.latest_candidates(limit=limit)
        candidates = list(latest.get("items") or [])
        if not candidates:
            candidate_status = self.candidates.status(limit=limit)
            pending_count = int(candidate_status.get("execution_pending_count") or 0)
            reconciliation_count = int(
                candidate_status.get("bridge_reconciliation_required_count") or 0
            )
            if pending_count > 0 or reconciliation_count > 0:
                return {
                    "ok": pending_count > 0,
                    "schema_version": "brain_governance_candidate_review_run.v1",
                    "status": "execution_pending" if pending_count > 0 else "bridge_reconciliation_required",
                    "item_count": 0,
                    "execution_pending_count": pending_count,
                    "bridge_reconciliation_required_count": reconciliation_count,
                    "items": [],
                    "boundary": self.boundary(),
                }
            return {
                "ok": False,
                "schema_version": "brain_governance_candidate_review_run.v1",
                "status": "missing_candidates",
                "items": [],
                "boundary": self.boundary(),
            }
        now = time.time()
        context = self._review_context()
        items = []
        skipped_unchanged = 0
        for candidate in candidates:
            fingerprint = self._evidence_fingerprint(candidate, context)
            if persist and self._has_reviewed_fingerprint(str(candidate.get("candidate_id") or ""), fingerprint):
                skipped_unchanged += 1
                continue
            items.append(
                self._review_candidate(
                    candidate,
                    context=context,
                    now=now,
                    run_llm=run_llm,
                    llm_dry_run=llm_dry_run,
                    evidence_fingerprint=fingerprint,
                )
            )
        if persist:
            self._persist(items)
        return {
            "ok": True,
            "schema_version": "brain_governance_candidate_review_run.v1",
            "status": "reviewed" if items else "no_new_evidence",
            "item_count": len(items),
            "skipped_unchanged_count": skipped_unchanged,
            "lifecycle_reconcile": lifecycle,
            "items": items,
            "run_llm": bool(run_llm),
            "llm_dry_run": bool(llm_dry_run),
            "boundary": self.boundary(),
            "created_at": now,
        }

    def _review_context(self) -> dict[str, Any]:
        return {
            "policy_suggestions": self._active_policy_suggestions(),
            "candidates": self.candidates.latest_candidates(limit=200).get("items") or [],
            "source_reliability": self._source_reliability(),
            "agent_scorecard": self._agent_scorecard(),
            "briefing": AgentBriefingContextService(self.db_path).build(limit=20),
        }

    def review_candidate(
        self,
        candidate_id: str,
        *,
        run_llm: bool = False,
        llm_dry_run: bool = True,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Review one candidate through the same gates used by batch review."""
        ensure_brain_governance_candidate_review_table(self.db_path)
        candidate = self.candidates.load_candidate(str(candidate_id or ""))
        if not candidate:
            return {
                "ok": False,
                "schema_version": "brain_governance_candidate_review_run.v1",
                "status": "missing_candidate",
                "candidate_id": str(candidate_id or ""),
                "items": [],
                "boundary": self.boundary(),
            }
        if str(candidate.get("status") or "") in CANDIDATE_EXECUTION_PENDING_STATUSES:
            return {
                "ok": True,
                "schema_version": "brain_governance_candidate_review_run.v1",
                "status": "execution_pending",
                "item_count": 0,
                "candidate_id": str(candidate_id or ""),
                "candidate_status": str(candidate.get("status") or ""),
                "items": [],
                "boundary": self.boundary(),
                "created_at": time.time(),
            }
        now = time.time()
        context = self._review_context()
        fingerprint = self._evidence_fingerprint(candidate, context)
        if persist and self._has_reviewed_fingerprint(str(candidate_id or ""), fingerprint):
            return {
                "ok": True,
                "schema_version": "brain_governance_candidate_review_run.v1",
                "status": "no_new_evidence",
                "item_count": 0,
                "candidate_id": str(candidate_id or ""),
                "items": [],
                "boundary": self.boundary(),
                "created_at": now,
            }
        item = self._review_candidate(
            candidate,
            context=context,
            now=now,
            run_llm=run_llm,
            llm_dry_run=llm_dry_run,
            evidence_fingerprint=fingerprint,
        )
        if persist:
            self._persist([item])
        return {
            "ok": True,
            "schema_version": "brain_governance_candidate_review_run.v1",
            "status": "reviewed",
            "item_count": 1,
            "candidate_id": str(candidate_id or ""),
            "review": item,
            "items": [item],
            "run_llm": bool(run_llm),
            "llm_dry_run": bool(llm_dry_run),
            "boundary": self.boundary(),
            "created_at": now,
        }

    def latest_reviews(self, *, limit: int = 50) -> dict[str, Any]:
        ensure_brain_governance_candidate_review_table(self.db_path)
        limit = max(1, min(int(limit), 200))
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_governance_candidate_review"):
                return self._missing_status("missing_table")
            rows = _execute(
                conn,
                """
                SELECT review_id, candidate_id, review_status, bridge_ready,
                       bridge_reason, evidence_gaps_json, conflict_json,
                       bridge_preview_json, source_reliability_json,
                       llm_advisory_json, boundary_json, created_at
                FROM brain_governance_candidate_review
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return {
                "ok": bool(rows),
                "schema_version": "brain_governance_candidate_review_list.v1",
                "status": "available" if rows else "missing_reviews",
                "items": [self._row_to_review(row) for row in rows],
                "boundary": self.boundary(),
            }
        finally:
            conn.close()

    def status(self, *, limit: int = 50) -> dict[str, Any]:
        latest = self.latest_reviews(limit=limit)
        items = list(latest.get("items") or [])
        if not items:
            candidate_status = self.candidates.status(limit=limit)
            pending_count = int(candidate_status.get("execution_pending_count") or 0)
            reconciliation_count = int(
                candidate_status.get("bridge_reconciliation_required_count") or 0
            )
            return {
                "ok": pending_count > 0,
                "schema_version": "brain_governance_candidate_review_readiness.v1",
                "status": (
                    "execution_pending"
                    if pending_count > 0
                    else "bridge_reconciliation_required"
                    if reconciliation_count > 0
                    else latest.get("status", "missing_reviews")
                ),
                "review_count": 0,
                "execution_pending_count": pending_count,
                "bridge_reconciliation_required_count": reconciliation_count,
                "review_only": True,
                "bridge_preview_only": True,
            }
        statuses: dict[str, int] = {}
        bridge_ready = 0
        for item in items:
            status = str(item.get("review_status") or "unknown")
            statuses[status] = statuses.get(status, 0) + 1
            if bool(item.get("bridge_ready")):
                bridge_ready += 1
        return {
            "ok": True,
            "schema_version": "brain_governance_candidate_review_readiness.v1",
            "status": "available",
            "review_count": len(items),
            "latest_created_at": max(_safe_float(item.get("created_at")) for item in items),
            "statuses": dict(sorted(statuses.items())),
            "bridge_ready_count": bridge_ready,
            "review_only": True,
            "bridge_preview_only": True,
        }

    def bridge_review_coverage(self, *, limit: int = 200) -> dict[str, Any]:
        """Audit whether bridged policy suggestions have a bridge-ready candidate review."""
        ensure_brain_governance_candidate_review_table(self.db_path)
        limit = max(1, min(int(limit), 1000))
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "policy_suggestion"):
                return {
                    "ok": False,
                    "schema_version": "candidate_bridge_review_coverage.v1",
                    "status": "missing_policy_suggestion",
                    "items": [],
                    "boundary": self.boundary(),
                }
            rows = _execute(
                conn,
                """
                SELECT suggestion_id, status, evidence_json, created_at
                FROM policy_suggestion
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            items = [self._bridge_coverage_item(conn, row) for row in rows]
        finally:
            conn.close()
        bridged = [item for item in items if item.get("is_candidate_bridge")]
        violations = [item for item in bridged if item.get("coverage_status") == "missing_required_review"]
        legacy_unreviewed = [item for item in bridged if item.get("coverage_status") == "legacy_unreviewed"]
        covered = [item for item in bridged if item.get("coverage_status") == "covered"]
        status = "ok" if not violations else "degraded"
        return {
            "ok": status == "ok",
            "schema_version": "candidate_bridge_review_coverage.v1",
            "status": status,
            "candidate_bridge_count": len(bridged),
            "covered_count": len(covered),
            "missing_required_review_count": len(violations),
            "legacy_unreviewed_count": len(legacy_unreviewed),
            "coverage_ratio": round(len(covered) / len(bridged), 4) if bridged else 1.0,
            "violations": violations[:25],
            "items": bridged[: min(limit, 100)],
            "boundary": {
                **self.boundary(),
                "read_only_bridge_coverage_audit": True,
                "does_not_modify_policy_suggestion": True,
                "does_not_submit_candidates": True,
            },
        }

    def _review_candidate(
        self,
        candidate: dict[str, Any],
        *,
        context: dict[str, Any],
        now: float,
        run_llm: bool,
        llm_dry_run: bool,
        evidence_fingerprint: str = "",
    ) -> dict[str, Any]:
        candidate_id = str(candidate.get("candidate_id") or "")
        evidence_gaps = self._evidence_gaps(candidate, now=now)
        conflict = self._conflict(candidate, context=context)
        bridge_preview = self.candidates.preview_policy_suggestion_bridge(candidate_id)
        source_reliability = dict(context.get("source_reliability") or {}).get(
            str(candidate.get("source_agent") or ""),
            {},
        )
        source_reliability["agent_scorecard"] = dict(context.get("agent_scorecard") or {}).get(
            str(candidate.get("source_agent") or ""),
            {},
        )
        source_reliability["briefing_refs"] = self._briefing_refs(candidate, context.get("briefing") or {})
        evidence_gaps = sorted(set(evidence_gaps + self._reliability_evidence_gaps(source_reliability)))
        review_status = self._review_status(
            candidate=candidate,
            evidence_gaps=evidence_gaps,
            conflict=conflict,
            bridge_preview=bridge_preview,
            now=now,
        )
        if review_status == "bridge_ready":
            bridge_reason = str(bridge_preview.get("reason") or review_status)
        elif review_status == "needs_evidence" and evidence_gaps:
            bridge_reason = f"needs_evidence:{evidence_gaps[0]}"
        else:
            # The preview is an input to review, not the final review
            # decision.  Keep its raw reason nested below, while the
            # top-level field remains a stable explanation of the persisted
            # review status.
            bridge_reason = review_status or str(bridge_preview.get("reason") or "blocked")
        llm_advisory = self._llm_advisory(
            candidate=candidate,
            review_status=review_status,
            evidence_gaps=evidence_gaps,
            conflict=conflict,
            bridge_preview=bridge_preview,
            source_reliability=source_reliability,
            run_llm=run_llm,
            llm_dry_run=llm_dry_run,
        )
        return {
            "review_id": f"brain_candidate_review_{uuid.uuid4().hex[:16]}",
            "schema_version": "brain_governance_candidate_review.v1",
            "candidate_id": candidate_id,
            "evidence_fingerprint": evidence_fingerprint,
            "candidate": {
                "source_agent": candidate.get("source_agent", ""),
                "source_kind": candidate.get("source_kind", ""),
                "proposal_stage": candidate.get("proposal_stage", ""),
                "status": candidate.get("status", ""),
                "scope_type": candidate.get("scope_type", ""),
                "scope_key": candidate.get("scope_key", ""),
                "action": candidate.get("action", ""),
                "confidence": _safe_float(candidate.get("confidence")),
                "evidence_score": _safe_float(candidate.get("evidence_score")),
            },
            "review_status": review_status,
            "bridge_ready": review_status == "bridge_ready",
            "bridge_reason": bridge_reason,
            "evidence_gaps": evidence_gaps,
            "conflict": conflict,
            "bridge_preview": bridge_preview,
            "source_reliability": source_reliability,
            "briefing_context": {
                "schema_version": "candidate_review_briefing_context.v1",
                "chain_health": (context.get("briefing") or {}).get("chain_health") or {},
                "review_rules": (context.get("briefing") or {}).get("review_rules") or {},
                "recent_trade_feedback": (context.get("briefing") or {}).get("recent_trade_feedback") or {},
            },
            "llm_advisory": llm_advisory,
            "boundary": self.boundary(),
            "created_at": now,
        }

    def _bridge_coverage_item(self, conn: Any, row: Any) -> dict[str, Any]:
        evidence = _loads(row["evidence_json"], {})
        if not isinstance(evidence, dict):
            evidence = {}
        candidate_id = str(evidence.get("candidate_id") or "")
        schema = str(evidence.get("schema_version") or "")
        bridge = evidence.get("bridge") if isinstance(evidence.get("bridge"), dict) else {}
        is_bridge = bool(candidate_id) and (
            schema == "brain_governance_candidate_policy_suggestion_evidence.v1"
            or bool(bridge)
            or str(evidence.get("source_agent") or "") in {"v16_brain", "factor_pruning_governance"}
        )
        if not is_bridge:
            return {
                "suggestion_id": str(row["suggestion_id"] or ""),
                "is_candidate_bridge": False,
                "coverage_status": "not_candidate_bridge",
            }
        created_at = _safe_float(row["created_at"])
        review = _execute(
            conn,
            """
            SELECT review_id, review_status, bridge_ready, evidence_gaps_json, created_at
            FROM brain_governance_candidate_review
            WHERE candidate_id=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        required = bool(bridge.get("candidate_review_required") or bridge.get("candidate_review_required_before_submit"))
        required_before_submit = bool(bridge.get("candidate_review_required_before_submit"))
        review_ref = bridge.get("candidate_review") if isinstance(bridge.get("candidate_review"), dict) else {}
        latest_review = self._row_to_bridge_review(review) if review else {}
        ref_covered = bool(review_ref.get("bridge_ready")) and bool(review_ref.get("review_id"))
        latest_covered = bool(latest_review.get("bridge_ready")) and _safe_float(latest_review.get("created_at")) <= created_at + 5.0
        covered = ref_covered or latest_covered
        if covered:
            coverage_status = "covered"
        elif required_before_submit:
            coverage_status = "missing_required_review"
        else:
            coverage_status = "legacy_unreviewed"
        return {
            "suggestion_id": str(row["suggestion_id"] or ""),
            "status": str(row["status"] or ""),
            "candidate_id": candidate_id,
            "source_agent": str(evidence.get("source_agent") or ""),
            "is_candidate_bridge": True,
            "candidate_review_required": required,
            "candidate_review_required_before_submit": required_before_submit,
            "coverage_status": coverage_status,
            "evidence_review": review_ref,
            "latest_review": latest_review,
            "created_at": created_at,
        }

    @staticmethod
    def _row_to_bridge_review(row: Any) -> dict[str, Any]:
        if not row:
            return {}
        return {
            "review_id": str(row["review_id"] or ""),
            "review_status": str(row["review_status"] or ""),
            "bridge_ready": bool(row["bridge_ready"]),
            "evidence_gaps": _loads(row["evidence_gaps_json"], []),
            "created_at": _safe_float(row["created_at"]),
        }

    @staticmethod
    def _reliability_evidence_gaps(source_reliability: dict[str, Any]) -> list[str]:
        gaps: list[str] = []
        metric = dict(source_reliability.get("agent_scorecard") or {})
        score = _safe_float(metric.get("quality_score"), 0.55)
        if score < 0.5:
            gaps.append("agent_reliability_low_requires_extra_evidence")
        if int(metric.get("contract_violation_count") or 0) > 0:
            gaps.append("agent_contract_violation_history_requires_system_evidence")
        if int(metric.get("negative_effect_count") or 0) > 0:
            gaps.append("agent_negative_effect_history_requires_counter_evidence")
        if int(metric.get("low_reliability_count") or 0) >= 3:
            gaps.append("agent_low_reliability_history_requires_fresh_evidence")
        briefing = dict(source_reliability.get("briefing_refs") or {})
        if int(briefing.get("recent_loss_feedback_count") or 0) > 0 and score < 0.58:
            gaps.append("recent_loss_feedback_requires_counter_evidence")
        return sorted(set(gaps))

    @staticmethod
    def _briefing_refs(candidate: dict[str, Any], briefing: dict[str, Any]) -> dict[str, Any]:
        source = str(candidate.get("source_agent") or "")
        losses = []
        for item in (((briefing.get("recent_trade_feedback") or {}).get("recent_losses") or [])):
            targets = set(str(x) for x in (item.get("feedback_targets") or []))
            targets.update(str(x) for x in ((item.get("lesson") or {}).get("feedback_agents") or []))
            if source and source in targets:
                losses.append(item)
        return {
            "schema_version": "candidate_review_briefing_refs.v1",
            "source_agent": source,
            "recent_loss_feedback_count": len(losses),
            "recent_loss_review_ids": [str(item.get("review_id") or "") for item in losses[:5]],
            "chain_health_status": (briefing.get("chain_health") or {}).get("status", ""),
        }

    def _evidence_gaps(self, candidate: dict[str, Any], *, now: float) -> list[str]:
        gaps: list[str] = []
        if str(candidate.get("status") or "") != "active":
            gaps.append("candidate_not_active")
        if str(candidate.get("proposal_stage") or "") not in BRIDGE_READY_STAGES:
            gaps.append("proposal_stage_not_bridge_ready")
        if _safe_float(candidate.get("evidence_score")) < 0.5:
            gaps.append("evidence_score_below_threshold")
        expires_at = _safe_float(candidate.get("expires_at"))
        if expires_at > 0 and expires_at <= now:
            gaps.append("candidate_expired")
        if not bool((candidate.get("risk_verdict") or {}).get("allowed")):
            gaps.append("risk_policy_not_allowed")
        expected = dict(candidate.get("expected_effect") or {})
        source_presence = dict(expected.get("source_presence") or {})
        supervisor_bootstrap = False
        if (
            str(candidate.get("scope_type") or "") == "supervisor_template"
            and str(candidate.get("action") or "") == "switch_position_supervisor_template"
        ):
            mapped = dict((candidate.get("lineage") or {}).get("mapped_action") or {})
            target_template_id = str(
                mapped.get("target_template_id")
                or candidate.get("scope_key")
                or "position_supervisor"
            )
            try:
                from backend.services.learning_application_store import LearningApplicationStore

                supervisor_bootstrap = (
                    LearningApplicationStore(self.db_path).latest_effect(
                        scope_key=target_template_id,
                        scope_type="position_supervisor_template",
                    )
                    is None
                )
            except Exception:
                supervisor_bootstrap = False
        # ponytail: allow matured supervisor trace to bootstrap counterfactual/effect when eligible >=10
        # check supervisor trace count from expected_effect
        _supervisor_trace_cnt = 0
        try:
            _supervisor_trace_cnt = int((dict(expected.get("supervisor") or {}).get("trace_count") or 0))
        except Exception:
            _supervisor_trace_cnt = 0
        for source, present in sorted(source_presence.items()):
            if (
                not present
                and source == "learning_application_effect"
                and supervisor_bootstrap
            ):
                continue
            if not present and source in ("canonical_v2.counterfactual_review", "supervisor_counterfactual_review", "learning_application_effect") and _supervisor_trace_cnt >= 10:
                continue
            if not present:
                gaps.append(f"missing_{source}")
        action = str(candidate.get("action") or "")
        scope_type = str(candidate.get("scope_type") or "")
        if scope_type == "supervisor_template" and action == "switch_position_supervisor_template":
            replay = dict(expected.get("replay") or {})
            supervisor = dict(expected.get("supervisor") or {})
            if not replay.get("replay_run_id"):
                gaps.append("missing_replay_summary")
            if _safe_float(supervisor.get("trace_count")) <= 0:
                gaps.append("missing_supervisor_trace")
        if scope_type == "parameter_template" and action == "switch_parameter_template":
            mapped = dict((candidate.get("lineage") or {}).get("mapped_action") or {})
            if not mapped.get("target_template_id"):
                gaps.append("missing_target_template_id")
            if str(mapped.get("recommended_scope") or "") != "online_light":
                gaps.append("missing_online_light_recommended_scope")
        return sorted(set(gaps))

    def _conflict(self, candidate: dict[str, Any], *, context: dict[str, Any]) -> dict[str, Any]:
        surface = control_surface(self._candidate_as_row(candidate))
        policy_conflicts = []
        for row in list(context.get("policy_suggestions") or []):
            if control_surface(row) == surface:
                policy_conflicts.append(
                    {
                        "suggestion_id": row.get("suggestion_id", ""),
                        "status": row.get("status", ""),
                        "scope_type": row.get("scope_type", ""),
                        "scope_key": row.get("scope_key", ""),
                        "action": row.get("action", ""),
                    }
                )
        candidate_conflicts = []
        candidate_id = str(candidate.get("candidate_id") or "")
        for item in list(context.get("candidates") or []):
            if str(item.get("candidate_id") or "") == candidate_id:
                continue
            if str(item.get("status") or "") != "active":
                continue
            if control_surface(self._candidate_as_row(item)) == surface:
                candidate_conflicts.append(
                    {
                        "candidate_id": item.get("candidate_id", ""),
                        "proposal_stage": item.get("proposal_stage", ""),
                        "scope_type": item.get("scope_type", ""),
                        "scope_key": item.get("scope_key", ""),
                        "action": item.get("action", ""),
                    }
                )
        return {
            "schema_version": "brain_candidate_conflict_review.v1",
            "surface": surface,
            "active_policy_suggestions": policy_conflicts,
            "active_candidates": candidate_conflicts,
            "has_conflict": bool(policy_conflicts or candidate_conflicts),
            "resolver": "research.learning.governance_conflicts.control_surface",
        }

    @staticmethod
    def _candidate_as_row(candidate: dict[str, Any]) -> dict[str, Any]:
        scope_type = str(candidate.get("scope_type") or "")
        action = str(candidate.get("action") or "")
        lineage = dict(candidate.get("lineage") or {})
        mapped = dict(lineage.get("mapped_action") or {})
        if scope_type == "supervisor_template" and action == "switch_position_supervisor_template":
            return {
                "suggestion_id": candidate.get("candidate_id", ""),
                "scope_type": "position_supervisor_template",
                "scope_key": mapped.get("target_template_id") or candidate.get("scope_key", ""),
                "action": "switch_position_supervisor_template",
                "confidence": candidate.get("confidence", 0.0),
                "evidence_json": _dumps({"target_template_id": mapped.get("target_template_id", "")}),
                "status": "proposed",
            }
        return {
            "suggestion_id": candidate.get("candidate_id", ""),
            "scope_type": candidate.get("scope_type", ""),
            "scope_key": candidate.get("scope_key", ""),
            "action": candidate.get("action", ""),
            "confidence": candidate.get("confidence", 0.0),
            "evidence_json": _dumps(candidate.get("evidence_refs", {})),
            "status": "proposed",
        }

    @staticmethod
    def _review_status(
        *,
        candidate: dict[str, Any],
        evidence_gaps: list[str],
        conflict: dict[str, Any],
        bridge_preview: dict[str, Any],
        now: float,
    ) -> str:
        if candidate.get("submitted_suggestion_id"):
            return "submitted"
        expires_at = _safe_float(candidate.get("expires_at"))
        if expires_at > 0 and expires_at <= now:
            return "expired"
        if bool(conflict.get("has_conflict")):
            return "conflict_detected"
        if evidence_gaps:
            return "needs_evidence"
        if bool(bridge_preview.get("bridge_ready")):
            return "bridge_ready"
        reason = str(bridge_preview.get("reason") or "")
        if reason.startswith("unsupported_legacy_governor_surface"):
            return "not_bridge_compatible"
        return "blocked"

    def _active_policy_suggestions(self) -> list[dict[str, Any]]:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "policy_suggestion"):
                return []
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
                ORDER BY created_at DESC
                LIMIT 200
                """,
                (GOVERNANCE_ELIGIBILITY_VERSION,),
            ).fetchall()
            active = []
            for raw_row in rows:
                row = dict(raw_row)
                if str(row["status"] or "") not in ACTIVE_CONFLICT_STATUSES:
                    continue
                if str(row.get("scope_type") or "") == "position_supervisor_template":
                    evidence = _loads(row.get("evidence_json"), {})
                    if not is_v16_candidate_bridge_evidence(evidence):
                        # Legacy supervisor advisories are observation-only.
                        # They are terminalized by the demo policy path and
                        # must not prevent their V16 replacement from bridging.
                        continue
                active.append(row)
            return active
        finally:
            conn.close()

    def _source_reliability(self) -> dict[str, dict[str, Any]]:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_governance_candidate"):
                return {}
            rows = _execute(
                conn,
                """
                SELECT source_agent, status, COUNT(*) AS cnt
                FROM brain_governance_candidate
                GROUP BY source_agent, status
                """,
            ).fetchall()
            result: dict[str, dict[str, Any]] = {}
            for row in rows:
                source = str(row["source_agent"] or "unknown")
                status = str(row["status"] or "unknown")
                item = result.setdefault(
                    source,
                    {
                        "schema_version": "brain_source_reliability_snapshot.v1",
                        "source_agent": source,
                        "candidate_count": 0,
                        "status_counts": {},
                    },
                )
                count = int(row["cnt"] or 0)
                item["candidate_count"] += count
                item["status_counts"][status] = count
            for item in result.values():
                count = max(int(item.get("candidate_count") or 0), 1)
                submitted = int((item.get("status_counts") or {}).get("submitted", 0) or 0)
                item["submitted_rate"] = round(submitted / count, 6)
            return result
        finally:
            conn.close()

    def _agent_scorecard(self) -> dict[str, dict[str, Any]]:
        try:
            scorecard = AgentScorecardService(self.db_path).scorecard(limit=300)
            return {
                str(item.get("source_agent") or ""): item
                for item in (scorecard.get("items") or [])
                if str(item.get("source_agent") or "")
            }
        except Exception:
            return {}

    def _llm_advisory(
        self,
        *,
        candidate: dict[str, Any],
        review_status: str,
        evidence_gaps: list[str],
        conflict: dict[str, Any],
        bridge_preview: dict[str, Any],
        source_reliability: dict[str, Any],
        run_llm: bool,
        llm_dry_run: bool,
    ) -> dict[str, Any]:
        if not run_llm:
            return {
                "schema_version": "brain_candidate_llm_advisory.v1",
                "enabled": False,
                "advisory_only": True,
            }
        context = {
            "schema_version": "brain_candidate_llm_review_context.v1",
            "candidate": candidate,
            "review_status": review_status,
            "evidence_gaps": evidence_gaps,
            "conflict": conflict,
            "bridge_preview": bridge_preview,
            "source_reliability": source_reliability,
            "briefing": AgentBriefingContextService(self.db_path).build(limit=20),
            "forbidden_actions": [
                "do_not_submit_orders",
                "do_not_apply_factor_weights",
                "do_not_switch_templates",
                "do_not_bypass_RiskPolicyService",
                "do_not_submit_policy_suggestion",
            ],
        }
        result = LLMAdvisoryService(self.db_path).run(
            task_type="governance_review",
            context=context,
            target_type="brain_governance_candidate",
            target_id=str(candidate.get("candidate_id") or ""),
            dry_run=bool(llm_dry_run),
            max_tokens=800,
            temperature=0.1,
        )
        return {
            "schema_version": "brain_candidate_llm_advisory.v1",
            "enabled": True,
            "dry_run": bool(llm_dry_run),
            "status": result.get("status", ""),
            "audit": result.get("audit", {}),
            "advisory_only": True,
            "error": result.get("error", ""),
            "parsed": result.get("parsed", {}),
        }

    def _persist(self, items: list[dict[str, Any]]) -> None:
        ensure_brain_governance_candidate_review_table(self.db_path)
        conn = _connect(self.db_path)
        try:
            for item in items:
                _execute(
                    conn,
                    """
                    INSERT INTO brain_governance_candidate_review
                    (review_id, candidate_id, review_status, bridge_ready,
                     bridge_reason, evidence_gaps_json, conflict_json,
                    bridge_preview_json, source_reliability_json,
                     llm_advisory_json, boundary_json, evidence_fingerprint, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["review_id"],
                        item.get("candidate_id", ""),
                        item.get("review_status", ""),
                        1 if item.get("bridge_ready") else 0,
                        item.get("bridge_reason", ""),
                        _dumps(item.get("evidence_gaps", [])),
                        _dumps(item.get("conflict", {})),
                        _dumps(item.get("bridge_preview", {})),
                        _dumps(item.get("source_reliability", {})),
                        _dumps(item.get("llm_advisory", {})),
                        _dumps(item.get("boundary", {})),
                        str(item.get("evidence_fingerprint") or ""),
                        _safe_float(item.get("created_at")),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_review(row: Any) -> dict[str, Any]:
        return {
            "review_id": str(row["review_id"] or ""),
            "schema_version": "brain_governance_candidate_review.v1",
            "candidate_id": str(row["candidate_id"] or ""),
            "review_status": str(row["review_status"] or ""),
            "bridge_ready": bool(row["bridge_ready"]),
            "bridge_reason": str(row["bridge_reason"] or ""),
            "evidence_gaps": _loads(row["evidence_gaps_json"], []),
            "conflict": _loads(row["conflict_json"], {}),
            "bridge_preview": _loads(row["bridge_preview_json"], {}),
            "source_reliability": _loads(row["source_reliability_json"], {}),
            "llm_advisory": _loads(row["llm_advisory_json"], {}),
            "boundary": _loads(row["boundary_json"], BrainGovernanceCandidateReviewService.boundary()),
            "evidence_fingerprint": str(row["evidence_fingerprint"] or "") if "evidence_fingerprint" in row.keys() else "",
            "created_at": _safe_float(row["created_at"]),
        }

    @staticmethod
    def _evidence_fingerprint(candidate: dict[str, Any], context: dict[str, Any]) -> str:
        scorecard = dict(context.get("agent_scorecard") or {}).get(str(candidate.get("source_agent") or ""), {})
        briefing = dict(context.get("briefing") or {})
        candidate_row = BrainGovernanceCandidateReviewService._candidate_as_row(candidate)
        surface = control_surface(candidate_row)
        policy_conflicts = sorted(
            (
                str(row.get("status") or ""),
                str(row.get("scope_type") or ""),
                str(row.get("scope_key") or ""),
                str(row.get("action") or ""),
            )
            for row in (context.get("policy_suggestions") or [])
            if control_surface(row) == surface
        )
        candidate_conflicts = sorted(
            (
                str(item.get("proposal_stage") or ""),
                str(item.get("scope_type") or ""),
                str(item.get("scope_key") or ""),
                str(item.get("action") or ""),
            )
            for item in (context.get("candidates") or [])
            if str(item.get("candidate_id") or "") != str(candidate.get("candidate_id") or "")
            and str(item.get("status") or "") == "active"
            and control_surface(BrainGovernanceCandidateReviewService._candidate_as_row(item)) == surface
        )
        expected = dict(candidate.get("expected_effect") or {})
        mapped = dict((candidate.get("lineage") or {}).get("mapped_action") or {})
        payload = {
            "candidate_id": candidate.get("candidate_id"),
            "status": candidate.get("status"),
            "proposal_stage": candidate.get("proposal_stage"),
            "scope_type": candidate.get("scope_type"),
            "scope_key": candidate.get("scope_key"),
            "action": candidate.get("action"),
            "confidence": candidate.get("confidence"),
            "evidence_score": candidate.get("evidence_score"),
            "risk_allowed": bool((candidate.get("risk_verdict") or {}).get("allowed")),
            "source_presence": expected.get("source_presence") or {},
            "replay_status": (expected.get("replay") or {}).get("status"),
            "supervisor_trace_count": (expected.get("supervisor") or {}).get("trace_count"),
            "mapped_action": {
                "target_template_id": mapped.get("target_template_id"),
                "recommended_scope": mapped.get("recommended_scope"),
            },
            "evidence": _substantive_evidence(candidate.get("evidence_refs") or {}),
            "counter_evidence": _substantive_evidence(candidate.get("counter_evidence_refs") or {}),
            "control_surface": surface,
            "policy_conflicts": policy_conflicts,
            "candidate_conflicts": candidate_conflicts,
            "source_scorecard": {
                "quality_score": scorecard.get("quality_score"),
                "contract_violation_count": scorecard.get("contract_violation_count"),
                "negative_effect_count": scorecard.get("negative_effect_count"),
                "positive_effect_count": scorecard.get("positive_effect_count"),
            },
            "chain_health": (briefing.get("chain_health") or {}).get("status"),
        }
        return hashlib.sha256(_dumps(payload).encode("utf-8")).hexdigest()

    def _has_reviewed_fingerprint(self, candidate_id: str, fingerprint: str) -> bool:
        if not candidate_id or not fingerprint:
            return False
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_governance_candidate_review"):
                return False
            row = _execute(
                conn,
                """SELECT 1 FROM brain_governance_candidate_review
                   WHERE candidate_id=? AND evidence_fingerprint=?
                   ORDER BY created_at DESC LIMIT 1""",
                (candidate_id, fingerprint),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    @staticmethod
    def _missing_status(status: str) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "brain_governance_candidate_review_list.v1",
            "status": status,
            "items": [],
            "boundary": BrainGovernanceCandidateReviewService.boundary(),
        }
