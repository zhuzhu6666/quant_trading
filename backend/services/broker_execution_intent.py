"""Persistent broker mutation intents and unknown-outcome recovery.

The table is created only by the explicit state schema migration.  This
module never performs DDL: if the PostgreSQL state store or its required
schema is unavailable, callers must fail closed before a broker mutation.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from backend.core.db import get_state_pg_conn
from backend.core.db_helpers import dump_json as _json, row_value as _row_value


UNRESOLVED_EXECUTION_STATUSES = frozenset({"prepared", "submitting", "unknown"})
FINAL_EXECUTION_STATUSES = frozenset({"confirmed", "rejected"})
EXECUTION_INTENT_STATUSES = UNRESOLVED_EXECUTION_STATUSES | FINAL_EXECUTION_STATUSES


@dataclass(frozen=True)
class BrokerExecutionIntent:
    intent_id: str
    idempotency_key: str
    broker: str
    account_id: str
    symbol: str
    action: str
    side: str
    requested_volume: float
    status: str
    decision_id: str = ""
    trade_id: str = ""
    requested_price: float = 0.0
    target_stop_loss: float = 0.0
    target_take_profit: float = 0.0
    attempt_count: int = 0
    position_id: str = ""
    broker_order_id: str = ""
    request: Mapping[str, Any] | None = None
    broker_response: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    prepared_at: float = 0.0
    submitted_at: float = 0.0
    completed_at: float = 0.0
    updated_at: float = 0.0

    @property
    def unresolved(self) -> bool:
        return self.status in UNRESOLVED_EXECUTION_STATUSES


class BrokerExecutionIntentStore:
    """Small transaction boundary around ``broker_execution_intent``."""

    def __init__(self, connection_factory: Callable[..., Any] | None = None):
        self._connection_factory = connection_factory or get_state_pg_conn

    def _connect(self, *, read_only: bool = False):
        try:
            return self._connection_factory(read_only=read_only)
        except TypeError:
            return self._connection_factory()

    @staticmethod
    def new_identity() -> tuple[str, str]:
        intent_id = str(uuid.uuid4())
        return intent_id, intent_id

    def prepare(
        self,
        *,
        intent_id: str,
        idempotency_key: str,
        broker: str,
        account_id: str,
        symbol: str,
        action: str,
        side: str,
        requested_volume: float,
        requested_price: float = 0.0,
        target_stop_loss: float = 0.0,
        target_take_profit: float = 0.0,
        decision_id: str = "",
        trade_id: str = "",
        request: Mapping[str, Any] | None = None,
        risk_verdict: Mapping[str, Any] | None = None,
        config_version: int = 0,
        config_hash: str = "",
    ) -> BrokerExecutionIntent:
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO runtime.broker_execution_intent (
                    intent_id, idempotency_key, decision_id, trade_id,
                    broker, account_id, symbol, action, side,
                    requested_volume, requested_price, target_stop_loss,
                    target_take_profit, status, attempt_count, request_json,
                    risk_verdict_json, config_version, config_hash,
                    prepared_at, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, 'prepared', 0, %s, %s, %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (
                    str(intent_id), str(idempotency_key), str(decision_id), str(trade_id),
                    str(broker), str(account_id), str(symbol), str(action), str(side),
                    float(requested_volume or 0.0), float(requested_price or 0.0),
                    float(target_stop_loss or 0.0), float(target_take_profit or 0.0),
                    _json(dict(request or {})), _json(dict(risk_verdict or {})),
                    int(config_version or 0), str(config_hash or ""), now, now, now,
                ),
            )
            row = conn.execute(
                """
                SELECT intent_id, idempotency_key, broker, account_id, symbol,
                       action, side, requested_volume, status, attempt_count,
                       position_id, broker_order_id, request_json,
                       broker_response_json, error_json, prepared_at,
                       submitted_at, completed_at, updated_at,
                       requested_price, target_stop_loss, target_take_profit,
                       decision_id, trade_id
                FROM runtime.broker_execution_intent
                WHERE idempotency_key=%s
                """,
                (str(idempotency_key),),
            ).fetchone()
            if row is None:
                raise RuntimeError("broker_execution_intent prepare was not persisted")
            conn.commit()
            return self._decode(row)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def mark_submitting(
        self,
        intent_id: str,
        *,
        request: Mapping[str, Any] | None = None,
    ) -> BrokerExecutionIntent:
        now = time.time()
        conn = self._connect()
        try:
            if request is None:
                conn.execute(
                    """
                    UPDATE runtime.broker_execution_intent
                    SET status='submitting', attempt_count=attempt_count + 1,
                        submitted_at=%s, updated_at=%s
                    WHERE intent_id=%s AND status='prepared'
                    """,
                    (now, now, str(intent_id)),
                )
            else:
                conn.execute(
                    """
                    UPDATE runtime.broker_execution_intent
                    SET status='submitting', attempt_count=attempt_count + 1,
                        request_json=%s, submitted_at=%s, updated_at=%s
                    WHERE intent_id=%s AND status='prepared'
                    """,
                    (_json(dict(request)), now, now, str(intent_id)),
                )
            row = self._select_one(conn, intent_id)
            if row is None or str(_row_value(row, "status", 8, "")) != "submitting":
                raise RuntimeError("broker execution intent is not in prepared state")
            conn.commit()
            return self._decode(row)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def complete(
        self,
        intent_id: str,
        *,
        outcome: str,
        position_id: int | str = "",
        trade_id: int | str = "",
        broker_order_id: int | str = "",
        broker_response: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> BrokerExecutionIntent:
        status = str(outcome or "").strip().lower()
        if status not in {"confirmed", "rejected", "unknown"}:
            raise ValueError(f"invalid broker execution outcome: {outcome!r}")
        now = time.time()
        completed_at = 0.0 if status == "unknown" else now
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE runtime.broker_execution_intent
                SET status=%s, position_id=%s,
                    trade_id=COALESCE(NULLIF(%s, ''), trade_id),
                    broker_order_id=%s,
                    broker_response_json=%s, error_json=%s,
                    completed_at=%s, updated_at=%s
                WHERE intent_id=%s AND status IN ('prepared', 'submitting', 'unknown')
                """,
                (
                    status, str(position_id or ""), str(trade_id or ""),
                    str(broker_order_id or ""),
                    _json(dict(broker_response or {})), _json(dict(error or {})),
                    completed_at, now, str(intent_id),
                ),
            )
            row = self._select_one(conn, intent_id)
            if row is None:
                raise RuntimeError(f"broker execution intent not found: {intent_id}")
            conn.commit()
            return self._decode(row)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def unresolved(
        self,
        *,
        broker: str = "ctrader",
        account_id: str = "",
        symbol: str = "",
        limit: int = 100,
    ) -> list[BrokerExecutionIntent]:
        clauses = ["status IN ('prepared', 'submitting', 'unknown')", "broker=%s"]
        params: list[Any] = [str(broker)]
        if account_id:
            clauses.append("account_id=%s")
            params.append(str(account_id))
        if symbol:
            clauses.append("symbol=%s")
            params.append(str(symbol))
        params.append(max(1, min(int(limit or 100), 1000)))
        conn = self._connect(read_only=True)
        try:
            rows = conn.execute(
                f"""
                SELECT intent_id, idempotency_key, broker, account_id, symbol,
                       action, side, requested_volume, status, attempt_count,
                       position_id, broker_order_id, request_json,
                       broker_response_json, error_json, prepared_at,
                       submitted_at, completed_at, updated_at,
                       requested_price, target_stop_loss, target_take_profit,
                       decision_id, trade_id
                FROM runtime.broker_execution_intent
                WHERE {' AND '.join(clauses)}
                ORDER BY prepared_at ASC
                LIMIT %s
                """,
                tuple(params),
            ).fetchall()
            return [self._decode(row) for row in rows]
        finally:
            conn.close()

    def unresolved_count(
        self,
        *,
        broker: str = "ctrader",
        account_id: str = "",
        symbol: str = "",
    ) -> int:
        clauses = ["status IN ('prepared', 'submitting', 'unknown')", "broker=%s"]
        params: list[Any] = [str(broker)]
        if account_id:
            clauses.append("account_id=%s")
            params.append(str(account_id))
        if symbol:
            clauses.append("symbol=%s")
            params.append(str(symbol))
        conn = self._connect(read_only=True)
        try:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM runtime.broker_execution_intent WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchone()
            return int(_row_value(row, "n", 0, 0) or 0)
        finally:
            conn.close()

    @staticmethod
    def _select_one(conn: Any, intent_id: str):
        return conn.execute(
            """
            SELECT intent_id, idempotency_key, broker, account_id, symbol,
                   action, side, requested_volume, status, attempt_count,
                   position_id, broker_order_id, request_json,
                   broker_response_json, error_json, prepared_at,
                   submitted_at, completed_at, updated_at,
                   requested_price, target_stop_loss, target_take_profit,
                   decision_id, trade_id
            FROM runtime.broker_execution_intent
            WHERE intent_id=%s
            """,
            (str(intent_id),),
        ).fetchone()

    def latest_stop_loss_for_position(
        self,
        position_id: int | str,
        *,
        broker: str = "ctrader",
    ) -> BrokerExecutionIntent | None:
        """Return the newest confirmed SL/TP amend intent for a position.

        Read-only reference lookup.  Callers receive the durable intent row
        itself — never a copy of its payload — so the close-reason classifier
        can cite the broker-side stop-loss authority without duplicating it.
        """

        pid = str(int(position_id or 0) or 0)
        if not pid:
            return None
        conn = self._connect(read_only=True)
        try:
            row = conn.execute(
                """
                SELECT intent_id, idempotency_key, broker, account_id, symbol,
                       action, side, requested_volume, status, attempt_count,
                       position_id, broker_order_id, request_json,
                       broker_response_json, error_json, prepared_at,
                       submitted_at, completed_at, updated_at,
                       requested_price, target_stop_loss, target_take_profit,
                       decision_id, trade_id
                FROM runtime.broker_execution_intent
                WHERE position_id=%s AND broker=%s
                  AND action IN ('amend_position_sltp', 'market_open')
                  AND status='confirmed'
                ORDER BY prepared_at DESC
                LIMIT 1
                """,
                (pid, str(broker)),
            ).fetchone()
            return self._decode(row) if row is not None else None
        except Exception:
            # A missing table / unavailable state store must never upgrade a
            # close-reason classification: absence of evidence is not the
            # evidence of a stop-loss hit.
            return None
        finally:
            conn.close()


    @staticmethod
    def _decode(row: Any) -> BrokerExecutionIntent:
        def load_json(key: str, index: int) -> dict[str, Any]:
            raw = str(_row_value(row, key, index, "{}") or "{}")
            try:
                value = json.loads(raw)
            except Exception:
                value = {}
            return value if isinstance(value, dict) else {}

        return BrokerExecutionIntent(
            intent_id=str(_row_value(row, "intent_id", 0, "") or ""),
            idempotency_key=str(_row_value(row, "idempotency_key", 1, "") or ""),
            decision_id=str(_row_value(row, "decision_id", 22, "") or ""),
            trade_id=str(_row_value(row, "trade_id", 23, "") or ""),
            broker=str(_row_value(row, "broker", 2, "") or ""),
            account_id=str(_row_value(row, "account_id", 3, "") or ""),
            symbol=str(_row_value(row, "symbol", 4, "") or ""),
            action=str(_row_value(row, "action", 5, "") or ""),
            side=str(_row_value(row, "side", 6, "") or ""),
            requested_volume=float(_row_value(row, "requested_volume", 7, 0.0) or 0.0),
            status=str(_row_value(row, "status", 8, "") or ""),
            requested_price=float(_row_value(row, "requested_price", 19, 0.0) or 0.0),
            target_stop_loss=float(_row_value(row, "target_stop_loss", 20, 0.0) or 0.0),
            target_take_profit=float(_row_value(row, "target_take_profit", 21, 0.0) or 0.0),
            attempt_count=int(_row_value(row, "attempt_count", 9, 0) or 0),
            position_id=str(_row_value(row, "position_id", 10, "") or ""),
            broker_order_id=str(_row_value(row, "broker_order_id", 11, "") or ""),
            request=load_json("request_json", 12),
            broker_response=load_json("broker_response_json", 13),
            error=load_json("error_json", 14),
            prepared_at=float(_row_value(row, "prepared_at", 15, 0.0) or 0.0),
            submitted_at=float(_row_value(row, "submitted_at", 16, 0.0) or 0.0),
            completed_at=float(_row_value(row, "completed_at", 17, 0.0) or 0.0),
            updated_at=float(_row_value(row, "updated_at", 18, 0.0) or 0.0),
        )


def execution_intent_recovery_status(
    store: BrokerExecutionIntentStore,
    *,
    account_id: str = "",
    symbol: str = "",
) -> dict[str, Any]:
    """Read-only status payload used by startup/readiness recovery barriers."""

    items = store.unresolved(account_id=account_id, symbol=symbol)
    return {
        "schema": "broker_execution_intent_recovery.v2",
        "ready": not items,
        "unresolved_count": len(items),
        "unresolved": [
            {
                "intent_id": item.intent_id,
                "decision_id": item.decision_id,
                "trade_id": item.trade_id,
                "timing": _intent_causal_timing(item),
                "status": item.status,
                "attempt_count": item.attempt_count,
                "account_id": item.account_id,
                "symbol": item.symbol,
                "side": item.side,
                "requested_volume": item.requested_volume,
                "prepared_at": item.prepared_at,
                "submitted_at": item.submitted_at,
                "updated_at": item.updated_at,
            }
            for item in items
        ],
    }


def _intent_causal_timing(item: BrokerExecutionIntent) -> dict[str, Any]:
    """Expose intent timestamps without treating unresolved work as success."""
    def stage(ts: float, *, status: str, reason: str = "") -> dict[str, Any]:
        return {
            "ts": float(ts or 0.0) or None,
            "status": str(status),
            "reason": str(reason or ""),
        }

    submitted_status = "known" if float(item.submitted_at or 0.0) > 0.0 else "unknown"
    completed_status = "known" if float(item.completed_at or 0.0) > 0.0 else "unknown"
    return {
        "schema_version": "causal_timing.v1",
        "stages": {
            "decision": stage(
                0.0,
                status="unknown" if not item.decision_id else "partial",
                reason=("decision_timestamp_not_carried_on_intent" if item.decision_id
                        else "decision_id_missing"),
            ),
            "intent_prepared": stage(
                item.prepared_at,
                status="known" if item.prepared_at > 0.0 else "unknown",
                reason="" if item.prepared_at > 0.0 else "prepared_timestamp_missing",
            ),
            "intent_submitted": stage(
                item.submitted_at,
                status=submitted_status,
                reason="" if submitted_status == "known" else "intent_not_submitted_or_timestamp_missing",
            ),
            "broker_ack": stage(
                item.completed_at,
                status=completed_status,
                reason=(
                    ""
                    if completed_status == "known"
                    else "broker_outcome_unknown"
                    if item.status == "unknown"
                    else "broker_ack_timestamp_missing"
                ),
            ),
            "order_position": stage(
                item.completed_at,
                status="known" if item.position_id or item.trade_id else "unknown",
                reason=(
                    ""
                    if item.position_id or item.trade_id
                    else "order_or_position_identity_missing"
                ),
            ),
            "supervisor": stage(0.0, status="unknown", reason="position_supervision_pending"),
            "review": stage(0.0, status="unknown", reason="trade_review_pending"),
            "learning": stage(0.0, status="unknown", reason="learning_application_pending"),
            "effect": stage(0.0, status="unknown", reason="learning_effect_not_observed"),
        },
    }
