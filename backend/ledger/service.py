from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, STATE_DB_DDL


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


class DecisionLedger:
    """Structured decision and lifecycle ledger."""

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or str(STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
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
            conn.executescript(STATE_DB_DDL)

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
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO decision_ledger
                (decision_id, trade_id, position_id, event_type, symbol, timeframe,
                 decision_ts, regime_id, regime_confidence, portfolio_state_json,
                 risk_state_json, policy_version, factor_set_version, action_score,
                 action_reason, action_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    trade_id,
                    position_id,
                    event_type,
                    symbol,
                    timeframe,
                    float(decision_ts or now),
                    regime_id,
                    float(regime_confidence or 0.0),
                    _json_dumps(portfolio_state),
                    _json_dumps(risk_state),
                    policy_version,
                    factor_set_version,
                    float(action_score or 0.0),
                    action_reason,
                    _json_dumps(action_json),
                    now,
                ),
            )
            for row in factor_snapshots or []:
                conn.execute(
                    """
                    INSERT INTO decision_factor_snapshot
                    (decision_id, factor, source, raw_value, normalized_value, direction,
                     base_weight, policy_weight, shadow_score, health_score, gated,
                     gated_reason, contribution_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        str(row.get("factor", "")),
                        str(row.get("source", "registry")),
                        float(row.get("raw_value", 0.0) or 0.0),
                        float(row.get("normalized_value", 0.0) or 0.0),
                        float(row.get("direction", 0.0) or 0.0),
                        float(row.get("base_weight", 0.0) or 0.0),
                        float(row.get("policy_weight", 0.0) or 0.0),
                        float(row.get("shadow_score", 0.0) or 0.0),
                        float(row.get("health_score", 0.0) or 0.0),
                        int(1 if row.get("gated") else 0),
                        str(row.get("gated_reason", "")),
                        float(row.get("contribution_score", 0.0) or 0.0),
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
            conn.execute(
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
            conn.execute(
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

    def get_latest_entry_decision(self, position_id: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
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
                conn.execute(
                    """
                    SELECT * FROM decision_factor_snapshot
                    WHERE decision_id=?
                    ORDER BY ABS(contribution_score) DESC, factor ASC
                    """,
                    (decision_id,),
                )
            )
