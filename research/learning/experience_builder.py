from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from backend.core.db import (
    STATE_DB,
    STATE_DB_DDL,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
)
from backend.services.trade_lesson_memory import (
    ensure_trade_lesson_memory_schema,
    upsert_trade_lesson_memory,
)
from backend.services.live_position_lifecycle import _compact_supervisor_mapping
from backend.services.review_contract import NON_FACTOR_RESPONSIBILITIES


class ExperienceBuilder:
    """Convert trade reviews into reusable experience samples."""

    def __init__(self, db_path: str | Path | None = None, *, ensure_schema: bool = True):
        self.db_path = Path(db_path or str(STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if ensure_schema:
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
            ensure_trade_lesson_memory_schema(conn)

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

    def build_from_review(self, review: dict, *, conn: Any | None = None) -> dict:
        review_json = review.get("review_json", {}) or {}
        failure_tags = list(review.get("failure_tags", []) or [])
        outcome_label = str(review.get("outcome_label", "") or "")
        close_reason = str(review_json.get("close_reason", "") or "")
        context_integrity = str(review_json.get("context_integrity", "full") or "full")
        attribution_integrity = str(review_json.get("attribution_integrity", "full") or "full")
        inferred_supervisor = _compact_supervisor_mapping(
            review_json.get("inferred_close_supervisor"),
            nested_keys=frozenset({"evidence", "recommended_controls", "execution", "risk_state"}),
        )
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
        primary_responsibility = str(review_json.get("primary_responsibility") or "")
        # B1: never downweight a factor for a responsibility that is not the
        # factor's own doing.  execution_timing / operator_intervention come
        # from the system-issue path (signal_execution_delay, manual/restart
        # close) and must be excluded the same way as the other system domains;
        # otherwise execution noise is misattributed as a factor defect.  The
        # exclusion vocabulary is the single review_contract authority.
        if (
            (review.get("outcome_label") == "bad_loss" or supervisor_entry_failure)
            and primary_responsibility not in NON_FACTOR_RESPONSIBILITIES
        ):
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
            "primary_responsibility": primary_responsibility,
            "responsibility_labels": list(review_json.get("responsibility_labels", []) or []),
            "direction": review_json.get("direction", 0),
            "action_score": review_json.get("entry_score", 0.0),
            "entry_ts": review_json.get("entry_ts", 0.0),
            "close_ts": review_json.get("close_ts", 0.0),
            "holding_seconds": review_json.get("holding_seconds", 0.0),
            "holding_minutes": review_json.get("holding_minutes", 0.0),
            "timeframe": review_json.get("timeframe", ""),
            "same_direction_open_count": review_json.get("same_direction_open_count", 0),
            "recent_same_direction_entries": review_json.get("recent_same_direction_entries", {}),
            "entry_cluster": review_json.get("entry_cluster", {}),
            "portfolio_exposure": review_json.get("portfolio_exposure", {}),
            "market_micro_context": review_json.get("market_micro_context", {}),
            "spread": review_json.get("spread", 0.0),
            "bar_context": review_json.get("bar_context", {}),
            "event_context": review_json.get("event_context", {}),
            "execution_context": review_json.get("execution_context", {}),
            "data_quality_context": review_json.get("data_quality_context", {}),
            "decision_quality_context": review_json.get("decision_quality_context", {}),
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
        }
        source_table = "trade_outcome_review"
        source_id = str(review.get("review_id") or review_json.get("review_id") or review.get("trade_id") or "")
        append_source = "trade_lesson_memory.v1"
        experience_id = f"trade_lesson:{source_id}"
        event_ts = self._review_event_ts(review, review_json)
        context["experience_source"] = {
            "source_table": source_table,
            "source_id": source_id,
            "append_source": append_source,
            "event_ts": event_ts,
        }
        lesson = {
            "experience_id": experience_id,
            "trade_id": str(review.get("trade_id", "")),
            "source_table": source_table,
            "source_id": source_id,
            "append_source": append_source,
            "regime_id": str(review.get("regime_id", "") or ""),
            "setup_hash": setup_hash,
            "decision_context_json": json.dumps(
                context, ensure_ascii=False, default=str
            ),
            "outcome_label": str(review.get("outcome_label", "")),
            "reward_score": round(reward_score, 6),
            "failure_tags_json": json.dumps(failure_tags, ensure_ascii=False),
            "recommended_action": recommended_action,
            "evidence_strength": round(evidence_strength, 6),
            "artifact_version": "trade_lesson.v1",
            "evolution_run_id": "",
            "created_at": event_ts,
        }
        if conn is None:
            with self._conn() as write_conn:
                upsert_trade_lesson_memory(write_conn, review, lesson=lesson)
        else:
            upsert_trade_lesson_memory(conn, review, lesson=lesson)

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
