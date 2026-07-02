from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, STATE_DB_DDL, connect_sqlite, ensure_sqlite_columns, get_state_pg_conn, is_state_db_path

logger = logging.getLogger(__name__)


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


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


def _classify_trading_session(hour_utc: int) -> str:
    if 0 <= hour_utc < 7:
        return "asia"
    if 7 <= hour_utc < 13:
        return "europe"
    if 13 <= hour_utc < 21:
        return "us"
    return "rollover"


def _normalize_temporal_context(existing: Any, *, decision_ts: float, timeframe: str) -> dict:
    base = dict(existing or {}) if isinstance(existing, dict) else {}
    original_ts = _safe_float(base.get("decision_ts"), 0.0)
    ts = _safe_float(decision_ts, 0.0)
    if ts <= 0:
        return base
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return base

    drift = original_ts - ts if original_ts > 0 else 0.0
    if original_ts > 0 and abs(drift) > 1e-6:
        base.setdefault("runtime_decision_ts", original_ts)
        base["market_runtime_drift_seconds"] = round(drift, 6)
        if "seconds_since_last_trade" in base:
            adjusted = _safe_float(base.get("seconds_since_last_trade"), 0.0) - drift
            base["seconds_since_last_trade"] = round(max(0.0, adjusted), 3)
        tf_for_adjust = _timeframe_seconds(str(base.get("timeframe") or timeframe))
        if tf_for_adjust > 0 and "seconds_since_last_trade" in base:
            base["bars_since_last_trade"] = round(
                _safe_float(base.get("seconds_since_last_trade"), 0.0) / tf_for_adjust,
                3,
            )

    tf = str(base.get("timeframe") or timeframe or "")
    tf_seconds = _timeframe_seconds(tf)
    base.update(
        {
            "decision_ts": ts,
            "time_basis": "market_epoch_seconds_utc",
            "timeframe": tf,
            "timeframe_seconds": tf_seconds,
            "hour_utc": int(dt.hour),
            "minute_utc": int(dt.minute),
            "weekday_utc": int(dt.weekday()),
            "session_label": _classify_trading_session(int(dt.hour)),
            "is_weekend_utc": bool(dt.weekday() >= 5),
        }
    )
    return base


def _normalize_audit_payload_temporal(payload: Any, *, decision_ts: float, timeframe: str) -> None:
    if not isinstance(payload, dict):
        return
    if isinstance(payload.get("temporal_context"), dict):
        payload["temporal_context"] = _normalize_temporal_context(
            payload.get("temporal_context"),
            decision_ts=decision_ts,
            timeframe=timeframe,
        )
    state = payload.get("state")
    if isinstance(state, dict) and isinstance(state.get("temporal_context"), dict):
        state["temporal_context"] = _normalize_temporal_context(
            state.get("temporal_context"),
            decision_ts=decision_ts,
            timeframe=timeframe,
        )


def _normalize_decision_time_payloads(
    *,
    risk_state: dict | None,
    action_json: dict | None,
    decision_ts: float,
    timeframe: str,
) -> tuple[dict | None, dict | None]:
    """Ensure nested audit contexts use the same market decision time as the ledger row."""
    normalized_risk = deepcopy(risk_state) if isinstance(risk_state, dict) else risk_state
    normalized_action = deepcopy(action_json) if isinstance(action_json, dict) else action_json
    for container in (normalized_risk, normalized_action):
        if not isinstance(container, dict):
            continue
        for key in ("policy_verdict", "risk_verdict"):
            verdict = container.get(key)
            if isinstance(verdict, dict):
                _normalize_audit_payload_temporal(
                    verdict.get("audit_payload"),
                    decision_ts=decision_ts,
                    timeframe=timeframe,
                )
        if isinstance(container.get("temporal_context"), dict):
            container["temporal_context"] = _normalize_temporal_context(
                container.get("temporal_context"),
                decision_ts=decision_ts,
                timeframe=timeframe,
            )
    return normalized_risk, normalized_action


class DecisionLedger:
    """Structured decision and lifecycle ledger."""

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or str(STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._use_pg() else sql

    def _execute(self, conn, sql: str, params: tuple | list | None = None):
        if params is None:
            return conn.execute(self._sql(sql))
        return conn.execute(self._sql(sql), tuple(params))

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
                "position_supervisor_trace",
                {
                    "trace_integrity": "trace_integrity TEXT DEFAULT 'full'",
                    "config_version": "config_version INTEGER DEFAULT 0",
                    "config_hash": "config_hash TEXT DEFAULT ''",
                    "evolution_run_id": "evolution_run_id TEXT DEFAULT ''",
                },
            )

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

    def log_decision(
        self,
        *,
        event_type: str,
        symbol: str = "",
        timeframe: str = "",
        decision_ts: float | None = None,
        regime_id: str = "",
        regime_confidence: float = 0.0,
        trade_id: str = "",
        position_id: str = "",
        portfolio_state: dict | None = None,
        risk_state: dict | None = None,
        policy_version: str = "",
        factor_set_version: str = "",
        action_score: float = 0.0,
        action_reason: str = "",
        action_json: dict | None = None,
        factor_snapshots: list[dict] | None = None,
    ) -> str:
        now = time.time()
        decision_id = self.new_id("dec")
        normalized_decision_ts = float(decision_ts or now)
        normalized_risk_state, normalized_action_json = _normalize_decision_time_payloads(
            risk_state=risk_state,
            action_json=action_json,
            decision_ts=normalized_decision_ts,
            timeframe=str(timeframe or ""),
        )
        decision_payload = {
            "decision_id": decision_id,
            "trade_id": str(trade_id or ""),
            "position_id": str(position_id or ""),
            "event_type": str(event_type),
            "symbol": str(symbol or ""),
            "timeframe": str(timeframe or ""),
            "decision_ts": normalized_decision_ts,
            "regime_id": str(regime_id or ""),
            "regime_confidence": float(regime_confidence or 0.0),
            "portfolio_state_json": _json_dumps(portfolio_state),
            "risk_state_json": _json_dumps(normalized_risk_state),
            "policy_version": str(policy_version or ""),
            "factor_set_version": str(factor_set_version or ""),
            "action_score": float(action_score or 0.0),
            "action_reason": str(action_reason or ""),
            "action_json": _json_dumps(normalized_action_json),
            "created_at": now,
        }
        factor_payloads: list[dict[str, Any]] = []
        for row in factor_snapshots or []:
            factor_payloads.append(
                {
                    "decision_id": decision_id,
                    "factor": str(row.get("factor", "")),
                    "source": str(row.get("source", "registry")),
                    "raw_value": float(row.get("raw_value", 0.0) or 0.0),
                    "normalized_value": float(row.get("normalized_value", 0.0) or 0.0),
                    "direction": float(row.get("direction", 0.0) or 0.0),
                    "base_weight": float(row.get("base_weight", 0.0) or 0.0),
                    "policy_weight": float(row.get("policy_weight", 0.0) or 0.0),
                    "shadow_score": float(row.get("shadow_score", 0.0) or 0.0),
                    "health_score": float(row.get("health_score", 0.0) or 0.0),
                    "gated": int(1 if row.get("gated") else 0),
                    "gated_reason": str(row.get("gated_reason", "")),
                    "contribution_score": float(row.get("contribution_score", 0.0) or 0.0),
                }
            )
        with self._conn() as conn:
            self._execute(conn,
                """
                INSERT INTO decision_ledger
                (decision_id, trade_id, position_id, event_type, symbol, timeframe,
                 decision_ts, regime_id, regime_confidence, portfolio_state_json,
                 risk_state_json, policy_version, factor_set_version, action_score,
                 action_reason, action_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(decision_payload[k] for k in (
                    "decision_id",
                    "trade_id",
                    "position_id",
                    "event_type",
                    "symbol",
                    "timeframe",
                    "decision_ts",
                    "regime_id",
                    "regime_confidence",
                    "portfolio_state_json",
                    "risk_state_json",
                    "policy_version",
                    "factor_set_version",
                    "action_score",
                    "action_reason",
                    "action_json",
                    "created_at",
                )),
            )
            for row in factor_payloads:
                self._execute(conn,
                    """
                    INSERT INTO decision_factor_snapshot
                    (decision_id, factor, source, raw_value, normalized_value, direction,
                     base_weight, policy_weight, shadow_score, health_score, gated,
                     gated_reason, contribution_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        row["factor"],
                        row["source"],
                        row["raw_value"],
                        row["normalized_value"],
                        row["direction"],
                        row["base_weight"],
                        row["policy_weight"],
                        row["shadow_score"],
                        row["health_score"],
                        row["gated"],
                        row["gated_reason"],
                        row["contribution_score"],
                    ),
                )
        return decision_id

    def log_composite_decision(
        self,
        *,
        event_type: str,
        composite: Any,
        gate_result: Any | None = None,
        symbol: str = "XAUUSD+",
        timeframe: str = "",
        decision_ts: float | None = None,
        trade_id: str = "",
        position_id: str = "",
        portfolio_state: dict | None = None,
        risk_state: dict | None = None,
        action_reason: str = "",
        action_json: dict | None = None,
    ) -> str:
        factor_snapshots = []
        signals = getattr(composite, "factor_signals", {}) or {}
        values = getattr(composite, "factor_values", {}) or {}
        weights = getattr(composite, "active_weights", {}) or {}
        for factor in sorted(set(signals.keys()) | set(values.keys()) | set(weights.keys())):
            signal = signals.get(factor)
            weight = float(weights.get(factor, 0.0) or 0.0)
            factor_snapshots.append(
                {
                    "factor": factor,
                    "raw_value": values.get(factor, 0.0) or 0.0,
                    "normalized_value": signal if signal is not None else 0.0,
                    "direction": 1.0 if (signal or 0.0) > 0 else -1.0 if (signal or 0.0) < 0 else 0.0,
                    "base_weight": weight,
                    "policy_weight": weight,
                    "gated": bool(signal is None),
                    "gated_reason": "" if signal is not None else "abstain",
                    "contribution_score": float((signal or 0.0) * weight),
                }
            )

        gate_reason = str(getattr(gate_result, "reason", "")) if gate_result else ""
        gate_passed = bool(getattr(gate_result, "passed", False)) if gate_result else False
        action_payload = {
            "direction": getattr(composite, "direction", 0),
            "score": getattr(composite, "score", 0.0),
            "tactical_score": getattr(composite, "tactical_score", 0.0),
            "macro_score": getattr(composite, "macro_score", 0.0),
            "n_active_factors": getattr(composite, "n_active_factors", 0),
            "n_abstain_factors": getattr(composite, "n_abstain_factors", 0),
            "tags_breakdown": getattr(composite, "tags_breakdown", {}) or {},
            "gate_passed": gate_passed,
            "gate_reason": gate_reason,
        }
        if action_json:
            action_payload.update(action_json)
        return self.log_decision(
            event_type=event_type,
            symbol=symbol,
            timeframe=timeframe,
            decision_ts=decision_ts or getattr(composite, "timestamp", None),
            trade_id=trade_id,
            position_id=position_id,
            portfolio_state=portfolio_state,
            risk_state=risk_state,
            action_score=float(getattr(composite, "score", 0.0) or 0.0),
            action_reason=action_reason or gate_reason or event_type,
            action_json=action_payload,
            factor_snapshots=factor_snapshots,
        )

    def log_order_event(
        self,
        *,
        event_type: str,
        decision_id: str = "",
        trade_id: str = "",
        order_id: str = "",
        broker_order_id: str = "",
        price: float = 0.0,
        volume: float = 0.0,
        status: str = "",
        details: dict | None = None,
        event_ts: float | None = None,
    ) -> str:
        event_id = self.new_id("ordevt")
        with self._conn() as conn:
            self._execute(conn,
                """
                INSERT INTO order_lifecycle_event
                (event_id, decision_id, trade_id, order_id, broker_order_id,
                 event_type, event_ts, price, volume, status, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    decision_id,
                    trade_id,
                    order_id,
                    broker_order_id,
                    event_type,
                    float(event_ts or time.time()),
                    float(price or 0.0),
                    float(volume or 0.0),
                    status,
                    _json_dumps(details),
                ),
            )
        return event_id

    def log_position_event(
        self,
        *,
        position_id: str,
        event_type: str,
        trade_id: str = "",
        symbol: str = "",
        net_volume: float = 0.0,
        avg_price: float = 0.0,
        unrealized_pnl: float = 0.0,
        realized_pnl: float = 0.0,
        details: dict | None = None,
        event_ts: float | None = None,
    ) -> str:
        event_id = self.new_id("posevt")
        with self._conn() as conn:
            self._execute(conn,
                """
                INSERT INTO position_lifecycle_event
                (event_id, position_id, trade_id, symbol, event_type, event_ts,
                 net_volume, avg_price, unrealized_pnl, realized_pnl, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    position_id,
                    trade_id,
                    symbol,
                    event_type,
                    float(event_ts or time.time()),
                    float(net_volume or 0.0),
                    float(avg_price or 0.0),
                    float(unrealized_pnl or 0.0),
                    float(realized_pnl or 0.0),
                    _json_dumps(details),
                ),
            )
        return event_id

    def log_position_supervisor_trace(
        self,
        *,
        position_id: str,
        decision_id: str = "",
        trade_id: str = "",
        symbol: str = "",
        timeframe: str = "",
        tick: int = 0,
        event_ts: float | None = None,
        action: str = "",
        summary_reason: str = "",
        confidence: float = 0.0,
        template_id: str = "",
        template_version: str = "",
        stage: str = "",
        outcome: str = "",
        risk_action: str = "",
        risk_allowed: bool = False,
        risk_reason: str = "",
        execution_status: str = "",
        execution_reason: str = "",
        context: dict | None = None,
        verdict: dict | None = None,
        risk_verdict: dict | None = None,
        execution: dict | None = None,
        trace_integrity: str = "full",
        config_version: int = 0,
        config_hash: str = "",
        evolution_run_id: str = "",
    ) -> str:
        trace_id = self.new_id("psvtrace")
        now = time.time()
        if not config_version or not config_hash:
            try:
                from backend.services.evolution_ledger import current_runtime_config_snapshot

                snapshot = current_runtime_config_snapshot(db_path=self.db_path, create_if_missing=False)
                config_version = int(snapshot.get("config_version") or 0)
                config_hash = str(snapshot.get("config_hash") or "")
            except Exception:
                config_version = int(config_version or 0)
                config_hash = str(config_hash or "")
        trace_payload = {
            "trace_id": trace_id,
            "decision_id": decision_id,
            "position_id": str(position_id or ""),
            "trade_id": trade_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "tick": int(tick or 0),
            "event_ts": float(event_ts or now),
            "action": action,
            "summary_reason": summary_reason,
            "confidence": float(confidence or 0.0),
            "template_id": template_id,
            "template_version": template_version,
            "stage": stage,
            "outcome": outcome,
            "risk_action": risk_action,
            "risk_allowed": 1 if risk_allowed else 0,
            "risk_reason": risk_reason,
            "execution_status": execution_status,
            "execution_reason": execution_reason,
            "context_json": _json_dumps(context),
            "verdict_json": _json_dumps(verdict),
            "risk_verdict_json": _json_dumps(risk_verdict),
            "execution_json": _json_dumps(execution),
            "trace_integrity": str(trace_integrity or "full"),
            "config_version": int(config_version or 0),
            "config_hash": str(config_hash or ""),
            "evolution_run_id": str(evolution_run_id or ""),
            "created_at": now,
        }
        with self._conn() as conn:
            self._execute(conn,
                """
                INSERT INTO position_supervisor_trace
                (trace_id, decision_id, position_id, trade_id, symbol, timeframe,
                 tick, event_ts, action, summary_reason, confidence, template_id,
                 template_version, stage, outcome, risk_action, risk_allowed,
                 risk_reason, execution_status, execution_reason, context_json,
                 verdict_json, risk_verdict_json, execution_json, trace_integrity,
                 config_version, config_hash, evolution_run_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(trace_payload[k] for k in (
                    "trace_id",
                    "decision_id",
                    "position_id",
                    "trade_id",
                    "symbol",
                    "timeframe",
                    "tick",
                    "event_ts",
                    "action",
                    "summary_reason",
                    "confidence",
                    "template_id",
                    "template_version",
                    "stage",
                    "outcome",
                    "risk_action",
                    "risk_allowed",
                    "risk_reason",
                    "execution_status",
                    "execution_reason",
                    "context_json",
                    "verdict_json",
                    "risk_verdict_json",
                    "execution_json",
                    "trace_integrity",
                    "config_version",
                    "config_hash",
                    "evolution_run_id",
                    "created_at",
                )),
            )
        return trace_id

    def get_latest_entry_decision(self, position_id: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return self._execute(conn,
                """
                SELECT * FROM decision_ledger
                WHERE position_id=? AND event_type='open'
                ORDER BY decision_ts DESC LIMIT 1
                """,
                (position_id,),
            ).fetchone()

    def get_factor_snapshots(self, decision_id: str) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return list(
                self._execute(conn,
                    """
                    SELECT * FROM decision_factor_snapshot
                    WHERE decision_id=?
                    ORDER BY ABS(contribution_score) DESC, factor ASC
                    """,
                    (decision_id,),
                )
            )

