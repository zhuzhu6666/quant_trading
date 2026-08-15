from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.db import (
    STATE_DB,
    STATE_DB_DDL,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_columns,
)
from backend.services.review_contract import review_has_system_contamination
from backend.services.state_payload_archive import load_json_payload
from research.features.evidence_contract import build_evidence_contract


SCHEMA_VERSION = "learning_sample.v2"
DECISION_SCHEMA_VERSION = "decision_sample.v2"


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _fmt(value: Any, digits: int = 4) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value)


def _timeframe_seconds(timeframe: str) -> int:
    mapping = {
        "M1": 60,
        "M5": 300,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
        "H4": 14400,
        "D1": 86400,
    }
    return mapping.get(str(timeframe or "").upper(), 0)


def _chunks(items: list[str], size: int = 500) -> list[list[str]]:
    return [items[idx: idx + size] for idx in range(0, len(items), max(1, int(size)))]


def _review_archive_select(conn: Any, *, alias: str = "r", output: str = "review_archive_hash") -> str:
    try:
        has_archive = "review_archive_hash" in state_table_columns(conn, "trade_outcome_review")
    except Exception:
        has_archive = False
    return f", {alias}.review_archive_hash AS {output}" if has_archive else ""


def _restore_review_payload(
    conn: Any,
    row: dict[str, Any],
    *,
    source_id_key: str = "review_id",
    inline_key: str = "review_json",
    archive_key: str = "review_archive_hash",
) -> None:
    row[inline_key] = load_json_payload(
        conn,
        source_table="trade_outcome_review",
        source_id=str(row.get(source_id_key) or ""),
        inline_json=row.get(inline_key),
        archive_hash=row.get(archive_key, ""),
        default={},
    )


def _base_temporal_context(decision_ts: float, timeframe: str) -> dict:
    ts = _safe_float(decision_ts)
    if ts <= 0:
        return {}
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    hour = int(dt.hour)
    if 0 <= hour < 7:
        session_label = "asia"
    elif 7 <= hour < 13:
        session_label = "europe"
    elif 13 <= hour < 21:
        session_label = "us"
    else:
        session_label = "rollover"
    return {
        "decision_ts": ts,
        "timeframe": str(timeframe or ""),
        "timeframe_seconds": _timeframe_seconds(timeframe),
        "hour_utc": hour,
        "minute_utc": int(dt.minute),
        "weekday_utc": int(dt.weekday()),
        "session_label": session_label,
        "is_weekend_utc": bool(dt.weekday() >= 5),
    }


def _derive_temporal_context(decision_ts: float, timeframe: str, risk_state: dict | None = None) -> dict:
    """Build model-facing temporal features from the ledger market decision time.

    Runtime audit payloads may contain an evaluation-time temporal context.  That
    is useful for execution audit, but it must not override market-time calendar
    features used by learning.
    """
    base = _base_temporal_context(decision_ts, timeframe)
    if not base:
        return {}

    risk_state = risk_state or {}
    verdict = ((risk_state.get("policy_verdict") or {}).get("audit_payload") or {})
    existing = verdict.get("temporal_context") or {}
    if not isinstance(existing, dict) or not existing:
        base["temporal_context_source"] = "decision_ledger"
        return base

    existing_ts = _safe_float(existing.get("decision_ts"))
    drift = existing_ts - _safe_float(decision_ts) if existing_ts > 0 else 0.0
    trusted_existing = existing_ts > 0 and abs(drift) <= 5.0
    if trusted_existing:
        for key in (
            "evaluated_at",
            "runtime_basis",
            "seconds_since_last_trade",
            "bars_since_last_trade",
            "loop_uptime_seconds",
        ):
            if key in existing:
                base[key] = existing[key]
        base["temporal_context_source"] = "risk_audit_verified"
        return base

    base["temporal_context_source"] = "decision_ledger"
    if existing_ts > 0:
        base["discarded_audit_decision_ts"] = existing_ts
        base["audit_market_time_drift_seconds"] = round(drift, 6)
        base["runtime_decision_ts"] = _safe_float(
            existing.get("runtime_decision_ts"),
            existing_ts,
        )
    if "evaluated_at" in existing:
        base["evaluated_at"] = existing["evaluated_at"]
    return base


class LearningFeatureProvider:
    """Build model-ready, explainable samples from the rule-learning ledger.

    This is intentionally read-only. It gives future statistical models or LLM
    review tools one stable contract instead of letting them scrape runtime
    tables directly.
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or str(STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _p(self) -> str:
        return "%s" if self._use_pg() else "?"

    @contextmanager
    def _conn(self, *, read_only: bool = True):
        conn = (
            get_state_pg_conn(read_only=read_only)
            if self._use_pg()
            else connect_sqlite(self.db_path, read_only=read_only)
        )
        if not self._use_pg():
            conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn(read_only=self._use_pg()) as conn:
            if not self._use_pg():
                conn.executescript(STATE_DB_DDL)
            conn.commit()

    @staticmethod
    def _parse_factor_snapshot_rows(rows: list[Any]) -> list[dict]:
        return [
            {
                "factor": str(row["factor"] or ""),
                "source": str(row["source"] or "registry"),
                "raw_value": _safe_float(row["raw_value"]),
                "normalized_value": _safe_float(row["normalized_value"]),
                "direction": _safe_float(row["direction"]),
                "base_weight": _safe_float(row["base_weight"]),
                "policy_weight": _safe_float(row["policy_weight"]),
                "shadow_score": _safe_float(row["shadow_score"]),
                "health_score": _safe_float(row["health_score"]),
                "gated": bool(row["gated"]),
                "gated_reason": str(row["gated_reason"] or ""),
                "contribution_score": _safe_float(row["contribution_score"]),
            }
            for row in rows
        ]

    def _factor_snapshots(self, decision_id: str) -> list[dict]:
        if not decision_id:
            return []
        with self._conn() as conn:
            p = self._p()
            rows = conn.execute(
                f"""
                SELECT factor, source, raw_value, normalized_value, direction,
                       base_weight, policy_weight, shadow_score, health_score,
                       gated, gated_reason, contribution_score
                FROM decision_factor_snapshot
                WHERE decision_id={p}
                ORDER BY ABS(contribution_score) DESC, factor ASC
                """,
                (decision_id,),
            ).fetchall()
        return self._parse_factor_snapshot_rows(rows)

    def _factor_snapshots_by_decision(self, decision_ids: list[str]) -> dict[str, list[dict]]:
        ids = [str(item) for item in decision_ids if str(item)]
        if not ids:
            return {}
        by_decision: dict[str, list[Any]] = defaultdict(list)
        p = self._p()
        with self._conn() as conn:
            for chunk in _chunks(ids):
                placeholders = ",".join(p for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT decision_id, factor, source, raw_value, normalized_value, direction,
                           base_weight, policy_weight, shadow_score, health_score,
                           gated, gated_reason, contribution_score
                    FROM decision_factor_snapshot
                    WHERE decision_id IN ({placeholders})
                    ORDER BY decision_id ASC, ABS(contribution_score) DESC, factor ASC
                    """,
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    by_decision[str(row["decision_id"] or "")].append(row)
        return {decision_id: self._parse_factor_snapshot_rows(rows) for decision_id, rows in by_decision.items()}

    def _factor_contribution_reviews(self, review_id: str) -> list[dict]:
        if not review_id:
            return []
        with self._conn() as conn:
            p = self._p()
            rows = conn.execute(
                f"""
                SELECT factor, entry_contribution, hold_contribution,
                       exit_contribution, net_contribution, confidence, notes
                FROM factor_contribution_review
                WHERE review_id={p}
                ORDER BY ABS(net_contribution) DESC, factor ASC
                """,
                (review_id,),
            ).fetchall()
        return [
            self._parse_factor_contribution_row(row)
            for row in rows
        ]

    @staticmethod
    def _parse_factor_contribution_row(row: Any) -> dict:
        return {
            "factor": str(row["factor"] or ""),
            "entry_contribution": _safe_float(row["entry_contribution"]),
            "hold_contribution": _safe_float(row["hold_contribution"]),
            "exit_contribution": _safe_float(row["exit_contribution"]),
            "net_contribution": _safe_float(row["net_contribution"]),
            "confidence": _safe_float(row["confidence"]),
            "notes": str(row["notes"] or ""),
            "note_payload": _loads(row["notes"], {}) if str(row["notes"] or "").startswith("{") else {},
        }

    def _factor_contribution_reviews_by_review(self, review_ids: list[str]) -> dict[str, list[dict]]:
        ids = [str(item) for item in review_ids if str(item)]
        if not ids:
            return {}
        p = self._p()
        by_review: dict[str, list[dict]] = defaultdict(list)
        with self._conn() as conn:
            for chunk in _chunks(ids):
                placeholders = ",".join(p for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT review_id, factor, entry_contribution, hold_contribution,
                           exit_contribution, net_contribution, confidence, notes
                    FROM factor_contribution_review
                    WHERE review_id IN ({placeholders})
                    ORDER BY review_id ASC, ABS(net_contribution) DESC, factor ASC
                    """,
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    by_review[str(row["review_id"] or "")].append(self._parse_factor_contribution_row(row))
        return dict(by_review)

    def _decision_rows_by_id(self, decision_ids: list[str]) -> dict[str, Any]:
        ids = [str(item) for item in decision_ids if str(item)]
        if not ids:
            return {}
        p = self._p()
        found = {}
        with self._conn() as conn:
            for chunk in _chunks(ids):
                placeholders = ",".join(p for _ in chunk)
                rows = conn.execute(
                    f"SELECT * FROM decision_ledger WHERE decision_id IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    found[str(row["decision_id"] or "")] = row
        return found

    def _experiences_by_trade(self, trade_ids: list[str]) -> dict[str, dict]:
        ids = [str(item) for item in trade_ids if str(item)]
        if not ids:
            return {}
        p = self._p()
        found = {}
        with self._conn() as conn:
            for chunk in _chunks(ids):
                placeholders = ",".join(p for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT e.*, r.review_json AS source_review_json{_review_archive_select(conn, output="source_review_archive_hash")}
                    FROM experience_memory e
                    JOIN trade_outcome_review r
                      ON e.source_table='trade_outcome_review'
                     AND r.review_id=e.source_id
                    WHERE e.append_source='trade_lesson_memory.v1'
                      AND e.trade_id IN ({placeholders})
                    ORDER BY e.trade_id ASC, e.created_at DESC
                    """,
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    _restore_review_payload(
                        conn,
                        item,
                        source_id_key="source_id",
                        inline_key="source_review_json",
                        archive_key="source_review_archive_hash",
                    )
                    if review_has_system_contamination(item.get("source_review_json")):
                        continue
                    trade_id = str(item["trade_id"] or "")
                    if trade_id and trade_id not in found:
                        found[trade_id] = self._parse_experience(item)
        return found

    @staticmethod
    def _parse_application_context_row(row: Any) -> dict:
        return {
            "application_id": str(row["application_id"] or ""),
            "scope_type": str(row["scope_type"] or ""),
            "scope_key": str(row["scope_key"] or ""),
            "action": str(row["action"] or ""),
            "bias_multiplier": _safe_float(row["bias_multiplier"], 1.0),
            "old_weight": _safe_float(row["old_weight"]),
            "new_weight": _safe_float(row["new_weight"]),
            "status": str(row["status"] or ""),
            "effect_status": str(row["effect_status"] or ""),
            "observed_trade_count": int(row["observed_trade_count"] or 0),
            "baseline_trade_count": int(row["baseline_trade_count"] or 0),
            "delta_avg_reward": _safe_float(row["delta_avg_reward"]),
            "post_win_rate": _safe_float(row["post_win_rate"]),
            "baseline_win_rate": _safe_float(row["baseline_win_rate"]),
            "decision": _loads(row["decision_json"], {}),
            "_cycle_ts": _safe_float(row["cycle_ts"]),
        }

    def _application_context_rows_for_factors(self, factor_names: list[str], max_created_at: float) -> list[dict]:
        names = [str(item) for item in factor_names if str(item)]
        if not names:
            return []
        p = self._p()
        rows_out: list[dict] = []
        with self._conn() as conn:
            for chunk in _chunks(names):
                placeholders = ",".join(p for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT l.application_id, l.scope_type, l.scope_key, l.action,
                           l.bias_multiplier, l.old_weight, l.new_weight, l.status,
                           l.cycle_ts, e.status AS effect_status,
                           e.observed_trade_count, e.baseline_trade_count,
                           e.delta_avg_reward, e.post_win_rate, e.baseline_win_rate,
                           e.decision_json
                    FROM learning_application_log l
                    LEFT JOIN learning_application_effect e
                      ON e.application_id = l.application_id
                    WHERE l.scope_type='factor'
                      AND l.scope_key IN ({placeholders})
                      AND l.cycle_ts <= {p}
                    ORDER BY l.cycle_ts DESC
                    """,
                    (*chunk, max_created_at),
                ).fetchall()
                rows_out.extend(self._parse_application_context_row(row) for row in rows)
        return rows_out

    @staticmethod
    def _application_context_from_prefetch(factors: list[dict], review_created_at: float, rows: list[dict]) -> list[dict]:
        names = {str(f["factor"] or "") for f in factors if f.get("factor")}
        if not names:
            return []
        filtered = [
            {key: value for key, value in item.items() if key != "_cycle_ts"}
            for item in rows
            if str(item.get("scope_key") or "") in names and _safe_float(item.get("_cycle_ts")) <= review_created_at
        ]
        return filtered[:20]

    def _order_events(self, *, decision_ids: list[str] | None = None, trade_id: str = "") -> list[dict]:
        ids = [str(item) for item in (decision_ids or []) if str(item)]
        clauses = []
        params: list[Any] = []
        p = self._p()
        if trade_id:
            clauses.append(f"trade_id={p}")
            params.append(str(trade_id))
        if ids:
            placeholders = ",".join(p for _ in ids)
            clauses.append(f"decision_id IN ({placeholders})")
            params.extend(ids)
        if not clauses:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT event_id, decision_id, trade_id, order_id, broker_order_id,
                       event_type, event_ts, price, volume, status, details_json
                FROM order_lifecycle_event
                WHERE {" OR ".join(clauses)}
                ORDER BY event_ts ASC, event_id ASC
                """,
                tuple(params),
            ).fetchall()
        return self._parse_order_event_rows(rows)

    @staticmethod
    def _parse_order_event_rows(rows: list[Any]) -> list[dict]:
        seen = set()
        events = []
        for row in rows:
            event_id = str(row["event_id"] or "")
            if event_id and event_id in seen:
                continue
            seen.add(event_id)
            events.append(
                {
                    "event_id": event_id,
                    "decision_id": str(row["decision_id"] or ""),
                    "trade_id": str(row["trade_id"] or ""),
                    "order_id": str(row["order_id"] or ""),
                    "broker_order_id": str(row["broker_order_id"] or ""),
                    "event_type": str(row["event_type"] or ""),
                    "event_ts": _safe_float(row["event_ts"]),
                    "price": _safe_float(row["price"]),
                    "volume": _safe_float(row["volume"]),
                    "status": str(row["status"] or ""),
                    "details": _loads(row["details_json"], {}),
                }
            )
        return events

    def _order_events_for_decisions(self, *, decision_ids: list[str], trade_ids: list[str] | None = None) -> list[dict]:
        ids = [str(item) for item in decision_ids if str(item)]
        trades = [str(item) for item in (trade_ids or []) if str(item)]
        if not ids and not trades:
            return []
        p = self._p()
        rows: list[Any] = []
        with self._conn() as conn:
            for chunk in _chunks(ids):
                placeholders = ",".join(p for _ in chunk)
                rows.extend(
                    conn.execute(
                        f"""
                        SELECT event_id, decision_id, trade_id, order_id, broker_order_id,
                               event_type, event_ts, price, volume, status, details_json
                        FROM order_lifecycle_event
                        WHERE decision_id IN ({placeholders})
                        ORDER BY event_ts ASC, event_id ASC
                        """,
                        tuple(chunk),
                    ).fetchall()
                )
            for chunk in _chunks(trades):
                placeholders = ",".join(p for _ in chunk)
                rows.extend(
                    conn.execute(
                        f"""
                        SELECT event_id, decision_id, trade_id, order_id, broker_order_id,
                               event_type, event_ts, price, volume, status, details_json
                        FROM order_lifecycle_event
                        WHERE trade_id IN ({placeholders})
                        ORDER BY event_ts ASC, event_id ASC
                        """,
                        tuple(chunk),
                    ).fetchall()
                )
        return sorted(self._parse_order_event_rows(rows), key=lambda item: (_safe_float(item.get("event_ts")), str(item.get("event_id") or "")))

    def _position_events(self, *, position_id: str = "", trade_id: str = "") -> list[dict]:
        clauses = []
        params: list[Any] = []
        p = self._p()
        if position_id:
            clauses.append(f"position_id={p}")
            params.append(str(position_id))
        if trade_id:
            clauses.append(f"trade_id={p}")
            params.append(str(trade_id))
        if not clauses:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT event_id, position_id, trade_id, symbol, event_type, event_ts,
                       net_volume, avg_price, unrealized_pnl, realized_pnl, details_json
                FROM position_lifecycle_event
                WHERE {" OR ".join(clauses)}
                ORDER BY event_ts ASC, event_id ASC
                """,
                tuple(params),
            ).fetchall()
        return self._parse_position_event_rows(rows)

    @staticmethod
    def _parse_position_event_rows(rows: list[Any]) -> list[dict]:
        seen = set()
        events = []
        for row in rows:
            event_id = str(row["event_id"] or "")
            if event_id and event_id in seen:
                continue
            seen.add(event_id)
            events.append(
                {
                    "event_id": event_id,
                    "position_id": str(row["position_id"] or ""),
                    "trade_id": str(row["trade_id"] or ""),
                    "symbol": str(row["symbol"] or ""),
                    "event_type": str(row["event_type"] or ""),
                    "event_ts": _safe_float(row["event_ts"]),
                    "net_volume": _safe_float(row["net_volume"]),
                    "avg_price": _safe_float(row["avg_price"]),
                    "unrealized_pnl": _safe_float(row["unrealized_pnl"]),
                    "realized_pnl": _safe_float(row["realized_pnl"]),
                    "details": _loads(row["details_json"], {}),
                }
            )
        return events

    def _position_events_for_positions(self, *, position_ids: list[str], trade_ids: list[str] | None = None) -> list[dict]:
        positions = [str(item) for item in position_ids if str(item)]
        trades = [str(item) for item in (trade_ids or []) if str(item)]
        if not positions and not trades:
            return []
        p = self._p()
        rows: list[Any] = []
        with self._conn() as conn:
            for chunk in _chunks(positions):
                placeholders = ",".join(p for _ in chunk)
                rows.extend(
                    conn.execute(
                        f"""
                        SELECT event_id, position_id, trade_id, symbol, event_type, event_ts,
                               net_volume, avg_price, unrealized_pnl, realized_pnl, details_json
                        FROM position_lifecycle_event
                        WHERE position_id IN ({placeholders})
                        ORDER BY event_ts ASC, event_id ASC
                        """,
                        tuple(chunk),
                    ).fetchall()
                )
            for chunk in _chunks(trades):
                placeholders = ",".join(p for _ in chunk)
                rows.extend(
                    conn.execute(
                        f"""
                        SELECT event_id, position_id, trade_id, symbol, event_type, event_ts,
                               net_volume, avg_price, unrealized_pnl, realized_pnl, details_json
                        FROM position_lifecycle_event
                        WHERE trade_id IN ({placeholders})
                        ORDER BY event_ts ASC, event_id ASC
                        """,
                        tuple(chunk),
                    ).fetchall()
                )
        return sorted(self._parse_position_event_rows(rows), key=lambda item: (_safe_float(item.get("event_ts")), str(item.get("event_id") or "")))

    @staticmethod
    def _execution_trace_from_events(order_events: list[dict], position_events: list[dict]) -> dict:
        order_statuses: dict[str, int] = {}
        position_statuses: dict[str, int] = {}
        for item in order_events:
            key = str(item.get("status") or item.get("event_type") or "unknown")
            order_statuses[key] = order_statuses.get(key, 0) + 1
        for item in position_events:
            key = str(item.get("event_type") or "unknown")
            position_statuses[key] = position_statuses.get(key, 0) + 1
        return {
            "order_events": order_events,
            "position_events": position_events,
            "summary": {
                "order_event_count": len(order_events),
                "position_event_count": len(position_events),
                "order_statuses": order_statuses,
                "position_event_types": position_statuses,
                "has_broker_lifecycle": bool(order_events or position_events),
                "has_failed_order": any(str(item.get("status") or "") == "failed" for item in order_events),
            },
        }

    def _execution_trace(
        self,
        *,
        decision_ids: list[str] | None = None,
        trade_id: str = "",
        position_id: str = "",
    ) -> dict:
        order_events = self._order_events(decision_ids=decision_ids, trade_id=trade_id)
        position_events = self._position_events(position_id=position_id, trade_id=trade_id)
        return self._execution_trace_from_events(order_events, position_events)

    @staticmethod
    def _decision_llm_context(
        *,
        decision: dict,
        target: dict,
        factors: list[dict],
        execution_trace: dict,
    ) -> dict:
        event_type = str(target.get("event_type") or decision.get("event_type") or "")
        top = factors[:3]
        evidence = [
            f"event={event_type}, score={_fmt(target.get('action_score'))}, direction={target.get('direction')}",
            f"gate_passed={target.get('gate_passed')}, reason={target.get('gate_reason') or decision.get('action_reason') or 'n/a'}",
        ]
        if target.get("failed_execution"):
            evidence.append(f"execution_failed at {target.get('skip_stage') or 'unknown_stage'}")
        if top:
            evidence.append(
                "top_factors="
                + ", ".join(
                    f"{item.get('factor')}:{_fmt(item.get('contribution_score'))}"
                    for item in top
                )
            )
        summary = execution_trace.get("summary") or {}
        if summary.get("order_event_count") or summary.get("position_event_count"):
            evidence.append(
                f"lifecycle orders={summary.get('order_event_count', 0)}, positions={summary.get('position_event_count', 0)}"
            )
        prompt_card = (
            f"{event_type} | {decision.get('symbol')}/{decision.get('timeframe')} | "
            f"score={_fmt(target.get('action_score'))} | "
            f"reason={target.get('gate_reason') or decision.get('action_reason') or 'n/a'}"
        )
        return {
            "task_hint": "Explain this decision and identify whether factors, gates, or execution lifecycle drove the outcome.",
            "prompt_card": prompt_card,
            "evidence_bullets": evidence,
            "label_summary": {
                "event_type": event_type,
                "executed": bool(target.get("executed")),
                "skipped": bool(target.get("skipped")),
                "failed_execution": bool(target.get("failed_execution")),
            },
        }

    @staticmethod
    def _trade_llm_context(
        *,
        review: dict,
        target: dict,
        factor_outcomes: list[dict],
        attribution_alignment: dict,
        execution_trace: dict,
        application_context: list[dict],
    ) -> dict:
        harmful = [
            item for item in factor_outcomes
            if (item.get("outcome_contribution") or {}).get("outcome_role") == "harmful"
        ][:3]
        helpful = [
            item for item in factor_outcomes
            if (item.get("outcome_contribution") or {}).get("outcome_role") == "helpful"
        ][:3]
        evidence = [
            f"outcome={target.get('outcome_label')}, pnl={_fmt(target.get('pnl'), 2)}, reward={_fmt(target.get('reward_score'))}",
            f"failure_tags={','.join(target.get('failure_tags') or []) or 'none'}",
        ]
        if harmful:
            evidence.append(
                "harmful_factors="
                + ", ".join(
                    f"{item.get('factor')}:{_fmt((item.get('outcome_contribution') or {}).get('net_contribution'), 2)}"
                    for item in harmful
                )
            )
        if helpful:
            evidence.append(
                "helpful_factors="
                + ", ".join(
                    f"{item.get('factor')}:{_fmt((item.get('outcome_contribution') or {}).get('net_contribution'), 2)}"
                    for item in helpful
                )
            )
        labels = attribution_alignment.get("labels") or {}
        if labels:
            evidence.append(
                "attribution_labels="
                + ", ".join(f"{key}:{value}" for key, value in sorted(labels.items()))
            )
        summary = execution_trace.get("summary") or {}
        if summary.get("order_event_count") or summary.get("position_event_count"):
            evidence.append(
                f"execution_trace orders={summary.get('order_event_count', 0)}, positions={summary.get('position_event_count', 0)}, failed_order={summary.get('has_failed_order')}"
            )
        if application_context:
            latest = application_context[0]
            evidence.append(
                f"prior_learning={latest.get('scope_key')} {latest.get('action')} status={latest.get('status')}"
            )
        prompt_card = (
            f"trade {review.get('trade_id') or review.get('position_id')} | "
            f"{target.get('outcome_label')} | pnl={_fmt(target.get('pnl'), 2)} | "
            f"recommended={target.get('recommended_action') or 'n/a'}"
        )
        return {
            "task_hint": "Explain the trade outcome, cite factor evidence, and suggest whether the rule-learning action is justified.",
            "prompt_card": prompt_card,
            "evidence_bullets": evidence,
            "label_summary": {
                "outcome_label": target.get("outcome_label"),
                "recommended_action": target.get("recommended_action"),
                "pnl": target.get("pnl"),
                "failure_tags": target.get("failure_tags") or [],
            },
        }

    @staticmethod
    def _align_factor_outcomes(
        factors: list[dict],
        contribution_reviews: list[dict],
    ) -> list[dict]:
        by_factor = {str(item.get("factor") or ""): item for item in contribution_reviews}
        aligned: list[dict] = []
        for factor in factors:
            name = str(factor.get("factor") or "")
            review = by_factor.get(name, {})
            entry = _safe_float(factor.get("contribution_score"))
            net = _safe_float(review.get("net_contribution"))
            delta = net - entry
            same_direction = (entry == 0.0 and net == 0.0) or (entry > 0 and net > 0) or (entry < 0 and net < 0)
            if not review:
                attribution_label = "missing_outcome"
            elif same_direction and abs(net) >= abs(entry):
                attribution_label = "confirmed"
            elif same_direction:
                attribution_label = "weakened"
            else:
                attribution_label = "contradicted"
            outcome_role = "neutral"
            if net > 0:
                outcome_role = "helpful"
            elif net < 0:
                outcome_role = "harmful"
            aligned.append(
                {
                    **factor,
                    "outcome_contribution": {
                        "entry_contribution": entry,
                        "hold_contribution": _safe_float(review.get("hold_contribution")),
                        "exit_contribution": _safe_float(review.get("exit_contribution")),
                        "net_contribution": net,
                        "contribution_delta": round(delta, 6),
                        "confidence": _safe_float(review.get("confidence")),
                        "notes": str(review.get("notes") or ""),
                        "primary_responsibility": str((review.get("note_payload") or {}).get("primary_responsibility") or ""),
                        "responsibility_labels": list((review.get("note_payload") or {}).get("responsibility_labels") or []),
                        "factor_role_hint": str((review.get("note_payload") or {}).get("factor_role") or ""),
                        "same_direction": same_direction,
                        "attribution_label": attribution_label,
                        "outcome_role": outcome_role,
                    },
                }
            )
        for review in contribution_reviews:
            name = str(review.get("factor") or "")
            if name and not any(str(item.get("factor") or "") == name for item in aligned):
                net = _safe_float(review.get("net_contribution"))
                aligned.append(
                    {
                        "factor": name,
                        "source": "review_only",
                        "raw_value": 0.0,
                        "normalized_value": 0.0,
                        "direction": 0.0,
                        "base_weight": 0.0,
                        "policy_weight": 0.0,
                        "shadow_score": 0.0,
                        "health_score": 0.0,
                        "gated": True,
                        "gated_reason": "missing_entry_snapshot",
                        "contribution_score": 0.0,
                        "outcome_contribution": {
                            "entry_contribution": 0.0,
                            "hold_contribution": _safe_float(review.get("hold_contribution")),
                            "exit_contribution": _safe_float(review.get("exit_contribution")),
                            "net_contribution": net,
                            "contribution_delta": round(net, 6),
                            "confidence": _safe_float(review.get("confidence")),
                            "notes": str(review.get("notes") or ""),
                            "primary_responsibility": str((review.get("note_payload") or {}).get("primary_responsibility") or ""),
                            "responsibility_labels": list((review.get("note_payload") or {}).get("responsibility_labels") or []),
                            "factor_role_hint": str((review.get("note_payload") or {}).get("factor_role") or ""),
                            "same_direction": False,
                            "attribution_label": "review_only",
                            "outcome_role": "helpful" if net > 0 else "harmful" if net < 0 else "neutral",
                        },
                    }
                )
        return sorted(aligned, key=lambda item: abs(_safe_float(item.get("outcome_contribution", {}).get("net_contribution"))), reverse=True)

    @staticmethod
    def _attribution_alignment(factors: list[dict]) -> dict:
        labels: dict[str, int] = {}
        total_abs_net = 0.0
        total_abs_delta = 0.0
        for factor in factors:
            outcome = factor.get("outcome_contribution") or {}
            label = str(outcome.get("attribution_label") or "unknown")
            labels[label] = labels.get(label, 0) + 1
            total_abs_net += abs(_safe_float(outcome.get("net_contribution")))
            total_abs_delta += abs(_safe_float(outcome.get("contribution_delta")))
        most_harmful = [
            {
                "factor": item.get("factor"),
                "net_contribution": _safe_float(item.get("outcome_contribution", {}).get("net_contribution")),
                "entry_contribution": _safe_float(item.get("outcome_contribution", {}).get("entry_contribution")),
                "attribution_label": item.get("outcome_contribution", {}).get("attribution_label"),
                "outcome_role": item.get("outcome_contribution", {}).get("outcome_role"),
            }
            for item in sorted(
                factors,
                key=lambda item: _safe_float(item.get("outcome_contribution", {}).get("net_contribution")),
            )[:5]
        ]
        return {
            "factor_count": len(factors),
            "labels": labels,
            "total_abs_net_contribution": round(total_abs_net, 6),
            "total_abs_contribution_delta": round(total_abs_delta, 6),
            "most_harmful_factors": most_harmful,
        }

    def _decision_features_from_row(self, row: Any, factors: list[dict]) -> dict:
        action = _loads(row["action_json"], {})
        risk_state = _loads(row["risk_state_json"], {})
        portfolio_state = _loads(row["portfolio_state_json"], {})
        tags_breakdown = action.get("tags_breakdown") if isinstance(action, dict) else {}
        temporal_context = _derive_temporal_context(
            _safe_float(row["decision_ts"]),
            str(row["timeframe"] or ""),
            risk_state,
        )
        return {
            "decision_id": str(row["decision_id"]),
            "event_type": str(row["event_type"] or ""),
            "symbol": str(row["symbol"] or ""),
            "timeframe": str(row["timeframe"] or ""),
            "decision_ts": _safe_float(row["decision_ts"]),
            "regime_id": str(row["regime_id"] or ""),
            "regime_confidence": _safe_float(row["regime_confidence"]),
            "policy_version": str(row["policy_version"] or ""),
            "factor_set_version": str(row["factor_set_version"] or ""),
            "action_score": _safe_float(row["action_score"]),
            "action_reason": str(row["action_reason"] or ""),
            "action": action,
            "risk_state": risk_state,
            "portfolio_state": portfolio_state,
            "factor_count": len(factors),
            "active_factor_count": sum(1 for f in factors if not f["gated"]),
            "top_factors": factors[:10],
            "factor_evidence": factors,
            "factor_tags": tags_breakdown or {},
            "temporal_context": temporal_context,
        }

    def build_decision_features(self, decision_id: str) -> dict:
        with self._conn() as conn:
            p = self._p()
            row = conn.execute(
                f"SELECT * FROM decision_ledger WHERE decision_id={p}",
                (decision_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"decision not found: {decision_id}")

        factors = self._factor_snapshots(decision_id)
        return self._decision_features_from_row(row, factors)

    @staticmethod
    def _decision_quality(decision: dict) -> dict:
        action = decision.get("action") if isinstance(decision.get("action"), dict) else {}
        checks = {
            "has_factor_snapshot": bool(decision.get("factor_evidence")),
            "has_action_payload": bool(action),
            "has_action_reason": bool(decision.get("action_reason")),
            "has_symbol": bool(decision.get("symbol")),
            "has_timeframe": bool(decision.get("timeframe")),
        }
        weights = {
            "has_factor_snapshot": 0.35,
            "has_action_payload": 0.25,
            "has_action_reason": 0.20,
            "has_symbol": 0.10,
            "has_timeframe": 0.10,
        }
        score = sum(weights[k] for k, ok in checks.items() if ok)
        return {
            "quality_score": round(score, 4),
            "model_ready": score >= 0.70 and checks["has_factor_snapshot"] and checks["has_action_reason"],
            "checks": checks,
            "missing": [k for k, ok in checks.items() if not ok],
        }

    @staticmethod
    def _decision_integrity(decision: dict, quality: dict) -> str:
        if quality.get("model_ready"):
            return "full"
        missing = set(quality.get("missing") or [])
        if {"has_factor_snapshot", "has_action_reason"} & missing:
            return "partial"
        return "recovered" if quality.get("quality_score", 0.0) >= 0.7 else "missing"

    def _decision_sample_from_features(self, decision: dict, execution_trace: dict | None = None) -> dict:
        decision_id = str(decision.get("decision_id") or "")
        action = decision.get("action") if isinstance(decision.get("action"), dict) else {}
        event_type = str(decision.get("event_type") or "")
        gate_passed = bool(action.get("gate_passed", False))
        target = {
            "event_type": event_type,
            "executed": event_type == "open",
            "skipped": event_type in {"skip", "hold", "order_failed", "amend_failed"} or (event_type == "signal" and not gate_passed),
            "failed_execution": event_type in {"order_failed", "amend_failed"},
            "gate_passed": gate_passed,
            "gate_reason": str(action.get("gate_reason") or decision.get("action_reason") or ""),
            "skip_stage": str(action.get("skip_stage") or ""),
            "direction": int(action.get("direction") or 0),
            "action_score": _safe_float(decision.get("action_score")),
        }
        factors = list(decision.get("factor_evidence") or [])
        if execution_trace is None:
            execution_trace = self._execution_trace(
                decision_ids=[decision_id],
                trade_id=str(action.get("position_id") or ""),
                position_id=str(action.get("position_id") or ""),
            )
        llm_context = self._decision_llm_context(
            decision=decision,
            target=target,
            factors=factors,
            execution_trace=execution_trace,
        )
        quality = self._decision_quality(decision)
        sample_id = f"decision:{decision_id}"
        explainability = {
            "summary_text": (
                f"{event_type} decision score={target['action_score']:.4f}; "
                f"reason={target['gate_reason'] or decision.get('action_reason') or 'n/a'}"
            ),
            "top_factors": factors[:5],
            "factor_count": len(factors),
            "execution_summary": execution_trace["summary"],
            "evidence_bullets": llm_context["evidence_bullets"],
            "ledger_links": {
                "decision_id": decision_id,
                "trade_id": str(action.get("position_id") or ""),
            },
        }
        contract = build_evidence_contract(
            sample_id=sample_id,
            sample_kind="decision",
            source={
                "table": "decision_ledger",
                "source_id": decision_id,
                "decision_id": decision_id,
                "event_type": event_type,
            },
            features={"decision": decision, "execution_trace": execution_trace},
            label=target,
            trace={
                "decision_id": decision_id,
                "ledger_links": explainability["ledger_links"],
                "execution_summary": execution_trace["summary"],
            },
            quality=quality,
            integrity=self._decision_integrity(decision, quality),
            causal_level="intervention_observed",
            label_status="matured",
            explanation=explainability,
        )
        quality = {**quality, "model_ready": bool(contract["model_ready"]), "evidence_contract": contract}
        return {
            "schema_version": DECISION_SCHEMA_VERSION,
            "sample_id": sample_id,
            "evidence_contract": contract,
            "quality": quality,
            "target": target,
            "decision": decision,
            "execution_trace": execution_trace,
            "llm_context": llm_context,
            "explainability": explainability,
        }

    def build_decision_sample(self, decision_id: str) -> dict:
        decision = self.build_decision_features(decision_id)
        return self._decision_sample_from_features(decision)

    def _experience_for_trade(self, trade_id: str) -> dict | None:
        if not trade_id:
            return None
        with self._conn() as conn:
            p = self._p()
            raw_rows = conn.execute(
                f"""
                SELECT e.*, r.review_json AS source_review_json{_review_archive_select(conn, output="source_review_archive_hash")}
                FROM experience_memory e
                JOIN trade_outcome_review r
                  ON e.source_table='trade_outcome_review'
                 AND r.review_id=e.source_id
                WHERE e.append_source='trade_lesson_memory.v1' AND e.trade_id={p}
                ORDER BY e.created_at DESC
                """,
                (trade_id,),
            ).fetchall()
            rows = []
            for raw_row in raw_rows:
                item = dict(raw_row)
                _restore_review_payload(
                    conn,
                    item,
                    source_id_key="source_id",
                    inline_key="source_review_json",
                    archive_key="source_review_archive_hash",
                )
                rows.append(item)
        for item in rows:
            if not review_has_system_contamination(item.get("source_review_json")):
                return self._parse_experience(item)
        return None

    def _application_context(self, factors: list[dict], review_created_at: float) -> list[dict]:
        names = [f["factor"] for f in factors if f.get("factor")]
        if not names:
            return []
        p = self._p()
        placeholders = ",".join(p for _ in names)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT l.application_id, l.scope_type, l.scope_key, l.action,
                       l.bias_multiplier, l.old_weight, l.new_weight, l.status,
                       l.cycle_ts, e.status AS effect_status,
                       e.observed_trade_count, e.baseline_trade_count,
                       e.delta_avg_reward, e.post_win_rate, e.baseline_win_rate,
                       e.decision_json
                FROM learning_application_log l
                LEFT JOIN learning_application_effect e
                  ON e.application_id = l.application_id
                WHERE l.scope_type='factor'
                  AND l.scope_key IN ({placeholders})
                  AND l.cycle_ts <= {p}
                ORDER BY l.cycle_ts DESC
                LIMIT 20
                """,
                (*names, review_created_at),
            ).fetchall()
        return [
            {
                "application_id": str(row["application_id"] or ""),
                "scope_type": str(row["scope_type"] or ""),
                "scope_key": str(row["scope_key"] or ""),
                "action": str(row["action"] or ""),
                "bias_multiplier": _safe_float(row["bias_multiplier"], 1.0),
                "old_weight": _safe_float(row["old_weight"]),
                "new_weight": _safe_float(row["new_weight"]),
                "status": str(row["status"] or ""),
                "effect_status": str(row["effect_status"] or ""),
                "observed_trade_count": int(row["observed_trade_count"] or 0),
                "baseline_trade_count": int(row["baseline_trade_count"] or 0),
                "delta_avg_reward": _safe_float(row["delta_avg_reward"]),
                "post_win_rate": _safe_float(row["post_win_rate"]),
                "baseline_win_rate": _safe_float(row["baseline_win_rate"]),
                "decision": _loads(row["decision_json"], {}),
            }
            for row in rows
        ]

    @staticmethod
    def _parse_experience(row: sqlite3.Row) -> dict:
        context = _loads(row["decision_context_json"], {})
        failure_tags = _loads(row["failure_tags_json"], [])
        return {
            "experience_id": str(row["experience_id"] or ""),
            "trade_id": str(row["trade_id"] or ""),
            "regime_id": str(row["regime_id"] or ""),
            "setup_hash": str(row["setup_hash"] or ""),
            "decision_context": context,
            "primary_responsibility": str(context.get("primary_responsibility") or ""),
            "responsibility_labels": list(context.get("responsibility_labels") or []),
            "outcome_label": str(row["outcome_label"] or ""),
            "reward_score": _safe_float(row["reward_score"]),
            "failure_tags": failure_tags if isinstance(failure_tags, list) else [],
            "recommended_action": str(row["recommended_action"] or ""),
            "evidence_strength": _safe_float(row["evidence_strength"]),
            "artifact_version": str(row["artifact_version"] or "v1"),
            "created_at": _safe_float(row["created_at"]),
        }

    @staticmethod
    def _quality(
        *,
        review: dict,
        decision: dict | None,
        factors: list[dict],
        contribution_reviews: list[dict],
        experience: dict | None,
    ) -> dict:
        review_json = review.get("review") or {}
        real_pnl = review_json.get("real_pnl") or {}
        has_real_pnl = isinstance(real_pnl, dict) and real_pnl.get("net") is not None
        context_integrity = str(review_json.get("context_integrity", "full") or "full")
        has_label = bool(review.get("outcome_label"))
        checks = {
            "has_real_pnl": has_real_pnl,
            "full_context": context_integrity == "full",
            "has_entry_decision": bool(decision),
            "has_factor_snapshot": bool(factors),
            "has_factor_contribution_review": bool(contribution_reviews),
            "has_outcome_label": has_label,
            "has_experience": bool(experience),
        }
        weights = {
            "has_real_pnl": 0.22,
            "full_context": 0.16,
            "has_entry_decision": 0.16,
            "has_factor_snapshot": 0.16,
            "has_factor_contribution_review": 0.12,
            "has_outcome_label": 0.10,
            "has_experience": 0.08,
        }
        score = sum(weights[k] for k, ok in checks.items() if ok)
        required = (
            "has_real_pnl",
            "full_context",
            "has_entry_decision",
            "has_factor_snapshot",
            "has_factor_contribution_review",
            "has_outcome_label",
            "has_experience",
        )
        return {
            "quality_score": round(score, 4),
            "model_ready": score >= 0.80 and all(checks[k] for k in required),
            "checks": checks,
            "missing": [k for k, ok in checks.items() if not ok],
        }

    @staticmethod
    def _trade_integrity(
        *,
        review: dict,
        decision: dict | None,
        factors: list[dict],
        contribution_reviews: list[dict],
        experience: dict | None,
    ) -> str:
        review_json = review.get("review") or {}
        explicit = str(review_json.get("attribution_integrity") or "").strip()
        if explicit in {"full", "recovered", "partial", "missing"}:
            return explicit
        context_integrity = str(review_json.get("context_integrity") or "").strip()
        if context_integrity in {"partial", "missing"}:
            return context_integrity
        if decision and factors and contribution_reviews and experience:
            return "recovered"
        if decision or factors or contribution_reviews:
            return "partial"
        return "missing"

    @staticmethod
    def _trade_causal_level(*, review: dict, execution_trace: dict, application_context: list[dict]) -> str:
        review_json = review.get("review") or {}
        if application_context:
            return "intervention_observed"
        if review.get("trade_id") and review.get("outcome_label"):
            return "intervention_observed"
        if review_json.get("counterfactual") or review_json.get("counterfactual_summary"):
            return "counterfactual"
        summary = execution_trace.get("summary") or {}
        if summary.get("has_broker_lifecycle"):
            return "replay_validated"
        return "observational"

    def build_trade_features(self, trade_id: str) -> dict:
        with self._conn() as conn:
            p = self._p()
            row = conn.execute(
                f"""
                SELECT *
                FROM trade_outcome_review
                WHERE trade_id={p} OR position_id={p} OR review_id={p}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (trade_id, trade_id, trade_id),
            ).fetchone()
            if row:
                row = dict(row)
                _restore_review_payload(conn, row)
        if not row:
            raise KeyError(f"trade review not found: {trade_id}")
        return self._sample_from_review_row(row)

    def build_experience_features(self, experience_id: str) -> dict:
        with self._conn() as conn:
            p = self._p()
            row = conn.execute(
                f"SELECT * FROM experience_memory WHERE experience_id={p}",
                (experience_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"experience not found: {experience_id}")
        exp = self._parse_experience(row)
        trade_id = exp.get("trade_id") or ""
        sample = self.build_trade_features(trade_id)
        sample["experience"] = exp
        return sample

    def _sample_from_review_row(
        self,
        row: sqlite3.Row,
        *,
        decision_by_id: dict[str, dict] | None = None,
        factors_by_decision: dict[str, list[dict]] | None = None,
        contribution_by_review: dict[str, list[dict]] | None = None,
        experience_by_trade: dict[str, dict] | None = None,
        application_rows: list[dict] | None = None,
        order_events: list[dict] | None = None,
        position_events: list[dict] | None = None,
    ) -> dict:
        failure_tags = _loads(row["failure_tags_json"], [])
        review_json = _loads(row["review_json"], {})
        review = {
            "review_id": str(row["review_id"] or ""),
            "trade_id": str(row["trade_id"] or ""),
            "position_id": str(row["position_id"] or ""),
            "entry_decision_id": str(row["entry_decision_id"] or ""),
            "exit_decision_id": str(row["exit_decision_id"] or ""),
            "entry_quality": _safe_float(row["entry_quality"]),
            "hold_quality": _safe_float(row["hold_quality"]),
            "exit_quality": _safe_float(row["exit_quality"]),
            "regime_fit_score": _safe_float(row["regime_fit_score"]),
            "execution_quality": _safe_float(row["execution_quality"]),
            "pnl": _safe_float(row["pnl"]),
            "mae": _safe_float(row["mae"]),
            "mfe": _safe_float(row["mfe"]),
            "outcome_label": str(row["outcome_label"] or ""),
            "failure_tags": failure_tags if isinstance(failure_tags, list) else [],
            "summary_text": str(row["summary_text"] or ""),
            "review": review_json if isinstance(review_json, dict) else {},
            "created_at": _safe_float(row["created_at"]),
        }
        review["primary_responsibility"] = str((review["review"] or {}).get("primary_responsibility") or "")
        review["responsibility_labels"] = list((review["review"] or {}).get("responsibility_labels") or [])
        decision = None
        factors: list[dict] = []
        if review["entry_decision_id"]:
            decision = (decision_by_id or {}).get(review["entry_decision_id"])
            if decision:
                factors = decision["factor_evidence"]
            else:
                try:
                    decision = self.build_decision_features(review["entry_decision_id"])
                    factors = decision["factor_evidence"]
                except KeyError:
                    decision = None
                    factors = (factors_by_decision or {}).get(review["entry_decision_id"])
                    if factors is None:
                        factors = self._factor_snapshots(review["entry_decision_id"])
        contribution_reviews = (contribution_by_review or {}).get(review["review_id"])
        if contribution_reviews is None:
            contribution_reviews = self._factor_contribution_reviews(review["review_id"])
        factor_outcomes = self._align_factor_outcomes(factors, contribution_reviews)
        attribution_alignment = self._attribution_alignment(factor_outcomes)
        experience = (experience_by_trade or {}).get(review["trade_id"])
        if experience is None:
            experience = self._experience_for_trade(review["trade_id"])
        if application_rows is not None:
            application_context = self._application_context_from_prefetch(factors, review["created_at"], application_rows)
        else:
            application_context = self._application_context(factors, review["created_at"])
        if order_events is not None or position_events is not None:
            decision_ids = {review["entry_decision_id"], review["exit_decision_id"]}
            sample_orders = [
                item for item in (order_events or [])
                if str(item.get("decision_id") or "") in decision_ids
                or (review["trade_id"] and str(item.get("trade_id") or "") == review["trade_id"])
            ]
            sample_positions = [
                item for item in (position_events or [])
                if (review["position_id"] and str(item.get("position_id") or "") == review["position_id"])
                or (review["trade_id"] and str(item.get("trade_id") or "") == review["trade_id"])
            ]
            execution_trace = self._execution_trace_from_events(sample_orders, sample_positions)
        else:
            execution_trace = self._execution_trace(
                decision_ids=[review["entry_decision_id"], review["exit_decision_id"]],
                trade_id=review["trade_id"],
                position_id=review["position_id"],
            )
        quality = self._quality(
            review=review,
            decision=decision,
            factors=factors,
            contribution_reviews=contribution_reviews,
            experience=experience,
        )
        target = {
            "outcome_label": review["outcome_label"],
            "reward_score": experience["reward_score"] if experience else None,
            "pnl": review["pnl"],
            "failure_tags": review["failure_tags"],
            "recommended_action": experience["recommended_action"] if experience else "",
            "primary_responsibility": review["primary_responsibility"],
            "responsibility_labels": review["responsibility_labels"],
        }
        llm_context = self._trade_llm_context(
            review=review,
            target=target,
            factor_outcomes=factor_outcomes,
            attribution_alignment=attribution_alignment,
            execution_trace=execution_trace,
            application_context=application_context,
        )
        sample_id = f"trade:{review['trade_id'] or review['review_id']}"
        explainability = {
            "summary_text": review["summary_text"],
            "top_factors": factor_outcomes[:5],
            "factor_count": len(factors),
            "attribution_alignment": attribution_alignment,
            "execution_summary": execution_trace["summary"],
            "evidence_bullets": llm_context["evidence_bullets"],
            "failure_tags": review["failure_tags"],
            "primary_responsibility": review["primary_responsibility"],
            "responsibility_labels": review["responsibility_labels"],
            "ledger_links": {
                "entry_decision_id": review["entry_decision_id"],
                "exit_decision_id": review["exit_decision_id"],
                "trade_id": review["trade_id"],
                "position_id": review["position_id"],
            },
        }
        integrity = self._trade_integrity(
            review=review,
            decision=decision,
            factors=factors,
            contribution_reviews=contribution_reviews,
            experience=experience,
        )
        contract = build_evidence_contract(
            sample_id=sample_id,
            sample_kind="trade",
            source={
                "table": "trade_outcome_review",
                "source_id": review["review_id"],
                "review_id": review["review_id"],
                "trade_id": review["trade_id"],
                "position_id": review["position_id"],
                "entry_decision_id": review["entry_decision_id"],
                "exit_decision_id": review["exit_decision_id"],
                "integrity_basis": "explicit_review" if (review["review"] or {}).get("attribution_integrity") else "derived_from_ledger",
                "close_reason_source": str((review["review"] or {}).get("close_reason_source") or ""),
            },
            features={
                "decision": decision,
                "factor_outcomes": factor_outcomes,
                "attribution_alignment": attribution_alignment,
                "application_context": application_context,
            },
            label=target,
            trace={
                "ledger_links": explainability["ledger_links"],
                "execution_summary": execution_trace["summary"],
                "application_count": len(application_context),
            },
            quality=quality,
            integrity=integrity,
            causal_level=self._trade_causal_level(
                review=review,
                execution_trace=execution_trace,
                application_context=application_context,
            ),
            label_status="matured",
            explanation=explainability,
        )
        quality = {**quality, "model_ready": bool(contract["model_ready"]), "evidence_contract": contract}
        return {
            "schema_version": SCHEMA_VERSION,
            "sample_id": sample_id,
            "evidence_contract": contract,
            "quality": quality,
            "target": target,
            "decision": decision,
            "factor_outcomes": factor_outcomes,
            "attribution_alignment": attribution_alignment,
            "review": review,
            "experience": experience,
            "execution_trace": execution_trace,
            "application_context": application_context,
            "llm_context": llm_context,
            "explainability": explainability,
        }

    def build_training_samples(
        self,
        *,
        limit: int = 200,
        model_ready_only: bool = False,
    ) -> list[dict]:
        with self._conn() as conn:
            p = self._p()
            rows = conn.execute(
                f"""
                SELECT *
                FROM trade_outcome_review
                ORDER BY created_at DESC
                LIMIT {p}
                """,
                (int(limit),),
            ).fetchall()
            rows = [dict(row) for row in rows]
            for row in rows:
                _restore_review_payload(conn, row)
        entry_decision_ids = [str(row["entry_decision_id"] or "") for row in rows if str(row["entry_decision_id"] or "")]
        exit_decision_ids = [str(row["exit_decision_id"] or "") for row in rows if str(row["exit_decision_id"] or "")]
        all_decision_ids = list(dict.fromkeys(entry_decision_ids + exit_decision_ids))
        trade_ids = [str(row["trade_id"] or "") for row in rows if str(row["trade_id"] or "")]
        position_ids = [str(row["position_id"] or "") for row in rows if str(row["position_id"] or "")]
        review_ids = [str(row["review_id"] or "") for row in rows if str(row["review_id"] or "")]
        factors_by_decision = self._factor_snapshots_by_decision(entry_decision_ids)
        decision_rows = self._decision_rows_by_id(entry_decision_ids)
        decision_by_id = {
            decision_id: self._decision_features_from_row(row, factors_by_decision.get(decision_id, []))
            for decision_id, row in decision_rows.items()
        }
        contribution_by_review = self._factor_contribution_reviews_by_review(review_ids)
        experience_by_trade = self._experiences_by_trade(trade_ids)
        order_events = self._order_events_for_decisions(decision_ids=all_decision_ids, trade_ids=trade_ids)
        position_events = self._position_events_for_positions(position_ids=position_ids, trade_ids=trade_ids)
        factor_names = list(
            dict.fromkeys(
                str(item.get("factor") or "")
                for factors in factors_by_decision.values()
                for item in factors
                if str(item.get("factor") or "")
            )
        )
        max_created_at = max((_safe_float(row["created_at"]) for row in rows), default=0.0)
        application_rows = self._application_context_rows_for_factors(factor_names, max_created_at)
        samples = [
            self._sample_from_review_row(
                row,
                decision_by_id=decision_by_id,
                factors_by_decision=factors_by_decision,
                contribution_by_review=contribution_by_review,
                experience_by_trade=experience_by_trade,
                application_rows=application_rows,
                order_events=order_events,
                position_events=position_events,
            )
            for row in rows
        ]
        if model_ready_only:
            samples = [s for s in samples if s["quality"]["model_ready"]]
        return samples

    def factor_evidence_summary(
        self,
        factor_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Summarize persisted factor evidence using the learning contract.

        This is intentionally a warm, read-only projection.  Matured,
        uncontaminated, governance-eligible counts come from the same
        ``autonomous_learning_sample`` eligibility fields populated by this
        provider's evidence-contract pipeline; decision snapshots remain
        observations and are never promoted to mature-trade evidence.
        """
        ids = list(dict.fromkeys(str(item) for item in factor_ids if str(item)))
        if not ids:
            return {}
        result = {
            factor_id: {
                "decision_observations": 0,
                "factor_linked_trade_reviews": 0,
                "governance_eligible_mature": 0,
                "contaminated_or_ineligible": 0,
                "effects_observed": 0,
                "status": "available",
            }
            for factor_id in ids
        }
        try:
            with self._conn() as conn:
                p = self._p()
                placeholders = ",".join(p for _ in ids)
                rows = conn.execute(
                    f"""
                    SELECT factor, COUNT(*) AS n
                    FROM decision_factor_snapshot
                    WHERE factor IN ({placeholders})
                    GROUP BY factor
                    """,
                    tuple(ids),
                ).fetchall()
                for row in rows:
                    factor_id = str(row["factor"] or "")
                    if factor_id in result:
                        result[factor_id]["decision_observations"] = int(row["n"] or 0)

                rows = conn.execute(
                    f"""
                    SELECT f.factor, COUNT(DISTINCT f.review_id) AS n
                    FROM factor_contribution_review f
                    JOIN trade_outcome_review r ON r.review_id=f.review_id
                    WHERE f.factor IN ({placeholders})
                    GROUP BY f.factor
                    """,
                    tuple(ids),
                ).fetchall()
                for row in rows:
                    factor_id = str(row["factor"] or "")
                    if factor_id in result:
                        result[factor_id]["factor_linked_trade_reviews"] = int(row["n"] or 0)

                rows = conn.execute(
                    f"""
                    SELECT factor,
                           COUNT(DISTINCT CASE
                               WHEN label_status='matured'
                                AND COALESCE(governance_eligible, 0)=1
                                AND COALESCE(system_contaminated, 0)=0
                               THEN sample_id END) AS mature_n,
                           COUNT(DISTINCT CASE
                               WHEN COALESCE(governance_eligible, 0)=0
                                 OR COALESCE(system_contaminated, 0)=1
                               THEN sample_id END) AS ineligible_n
                    FROM (
                        SELECT s.sample_id, s.label_status,
                               s.governance_eligible, s.system_contaminated,
                               d.factor AS factor
                        FROM autonomous_learning_sample s
                        JOIN decision_factor_snapshot d
                          ON d.decision_id=s.decision_id
                        WHERE d.factor IN ({placeholders})
                        UNION
                        SELECT s.sample_id, s.label_status,
                               s.governance_eligible, s.system_contaminated,
                               f.factor AS factor
                        FROM autonomous_learning_sample s
                        JOIN trade_outcome_review r
                          ON r.review_id=s.source_id
                         AND s.source_table='trade_outcome_review'
                        JOIN factor_contribution_review f
                          ON f.review_id=r.review_id
                        WHERE f.factor IN ({placeholders})
                    ) linked
                    GROUP BY factor
                    """,
                    tuple(ids) + tuple(ids),
                ).fetchall()
                for row in rows:
                    factor_id = str(row["factor"] or "")
                    if factor_id in result:
                        result[factor_id]["governance_eligible_mature"] = int(row["mature_n"] or 0)
                        result[factor_id]["contaminated_or_ineligible"] = int(row["ineligible_n"] or 0)

                rows = conn.execute(
                    f"""
                    SELECT scope_key AS factor, COUNT(*) AS n
                    FROM learning_application_effect
                    WHERE scope_type='factor' AND scope_key IN ({placeholders})
                    GROUP BY scope_key
                    """,
                    tuple(ids),
                ).fetchall()
                for row in rows:
                    factor_id = str(row["factor"] or "")
                    if factor_id in result:
                        result[factor_id]["effects_observed"] = int(row["n"] or 0)
        except Exception:
            for item in result.values():
                item["decision_observations"] = None
                item["factor_linked_trade_reviews"] = None
                item["governance_eligible_mature"] = None
                item["contaminated_or_ineligible"] = None
                item["effects_observed"] = None
                item["status"] = "unavailable"
        return result

    def build_decision_samples(
        self,
        *,
        limit: int = 200,
        event_types: list[str] | None = None,
        model_ready_only: bool = False,
    ) -> list[dict]:
        params: list[Any] = []
        where = ""
        if event_types:
            clean = [str(item) for item in event_types if str(item)]
            if clean:
                placeholders = ",".join(self._p() for _ in clean)
                where = f"WHERE event_type IN ({placeholders})"
                params.extend(clean)
        params.append(int(limit))
        with self._conn() as conn:
            p = self._p()
            rows = conn.execute(
                f"""
                SELECT *
                FROM decision_ledger
                {where}
                ORDER BY decision_ts DESC, created_at DESC
                LIMIT {p}
                """,
                tuple(params),
            ).fetchall()
        decision_ids = [str(row["decision_id"] or "") for row in rows if str(row["decision_id"] or "")]
        factors_by_decision = self._factor_snapshots_by_decision(decision_ids)
        decisions = [
            self._decision_features_from_row(row, factors_by_decision.get(str(row["decision_id"] or ""), []))
            for row in rows
        ]
        position_ids = [
            str((decision.get("action") or {}).get("position_id") or "")
            for decision in decisions
            if isinstance(decision.get("action"), dict) and str((decision.get("action") or {}).get("position_id") or "")
        ]
        order_events = self._order_events_for_decisions(decision_ids=decision_ids, trade_ids=position_ids)
        position_events = self._position_events_for_positions(position_ids=position_ids, trade_ids=position_ids)
        orders_by_decision: dict[str, list[tuple[int, dict]]] = {}
        orders_by_trade: dict[str, list[tuple[int, dict]]] = {}
        for index, item in enumerate(order_events):
            decision_id = str(item.get("decision_id") or "")
            trade_id = str(item.get("trade_id") or "")
            if decision_id:
                orders_by_decision.setdefault(decision_id, []).append((index, item))
            if trade_id:
                orders_by_trade.setdefault(trade_id, []).append((index, item))
        positions_by_position: dict[str, list[tuple[int, dict]]] = {}
        positions_by_trade: dict[str, list[tuple[int, dict]]] = {}
        for index, item in enumerate(position_events):
            position_id = str(item.get("position_id") or "")
            trade_id = str(item.get("trade_id") or "")
            if position_id:
                positions_by_position.setdefault(position_id, []).append((index, item))
            if trade_id:
                positions_by_trade.setdefault(trade_id, []).append((index, item))
        samples = []
        for decision in decisions:
            decision_id = str(decision.get("decision_id") or "")
            action = decision.get("action") if isinstance(decision.get("action"), dict) else {}
            position_id = str(action.get("position_id") or "")
            indexed_orders = {
                index: item
                for index, item in (
                    orders_by_decision.get(decision_id, [])
                    + (orders_by_trade.get(position_id, []) if position_id else [])
                )
            }
            sample_orders = [indexed_orders[index] for index in sorted(indexed_orders)]
            indexed_positions = {
                index: item
                for index, item in (
                    positions_by_position.get(position_id, [])
                    + positions_by_trade.get(position_id, [])
                )
            } if position_id else {}
            sample_positions = [indexed_positions[index] for index in sorted(indexed_positions)]
            samples.append(
                self._decision_sample_from_features(
                    decision,
                    execution_trace=self._execution_trace_from_events(sample_orders, sample_positions),
                )
            )
        if model_ready_only:
            samples = [s for s in samples if s["quality"]["model_ready"]]
        return samples
