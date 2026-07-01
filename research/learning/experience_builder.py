from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from backend.core.db import (
    STATE_DB,
    STATE_DB_DDL,
    connect_sqlite,
    ensure_sqlite_columns,
    get_state_pg_conn,
    is_state_db_path,
    state_pg_enabled,
)


class ExperienceBuilder:
    """Convert trade reviews into reusable experience samples."""

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or str(STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _p(self) -> str:
        return "%s" if self._use_pg() else "?"

    @contextmanager
    def _conn(self):
        conn = get_state_pg_conn() if self._use_pg() else connect_sqlite(self.db_path)
        if not self._use_pg():
            conn.row_factory = sqlite3.Row
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            if not self._use_pg():
                conn.executescript(STATE_DB_DDL)
        if not self._use_pg():
            ensure_sqlite_columns(
                self.db_path,
                "experience_memory",
                {
                    "source_table": "source_table TEXT DEFAULT ''",
                    "source_id": "source_id TEXT DEFAULT ''",
                    "append_source": "append_source TEXT DEFAULT ''",
                    "evolution_run_id": "evolution_run_id TEXT DEFAULT ''",
                },
            )
        with self._conn() as conn:
            if not self._use_pg():
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_experience_memory_source
                    ON experience_memory(source_table, source_id, append_source)
                    """
                )
            self._backfill_legacy_experience_sources(conn)
            self._repair_experience_event_timestamps(conn)

    def _backfill_legacy_experience_sources(self, conn) -> None:
        p = self._p()
        try:
            rows = conn.execute(
                """
                SELECT experience_id, trade_id, decision_context_json
                FROM experience_memory
                WHERE COALESCE(source_table, '')=''
                  AND COALESCE(source_id, '')=''
                  AND COALESCE(trade_id, '')!=''
                LIMIT 10000
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return
        for row in rows:
            review = conn.execute(
                f"""
                SELECT review_id, created_at
                FROM trade_outcome_review
                WHERE trade_id={p}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(row["trade_id"] or ""),),
            ).fetchone()
            if not review:
                continue
            try:
                context = json.loads(row["decision_context_json"] or "{}")
            except Exception:
                context = {}
            context["experience_source"] = {
                "source_table": "trade_outcome_review",
                "source_id": str(review["review_id"] or ""),
                "append_source": "legacy_experience_migrated.v1",
                "event_ts": float(review["created_at"] or 0.0),
            }
            conn.execute(
                f"""
                UPDATE experience_memory
                SET source_table='trade_outcome_review',
                    source_id={p},
                    append_source='legacy_experience_migrated.v1',
                    decision_context_json={p},
                    created_at=CASE
                        WHEN {p} > 0 THEN {p}
                        ELSE created_at
                    END
                WHERE experience_id={p}
                """,
                (
                    str(review["review_id"] or ""),
                    json.dumps(context, ensure_ascii=False, default=str),
                    float(review["created_at"] or 0.0),
                    float(review["created_at"] or 0.0),
                    str(row["experience_id"] or ""),
                ),
            )

    def _repair_experience_event_timestamps(self, conn) -> None:
        p = self._p()
        try:
            rows = conn.execute(
                """
                SELECT e.experience_id, e.decision_context_json, r.created_at AS review_created_at
                FROM experience_memory e
                JOIN trade_outcome_review r
                  ON e.source_table='trade_outcome_review'
                 AND e.source_id = r.review_id
                WHERE ABS(COALESCE(e.created_at, 0) - COALESCE(r.created_at, 0)) > 5.0
                LIMIT 10000
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return
        for row in rows:
            try:
                context = json.loads(row["decision_context_json"] or "{}")
            except Exception:
                context = {}
            source = context.get("experience_source") if isinstance(context.get("experience_source"), dict) else {}
            source["event_ts"] = float(row["review_created_at"] or 0.0)
            context["experience_source"] = source
            conn.execute(
                f"""
                UPDATE experience_memory
                SET created_at={p}, decision_context_json={p}
                WHERE experience_id={p}
                """,
                (
                    float(row["review_created_at"] or 0.0),
                    json.dumps(context, ensure_ascii=False, default=str),
                    str(row["experience_id"] or ""),
                ),
            )

    @staticmethod
    def _stable_experience_id(append_source: str, source_table: str, source_id: str) -> str:
        digest = hashlib.sha1(f"{append_source}:{source_table}:{source_id}".encode("utf-8")).hexdigest()[:18]
        return f"exp_{digest}"

    @staticmethod
    def _review_event_ts(review: dict, review_json: dict) -> float:
        for value in (
            review_json.get("close_ts"),
            review.get("created_at"),
            review_json.get("entry_ts"),
        ):
            try:
                ts = float(value or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            if ts > 0:
                return ts
        return time.time()

    def build_from_review(self, review: dict) -> dict:
        review_json = review.get("review_json", {}) or {}
        failure_tags = list(review.get("failure_tags", []) or [])
        outcome_label = str(review.get("outcome_label", "") or "")
        close_reason = str(review_json.get("close_reason", "") or "")
        context_integrity = str(review_json.get("context_integrity", "full") or "full")
        attribution_integrity = str(review_json.get("attribution_integrity", "full") or "full")
        inferred_supervisor = review_json.get("inferred_close_supervisor") or {}
        close_reason_source = str(review_json.get("close_reason_source", "") or "")
        supervisor_event_type = str(inferred_supervisor.get("event_type") or "")
        supervisor_action = str(inferred_supervisor.get("action") or "")
        supervisor_reason = str(
            inferred_supervisor.get("summary_reason")
            or inferred_supervisor.get("action_reason")
            or close_reason
            or ""
        )
        supervisor_evidence = inferred_supervisor.get("evidence") or {}
        thesis_status_at_exit = str(
            review_json.get("thesis_status_at_exit")
            or review_json.get("thesis_status")
            or supervisor_evidence.get("thesis_status")
            or ""
        )
        has_supervisor_feedback = bool(
            close_reason_source.startswith("supervisor")
            or supervisor_event_type.startswith("supervisor_")
            or supervisor_action in {"tighten", "reduce", "close"}
        )
        if context_integrity != "full" and "partial_context" not in failure_tags:
            failure_tags.append("partial_context")
        if attribution_integrity == "missing" and "attribution_missing" not in failure_tags:
            failure_tags.append("attribution_missing")
        if close_reason == "emergency_close" and "manual_intervention" not in failure_tags:
            failure_tags.append("manual_intervention")
        if close_reason == "restart_replay" and "restart_replay" not in failure_tags:
            failure_tags.append("restart_replay")
        if has_supervisor_feedback and "supervisor_entry_feedback" not in failure_tags:
            failure_tags.append("supervisor_entry_feedback")
        supervisor_thesis_broken = bool(
            has_supervisor_feedback
            and (
                thesis_status_at_exit == "broken"
                or supervisor_reason == "thesis_broken"
                or close_reason == "thesis_broken"
            )
        )
        if supervisor_thesis_broken and "supervisor_thesis_broken" not in failure_tags:
            failure_tags.append("supervisor_thesis_broken")

        def _factor_source(name: object) -> str:
            factor = str(name or "")
            if not factor:
                return ""
            try:
                from alpha.registry_adapter import RegistryAdapter
                meta = RegistryAdapter.shared().get_meta(factor)
                return str(meta.get("source", "") or "")
            except Exception:
                return ""

        def _is_actionable_factor(name: object) -> bool:
            factor = str(name or "")
            if not factor or factor.startswith("dsl_auto_"):
                return False
            return _factor_source(factor) in {"builtin", "discovered"}

        top_weight_factor = str(review_json.get("top_weight_factor", "") or "")
        top_factor = str(review_json.get("top_factor", "") or "")
        worst_factor = str(review_json.get("worst_factor", "") or "")

        if outcome_label in {"bad_loss", "good_loss"}:
            primary_factor = worst_factor if _is_actionable_factor(worst_factor) else (top_weight_factor or top_factor or worst_factor)
        else:
            primary_factor = top_weight_factor or top_factor or worst_factor
            if not _is_actionable_factor(primary_factor):
                primary_factor = top_weight_factor or top_factor or worst_factor
        scope_parts = [
            str(review.get("regime_id", "") or ""),
            str(primary_factor or ""),
            str(review.get("outcome_label", "") or ""),
        ]
        setup_hash = hashlib.sha1("|".join(scope_parts).encode("utf-8")).hexdigest()[:16]

        reward_score = 0.0
        pnl = float(review.get("pnl", 0.0) or 0.0)
        if pnl > 0:
            reward_score = min(1.0, pnl / max(abs(pnl), 50.0))
        elif pnl < 0:
            reward_score = -min(1.0, abs(pnl) / max(abs(pnl), 50.0))
        if supervisor_thesis_broken and pnl <= 0:
            reward_score = min(reward_score, -0.35)
        elif has_supervisor_feedback and pnl <= 0:
            reward_score = min(reward_score, -0.12)

        reward_scale = 1.0
        evidence_scale = 1.0
        if context_integrity != "full":
            reward_scale *= 0.5
            evidence_scale *= 0.35
        if attribution_integrity == "missing":
            reward_scale *= 0.5
            evidence_scale *= 0.25
        if close_reason in {"emergency_close", "restart_replay"}:
            reward_scale *= 0.6
            evidence_scale *= 0.5
        reward_score *= reward_scale

        supervisor_entry_failure = bool(
            supervisor_thesis_broken
            and pnl <= 0
            and context_integrity == "full"
            and attribution_integrity != "missing"
        )
        if review.get("outcome_label") == "bad_loss" or supervisor_entry_failure:
            recommended_action = "downweight"
        elif review.get("outcome_label") == "good_win":
            recommended_action = "watch"
        elif review.get("outcome_label") == "lucky_win":
            recommended_action = "watch"
        else:
            recommended_action = "watch"
        if context_integrity != "full" or attribution_integrity == "missing" or close_reason in {"emergency_close", "restart_replay"}:
            recommended_action = "watch"

        evidence_strength = min(1.0, max(0.15, abs(reward_score) + 0.20 * len(failure_tags)))
        evidence_strength = max(0.05, evidence_strength * evidence_scale)
        context = {
            "position_id": review.get("position_id", ""),
            "trade_id": review.get("trade_id", ""),
            "primary_factor": primary_factor,
            "primary_responsibility": review_json.get("primary_responsibility", ""),
            "responsibility_labels": list(review_json.get("responsibility_labels", []) or []),
            "entry_ts": review_json.get("entry_ts", 0.0),
            "close_ts": review_json.get("close_ts", 0.0),
            "holding_seconds": review_json.get("holding_seconds", 0.0),
            "holding_minutes": review_json.get("holding_minutes", 0.0),
            "timeframe": review_json.get("timeframe", ""),
            "failure_tags": failure_tags,
            "close_reason": close_reason,
            "close_reason_source": close_reason_source,
            "supervisor_feedback": {
                "has_feedback": has_supervisor_feedback,
                "entry_failure": supervisor_entry_failure,
                "event_type": supervisor_event_type,
                "action": supervisor_action,
                "reason": supervisor_reason,
                "thesis_status_at_exit": thesis_status_at_exit,
                "inferred_close_supervisor": inferred_supervisor,
            },
            "context_integrity": context_integrity,
            "attribution_integrity": attribution_integrity,
            "summary_text": review.get("summary_text", ""),
            "review_json": review_json,
        }
        source_table = "trade_outcome_review"
        source_id = str(review.get("review_id") or review_json.get("review_id") or review.get("trade_id") or "")
        append_source = "live_review"
        experience_id = self._stable_experience_id(append_source, source_table, source_id) if source_id else f"exp_{hashlib.sha1(str(time.time()).encode('utf-8')).hexdigest()[:18]}"
        event_ts = self._review_event_ts(review, review_json)
        context["experience_source"] = {
            "source_table": source_table,
            "source_id": source_id,
            "append_source": append_source,
            "event_ts": event_ts,
        }
        with self._conn() as conn:
            p = self._p()
            conn.execute(
                f"""
                INSERT INTO experience_memory
                (experience_id, trade_id, source_table, source_id, append_source,
                 regime_id, setup_hash, decision_context_json,
                 outcome_label, reward_score, failure_tags_json, recommended_action,
                 evidence_strength, artifact_version, evolution_run_id, created_at)
                VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, 'v1', '', {p})
                ON CONFLICT(experience_id) DO UPDATE SET
                    trade_id=excluded.trade_id,
                    source_table=excluded.source_table,
                    source_id=excluded.source_id,
                    append_source=excluded.append_source,
                    regime_id=excluded.regime_id,
                    setup_hash=excluded.setup_hash,
                    decision_context_json=excluded.decision_context_json,
                    outcome_label=excluded.outcome_label,
                    reward_score=excluded.reward_score,
                    failure_tags_json=excluded.failure_tags_json,
                    recommended_action=excluded.recommended_action,
                    evidence_strength=excluded.evidence_strength,
                    artifact_version=excluded.artifact_version,
                    evolution_run_id=excluded.evolution_run_id,
                    created_at=excluded.created_at
                """,
                (
                    experience_id,
                    str(review.get("trade_id", "")),
                    source_table,
                    source_id,
                    append_source,
                    str(review.get("regime_id", "") or ""),
                    setup_hash,
                    json.dumps(context, ensure_ascii=False, default=str),
                    str(review.get("outcome_label", "")),
                    round(reward_score, 6),
                    json.dumps(failure_tags, ensure_ascii=False),
                    recommended_action,
                    round(evidence_strength, 6),
                    event_ts,
                ),
            )

        return {
            "experience_id": experience_id,
            "trade_id": str(review.get("trade_id", "")),
            "source_table": source_table,
            "source_id": source_id,
            "append_source": append_source,
            "regime_id": str(review.get("regime_id", "") or ""),
            "setup_hash": setup_hash,
            "primary_factor": primary_factor,
            "primary_responsibility": str(review_json.get("primary_responsibility", "") or ""),
            "responsibility_labels": list(review_json.get("responsibility_labels", []) or []),
            "outcome_label": str(review.get("outcome_label", "")),
            "reward_score": float(reward_score),
            "failure_tags": failure_tags,
            "recommended_action": recommended_action,
            "evidence_strength": float(evidence_strength),
            "decision_context_json": context,
        }
