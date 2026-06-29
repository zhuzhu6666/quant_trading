from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite


DEFAULT_HORIZONS_MINUTES = [5, 15, 30, 60]


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
    real = review.get("real_pnl") or {}
    entry = _safe_float(real.get("entry_price") or review.get("entry_price"))
    close = _safe_float(review.get("close_price") or real.get("exec_price"))
    pnl = _safe_float(real.get("net") or review.get("pnl"))
    if entry <= 0 or close <= 0 or abs(pnl) < 1e-9:
        return 1
    return 1 if (close - entry) * pnl >= 0 else -1


def _position_open_event(conn, position_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT details_json, event_ts
        FROM position_lifecycle_event
        WHERE position_id=? AND event_type='opened'
        ORDER BY event_ts ASC
        LIMIT 1
        """,
        (str(position_id),),
    ).fetchone()
    if not row:
        return {}
    details = _loads(row["details_json"], {})
    details["event_ts"] = _safe_float(row["event_ts"])
    return details


def _latest_supervisor_before_close(conn, position_id: str, close_ts: float) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT decision_id, event_type, action_reason, action_score, action_json,
               risk_state_json, created_at, decision_ts
        FROM decision_ledger
        WHERE position_id=?
          AND event_type LIKE 'supervisor_%'
          AND decision_ts <= ?
          AND decision_ts >= ?
        ORDER BY decision_ts DESC, created_at DESC
        LIMIT 1
        """,
        (str(position_id), float(close_ts) + 5.0, float(close_ts) - 3600.0),
    ).fetchone()
    if not row:
        return {}
    action = _loads(row["action_json"], {})
    verdict = action.get("supervisor_verdict") or {}
    return {
        "decision_id": str(row["decision_id"] or ""),
        "event_type": str(row["event_type"] or ""),
        "action_reason": str(row["action_reason"] or ""),
        "action_score": _safe_float(row["action_score"]),
        "created_at": _safe_float(row["created_at"]),
        "decision_ts": _safe_float(row["decision_ts"]),
        "verdict": verdict,
        "risk_state": _loads(row["risk_state_json"], {}),
    }


def _load_future_bars(symbol: str, timeframe: str, close_ts: float, max_minutes: int):
    try:
        from data.store import DataStore

        return DataStore().load_bars(
            symbol or "XAUUSD+",
            timeframe or "M5",
            start=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(close_ts)),
            end=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(close_ts + max_minutes * 60 + 300)),
        )
    except Exception:
        return None


def _pnl_from_price(*, direction: int, entry_price: float, price: float, unit: float) -> float:
    if entry_price <= 0 or price <= 0 or unit <= 0:
        return 0.0
    return (price - entry_price) * int(direction or 1) * unit


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
    if "time" in bars.columns:
        try:
            import pandas as pd

            time_values = pd.to_datetime(bars["time"], utc=True, errors="coerce")
            bars = bars.copy()
            bars["_epoch_time"] = (time_values.astype("int64") / 1_000_000_000).astype(float)
        except Exception:
            bars = bars.copy()
            bars["_epoch_time"] = bars["time"].apply(_safe_float)
    else:
        bars = bars.copy()
        bars["_epoch_time"] = float(close_ts)
    out = []
    for minutes in horizons_minutes:
        cutoff = close_ts + int(minutes) * 60
        window = bars[bars["_epoch_time"] <= cutoff]
        if window is None or getattr(window, "empty", True):
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
        out.append(
            {
                "horizon_minutes": int(minutes),
                "best_pnl": round(best_pnl, 6),
                "worst_pnl": round(worst_pnl, 6),
                "end_pnl": round(end_pnl, 6),
                "best_delta_vs_close": round(best_pnl - close_pnl, 6),
                "worst_delta_vs_close": round(worst_pnl - close_pnl, 6),
                "end_delta_vs_close": round(end_pnl - close_pnl, 6),
                "hit_original_tp": bool(hit_tp),
                "hit_original_sl": bool(hit_sl),
            }
        )
    return out


def _classify_counterfactual(close_pnl: float, horizons: list[dict[str, Any]], supervisor: dict[str, Any]) -> tuple[str, float, list[str]]:
    if not horizons:
        return "insufficient_future_data", 0.2, ["no_future_bars"]
    max_best_delta = max(_safe_float(item.get("best_delta_vs_close")) for item in horizons)
    min_worst_delta = min(_safe_float(item.get("worst_delta_vs_close")) for item in horizons)
    max_end_delta = max(_safe_float(item.get("end_delta_vs_close")) for item in horizons)
    hit_tp = any(bool(item.get("hit_original_tp")) for item in horizons)
    hit_sl = any(bool(item.get("hit_original_sl")) for item in horizons)
    reason = str(supervisor.get("action_reason") or "")
    tags = []
    if hit_tp or max_best_delta >= max(1.0, abs(close_pnl) * 2.0):
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
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
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
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_supervisor_counterfactual_position
            ON supervisor_counterfactual_review(position_id, close_ts)
            """
        )
        conn.execute(
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
) -> dict[str, Any]:
    ensure_counterfactual_table(db_path)
    horizons_minutes = list(horizons_minutes or DEFAULT_HORIZONS_MINUTES)
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    try:
        rows = conn.execute(
            """
            SELECT review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
                   pnl, review_json, created_at
            FROM trade_outcome_review
            WHERE created_at > 0
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        items = []
        for row in rows:
            review = _loads(row["review_json"], {})
            close_reason = str(review.get("close_reason") or "")
            if close_reason not in {"broker_close", "restart_replay", "thesis_broken"}:
                continue
            position_id = str(row["position_id"] or "")
            close_ts = _safe_float(review.get("close_ts") or row["created_at"])
            supervisor = _latest_supervisor_before_close(conn, position_id, close_ts)
            if not supervisor:
                continue
            supervisor_event = str(supervisor.get("event_type") or "")
            if supervisor_event not in {"supervisor_tighten", "supervisor_reduce", "supervisor_close"}:
                continue
            opened = _position_open_event(conn, position_id)
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
            bars = _load_future_bars(
                str(review.get("symbol") or "XAUUSD+"),
                str(review.get("timeframe") or "M5"),
                close_ts,
                max(horizons_minutes),
            )
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
            evidence = {
                "schema_version": "supervisor_counterfactual.v1",
                "direction": direction,
                "entry_price": entry_price,
                "close_price": close_price,
                "close_pnl": close_pnl,
                "original_sl": _safe_float(opened.get("sl")),
                "original_tp": _safe_float(opened.get("tp")),
                "supervisor": supervisor,
                "tags": tags,
                "advisory_only": True,
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
            }
            items.append(item)
            if materialize:
                now = time.time()
                conn.execute(
                    """
                    INSERT OR REPLACE INTO supervisor_counterfactual_review
                    (counterfactual_id, review_id, trade_id, position_id, close_ts,
                     close_reason, supervisor_event_type, supervisor_reason, label,
                     confidence, horizons_json, evidence_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(
                        (SELECT created_at FROM supervisor_counterfactual_review WHERE counterfactual_id=?),
                        ?
                    ), ?)
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
                        item["counterfactual_id"],
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
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    try:
        rows = conn.execute(
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
