from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_exists,
)
from backend.core.state_store import (
    is_state_schema_write_sql,
    validate_runtime_state_schema,
)
from backend.services.canonical_v2_reader import (
    canonical_ready,
    iter_decision_rows,
    iter_position_rows,
    iter_review_rows,
)
from backend.services.review_contract import review_has_system_contamination
from backend.services.state_payload_archive import load_json_payload


DEFAULT_HORIZONS_MINUTES = [5, 15, 30, 60, 120]


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


from backend.core.db_helpers import (
    conn_is_pg as _conn_is_pg,
    pg_sql as _sql,
)


def _execute(conn, sql: str, params: Any = None):
    rendered = _sql(conn, sql)
    if _conn_is_pg(conn) and is_state_schema_write_sql(rendered):
        return validate_runtime_state_schema(conn, rendered)
    if params is None:
        return conn.execute(rendered)
    return conn.execute(rendered, params)


def _review_payload(conn: Any, row: Any) -> dict[str, Any]:
    payload = load_json_payload(
        conn,
        source_table="trade_outcome_review",
        source_id=str(row["review_id"] or ""),
        inline_json=row["review_json"],
        archive_hash=row["review_archive_hash"] if "review_archive_hash" in row.keys() else "",
        default={},
    )
    return payload if isinstance(payload, dict) else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _direction_from_review(review: dict[str, Any]) -> int:
    entry_action = review.get("entry_action") or {}
    direction = int(_safe_float(entry_action.get("direction"), 0.0))
    if direction != 0:
        return 1 if direction > 0 else -1
    real = review.get("real_pnl") or {}
    entry = _safe_float(real.get("entry_price") or review.get("entry_price"))
    close = _safe_float(review.get("close_price") or real.get("exec_price"))
    pnl = _safe_float(real.get("net") or review.get("pnl"))
    if entry <= 0 or close <= 0 or abs(pnl) < 1e-9:
        return 1
    return 1 if (close - entry) * pnl >= 0 else -1


def _load_future_bars(symbol: str, timeframe: str, close_ts: float, max_minutes: int):
    try:
        from data.store import DataStore

        bars = DataStore().load_bars(
            symbol or "XAUUSD+",
            timeframe or "M5",
            start=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(close_ts)),
            end=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(close_ts + max_minutes * 60 + 300)),
        )
        return _bars_with_epoch(bars)
    except Exception:
        return None


def _bars_with_epoch(bars):
    if bars is None or getattr(bars, "empty", True):
        return bars
    bars = bars.copy()
    if "time" not in bars.columns:
        bars["_epoch_time"] = 0.0
        return bars
    try:
        import pandas as pd

        if pd.api.types.is_numeric_dtype(bars["time"]):
            bars["_epoch_time"] = bars["time"].apply(_safe_float)
        else:
            time_values = pd.to_datetime(bars["time"], utc=True, errors="coerce")
            numeric_ts = time_values.astype("int64").astype(float)
            positive_ts = numeric_ts[numeric_ts > 0]
            scale = 1_000_000_000.0 if not positive_ts.empty and positive_ts.median() > 1_000_000_000_000 else 1.0
            bars["_epoch_time"] = numeric_ts / scale
    except Exception:
        bars["_epoch_time"] = bars["time"].apply(_safe_float)
    return bars


def _load_future_bar_cache(candidates: list[dict[str, Any]], max_minutes: int) -> dict[tuple[str, str], Any]:
    if not candidates:
        return {}
    try:
        from data.store import DataStore

        store = DataStore()
    except Exception:
        return {}
    grouped: dict[tuple[str, str], list[float]] = {}
    for item in candidates:
        symbol = str(item.get("symbol") or "XAUUSD+")
        timeframe = str(item.get("timeframe") or "M5")
        close_ts = _safe_float(item.get("close_ts"))
        if close_ts > 0:
            grouped.setdefault((symbol, timeframe), []).append(close_ts)
    cache: dict[tuple[str, str], Any] = {}
    for (symbol, timeframe), close_times in grouped.items():
        start_ts = min(close_times)
        end_ts = max(close_times) + int(max_minutes) * 60 + 300
        try:
            bars = store.load_bars(
                symbol,
                timeframe,
                start=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start_ts)),
                end=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(end_ts)),
            )
        except Exception:
            continue
        if bars is not None and not getattr(bars, "empty", True):
            cache[(symbol, timeframe)] = _bars_with_epoch(bars)
    return cache


def _slice_future_bars(bar_cache: dict[tuple[str, str], Any], symbol: str, timeframe: str, close_ts: float, max_minutes: int):
    bars = bar_cache.get((symbol or "XAUUSD+", timeframe or "M5"))
    if bars is None or getattr(bars, "empty", True):
        return None
    if "_epoch_time" not in bars.columns:
        bars = _bars_with_epoch(bars)
    end_ts = float(close_ts) + int(max_minutes) * 60 + 300
    window = bars[(bars["_epoch_time"] >= float(close_ts)) & (bars["_epoch_time"] <= end_ts)]
    return window.copy() if window is not None and not getattr(window, "empty", True) else None


def _future_loader_is_patched() -> bool:
    return getattr(_load_future_bars, "__module__", "") != __name__ or getattr(
        _load_future_bars, "__name__", ""
    ) != "_load_future_bars"


def _pnl_from_price(*, direction: int, entry_price: float, price: float, unit: float) -> float:
    if entry_price <= 0 or price <= 0 or unit <= 0:
        return 0.0
    return (price - entry_price) * int(direction or 1) * unit


def _first_original_barrier_hit(*, window, direction: int, original_tp: float, original_sl: float) -> dict[str, Any]:
    if original_tp <= 0 and original_sl <= 0:
        return {"first_original_hit": "", "first_original_hit_ts": 0.0}
    sorted_window = window.sort_values("_epoch_time")
    for _, row in sorted_window.iterrows():
        high = _safe_float(row.get("high"))
        low = _safe_float(row.get("low"))
        ts = _safe_float(row.get("_epoch_time"))
        if direction >= 0:
            tp_hit = bool(original_tp > 0 and high >= original_tp)
            sl_hit = bool(original_sl > 0 and low <= original_sl)
        else:
            tp_hit = bool(original_tp > 0 and low <= original_tp)
            sl_hit = bool(original_sl > 0 and high >= original_sl)
        if tp_hit and sl_hit:
            return {"first_original_hit": "ambiguous", "first_original_hit_ts": ts}
        if tp_hit:
            return {"first_original_hit": "tp", "first_original_hit_ts": ts}
        if sl_hit:
            return {"first_original_hit": "sl", "first_original_hit_ts": ts}
    return {"first_original_hit": "", "first_original_hit_ts": 0.0}


def _horizon_metrics(
    *,
    bars,
    direction: int,
    entry_price: float,
    close_pnl: float,
    close_ts: float,
    unit: float,
    horizons_minutes: list[int],
    original_tp: float,
    original_sl: float,
) -> list[dict[str, Any]]:
    if bars is None or getattr(bars, "empty", True):
        return []
    if "_epoch_time" not in bars.columns:
        bars = _bars_with_epoch(bars)
    if "_epoch_time" not in bars.columns:
        bars = bars.copy()
        bars["_epoch_time"] = float(close_ts)
    out = []
    latest_available_bar_ts = max((_safe_float(x) for x in bars["_epoch_time"].tolist()), default=0.0)
    for minutes in horizons_minutes:
        cutoff = close_ts + int(minutes) * 60
        window = bars[(bars["_epoch_time"] > float(close_ts)) & (bars["_epoch_time"] <= cutoff)]
        if window is None or getattr(window, "empty", True):
            window = bars.iloc[0:0]
        expected_bars = max(1, int(minutes))
        observed_bars = int(len(window.index))
        matured = bool(latest_available_bar_ts >= cutoff and observed_bars >= expected_bars)
        fingerprint_payload = [
            [round(_safe_float(row.get("_epoch_time")), 3), _safe_float(row.get("open")),
             _safe_float(row.get("high")), _safe_float(row.get("low")), _safe_float(row.get("close"))]
            for _, row in window.iterrows()
        ]
        common = {
            "horizon_minutes": int(minutes),
            "expected_bars": expected_bars,
            "observed_bars": observed_bars,
            "window_end_ts": round(float(cutoff), 3),
            "latest_available_bar_ts": round(latest_available_bar_ts, 3),
            "matured": matured,
            "data_fingerprint": hashlib.sha256(
                json.dumps(fingerprint_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }
        if window.empty:
            out.append(common)
            continue
        if direction >= 0:
            favorable_price = max(_safe_float(x) for x in window["high"].tolist())
            adverse_price = min(_safe_float(x) for x in window["low"].tolist())
            hit_tp = original_tp > 0 and favorable_price >= original_tp
            hit_sl = original_sl > 0 and adverse_price <= original_sl
        else:
            favorable_price = min(_safe_float(x) for x in window["low"].tolist())
            adverse_price = max(_safe_float(x) for x in window["high"].tolist())
            hit_tp = original_tp > 0 and favorable_price <= original_tp
            hit_sl = original_sl > 0 and adverse_price >= original_sl
        end_close = _safe_float(window.iloc[-1]["close"])
        best_pnl = _pnl_from_price(direction=direction, entry_price=entry_price, price=favorable_price, unit=unit)
        worst_pnl = _pnl_from_price(direction=direction, entry_price=entry_price, price=adverse_price, unit=unit)
        end_pnl = _pnl_from_price(direction=direction, entry_price=entry_price, price=end_close, unit=unit)
        first_hit = _first_original_barrier_hit(
            window=window,
            direction=direction,
            original_tp=original_tp,
            original_sl=original_sl,
        )
        out.append(
            {
                **common,
                "best_pnl": round(best_pnl, 6),
                "worst_pnl": round(worst_pnl, 6),
                "end_pnl": round(end_pnl, 6),
                "best_delta_vs_close": round(best_pnl - close_pnl, 6),
                "worst_delta_vs_close": round(worst_pnl - close_pnl, 6),
                "end_delta_vs_close": round(end_pnl - close_pnl, 6),
                "hit_original_tp": bool(hit_tp),
                "hit_original_sl": bool(hit_sl),
                "first_original_hit": first_hit["first_original_hit"],
                "first_original_hit_ts": round(_safe_float(first_hit["first_original_hit_ts"]), 3),
            }
        )
    return out


def _classify_counterfactual(close_pnl: float, horizons: list[dict[str, Any]], supervisor: dict[str, Any]) -> tuple[str, float, list[str]]:
    horizons = [item for item in horizons if bool(item.get("matured"))]
    if not horizons:
        return "insufficient_future_data", 0.2, ["no_future_bars"]
    max_best_delta = max(_safe_float(item.get("best_delta_vs_close")) for item in horizons)
    min_worst_delta = min(_safe_float(item.get("worst_delta_vs_close")) for item in horizons)
    max_end_delta = max(_safe_float(item.get("end_delta_vs_close")) for item in horizons)
    hit_tp = any(bool(item.get("hit_original_tp")) for item in horizons)
    hit_sl = any(bool(item.get("hit_original_sl")) for item in horizons)
    reason = str(supervisor.get("action_reason") or "")
    tags = []
    ordered_hits = sorted(
        (
            item
            for item in horizons
            if str(item.get("first_original_hit") or "") in {"tp", "sl", "ambiguous"}
            and _safe_float(item.get("first_original_hit_ts")) > 0
        ),
        key=lambda item: _safe_float(item.get("first_original_hit_ts")),
    )
    first_hit = str(ordered_hits[0].get("first_original_hit") or "") if ordered_hits else ""
    if first_hit == "sl":
        tags.extend(["future_worse", "original_sl_first"])
        return "correct_stop", 0.78, tags
    if first_hit == "tp":
        tags.extend(["future_recovered", "original_tp_first"])
        if reason == "thesis_weakening":
            return "premature_tighten", 0.8, tags
        return "protection_too_tight", 0.74, tags
    if first_hit == "ambiguous":
        tags.append("original_barrier_ambiguous")
        return "inconclusive", 0.4, tags
    if (hit_tp and not hit_sl) or max_best_delta >= max(1.0, abs(close_pnl) * 2.0):
        tags.append("future_recovered")
        if reason == "thesis_weakening":
            return "premature_tighten", 0.78, tags
        return "protection_too_tight", 0.72, tags
    if hit_sl or min_worst_delta <= -max(1.0, abs(close_pnl) * 2.0):
        tags.append("future_worse")
        return "correct_stop", 0.76, tags
    if max_end_delta > 0.5 and close_pnl <= 0:
        tags.append("future_mild_recovery")
        return "noise_stopout", 0.58, tags
    if close_pnl <= 0:
        tags.append("entry_or_thesis_failure")
        return "entry_failure_or_correct_stop", 0.54, tags
    tags.append("inconclusive")
    return "inconclusive", 0.35, tags


def ensure_counterfactual_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        if _conn_is_pg(conn):
            if not state_table_exists(conn, "supervisor_counterfactual_review"):
                raise RuntimeError("missing state table: supervisor_counterfactual_review")
            if not state_table_exists(conn, "supervisor_counterfactual_history"):
                raise RuntimeError("missing state table: supervisor_counterfactual_history")
            return
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS supervisor_counterfactual_review (
                counterfactual_id TEXT PRIMARY KEY,
                review_id TEXT DEFAULT '',
                trade_id TEXT DEFAULT '',
                position_id TEXT NOT NULL,
                close_ts REAL NOT NULL DEFAULT 0.0,
                close_reason TEXT DEFAULT '',
                supervisor_event_type TEXT DEFAULT '',
                supervisor_reason TEXT DEFAULT '',
                label TEXT DEFAULT '',
                confidence REAL DEFAULT 0.0,
                horizons_json TEXT DEFAULT '[]',
                evidence_json TEXT DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS supervisor_counterfactual_history (
                history_id TEXT PRIMARY KEY,
                counterfactual_id TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                archived_reason TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        _execute(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_supervisor_counterfactual_position
            ON supervisor_counterfactual_review(position_id, close_ts)
            """
        )
        _execute(
            conn,
            """
            CREATE INDEX IF NOT EXISTS idx_supervisor_counterfactual_label
            ON supervisor_counterfactual_review(label, updated_at)
            """
        )
        conn.commit()
    finally:
        conn.close()


def evaluate_counterfactuals(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 100,
    horizons_minutes: list[int] | None = None,
    materialize: bool = True,
    review_ids: list[str] | None = None,
) -> dict[str, Any]:
    ensure_counterfactual_table(db_path)
    horizons_minutes = list(horizons_minutes or DEFAULT_HORIZONS_MINUTES)
    conn = _connect(db_path)
    try:
        target_review_ids = sorted({str(item) for item in (review_ids or []) if str(item)})
        bounded_limit = max(1, int(limit))
        position_open: dict[str, dict[str, Any]] = {}
        supervisor_by_position: dict[str, list[dict[str, Any]]] = {}
        # Batch-preload canonical streams once per evaluation run; the
        # per-review lookups below are then in-memory (legacy indexed SQL
        # is not available on canonical events, which are keyed by event id).
        for item in iter_position_rows(conn, limit=0):
            if str(item.get("event_type") or "") != "opened":
                continue
            pid = str(item.get("position_id") or "")
            existing = position_open.get(pid)
            if existing is None or _safe_float(item.get("event_ts")) < _safe_float(existing.get("event_ts")):
                position_open[pid] = item
        for decision in iter_decision_rows(conn, limit=0):
            if not str(decision.get("event_type") or "").startswith("supervisor_"):
                continue
            pid = str(decision.get("position_id") or "")
            if pid:
                supervisor_by_position.setdefault(pid, []).append(decision)
        for items in supervisor_by_position.values():
            items.sort(
                key=lambda d: (
                    _safe_float(d.get("decision_ts")),
                    _safe_float(d.get("created_at")),
                ),
                reverse=True,
            )

        def _review_rows():
            if review_ids is not None and not target_review_ids:
                return
            rows = iter_review_rows(conn, limit=0)
            rows.sort(
                key=lambda r: (float(r.get("created_at") or 0.0), str(r.get("review_id") or "")),
                reverse=True,
            )
            if review_ids is not None:
                target = set(target_review_ids)
                rows = [r for r in rows if str(r.get("review_id") or "") in target]
            for row in rows:
                yield row

        candidates = []
        for row in _review_rows():
            review = _review_payload(conn, row)
            if review_has_system_contamination(review):
                continue
            close_reason = str(review.get("close_reason") or "")
            if close_reason not in {
                "broker_close",
                "restart_replay",
                "thesis_broken",
                "supervisor_reduce",
                "supervisor_tighten",
                "profit_giveback_after_mfe",
                "near_stop_loss_preemptive_exit",
                "time_decay_and_low_efficiency",
                "holding_timeout_exceeded",
            }:
                continue
            position_id = str(row["position_id"] or "")
            close_ts = _safe_float(review.get("close_ts") or row["created_at"])
            supervisor = next(
                (
                    item
                    for item in supervisor_by_position.get(position_id, [])
                    if _safe_float(item.get("decision_ts")) <= close_ts + 5.0
                    and _safe_float(item.get("decision_ts")) >= close_ts - 3600.0
                ),
                None,
            )
            if _conn_is_pg(conn):
                conn.commit()
            if not supervisor:
                continue
            supervisor_event = str(supervisor.get("event_type") or "")
            if supervisor_event not in {"supervisor_tighten", "supervisor_reduce", "supervisor_close"}:
                continue
            opened_row = position_open.get(position_id)
            opened = {}
            if opened_row is not None:
                opened = _loads(opened_row.get("details_json"), {})
                opened["event_ts"] = _safe_float(opened_row.get("event_ts"))
            if _conn_is_pg(conn):
                conn.commit()
            direction = int(opened.get("direction") or _direction_from_review(review) or 1)
            real = review.get("real_pnl") or {}
            entry_price = _safe_float(real.get("entry_price") or review.get("entry_price"))
            close_price = _safe_float(review.get("close_price") or real.get("exec_price"))
            close_pnl = _safe_float(row["pnl"] if row["pnl"] is not None else real.get("net"))
            if entry_price <= 0 or close_price <= 0:
                continue
            unit = abs(close_pnl / ((close_price - entry_price) * direction)) if abs(close_price - entry_price) > 1e-9 else 1.0
            if not math.isfinite(unit) or unit <= 0:
                unit = 1.0
            candidates.append(
                {
                    "row": row,
                    "review": review,
                    "position_id": position_id,
                    "close_ts": close_ts,
                    "close_reason": close_reason,
                    "supervisor": supervisor,
                    "supervisor_event": supervisor_event,
                    "opened": opened,
                    "direction": direction,
                    "entry_price": entry_price,
                    "close_price": close_price,
                    "close_pnl": close_pnl,
                    "unit": unit,
                    "symbol": str(review.get("symbol") or "XAUUSD+"),
                    "timeframe": str(review.get("counterfactual_timeframe") or "M1"),
                    "trade_timeframe": str(review.get("timeframe") or "M5"),
                }
            )
            if len(candidates) >= bounded_limit:
                break

        max_horizon = max(horizons_minutes)
        bar_cache = _load_future_bar_cache(candidates, max_horizon)
        items = []
        for candidate in candidates:
            row = candidate["row"]
            review = candidate["review"]
            position_id = candidate["position_id"]
            close_ts = candidate["close_ts"]
            close_reason = candidate["close_reason"]
            supervisor = candidate["supervisor"]
            supervisor_event = candidate["supervisor_event"]
            opened = candidate["opened"]
            direction = candidate["direction"]
            entry_price = candidate["entry_price"]
            close_price = candidate["close_price"]
            close_pnl = candidate["close_pnl"]
            unit = candidate["unit"]
            symbol = candidate["symbol"]
            timeframe = candidate["timeframe"]
            trade_timeframe = candidate["trade_timeframe"]
            bars = _slice_future_bars(bar_cache, symbol, timeframe, close_ts, max_horizon)
            if bars is None and _future_loader_is_patched():
                bars = _load_future_bars(symbol, timeframe, close_ts, max_horizon)
            horizon_items = _horizon_metrics(
                bars=bars,
                direction=direction,
                entry_price=entry_price,
                close_pnl=close_pnl,
                close_ts=close_ts,
                unit=unit,
                horizons_minutes=horizons_minutes,
                original_tp=_safe_float(opened.get("tp")),
                original_sl=_safe_float(opened.get("sl")),
            )
            label, confidence, tags = _classify_counterfactual(close_pnl, horizon_items, supervisor)
            matured_minutes = sorted(
                int(item.get("horizon_minutes") or 0)
                for item in horizon_items
                if bool(item.get("matured"))
            )
            try:
                from config.runtime_config import shared as _runtime_config_shared

                runtime_cfg = _runtime_config_shared()
                governance_horizon = int(
                    getattr(runtime_cfg, "supervisor_counterfactual_governance_horizon_minutes", 60) or 60
                )
                full_horizon = int(
                    getattr(runtime_cfg, "supervisor_counterfactual_full_horizon_minutes", 120) or 120
                )
            except Exception:
                governance_horizon, full_horizon = 60, 120
            max_matured = max(matured_minutes, default=0)
            maturity_status = (
                "fully_matured" if max_matured >= full_horizon
                else "governance_ready" if max_matured >= governance_horizon
                else "partially_matured" if max_matured > 0
                else "pending"
            )
            evidence = {
                "schema_version": "supervisor_counterfactual.v2",
                "direction": direction,
                "entry_price": entry_price,
                "close_price": close_price,
                "close_pnl": close_pnl,
                "original_sl": _safe_float(opened.get("sl")),
                "original_tp": _safe_float(opened.get("tp")),
                "supervisor": supervisor,
                "tags": tags,
                "advisory_only": True,
                "bar_timeframe": timeframe,
                "trade_timeframe": trade_timeframe,
                "session": str(review.get("session") or review.get("market_session") or "unknown"),
                "regime": str(review.get("regime_id") or review.get("regime") or "unknown"),
                "maturity": {
                    "status": maturity_status,
                    "matured_horizons_minutes": matured_minutes,
                    "governance_horizon_minutes": governance_horizon,
                    "full_horizon_minutes": full_horizon,
                    "governance_eligible": maturity_status in {"governance_ready", "fully_matured"},
                },
            }
            counterfactual_id = "scf_" + hashlib.sha1(
                f"{row['review_id']}:{position_id}:{close_ts}".encode("utf-8")
            ).hexdigest()[:16]
            item = {
                "counterfactual_id": counterfactual_id,
                "review_id": str(row["review_id"] or ""),
                "trade_id": str(row["trade_id"] or position_id),
                "position_id": position_id,
                "close_ts": close_ts,
                "close_reason": close_reason,
                "supervisor_event_type": supervisor_event,
                "supervisor_reason": str(supervisor.get("action_reason") or ""),
                "label": label,
                "confidence": round(confidence, 4),
                "horizons": horizon_items,
                "evidence": evidence,
                "maturity_status": maturity_status,
                "governance_eligible": evidence["maturity"]["governance_eligible"],
            }
            items.append(item)
            if materialize:
                now = time.time()
                previous = _execute(
                    conn,
                    "SELECT * FROM supervisor_counterfactual_review WHERE counterfactual_id=?",
                    (item["counterfactual_id"],),
                ).fetchone()
                if previous:
                    previous_payload = dict(previous)
                    previous_evidence = previous_payload.get("evidence_json") or {}
                    if isinstance(previous_evidence, str):
                        previous_evidence = _loads(previous_evidence, {})
                    # Terminal evidence invalidation is an audit decision.  A
                    # later idempotent rematerialization must not resurrect it.
                    if bool((previous_evidence or {}).get("evidence_invalidated")):
                        item["evidence"]["evidence_invalidated"] = True
                        item["evidence"]["invalidation_reason"] = str(
                            (previous_evidence or {}).get("invalidation_reason") or "invalidated_evidence"
                        )
                        item["evidence"]["maturity"]["status"] = "invalidated_missing_m1"
                        item["evidence"]["maturity"]["governance_eligible"] = False
                        item["maturity_status"] = "invalidated_missing_m1"
                        item["governance_eligible"] = False
                    if str((previous_evidence or {}).get("schema_version") or "") != "supervisor_counterfactual.v2":
                        history_id = "scfh_" + hashlib.sha1(
                            f"{item['counterfactual_id']}:{previous_payload.get('updated_at', 0)}".encode("utf-8")
                        ).hexdigest()[:20]
                        _execute(
                            conn,
                            """
                            INSERT INTO supervisor_counterfactual_history
                            (history_id, counterfactual_id, payload_json, archived_reason, created_at)
                            VALUES (?, ?, ?, 'maturity_contract_v2_rematerialization', ?)
                            ON CONFLICT(history_id) DO NOTHING
                            """,
                            (history_id, item["counterfactual_id"], json.dumps(previous_payload, ensure_ascii=False, default=str), now),
                        )
                _execute(
                    conn,
                    """
                    INSERT INTO supervisor_counterfactual_review
                    (counterfactual_id, review_id, trade_id, position_id, close_ts,
                     close_reason, supervisor_event_type, supervisor_reason, label,
                     confidence, horizons_json, evidence_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(counterfactual_id) DO UPDATE SET
                        review_id=excluded.review_id,
                        trade_id=excluded.trade_id,
                        position_id=excluded.position_id,
                        close_ts=excluded.close_ts,
                        close_reason=excluded.close_reason,
                        supervisor_event_type=excluded.supervisor_event_type,
                        supervisor_reason=excluded.supervisor_reason,
                        label=excluded.label,
                        confidence=excluded.confidence,
                        horizons_json=excluded.horizons_json,
                        evidence_json=excluded.evidence_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        item["counterfactual_id"],
                        item["review_id"],
                        item["trade_id"],
                        item["position_id"],
                        item["close_ts"],
                        item["close_reason"],
                        item["supervisor_event_type"],
                        item["supervisor_reason"],
                        item["label"],
                        item["confidence"],
                        json.dumps(item["horizons"], ensure_ascii=False, sort_keys=True),
                        json.dumps(item["evidence"], ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )
        if materialize:
            conn.commit()
        return {
            "schema_version": "supervisor_counterfactual_batch.v1",
            "materialized": bool(materialize),
            "items": items,
            "count": len(items),
            "candidate_count": len(candidates),
            "bar_cache_groups": len(bar_cache),
            "requested_review_count": len(target_review_ids) if review_ids is not None else None,
        }
    finally:
        conn.close()


def list_counterfactuals(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 100,
    position_id: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    ensure_counterfactual_table(db_path)
    clauses = []
    params: list[Any] = []
    if position_id:
        clauses.append("position_id=?")
        params.append(str(position_id))
    if label:
        clauses.append("label=?")
        params.append(str(label))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    conn = _connect(db_path, read_only=True)
    try:
        rows = _execute(
            conn,
            f"""
            SELECT *
            FROM supervisor_counterfactual_review
            {where}
            ORDER BY close_ts DESC
            LIMIT ?
            """,
            (*params, int(limit)),
        ).fetchall()
        items = []
        for row in rows:
            items.append(
                {
                    "counterfactual_id": str(row["counterfactual_id"] or ""),
                    "review_id": str(row["review_id"] or ""),
                    "trade_id": str(row["trade_id"] or ""),
                    "position_id": str(row["position_id"] or ""),
                    "close_ts": _safe_float(row["close_ts"]),
                    "close_reason": str(row["close_reason"] or ""),
                    "supervisor_event_type": str(row["supervisor_event_type"] or ""),
                    "supervisor_reason": str(row["supervisor_reason"] or ""),
                    "label": str(row["label"] or ""),
                    "confidence": _safe_float(row["confidence"]),
                    "horizons": _loads(row["horizons_json"], []),
                    "evidence": _loads(row["evidence_json"], {}),
                    "created_at": _safe_float(row["created_at"]),
                    "updated_at": _safe_float(row["updated_at"]),
                }
            )
        return {"items": items, "count": len(items)}
    finally:
        conn.close()
