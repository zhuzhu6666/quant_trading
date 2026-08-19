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

from backend.core.db import (
    STATE_DB,
    STATE_DB_DDL,
    connect_sqlite,
    ensure_sqlite_columns,
    get_state_pg_conn,
    is_state_db_path,
    state_table_columns,
)
from backend.services.market_regime import resolve_market_regime
from backend.services.state_payload_archive import (
    archive_json_payload,
    supervisor_trace_archive_text,
)
from backend.services.supervisor_payload_contract import (
    compact_supervisor_mapping,
    strip_recursive_supervisor_snapshots,
)

logger = logging.getLogger(__name__)


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
        runtime_selection_fingerprint: str = "",
        config_hash: str = "",
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
        normalized_action = dict(normalized_action_json or {})
        runtime_binding = dict(normalized_action.get("runtime_binding") or {})
        selection_fingerprint = str(
            runtime_selection_fingerprint
            or runtime_binding.get("selection_fingerprint")
            or normalized_action.get("factor_set_version")
            or factor_set_version
            or ""
        )
        bound_config_hash = str(
            config_hash
            or runtime_binding.get("config_hash")
            or normalized_action.get("config_hash")
            or ""
        )
        factor_payloads: list[dict[str, Any]] = []
        for row in factor_snapshots or []:
            factor_payloads.append(
                {
                    "decision_id": decision_id,
                    "factor": str(row.get("factor", "")),
                    "source": str(row.get("source", "registry")),
                    "raw_value": _optional_float(row.get("raw_value")),
                    "normalized_value": _optional_float(row.get("normalized_value")),
                    "direction": float(row.get("direction", 0.0) or 0.0),
                    "base_weight": float(row.get("base_weight", 0.0) or 0.0),
                    "policy_weight": float(row.get("policy_weight", 0.0) or 0.0),
                    "shadow_score": float(row.get("shadow_score", 0.0) or 0.0),
                    "health_score": float(row.get("health_score", 0.0) or 0.0),
                    "gated": int(1 if row.get("gated") else 0),
                    "gated_reason": str(row.get("gated_reason", "")),
                    "contribution_score": float(row.get("contribution_score", 0.0) or 0.0),
                    "generation": int(row.get("generation", 0) or 0),
                    "artifact_hash": str(row.get("artifact_hash", "") or ""),
                    "definition_fingerprint": str(
                        row.get("definition_fingerprint", "") or ""
                    ),
                    "runtime_selection_fingerprint": str(
                        row.get("runtime_selection_fingerprint")
                        or selection_fingerprint
                        or ""
                    ),
                    "config_hash": str(row.get("config_hash") or bound_config_hash or ""),
                }
            )
        with self._conn() as conn:
            lifecycle_by_name: dict[str, dict[str, Any]] = {}
            factor_names = sorted(
                {str(row.get("factor") or "") for row in factor_payloads if str(row.get("factor") or "")}
            )
            if factor_names:
                placeholders = ",".join("?" for _ in factor_names)
                try:
                    lifecycle_rows = self._execute(
                        conn,
                        f"""SELECT factor_name, generation, artifact_hash,
                                   definition_fingerprint
                            FROM factor_lifecycle_state
                            WHERE factor_name IN ({placeholders})""",
                        tuple(factor_names),
                    ).fetchall()
                    lifecycle_by_name = {
                        str(item["factor_name"] or ""): {
                            "generation": int(item["generation"] or 0),
                            "artifact_hash": str(item["artifact_hash"] or ""),
                            "definition_fingerprint": str(
                                item["definition_fingerprint"] or ""
                            ),
                        }
                        for item in lifecycle_rows
                    }
                except Exception:
                    # Isolated legacy fixtures may not carry lifecycle tables.
                    # The row remains explicit lineage_missing; values are never
                    # guessed from a mutable Registry object.
                    lifecycle_by_name = {}
            for row in factor_payloads:
                lineage = lifecycle_by_name.get(row["factor"], {})
                row["generation"] = int(row["generation"] or lineage.get("generation") or 0)
                row["artifact_hash"] = str(row["artifact_hash"] or lineage.get("artifact_hash") or "")
                row["definition_fingerprint"] = str(
                    row["definition_fingerprint"]
                    or lineage.get("definition_fingerprint")
                    or ""
                )
                row["lineage_status"] = (
                    "bound"
                    if row["generation"] > 0
                    and bool(row["artifact_hash"])
                    and bool(row["definition_fingerprint"])
                    and bool(row["runtime_selection_fingerprint"])
                    and bool(row["config_hash"])
                    else "lineage_missing"
                )
            # ── P4 单轨写入：canonical 是唯一事实源 ──
            # PG 环境 fail-closed；SQLite fixture 回退 legacy（测试兼容）
            _use_canonical = self._use_pg()
            if _use_canonical:
                from backend.services.canonical_v2 import record_decision_event
                record_decision_event(
                    conn,
                    decision_id=str(decision_id or ""),
                    trade_id=str(decision_payload.get("trade_id") or ""),
                    position_id=str(decision_payload.get("position_id") or ""),
                    event_type=str(decision_payload.get("event_type") or ""),
                    symbol=str(decision_payload.get("symbol") or ""),
                    timeframe=str(decision_payload.get("timeframe") or ""),
                    decision_ts=decision_payload.get("decision_ts"),
                    regime_id=str(decision_payload.get("regime_id") or ""),
                    regime_confidence=decision_payload.get("regime_confidence"),
                    policy_version=str(decision_payload.get("policy_version") or ""),
                    factor_set_version=str(decision_payload.get("factor_set_version") or ""),
                    action_score=decision_payload.get("action_score"),
                    action_reason=str(decision_payload.get("action_reason") or ""),
                    action=decision_payload.get("action_json"),
                    risk_state=decision_payload.get("risk_state_json"),
                    portfolio_state=decision_payload.get("portfolio_state_json"),
                    created_at=decision_payload.get("created_at"),
                    factor_snapshots=factor_payloads if factor_payloads else None,
                )
            else:
                # SQLite fixture fallback: legacy writes for test compatibility
                self._execute(conn,
                    "INSERT INTO decision_ledger"
                    " (decision_id, trade_id, position_id, event_type, symbol, timeframe,"
                    " decision_ts, regime_id, regime_confidence, portfolio_state_json,"
                    " risk_state_json, policy_version, factor_set_version, action_score,"
                    " action_reason, action_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(decision_payload[k] for k in (
                        "decision_id", "trade_id", "position_id", "event_type", "symbol",
                        "timeframe", "decision_ts", "regime_id", "regime_confidence",
                        "portfolio_state_json", "risk_state_json", "policy_version",
                        "factor_set_version", "action_score", "action_reason",
                        "action_json", "created_at",
                    )),
                )
                for row in factor_payloads:
                    self._execute(conn,
                        "INSERT INTO decision_factor_snapshot"
                        " (decision_id, factor, source, raw_value, normalized_value, direction,"
                        " base_weight, policy_weight, shadow_score, health_score, gated,"
                        " gated_reason, contribution_score, generation, artifact_hash,"
                        " definition_fingerprint, runtime_selection_fingerprint,"
                        " config_hash, lineage_status)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            decision_id, row["factor"], row["source"],
                            row["raw_value"], row["normalized_value"], row["direction"],
                            row["base_weight"], row["policy_weight"],
                            row["shadow_score"], row["health_score"],
                            row["gated"], row["gated_reason"],
                            row["contribution_score"], row["generation"],
                            row["artifact_hash"], row["definition_fingerprint"],
                            row["runtime_selection_fingerprint"],
                            row["config_hash"], row["lineage_status"],
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
        policy_version: str = "",
        factor_set_version: str = "",
        action_reason: str = "",
        action_json: dict | None = None,
    ) -> str:
        factor_snapshots = []
        signals = getattr(composite, "factor_signals", {}) or {}
        values = getattr(composite, "factor_values", {}) or {}
        weights = getattr(composite, "active_weights", {}) or {}
        roles = getattr(composite, "factor_roles", {}) or {}
        for factor in sorted(set(signals.keys()) | set(values.keys()) | set(weights.keys())):
            signal = signals.get(factor)
            weight = float(weights.get(factor, 0.0) or 0.0)
            role = str(roles.get(factor) or "alpha")
            used_in_score = role == "alpha" and abs(weight) > 0
            factor_snapshots.append(
                {
                    "factor": factor,
                    "raw_value": values.get(factor),
                    "normalized_value": signal,
                    "direction": (
                        1.0 if (signal or 0.0) > 0 else -1.0 if (signal or 0.0) < 0 else 0.0
                    ) if used_in_score else 0.0,
                    "base_weight": weight,
                    "policy_weight": weight,
                    "gated": bool(signal is None),
                    "gated_reason": "" if signal is not None else "abstain",
                    "contribution_score": float((signal or 0.0) * weight),
                }
            )

        gate_reason = str(getattr(gate_result, "reason", "")) if gate_result else ""
        gate_passed = bool(getattr(gate_result, "passed", False)) if gate_result else False
        regime = resolve_market_regime(composite)
        action_payload = {
            "direction": getattr(composite, "direction", 0),
            "score": getattr(composite, "score", 0.0),
            "composer_version": getattr(composite, "composer_version", ""),
            "alpha_score": getattr(composite, "alpha_score", getattr(composite, "score", 0.0)),
            "tactical_score": getattr(composite, "tactical_score", 0.0),
            "macro_score": getattr(composite, "macro_score", 0.0),
            "n_active_factors": getattr(composite, "n_active_factors", 0),
            "n_available_factors": getattr(
                composite,
                "n_available_factors",
                getattr(composite, "n_active_factors", 0),
            ),
            "n_scoring_factors": getattr(
                composite,
                "n_scoring_factors",
                getattr(composite, "n_active_alpha_factors", 0),
            ),
            "n_contributing_factors": getattr(composite, "n_contributing_factors", 0),
            "n_active_alpha_factors": getattr(composite, "n_active_alpha_factors", 0),
            "effective_alpha_factor_count": getattr(
                composite,
                "effective_alpha_factor_count",
                getattr(composite, "n_active_alpha_factors", 0),
            ),
            "n_abstain_factors": getattr(composite, "n_abstain_factors", 0),
            "factor_roles": roles,
            "context_signals": getattr(composite, "context_signals", {}) or {},
            "context_state": getattr(composite, "context_state", {}) or {},
            "context_policy": getattr(composite, "context_policy", {}) or {},
            "redundancy_groups": getattr(composite, "redundancy_groups", {}) or {},
            "tags_breakdown": getattr(composite, "tags_breakdown", {}) or {},
            "gate_passed": gate_passed,
            "gate_reason": gate_reason,
            "regime_id": regime["regime_id"],
            "regime_confidence": regime["confidence"],
            "regime_source": regime["source"],
        }
        if action_json:
            action_payload.update(action_json)
        runtime_binding = dict(action_payload.get("runtime_binding") or {})
        return self.log_decision(
            event_type=event_type,
            symbol=symbol,
            timeframe=timeframe,
            decision_ts=decision_ts or getattr(composite, "timestamp", None),
            regime_id=regime["regime_id"],
            regime_confidence=regime["confidence"],
            trade_id=trade_id,
            position_id=position_id,
            portfolio_state=portfolio_state,
            risk_state=risk_state,
            policy_version=policy_version,
            factor_set_version=factor_set_version,
            action_score=float(getattr(composite, "score", 0.0) or 0.0),
            action_reason=action_reason or gate_reason or event_type,
            action_json=action_payload,
            factor_snapshots=factor_snapshots,
            runtime_selection_fingerprint=str(
                runtime_binding.get("selection_fingerprint")
                or factor_set_version
                or ""
            ),
            config_hash=str(runtime_binding.get("config_hash") or ""),
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
        event_ts = float(event_ts or time.time())
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
                    event_ts,
                    float(price or 0.0),
                    float(volume or 0.0),
                    status,
                    _json_dumps(details),
                ),
            )
            # ── canonical 增量镜像（同事务、幂等；过渡期 fail-open）──
            try:
                from backend.services.canonical_v2 import record_order_event
                record_order_event(
                    conn,
                    event_id=event_id,
                    event_type=event_type,
                    event_ts=event_ts,
                    decision_id=decision_id,
                    trade_id=trade_id,
                    order_id=order_id,
                    broker_order_id=broker_order_id,
                    price=price,
                    volume=volume,
                    status=status,
                    details=details,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[ledger] canonical order mirror failed event_id=%s: %s",
                    event_id,
                    exc,
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
        event_ts = float(event_ts or time.time())
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
                    str(position_id),
                    trade_id,
                    symbol,
                    event_type,
                    event_ts,
                    float(net_volume or 0.0),
                    float(avg_price or 0.0),
                    float(unrealized_pnl or 0.0),
                    float(realized_pnl or 0.0),
                    _json_dumps(details),
                ),
            )
            # ── canonical 增量镜像（同事务、幂等；过渡期 fail-open）──
            try:
                from backend.services.canonical_v2 import record_position_event
                record_position_event(
                    conn,
                    event_id=event_id,
                    position_id=str(position_id),
                    event_type=event_type,
                    event_ts=event_ts,
                    trade_id=trade_id,
                    symbol=symbol,
                    net_volume=net_volume,
                    avg_price=avg_price,
                    unrealized_pnl=unrealized_pnl,
                    realized_pnl=realized_pnl,
                    details=details,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[ledger] canonical position mirror failed event_id=%s: %s",
                    event_id,
                    exc,
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
        raw_context = strip_recursive_supervisor_snapshots(dict(context or {}))
        raw_verdict = strip_recursive_supervisor_snapshots(dict(verdict or {}))
        raw_risk_verdict = strip_recursive_supervisor_snapshots(dict(risk_verdict or {}))
        raw_execution = strip_recursive_supervisor_snapshots(dict(execution or {}))
        compact_context = compact_supervisor_mapping(
            raw_context,
            nested_keys=frozenset({"position", "account"}),
        )
        compact_verdict = compact_supervisor_mapping(
            raw_verdict,
            nested_keys=frozenset({"evidence", "recommended_controls", "supervisor_template"}),
        )
        compact_risk_verdict = compact_supervisor_mapping(
            raw_risk_verdict,
            nested_keys=frozenset({"evidence", "controls"}),
        )
        compact_execution = compact_supervisor_mapping(
            raw_execution,
            nested_keys=frozenset({"evidence", "controls"}),
        )
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
            # The inline trace is always a bounded projection.  If archive is
            # available, the sanitized full semantic payload is stored there.
            # Old stores without archive columns must not reintroduce the
            # recursive payload growth that caused the training OOM gate.
            "context_json": _json_dumps(compact_context),
            "verdict_json": _json_dumps(compact_verdict),
            "risk_verdict_json": _json_dumps(compact_risk_verdict),
            "execution_json": _json_dumps(compact_execution),
            "trace_integrity": str(trace_integrity or "full"),
            "config_version": int(config_version or 0),
            "config_hash": str(config_hash or ""),
            "evolution_run_id": str(evolution_run_id or ""),
            "created_at": now,
        }
        with self._conn() as conn:
            trace_columns = set(state_table_columns(conn, "position_supervisor_trace"))
            archive_ready = {
                "verdict_archive_hash",
                "verdict_raw_sha256",
                "verdict_raw_bytes",
            } <= trace_columns
            archive = None
            if archive_ready:
                archive = archive_json_payload(
                    conn,
                    source_table="position_supervisor_trace",
                    source_id=trace_id,
                    payload_kind="supervisor_trace",
                    raw_json=supervisor_trace_archive_text(
                        context_json=_json_dumps(raw_context),
                        verdict_json=_json_dumps(raw_verdict),
                        risk_verdict_json=_json_dumps(raw_risk_verdict),
                        execution_json=_json_dumps(raw_execution),
                    ),
                )
            if archive:
                trace_payload.update(
                    {
                        "context_json": _json_dumps(compact_context),
                        "verdict_json": _json_dumps(compact_verdict),
                        "risk_verdict_json": _json_dumps(compact_risk_verdict),
                        "execution_json": _json_dumps(compact_execution),
                    }
                )
                self._execute(
                    conn,
                    """
                    INSERT INTO position_supervisor_trace
                    (trace_id, decision_id, position_id, trade_id, symbol, timeframe,
                     tick, event_ts, action, summary_reason, confidence, template_id,
                     template_version, stage, outcome, risk_action, risk_allowed,
                     risk_reason, execution_status, execution_reason, context_json,
                     verdict_json, risk_verdict_json, execution_json, trace_integrity,
                     config_version, config_hash, evolution_run_id, created_at,
                     verdict_archive_hash, verdict_raw_sha256, verdict_raw_bytes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(trace_payload[k] for k in (
                        "trace_id", "decision_id", "position_id", "trade_id", "symbol", "timeframe",
                        "tick", "event_ts", "action", "summary_reason", "confidence", "template_id",
                        "template_version", "stage", "outcome", "risk_action", "risk_allowed", "risk_reason",
                        "execution_status", "execution_reason", "context_json", "verdict_json",
                        "risk_verdict_json", "execution_json", "trace_integrity", "config_version",
                        "config_hash", "evolution_run_id", "created_at",
                    )) + (archive["archive_hash"], archive["raw_sha256"], archive["raw_bytes"]),
                )
            else:
                self._execute(
                    conn,
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
                        "trace_id", "decision_id", "position_id", "trade_id", "symbol", "timeframe",
                        "tick", "event_ts", "action", "summary_reason", "confidence", "template_id",
                        "template_version", "stage", "outcome", "risk_action", "risk_allowed", "risk_reason",
                        "execution_status", "execution_reason", "context_json", "verdict_json",
                        "risk_verdict_json", "execution_json", "trace_integrity", "config_version",
                        "config_hash", "evolution_run_id", "created_at",
                    )),
                )
        return trace_id

    def get_latest_entry_decision(self, position_id: str) -> Any | None:
        """Latest open decision for a position (canonical position index first).

        The materialized index (scripts/canonical_v2_position_decision_index.py)
        resolves the position to its entry decision; the decision is then read
        through the canonical reader (legacy-shaped row).  Positions missing
        from the index and non-canonical connections keep the legacy lookup.
        """
        try:
            from backend.services.canonical_v2_reader import (
                decision_row,
                load_position_decision_index,
            )
        except Exception:  # noqa: BLE001
            decision_row = None
            load_position_decision_index = None
        if decision_row is not None and load_position_decision_index is not None:
            try:
                index_path = (
                    Path(__file__).resolve().parents[2]
                    / "run_artifacts"
                    / "canonical_v2_position_decision_index.json"
                )
                index = load_position_decision_index(index_path)
                if index is not None:
                    entry = index.get(str(position_id))
                    if entry is not None and entry.get("decision_id"):
                        with self._conn() as conn:
                            row = decision_row(conn, str(entry["decision_id"]))
                        if row is not None:
                            return row
            except Exception:  # noqa: BLE001
                pass  # transitional fallback to the legacy lookup below
        with self._conn() as conn:
            return self._execute(conn,
                """
                SELECT * FROM decision_ledger
                WHERE position_id=? AND event_type='open'
                ORDER BY decision_ts DESC LIMIT 1
                """,
                (str(position_id),),
            ).fetchone()

    def get_factor_snapshots(self, decision_id: str) -> list[dict]:
        """Return per-factor snapshot rows for a decision.

        Reads from canonical payload (new decisions) with legacy fallback.
        """
        try:
            from backend.services.canonical_v2_reader import (
                iter_decision_factor_snapshots,
            )
            with self._conn() as conn:
                return iter_decision_factor_snapshots(conn, decision_id)
        except Exception:
            with self._conn() as conn:
                return list(
                    self._execute(conn,
                        "SELECT * FROM decision_factor_snapshot"
                        " WHERE decision_id=?"
                        " ORDER BY ABS(contribution_score) DESC, factor ASC",
                        (decision_id,),
                    )
                )
