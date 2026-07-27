from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from alpha.evaluation.evaluation_context import EvaluationContext
from alpha.evaluation.purged_walkforward import PurgedWalkForward
from alpha.streaming_factor_engine import StreamingFactorEngine
from backend.core.db import (
    DATA_DIR,
    STATE_DB,
    STATE_DB_DDL,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_pg_enabled,
)
from backend.jobs.progress import ProgressCB
from backend.services.backtest_service import run_backtest
from backend.services.parity_replay import MonthlyPITBarLoader, ParityReplayRequest
from backend.services.factor_cards import clear_factor_card_cache
from backend.services.parameter_templates import (
    ParameterTemplateService,
    clear_parameter_template_recommendation_cache,
)
from backend.services.research_evidence import (
    RESEARCH_EVIDENCE_POLICY_VERSION,
    ResearchEvidenceRejected,
    ResearchEvidenceVerdict,
    evaluate_research_evidence,
    has_research_trust_metadata,
    require_executable_research_evidence,
)
from research.learning.governor import RuleEvolutionGovernor


def _use_pg(db_path: str | Path) -> bool:
    return Path(db_path).resolve() == Path(STATE_DB).resolve()


def _conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn, sql: str, params: Any = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), params)


class ParameterTemplateValidationService:
    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or str(STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_dir = DATA_DIR / "parameter_template_validation_reports"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        conn = get_state_pg_conn() if _use_pg(self.db_path) else connect_sqlite(self.db_path)
        try:
            if not _conn_is_pg(conn):
                conn.executescript(STATE_DB_DDL)
            conn.commit()
        finally:
            conn.close()

    def _clear_governance_caches(self) -> None:
        clear_factor_card_cache(self.db_path)
        clear_parameter_template_recommendation_cache(self.db_path)

    def _log_lifecycle_event(
        self,
        *,
        factor_id: str,
        event: str,
        status: str,
        description: str,
        reason: str = "",
        score: float = 0.0,
    ) -> None:
        now = time.time()
        conn = get_state_pg_conn() if _use_pg(self.db_path) else connect_sqlite(self.db_path)
        try:
            _execute(
                conn,
                """
                INSERT INTO lifecycle_events
                (timestamp, event, factor, source, description, score, status, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    event,
                    factor_id,
                    "parameter_template",
                    description,
                    float(score or 0.0),
                    status,
                    reason,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

    def _template_service(self) -> ParameterTemplateService:
        try:
            return ParameterTemplateService(str(self.db_path))
        except TypeError:
            return ParameterTemplateService()

    @staticmethod
    def _candidate_research_evidence(candidate: dict[str, Any]) -> dict[str, Any]:
        summary = dict(candidate.get("validation_summary") or {})
        evidence = summary.get("research_evidence")
        return dict(evidence) if isinstance(evidence, dict) else {}

    @staticmethod
    def _research_evidence_gate(
        summary: dict[str, Any],
    ) -> tuple[dict[str, Any], ResearchEvidenceVerdict]:
        evidence = summary.get("research_evidence")
        evidence = dict(evidence) if isinstance(evidence, dict) else {}
        recorded_policy_version = str(
            summary.get("research_evidence_policy_version") or ""
        ).strip()
        verdict = evaluate_research_evidence(
            evidence,
            executable_use="parameter_template_release",
        )
        legacy_record = recorded_policy_version != RESEARCH_EVIDENCE_POLICY_VERSION
        if legacy_record:
            blocker = "candidate_research_policy_marker_missing"
            verdict = replace(
                verdict,
                allowed=False,
                reason=blocker,
                blockers=tuple(dict.fromkeys((blocker, *verdict.blockers))),
            )
        has_metadata = has_research_trust_metadata(evidence)
        state = (
            "verified"
            if verdict.allowed
            else "legacy_quarantined"
            if legacy_record
            else "diagnostic_only"
            if has_metadata
            else "require_revalidation"
        )
        gate = {
            "policy_version": RESEARCH_EVIDENCE_POLICY_VERSION,
            "recorded_policy_version": recorded_policy_version,
            "state": state,
            "allowed": bool(verdict.allowed),
            "legacy_record": legacy_record,
            "legacy_quarantined": not verdict.allowed,
            "require_revalidation": not verdict.allowed,
            "reason": verdict.reason,
            "blockers": list(verdict.blockers),
        }
        return gate, verdict

    @classmethod
    def _decorate_release_candidate(cls, item: dict[str, Any]) -> dict[str, Any]:
        candidate = dict(item)
        summary = dict(candidate.get("validation_summary") or {})
        gate, _verdict = cls._research_evidence_gate(summary)
        summary["research_evidence_gate"] = gate
        candidate["validation_summary"] = summary
        candidate["research_evidence_gate"] = gate
        candidate["legacy_quarantined"] = bool(gate["legacy_quarantined"])
        candidate["require_revalidation"] = bool(gate["require_revalidation"])
        # Never let a tampered/new actionable row retain an executable-looking
        # status after central re-evaluation. Historical/manual rows preserve
        # their payload and deployed rollback semantics, but must be
        # re-registered with a freshly verified parity artifact.
        if not gate["allowed"] and str(candidate.get("status") or "") in {
            "pending_review",
            "approved",
        }:
            candidate["legacy_status"] = str(candidate.get("status") or "")
            candidate["status"] = (
                "diagnostic_only"
                if gate["state"] == "diagnostic_only"
                else "legacy_quarantined"
            )
        return candidate

    def _require_candidate_executable_research_evidence(
        self,
        candidate: dict[str, Any],
        *,
        executable_use: str,
    ):
        summary = dict(candidate.get("validation_summary") or {})
        gate, verdict = self._research_evidence_gate(summary)
        if not verdict.allowed:
            # Persist the additive compatibility marker before rejecting so an
            # old approved row cannot keep presenting itself as deployable.
            summary["research_evidence_gate"] = gate
            summary["legacy_quarantined"] = True
            summary["require_revalidation"] = True
            candidate_id = str(candidate.get("candidate_id") or "")
            if candidate_id:
                current_status = str(candidate.get("status") or "")
                quarantine_status = (
                    current_status
                    if current_status in {
                        "deployed",
                        "rolled_back",
                        "rejected",
                        "diagnostic_only",
                    }
                    else "legacy_quarantined"
                )
                self._update_release_candidate(
                    candidate_id=candidate_id,
                    status=quarantine_status,
                    validation_summary=summary,
                    updated_at=time.time(),
                )
            raise ResearchEvidenceRejected(verdict)
        return require_executable_research_evidence(
            self._candidate_research_evidence(candidate),
            executable_use=executable_use,
        )

    @staticmethod
    def _candidate_template_snapshot(candidate: dict[str, Any]) -> dict[str, Any] | None:
        factor_id = str(candidate.get("factor_id") or "")
        template_id = str(candidate.get("template_id") or "")
        regime_key = str(candidate.get("regime_key") or "")
        summary = dict(candidate.get("validation_summary") or {})
        boundary = dict(candidate.get("boundary") or {})
        for raw in (
            summary.get("template_snapshot"),
            boundary.get("target_template"),
        ):
            if not isinstance(raw, dict):
                continue
            if not (raw.get("template_id") or raw.get("template_version") or raw.get("parameters")):
                continue
            snapshot = deepcopy(raw)
            snapshot_template_id = str(snapshot.get("template_id") or "")
            if template_id and snapshot_template_id and snapshot_template_id != template_id:
                continue
            if template_id:
                snapshot["template_id"] = template_id
            if factor_id:
                snapshot["factor_id"] = factor_id
            snapshot.setdefault("regime_key", regime_key)
            if (
                str(snapshot.get("template_id") or "")
                and str(snapshot.get("factor_id") or "")
                and str(snapshot.get("template_version") or "")
            ):
                return snapshot
        return None

    def ensure_candidate_template_materialized(
        self,
        candidate: dict[str, Any],
        *,
        template_service: ParameterTemplateService | None = None,
    ) -> dict[str, Any] | None:
        template_id = str(candidate.get("template_id") or "")
        if not template_id:
            return None
        template_service = template_service or self._template_service()
        existing = template_service.get_template(template_id=template_id)
        if existing:
            return existing
        snapshot = self._candidate_template_snapshot(candidate)
        if not snapshot:
            return None
        return template_service.upsert_template(
            snapshot,
            source="offline_validation_candidate",
            activate=False,
        )

    def list_release_candidates(
        self,
        *,
        factor_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if factor_id:
            clauses.append("factor_id=?")
            params.append(factor_id)
        if status:
            if status == "legacy_quarantined":
                clauses.append("status IN ('pending_review','approved','legacy_quarantined')")
            else:
                clauses.append("status=?")
                params.append(status)
        sql = f"""
            SELECT *
            FROM parameter_template_release_candidate
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT ?
        """
        fetch_limit = max(int(limit), min(500, int(limit) * 5))
        params.append(fetch_limit)
        conn = get_state_pg_conn(read_only=True) if _use_pg(self.db_path) else connect_sqlite(self.db_path, read_only=True)
        if not _use_pg(self.db_path):
            conn.row_factory = sqlite3.Row
        try:
            rows = _execute(conn, sql, tuple(params)).fetchall()
        finally:
            conn.close()
        items = [self._parse_release_candidate_row(row) for row in rows]
        if status:
            items = [item for item in items if item.get("status") == status]
        # Group by dedup key, then pick the entry with the highest status
        # priority. Diagnostic-only records remain visible but never outrank
        # actionable review/deployment states for the same lineage.
        STATUS_PRIORITY = {
            "pending_review": 0,
            "approved": 1,
            "deployed": 2,
            "rolled_back": 3,
            "legacy_quarantined": 4,
            "diagnostic_only": 5,
            "rejected": 6,
        }
        groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        for item in items:
            key = self._release_candidate_dedupe_key(item)
            groups.setdefault(key, []).append(item)
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        # Iterate in original SQL order (created_at DESC) so first-encountered key wins priority tie
        for item in items:
            key = self._release_candidate_dedupe_key(item)
            if key in seen:
                continue
            seen.add(key)
            group = groups[key]
            if len(group) > 1:
                # Sort within group: status priority ASC, then created_at DESC
                group.sort(key=lambda x: (
                    STATUS_PRIORITY.get(x.get("status", ""), 99),
                    -(x.get("created_at", 0) or 0),
                ))
            deduped.append(group[0])
            if len(deduped) >= int(limit):
                break
        return deduped

    def get_release_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        conn = get_state_pg_conn(read_only=True) if _use_pg(self.db_path) else connect_sqlite(self.db_path, read_only=True)
        if not _use_pg(self.db_path):
            conn.row_factory = sqlite3.Row
        try:
            row = _execute(
                conn,
                """
                SELECT *
                FROM parameter_template_release_candidate
                WHERE candidate_id=?
                """,
                (candidate_id,),
            ).fetchone()
        finally:
            conn.close()
        return self._parse_release_candidate_row(row) if row else None

    def register_release_candidate(
        self,
        *,
        factor_id: str,
        template_id: str,
        regime_key: str,
        boundary: dict[str, Any],
        walk_forward: dict[str, Any],
        validation_report_path: str,
        recommendation_context: dict[str, Any] | None = None,
        template_snapshot: dict[str, Any] | None = None,
        research_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        evidence = dict(research_evidence or {})
        evidence_verdict = evaluate_research_evidence(
            evidence,
            executable_use="parameter_template_release",
        )
        candidate_status = (
            "pending_review"
            if evidence_verdict.allowed
            else "diagnostic_only"
            if has_research_trust_metadata(evidence)
            else "legacy_quarantined"
        )
        existing = self._find_existing_release_candidate(
            factor_id=factor_id,
            template_id=template_id,
            regime_key=regime_key,
            recommendation_context=recommendation_context,
        )
        if existing:
            existing_status = str(existing.get("status") or "")
            if existing_status == "rejected" or (
                existing_status in {"legacy_quarantined", "diagnostic_only"}
                and evidence_verdict.allowed
            ):
                # Rejected/quarantined candidates get a fresh chance only after
                # a new validation run presents centrally verified evidence.
                now = time.time()
                summary = {
                    "walk_forward_passed": bool(walk_forward.get("passed")),
                    "candidate_avg_ic": float((walk_forward.get("candidate_summary") or {}).get("avg_ic") or 0.0),
                    "baseline_avg_ic": float((walk_forward.get("baseline_summary") or {}).get("avg_ic") or 0.0),
                    "candidate_avg_directional_accuracy": float(
                        (walk_forward.get("candidate_summary") or {}).get("avg_directional_accuracy") or 0.0
                    ),
                    "baseline_avg_directional_accuracy": float(
                        (walk_forward.get("baseline_summary") or {}).get("avg_directional_accuracy") or 0.0
                    ),
                    "fold_count": int((walk_forward.get("config") or {}).get("n_folds") or 0),
                    "recommendation_source": dict(recommendation_context or {}),
                    "research_evidence": evidence,
                    "research_evidence_policy_version": RESEARCH_EVIDENCE_POLICY_VERSION,
                    "research_evidence_verdict": evidence_verdict.to_dict(),
                }
                gate, _ = self._research_evidence_gate(summary)
                summary["research_evidence_gate"] = gate
                summary["legacy_quarantined"] = bool(gate["legacy_quarantined"])
                summary["require_revalidation"] = bool(gate["require_revalidation"])
                snapshot = self._candidate_template_snapshot(
                    {
                        "factor_id": factor_id,
                        "template_id": template_id,
                        "regime_key": regime_key,
                        "boundary": boundary,
                        "validation_summary": {"template_snapshot": template_snapshot or {}},
                    }
                )
                if snapshot:
                    summary["template_snapshot"] = snapshot
                    self.ensure_candidate_template_materialized(
                        {
                            "factor_id": factor_id,
                            "template_id": template_id,
                            "regime_key": regime_key,
                            "boundary": boundary,
                            "validation_summary": summary,
                        }
                    )
                self._update_release_candidate(
                    candidate_id=existing["candidate_id"],
                    status=candidate_status,
                    validation_summary=summary,
                    updated_at=time.time(),
                )
                self._log_lifecycle_event(
                    factor_id=factor_id,
                    event="parameter_template_candidate_updated",
                    status=candidate_status,
                    description=f"re-registered rejected candidate {existing['candidate_id']} as {candidate_status}",
                    reason=(
                        f"offline_deep_validation_passed:{(recommendation_context or {}).get('recommendation_id', '')}"
                        if recommendation_context else
                        "offline_deep_validation_passed"
                    ),
                    score=float(summary.get("candidate_avg_ic") or 0.0),
                )
                self._clear_governance_caches()
                return self._decorate_release_candidate({
                    **existing,
                    "status": candidate_status,
                    "validation_summary": summary,
                    "updated_at": now,
                    "boundary": boundary,
                    "validation_report_path": validation_report_path,
                })
            return existing
        now = time.time()
        candidate_id = self._new_id("ptrc")
        summary = {
            "walk_forward_passed": bool(walk_forward.get("passed")),
            "candidate_avg_ic": float((walk_forward.get("candidate_summary") or {}).get("avg_ic") or 0.0),
            "baseline_avg_ic": float((walk_forward.get("baseline_summary") or {}).get("avg_ic") or 0.0),
            "candidate_avg_directional_accuracy": float(
                (walk_forward.get("candidate_summary") or {}).get("avg_directional_accuracy") or 0.0
            ),
            "baseline_avg_directional_accuracy": float(
                (walk_forward.get("baseline_summary") or {}).get("avg_directional_accuracy") or 0.0
            ),
            "fold_count": int((walk_forward.get("config") or {}).get("n_folds") or 0),
            "recommendation_source": dict(recommendation_context or {}),
            "research_evidence": evidence,
            "research_evidence_policy_version": RESEARCH_EVIDENCE_POLICY_VERSION,
            "research_evidence_verdict": evidence_verdict.to_dict(),
        }
        gate, _ = self._research_evidence_gate(summary)
        summary["research_evidence_gate"] = gate
        summary["legacy_quarantined"] = bool(gate["legacy_quarantined"])
        summary["require_revalidation"] = bool(gate["require_revalidation"])
        snapshot = self._candidate_template_snapshot(
            {
                "factor_id": factor_id,
                "template_id": template_id,
                "regime_key": regime_key,
                "boundary": boundary,
                "validation_summary": {"template_snapshot": template_snapshot or {}},
            }
        )
        if snapshot:
            summary["template_snapshot"] = snapshot
            self.ensure_candidate_template_materialized(
                {
                    "factor_id": factor_id,
                    "template_id": template_id,
                    "regime_key": regime_key,
                    "boundary": boundary,
                    "validation_summary": summary,
                }
            )
        conn = get_state_pg_conn() if _use_pg(self.db_path) else connect_sqlite(self.db_path)
        try:
            _execute(
                conn,
                """
                INSERT INTO parameter_template_release_candidate
                (candidate_id, factor_id, template_id, regime_key, status,
                 boundary_json, validation_summary_json, validation_report_path,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    factor_id,
                    template_id,
                    regime_key,
                    candidate_status,
                    json.dumps(boundary, ensure_ascii=False, default=str),
                    json.dumps(summary, ensure_ascii=False, default=str),
                    validation_report_path,
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        item = {
            "candidate_id": candidate_id,
            "factor_id": factor_id,
            "template_id": template_id,
            "regime_key": regime_key,
            "status": candidate_status,
            "boundary": boundary,
            "validation_summary": summary,
            "validation_report_path": validation_report_path,
            "created_at": now,
            "updated_at": now,
        }
        self._log_lifecycle_event(
            factor_id=factor_id,
            event="parameter_template_candidate_registered",
            status=candidate_status,
            description=f"registered {candidate_status} candidate {candidate_id} for {template_id}",
            reason=(
                f"offline_deep_validation_passed:{(recommendation_context or {}).get('recommendation_id', '')}"
                if recommendation_context else
                "offline_deep_validation_passed"
            ),
            score=float(summary.get("candidate_avg_ic") or 0.0),
        )
        self._clear_governance_caches()
        return self._decorate_release_candidate(item)

    def _find_existing_release_candidate(
        self,
        *,
        factor_id: str,
        template_id: str,
        regime_key: str,
        recommendation_context: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        recommendation_id = str((recommendation_context or {}).get("recommendation_id") or "").strip()
        clauses = ["factor_id=?", "template_id=?", "COALESCE(regime_key, '')=?"]
        params: list[Any] = [factor_id, template_id, regime_key or ""]
        if recommendation_id:
            clauses.append("validation_summary_json LIKE ?")
            params.append(f"%{recommendation_id}%")
        else:
            clauses.append(
                "status IN ('pending_review','approved','deployed','legacy_quarantined','diagnostic_only')"
            )
        sql = f"""
            SELECT *
            FROM parameter_template_release_candidate
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT 1
        """
        conn = get_state_pg_conn(read_only=True) if _use_pg(self.db_path) else connect_sqlite(self.db_path, read_only=True)
        if not _use_pg(self.db_path):
            conn.row_factory = sqlite3.Row
        try:
            row = _execute(conn, sql, tuple(params)).fetchone()
        finally:
            conn.close()
        return self._parse_release_candidate_row(row) if row else None

    def review_release_candidate(
        self,
        *,
        candidate_id: str,
        status: str,
        note: str = "",
    ) -> dict[str, Any]:
        if status not in {"approved", "rejected"}:
            raise ValueError(f"unsupported review status: {status}")
        current = self.get_release_candidate(candidate_id)
        if not current:
            raise ValueError(f"candidate not found: {candidate_id}")
        if status == "approved":
            self._require_candidate_executable_research_evidence(
                current,
                executable_use="parameter_template_review",
            )
        if current["status"] not in {
            "pending_review",
            "approved",
            "rejected",
            "legacy_quarantined",
            "diagnostic_only",
        }:
            raise ValueError(f"candidate status not reviewable: {current['status']}")
        now = time.time()
        summary = dict(current.get("validation_summary") or {})
        summary["review"] = {
            "status": status,
            "note": note,
            "reviewed_at": now,
        }
        self._update_release_candidate(
            candidate_id=candidate_id,
            status=status,
            validation_summary=summary,
            updated_at=now,
        )
        updated = self.get_release_candidate(candidate_id)
        assert updated is not None
        self._log_lifecycle_event(
            factor_id=str(updated.get("factor_id") or ""),
            event="parameter_template_candidate_reviewed",
            status=status,
            description=f"release candidate {candidate_id} reviewed as {status}",
            reason=note or f"candidate_{status}",
            score=float((updated.get("validation_summary") or {}).get("candidate_avg_ic") or 0.0),
        )
        self._clear_governance_caches()
        return updated

    def reject_orphan_approved_candidates(self, note: str = "") -> list[dict[str, Any]]:
        """
        Reject approved release candidates whose target template cannot be resolved anymore.
        """
        template_service = self._template_service()
        custom_note = (
            str(note).strip()
            or "target template missing/orphan candidate: reviewed/reject because template not resolvable"
        )
        conn = get_state_pg_conn(read_only=True) if _use_pg(self.db_path) else connect_sqlite(self.db_path, read_only=True)
        if not _use_pg(self.db_path):
            conn.row_factory = sqlite3.Row
        try:
            rows = _execute(
                conn,
                """
                SELECT candidate_id, factor_id, template_id, regime_key
                FROM parameter_template_release_candidate
                WHERE status='approved'
                """
            ).fetchall()
        finally:
            conn.close()

        rejected: list[dict[str, Any]] = []
        for row in rows:
            candidate_id = str(row["candidate_id"] or "")
            template_id = str(row["template_id"] or "")
            if not template_id:
                continue
            if template_service.get_template(template_id=template_id):
                continue
            candidate = self.get_release_candidate(candidate_id)
            if not candidate:
                continue
            if self.ensure_candidate_template_materialized(candidate, template_service=template_service):
                continue
            if candidate.get("status") != "approved":
                continue
            note_text = f"{custom_note} {template_id}"
            updated = self.review_release_candidate(
                candidate_id=candidate_id,
                status="rejected",
                note=note_text,
            )
            rejected.append(
                {
                    "candidate_id": candidate_id,
                    "template_id": template_id,
                    "from_status": "approved",
                    "to_status": "rejected",
                    "candidate": updated,
                }
            )
        return rejected

    def deploy_release_candidate(
        self,
        *,
        candidate_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        candidate = self.get_release_candidate(candidate_id)
        if not candidate:
            raise ValueError(f"candidate not found: {candidate_id}")
        self._require_candidate_executable_research_evidence(
            candidate,
            executable_use="parameter_template_deploy",
        )
        if candidate["status"] not in {"approved", "deployed"}:
            raise ValueError(f"candidate not approved for release: {candidate['status']}")
        template_service = self._template_service()
        factor_id = str(candidate.get("factor_id") or "")
        regime_key = str(candidate.get("regime_key") or "")
        template_id = str(candidate.get("template_id") or "")
        if not template_service.get_template(template_id=template_id):
            materialized = self.ensure_candidate_template_materialized(
                candidate,
                template_service=template_service,
            )
            if not materialized:
                raise ValueError(
                    f"candidate template missing/orphan candidate: {template_id}, please regenerate candidate first"
                )
        active_before = template_service.get_active_template(factor_id=factor_id, regime_key=regime_key)
        old_template_id = str((active_before or {}).get("template_id") or "")
        suggestion = template_service.create_switch_suggestion(
            factor_id=factor_id,
            template_id=template_id,
            regime_key=regime_key,
            note=f"release_candidate:{candidate_id}",
        )
        RuleEvolutionGovernor(str(self.db_path)).set_status(
            suggestion["suggestion_id"],
            "approved",
            f"approved by release candidate {candidate_id}",
        )
        release_result = template_service.activate_template(
            factor_id=factor_id,
            template_id=template_id,
            regime_key=regime_key,
            suggestion_id=suggestion["suggestion_id"],
            note=note or f"deploy release candidate {candidate_id}",
            allow_offline_deep=True,
        )
        if release_result.get("blocked"):
            return {
                "ok": False,
                "blocked": True,
                "candidate": candidate,
                "release_result": release_result,
            }
        now = time.time()
        summary = dict(candidate.get("validation_summary") or {})
        summary["deployment"] = {
            "status": "deployed",
            "note": note,
            "deployed_at": now,
            "old_template_id": old_template_id,
            "new_template_id": template_id,
            "switch_id": release_result.get("switch_id", ""),
            "suggestion_id": suggestion["suggestion_id"],
        }
        self._update_release_candidate(
            candidate_id=candidate_id,
            status="deployed",
            validation_summary=summary,
            updated_at=now,
        )
        updated = self.get_release_candidate(candidate_id)
        assert updated is not None
        self._log_lifecycle_event(
            factor_id=factor_id,
            event="parameter_template_candidate_deployed",
            status="deployed",
            description=f"deployed release candidate {candidate_id} to {template_id}",
            reason=note or "gray_release_deployed",
            score=float((updated.get("validation_summary") or {}).get("candidate_avg_ic") or 0.0),
        )
        self._clear_governance_caches()
        return {
            "ok": True,
            "candidate": updated,
            "release_result": release_result,
        }

    def rollback_release_candidate(
        self,
        *,
        candidate_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        candidate = self.get_release_candidate(candidate_id)
        if not candidate:
            raise ValueError(f"candidate not found: {candidate_id}")
        summary = dict(candidate.get("validation_summary") or {})
        deployment = dict(summary.get("deployment") or {})
        if candidate["status"] != "deployed":
            raise ValueError(f"candidate not deployed: {candidate['status']}")
        old_template_id = str(deployment.get("old_template_id") or "")
        if not old_template_id:
            raise ValueError("candidate has no old_template_id for rollback")
        factor_id = str(candidate.get("factor_id") or "")
        regime_key = str(candidate.get("regime_key") or "")
        template_service = ParameterTemplateService(str(self.db_path))
        rollback_result = template_service.activate_template(
            factor_id=factor_id,
            template_id=old_template_id,
            regime_key=regime_key,
            note=note or f"rollback release candidate {candidate_id}",
            allow_offline_deep=True,
        )
        if rollback_result.get("blocked"):
            return {
                "ok": False,
                "blocked": True,
                "candidate": candidate,
                "rollback_result": rollback_result,
            }
        now = time.time()
        summary["rollback"] = {
            "status": "rolled_back",
            "note": note,
            "rolled_back_at": now,
            "restored_template_id": old_template_id,
            "switch_id": rollback_result.get("switch_id", ""),
        }
        self._update_release_candidate(
            candidate_id=candidate_id,
            status="rolled_back",
            validation_summary=summary,
            updated_at=now,
        )
        updated = self.get_release_candidate(candidate_id)
        assert updated is not None
        self._log_lifecycle_event(
            factor_id=factor_id,
            event="parameter_template_candidate_rolled_back",
            status="rolled_back",
            description=f"rolled back release candidate {candidate_id} to {old_template_id}",
            reason=note or "gray_release_rolled_back",
            score=float((updated.get("validation_summary") or {}).get("candidate_avg_ic") or 0.0),
        )
        self._clear_governance_caches()
        return {
            "ok": True,
            "candidate": updated,
            "rollback_result": rollback_result,
        }

    def _update_release_candidate(
        self,
        *,
        candidate_id: str,
        status: str,
        validation_summary: dict[str, Any],
        updated_at: float,
    ) -> None:
        conn = get_state_pg_conn() if _use_pg(self.db_path) else connect_sqlite(self.db_path)
        try:
            _execute(
                conn,
                """
                UPDATE parameter_template_release_candidate
                SET status=?, validation_summary_json=?, updated_at=?
                WHERE candidate_id=?
                """,
                (
                    status,
                    json.dumps(validation_summary, ensure_ascii=False, default=str),
                    updated_at,
                    candidate_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def _parse_release_candidate_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        return cls._decorate_release_candidate({
            "candidate_id": str(row["candidate_id"] or ""),
            "factor_id": str(row["factor_id"] or ""),
            "template_id": str(row["template_id"] or ""),
            "regime_key": str(row["regime_key"] or ""),
            "status": str(row["status"] or ""),
            "boundary": json.loads(row["boundary_json"] or "{}"),
            "validation_summary": json.loads(row["validation_summary_json"] or "{}"),
            "validation_report_path": str(row["validation_report_path"] or ""),
            "created_at": float(row["created_at"] or 0.0),
            "updated_at": float(row["updated_at"] or 0.0),
        })

    @staticmethod
    def _release_candidate_dedupe_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
        summary = dict(item.get("validation_summary") or {})
        source = dict(summary.get("recommendation_source") or {})
        recommendation_id = str(source.get("recommendation_id") or "").strip()
        if not recommendation_id:
            recommendation_id = "manual_or_unknown"
        return (
            str(item.get("factor_id") or ""),
            str(item.get("template_id") or ""),
            str(item.get("regime_key") or ""),
            recommendation_id,
        )


def build_offline_validation_plan(boundary: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "stage": "boundary_check",
            "status": "completed",
            "kind": "governance_guardrail",
            "note": "offline_deep required before runtime switch",
        },
        {
            "stage": "parity_backtest",
            "status": "queued",
            "kind": "backtest",
            "note": "run parameter-template candidate against existing backtest sweep entry",
        },
        {
            "stage": "walk_forward_review",
            "status": "queued",
            "kind": "walk_forward",
            "note": "attach out-of-sample fold evidence before approval",
        },
        {
            "stage": "gray_release_review",
            "status": "queued",
            "kind": "gray_release",
            "note": "materialize a pending_review release candidate after offline evidence passes",
        },
    ]


def _sanitize_series(values: Any) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if arr.ndim != 1:
        arr = arr.reshape(-1)
    arr[np.isinf(arr)] = np.nan
    return arr


def _corr(a: np.ndarray, b: np.ndarray) -> float | None:
    mask = np.isfinite(a) & np.isfinite(b)
    if int(mask.sum()) < 3:
        return None
    av = a[mask]
    bv = b[mask]
    if np.nanstd(av) <= 1e-12 or np.nanstd(bv) <= 1e-12:
        return None
    value = float(np.corrcoef(av, bv)[0, 1])
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _directional_accuracy(signal: np.ndarray, fwd_returns: np.ndarray) -> float | None:
    mask = np.isfinite(signal) & np.isfinite(fwd_returns) & (signal != 0) & (fwd_returns != 0)
    if int(mask.sum()) < 3:
        return None
    pred = np.sign(signal[mask])
    actual = np.sign(fwd_returns[mask])
    return float(np.mean(pred == actual))


def _mean(values: list[float | None]) -> float | None:
    clean = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v))]
    if not clean:
        return None
    return float(sum(clean) / len(clean))


def _fit_walk_forward_config(
    *,
    n_total: int,
    n_folds: int,
    train_bars: int,
    test_bars: int,
    purge_bars: int,
    embargo_bars: int,
) -> dict[str, int]:
    fitted_folds = max(1, int(n_folds))
    fitted_test = max(10, int(test_bars))
    fitted_train = max(StreamingFactorEngine.MIN_BARS, int(train_bars))
    purge = max(0, int(purge_bars))
    embargo = max(0, int(embargo_bars))

    max_train = n_total - purge - embargo - fitted_test * fitted_folds
    if max_train < fitted_train:
        fitted_train = max(StreamingFactorEngine.MIN_BARS, max_train)

    max_folds = max(1, (n_total - fitted_train - purge - embargo) // max(fitted_test, 1))
    fitted_folds = max(1, min(fitted_folds, max_folds))

    if fitted_train + purge + embargo + fitted_test * fitted_folds > n_total:
        fitted_test = max(
            10,
            (n_total - fitted_train - purge - embargo) // max(fitted_folds, 1),
        )
    if fitted_train + purge + embargo + fitted_test * fitted_folds > n_total:
        fitted_folds = 1
        fitted_test = max(10, n_total - fitted_train - purge - embargo)
    if fitted_train + purge + embargo + fitted_test * fitted_folds > n_total:
        fitted_train = max(
            StreamingFactorEngine.MIN_BARS,
            n_total - purge - embargo - fitted_test * fitted_folds,
        )
    return {
        "n_folds": max(1, fitted_folds),
        "train_bars": max(StreamingFactorEngine.MIN_BARS, fitted_train),
        "test_bars": max(10, fitted_test),
        "purge_bars": purge,
        "embargo_bars": embargo,
    }


def _evaluate_factor_template(
    *,
    factor_id: str,
    base_df: pd.DataFrame,
    candidate_overrides: dict[str, Any],
    baseline_overrides: dict[str, Any],
    n_folds: int,
    train_bars: int,
    test_bars: int,
    purge_bars: int,
    embargo_bars: int,
) -> dict[str, Any]:
    fitted = _fit_walk_forward_config(
        n_total=len(base_df),
        n_folds=n_folds,
        train_bars=train_bars,
        test_bars=test_bars,
        purge_bars=purge_bars,
        embargo_bars=embargo_bars,
    )
    eval_ctx = EvaluationContext(
        train_bars=fitted["train_bars"],
        test_bars=fitted["test_bars"],
        purge_bars=fitted["purge_bars"],
        embargo_bars=fitted["embargo_bars"],
    )
    folds = list(PurgedWalkForward(eval_ctx, n_folds=fitted["n_folds"]).folds(n_total=len(base_df)))
    candidate_engine = StreamingFactorEngine(
        max_buffer=max(len(base_df), StreamingFactorEngine.MIN_BARS + 1),
        factor_runtime_config={factor_id: {"parameter_overrides": dict(candidate_overrides or {})}},
    )
    baseline_engine = StreamingFactorEngine(
        max_buffer=max(len(base_df), StreamingFactorEngine.MIN_BARS + 1),
        factor_runtime_config={factor_id: {"parameter_overrides": dict(baseline_overrides or {})}},
    )
    candidate_series = _sanitize_series(candidate_engine._compute_factor_series(factor_id, base_df))
    baseline_series = _sanitize_series(baseline_engine._compute_factor_series(factor_id, base_df))
    close = base_df["close"].to_numpy(dtype=float)
    fwd_returns = np.append((close[1:] - close[:-1]) / close[:-1], np.nan)

    fold_items: list[dict[str, Any]] = []
    candidate_ic_values: list[float | None] = []
    baseline_ic_values: list[float | None] = []
    candidate_da_values: list[float | None] = []
    baseline_da_values: list[float | None] = []
    for fold in folds:
        test_idx = fold.test_indices
        cand_test = candidate_series[test_idx]
        base_test = baseline_series[test_idx]
        ret_test = fwd_returns[test_idx]
        candidate_ic = _corr(cand_test, ret_test)
        baseline_ic = _corr(base_test, ret_test)
        candidate_da = _directional_accuracy(cand_test, ret_test)
        baseline_da = _directional_accuracy(base_test, ret_test)
        candidate_ic_values.append(candidate_ic)
        baseline_ic_values.append(baseline_ic)
        candidate_da_values.append(candidate_da)
        baseline_da_values.append(baseline_da)
        fold_items.append(
            {
                "fold_id": int(fold.fold_id),
                "test_size": int(len(test_idx)),
                "candidate_ic": candidate_ic,
                "baseline_ic": baseline_ic,
                "candidate_directional_accuracy": candidate_da,
                "baseline_directional_accuracy": baseline_da,
            }
        )

    candidate_summary = {
        "avg_ic": _mean(candidate_ic_values),
        "avg_directional_accuracy": _mean(candidate_da_values),
    }
    baseline_summary = {
        "avg_ic": _mean(baseline_ic_values),
        "avg_directional_accuracy": _mean(baseline_da_values),
    }
    candidate_avg_ic = candidate_summary["avg_ic"] if candidate_summary["avg_ic"] is not None else -1.0
    baseline_avg_ic = baseline_summary["avg_ic"] if baseline_summary["avg_ic"] is not None else -1.0
    passed = candidate_avg_ic >= baseline_avg_ic - 1e-9
    return {
        "passed": passed,
        "config": {
            **fitted,
        },
        "candidate_summary": candidate_summary,
        "baseline_summary": baseline_summary,
        "folds": fold_items,
    }


def _write_validation_report(
    *,
    report_dir: Path,
    factor_id: str,
    template_id: str,
    payload: dict[str, Any],
) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = report_dir / f"parameter_template_validation_{factor_id}_{template_id.replace(':', '_')}_{ts}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return str(path)


def run_parameter_template_offline_validation(
    params: dict[str, Any],
    progress_cb: ProgressCB,
) -> dict[str, Any]:
    service = ParameterTemplateService()
    validation_service = ParameterTemplateValidationService(service.db_path)
    factor_id = str(params.get("factor_id") or "")
    template_id = str(params.get("template_id") or "")
    regime_key = str(params.get("regime_key") or "")
    boundary = service.assess_template_change(
        factor_id=factor_id,
        target_template_id=template_id,
        regime_key=regime_key,
    )
    plan = build_offline_validation_plan(boundary)
    if boundary.get("recommended_scope") != "offline_deep":
        progress_cb("skipped", 100, "template fits online_light; offline validation not required")
        return {
            "ok": False,
            "skipped": True,
            "message": "template fits online_light; use governed apply-switch flow instead",
            "boundary": boundary,
            "validation_plan": plan,
        }

    progress_cb("planning", 5, f"planning offline validation for {factor_id}")
    backtest_params = {
        "symbol": params.get("symbol", "XAUUSD+"),
        "timeframe": params.get("timeframe", "M5"),
        "start": params.get("start"),
        "end": params.get("end"),
        "max_bars": min(20_000, int(params.get("max_bars") or 5000)),
        "warmup_bars": int(params.get("warmup_bars") or 150),
        "initial_equity": float(params.get("initial_equity") or 10_000.0),
        "volume_lots": float(params.get("volume_lots") or 0.01),
        "commission_per_lot_round_turn": float(
            params.get("commission_per_lot_round_turn") or 6.0
        ),
        "slippage_bps": float(params.get("slippage_bps") or 0.0),
    }
    backtest_result = run_backtest(backtest_params, progress_cb)
    plan[1]["status"] = "completed"

    progress_cb("walk_forward", 93, f"running purged walk-forward for {factor_id}")
    target_template = boundary.get("target_template") or {}
    current_template = boundary.get("current_template") or {}
    base_df, _data_source = MonthlyPITBarLoader().load(
        ParityReplayRequest.from_mapping(backtest_params)
    )
    walk_forward = _evaluate_factor_template(
        factor_id=factor_id,
        base_df=base_df,
        candidate_overrides=target_template.get("parameters") or {},
        baseline_overrides=current_template.get("parameters") or {},
        n_folds=max(2, int(params.get("walk_forward_folds") or 3)),
        train_bars=max(80, int(params.get("walk_forward_train_bars") or 180)),
        test_bars=max(20, int(params.get("walk_forward_test_bars") or 40)),
        purge_bars=max(0, int(params.get("walk_forward_purge_bars") or 5)),
        embargo_bars=max(0, int(params.get("walk_forward_embargo_bars") or 5)),
    )
    plan[2]["status"] = "completed"

    report_payload = {
        "schema_version": "parameter_template_validation.v1",
        "factor_id": factor_id,
        "template_id": template_id,
        "regime_key": regime_key,
        "boundary": boundary,
        "template_snapshot": target_template,
        "recommendation_context": dict(params.get("recommendation_context") or {}),
        "backtest": backtest_result,
        "walk_forward": walk_forward,
        "validation_plan": plan,
        "created_at": time.time(),
    }
    report_path = _write_validation_report(
        report_dir=validation_service.report_dir,
        factor_id=factor_id,
        template_id=template_id,
        payload=report_payload,
    )
    release_candidate = validation_service.register_release_candidate(
        factor_id=factor_id,
        template_id=template_id,
        regime_key=regime_key,
        boundary=boundary,
        walk_forward=walk_forward,
        validation_report_path=report_path,
        recommendation_context=dict(params.get("recommendation_context") or {}),
        template_snapshot=target_template,
        research_evidence=backtest_result,
    )
    release_status = str(release_candidate.get("status") or "")
    plan[3]["status"] = (
        release_status
        if release_status in {"diagnostic_only", "legacy_quarantined"}
        else "completed"
    )
    return {
        "ok": True,
        "mode": "offline_deep",
        "factor_id": factor_id,
        "template_id": template_id,
        "regime_key": regime_key,
        "boundary": boundary,
        "validation_plan": plan,
        "backtest": backtest_result,
        "walk_forward": walk_forward,
        "release_candidate": release_candidate,
        "report_path": report_path,
        "note": (
            "Parity 回测仅作模拟研究；候选仍需真实证据后才能批准或部署"
            if release_status == "diagnostic_only"
            else "research metadata is missing; candidate requires revalidation before review or deploy"
            if release_status == "legacy_quarantined"
            else "offline_deep emits walk-forward evidence and a pending gray-release candidate"
        ),
    }
