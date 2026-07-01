from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from typing import Any

from backend.core.db import get_state_pg_conn, state_table_columns
from backend.services.failure_taxonomy import build_failure_taxonomy
from backend.services.position_metrics import update_position_path_metrics
from backend.services.review_contract import normalize_trade_review_contract


logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 100
_DEFAULT_DELAY_SEC = 180.0
_backfill_thread: threading.Thread | None = None


def _connect_state():
    return get_state_pg_conn()


def _conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s")


def _execute(conn, sql: str, params: Any = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), params)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return float(default)


def _timeframe_seconds(timeframe: str) -> int:
    tf = str(timeframe or "").upper()
    mapping = {
        "M1": 60,
        "M5": 300,
        "M10": 600,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
        "H4": 14400,
        "D1": 86400,
    }
    return int(mapping.get(tf, 300))


def _infer_path_metrics_from_bars(
    *,
    symbol: str,
    timeframe: str,
    entry_ts: float,
    close_ts: float,
    entry_price: float,
    close_price: float,
    pnl: float,
) -> dict[str, Any]:
    result = {
        "mfe": max(0.0, pnl),
        "mae": max(0.0, -pnl),
        "giveback_ratio": 0.0,
        "profit_capture_ratio": 0.0,
        "time_in_profit_seconds": 0.0,
        "time_in_profit_ratio": 0.0,
        "holding_efficiency": 0.0,
        "time_decay_score": 0.0,
        "thesis_status": "",
        "regime_shift": "",
        "position_path_state": {},
        "path_source": "",
    }
    if entry_ts <= 0 or close_ts <= entry_ts or entry_price <= 0 or close_price <= 0:
        return result
    try:
        from data.store import DataStore

        bars = DataStore().load_bars(
            symbol or "XAUUSD+",
            timeframe or "M5",
            start=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(max(0.0, entry_ts - _timeframe_seconds(timeframe)))),
            end=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(close_ts + _timeframe_seconds(timeframe))),
        )
    except Exception:
        return result
    if bars is None or getattr(bars, "empty", True):
        return result

    last_bar_close = _safe_float(bars.iloc[-1]["close"] if len(bars.index) else 0.0)
    normalized_close_price = close_price
    if normalized_close_price <= 0:
        normalized_close_price = last_bar_close
    elif entry_price > 0:
        ratio = normalized_close_price / entry_price
        if ratio < 0.2 or ratio > 5.0:
            normalized_close_price = last_bar_close

    price_move = normalized_close_price - entry_price
    if abs(price_move) < 1e-9 or abs(pnl) < 1e-9:
        return result
    pnl_per_price = pnl / price_move
    is_long = pnl_per_price > 0
    tf_seconds = _timeframe_seconds(timeframe)
    favorable_price_move = 0.0
    adverse_price_move = 0.0
    time_in_profit_seconds = 0.0

    for _, bar in bars.iterrows():
        high = _safe_float(bar.get("high"))
        low = _safe_float(bar.get("low"))
        close_bar = _safe_float(bar.get("close"))
        if high <= 0 or low <= 0:
            continue
        if is_long:
            favorable_price_move = max(favorable_price_move, high - entry_price)
            adverse_price_move = max(adverse_price_move, entry_price - low)
            if close_bar > entry_price:
                time_in_profit_seconds += tf_seconds
        else:
            favorable_price_move = max(favorable_price_move, entry_price - low)
            adverse_price_move = max(adverse_price_move, high - entry_price)
            if close_bar < entry_price:
                time_in_profit_seconds += tf_seconds

    estimated_mfe = max(0.0, favorable_price_move * abs(pnl_per_price))
    estimated_mae = max(0.0, adverse_price_move * abs(pnl_per_price))
    holding_seconds = max(0.0, close_ts - entry_ts)
    seed_state = {
        "mfe": estimated_mfe,
        "mae": estimated_mae,
        "time_in_profit_seconds": min(time_in_profit_seconds, holding_seconds),
        "last_observed_ts": close_ts,
        "last_unrealized_pnl": pnl,
        "entry_regime": "",
        "current_regime": "",
        "thesis_status": "intact",
        "regime_shift": "none",
    }
    next_state, metrics = update_position_path_metrics(
        previous_state=seed_state,
        current_pnl=pnl,
        now_ts=close_ts,
        holding_seconds=holding_seconds,
        max_holding_seconds=0.0,
        entry_regime="",
        current_regime="",
    )
    return {
        **metrics,
        "mfe": round(max(estimated_mfe, metrics["mfe"]), 6),
        "mae": round(max(estimated_mae, metrics["mae"]), 6),
        "position_path_state": next_state,
        "path_source": "duckdb_bars",
    }


def classify_outcome(entry_score: float, pnl: float) -> str:
    if pnl > 0:
        return "lucky_win"
    conviction = abs(float(entry_score or 0.0))
    return "bad_loss" if conviction >= 0.55 else "good_loss"


def _infer_close_reason(row: sqlite3.Row, path_metrics: dict[str, Any]) -> str:
    entry_ts = _safe_float(row["entry_ts"] or row["broker_entry_ts"])
    close_ts = _safe_float(row["close_ts"])
    holding_seconds = max(0.0, close_ts - entry_ts) if entry_ts > 0 and close_ts > 0 else 0.0
    if holding_seconds >= 24 * 3600 and _safe_float(path_metrics.get("time_decay_score")) <= 0.35:
        return "holding_timeout"
    if str(path_metrics.get("thesis_status") or "") == "broken" and _safe_float(path_metrics.get("giveback_ratio")) >= 0.5:
        return "profit_giveback_after_mfe"
    return "broker_close"


def _close_reason_source(close_reason: str) -> str:
    if close_reason in {"holding_timeout", "profit_giveback_after_mfe"}:
        return "phase_c_inferred"
    if close_reason == "broker_close":
        return "broker_deal_backfill"
    return "unknown"


def _phase_c_diagnosis(review_json: dict[str, Any]) -> dict[str, Any]:
    holding_seconds = _safe_float(review_json.get("holding_seconds"))
    giveback_ratio = _safe_float(review_json.get("giveback_ratio"))
    profit_capture_ratio = _safe_float(review_json.get("profit_capture_ratio"))
    mfe = _safe_float(review_json.get("mfe"))
    pnl = _safe_float((review_json.get("real_pnl") or {}).get("net"))
    holding_efficiency = _safe_float(review_json.get("holding_efficiency"))
    thesis_status = str(review_json.get("thesis_status") or "")
    close_reason = str(review_json.get("close_reason") or "")
    drivers: list[str] = []
    primary_issue = "unclear"
    if close_reason == "holding_timeout" or holding_seconds >= 24 * 3600 > 0:
        drivers.append("holding_too_long")
        primary_issue = "timing_exit"
    if giveback_ratio >= 0.5 or (mfe > 0 and pnl > 0 and profit_capture_ratio < 0.9):
        drivers.append("profit_giveback")
        primary_issue = "exit_capture"
    if thesis_status == "broken":
        drivers.append("thesis_broken")
        if primary_issue == "unclear":
            primary_issue = "thesis_failure"
    if holding_efficiency < 0.35 and holding_seconds > 0:
        drivers.append("holding_inefficient")
        if primary_issue == "unclear":
            primary_issue = "holding_quality"
    return {
        "primary_issue": primary_issue,
        "drivers": drivers,
        "confidence": round(min(1.0, 0.35 + 0.2 * len(drivers)), 3),
    }


def fetch_missing_positions(
    conn: sqlite3.Connection,
    *,
    limit: int = _DEFAULT_LIMIT,
    require_decision: bool = True,
) -> list[sqlite3.Row]:
    sql = """
    WITH close_positions AS (
        SELECT
            position_id,
            MAX(exec_timestamp) AS close_ts,
            SUM(COALESCE(gross_profit, 0) + COALESCE(swap, 0) - COALESCE(close_commission, 0)) AS net_pnl,
            MAX(entry_price) AS entry_price,
            MAX(exec_price) AS exec_price,
            MAX(balance) AS balance,
            MAX(deal_id) AS deal_id,
            SUM(COALESCE(close_commission, 0)) AS close_commission,
            MAX(gross_profit) AS gross_profit,
            MAX(swap) AS swap,
            MIN(CASE WHEN is_close = 0 THEN exec_timestamp END) AS broker_entry_ts,
            MAX(CASE WHEN is_close = 0 THEN exec_price END) AS broker_entry_price
        FROM ctrader_deals
        GROUP BY position_id
        HAVING MAX(CASE WHEN is_close = 1 THEN 1 ELSE 0 END) = 1
    ),
    missing AS (
        SELECT c.*
        FROM close_positions c
        LEFT JOIN trade_outcome_review r
            ON CAST(r.position_id AS INTEGER) = c.position_id
        WHERE r.position_id IS NULL
    )
    SELECT
        m.position_id,
        m.close_ts,
        m.net_pnl,
        m.entry_price,
        m.exec_price,
        m.balance,
        m.deal_id,
        m.close_commission,
        m.gross_profit,
        m.swap,
        d.decision_id AS entry_decision_id,
        d.trade_id,
        d.regime_id,
        d.action_score AS entry_score,
        d.decision_ts AS entry_ts,
        d.symbol,
        d.timeframe,
        m.broker_entry_ts,
        m.broker_entry_price
    FROM missing m
    LEFT JOIN decision_ledger d
        ON d.position_id = CAST(m.position_id AS TEXT) AND d.event_type = 'open'
    """
    params: list[object] = []
    if require_decision:
        sql += " WHERE d.decision_id IS NOT NULL"
    sql += " ORDER BY m.close_ts DESC LIMIT ?"
    params.append(int(limit))
    try:
        return list(_execute(conn, sql, params).fetchall())
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            logger.warning("[learning_backfill] skipped: required tables missing: %s", exc)
            return []
        raise


def build_review_record(row: sqlite3.Row) -> dict:
    position_id = str(row["position_id"])
    trade_id = str(row["trade_id"] or position_id)
    pnl = float(row["net_pnl"] or 0.0)
    entry_score = float(row["entry_score"] or 0.0)
    decision_entry_ts = float(row["entry_ts"] or 0.0)
    broker_entry_ts = float(row["broker_entry_ts"] or 0.0)
    entry_ts = decision_entry_ts if decision_entry_ts > 0 else broker_entry_ts
    close_ts = float(row["close_ts"] or 0.0)
    holding_seconds = max(0.0, close_ts - entry_ts) if entry_ts > 0 and close_ts > 0 else 0.0
    entry_ts_source = "decision_ledger" if decision_entry_ts > 0 else ("ctrader_deals" if broker_entry_ts > 0 else "")
    entry_price = _safe_float(row["entry_price"] or row["broker_entry_price"])
    close_price = _safe_float(row["exec_price"])
    timeframe = str(row["timeframe"] or "M5")
    symbol = str(row["symbol"] or "XAUUSD+")
    path_metrics = _infer_path_metrics_from_bars(
        symbol=symbol,
        timeframe=timeframe,
        entry_ts=entry_ts,
        close_ts=close_ts,
        entry_price=entry_price,
        close_price=close_price,
        pnl=pnl,
    )
    inferred_close_reason = _infer_close_reason(row, path_metrics)
    outcome_label = classify_outcome(entry_score, pnl)
    summary = (
        f"trade {position_id} closed pnl={pnl:.2f}; "
        f"outcome={outcome_label}; "
        f"primary_factor=n/a; worst_factor=n/a"
    )
    real_pnl = {
        "gross": float(row["gross_profit"] or 0.0),
        "swap": float(row["swap"] or 0.0),
        "commission": float(row["close_commission"] or 0.0),
        "net": pnl,
        "entry_price": float(row["entry_price"] or 0.0),
        "exec_price": float(row["exec_price"] or 0.0),
        "balance": float(row["balance"] or 0.0),
        "deal_id": int(row["deal_id"] or 0),
        "exec_timestamp": float(row["close_ts"] or 0.0),
    }
    review_json = {
        "contract_version": "phase_d.v1",
        "position_id": position_id,
        "trade_id": trade_id,
        "entry_decision_id": str(row["entry_decision_id"] or ""),
        "exit_decision_id": "",
        "entry_ts": entry_ts,
        "entry_ts_source": entry_ts_source,
        "close_ts": close_ts,
        "holding_seconds": round(holding_seconds, 3),
        "holding_minutes": round(holding_seconds / 60.0, 3),
        "timeframe": timeframe,
        "mfe": round(_safe_float(path_metrics.get("mfe"), max(0.0, pnl)), 6),
        "mae": round(_safe_float(path_metrics.get("mae"), max(0.0, -pnl)), 6),
        "giveback_ratio": round(_safe_float(path_metrics.get("giveback_ratio")), 6),
        "profit_capture_ratio": round(_safe_float(path_metrics.get("profit_capture_ratio")), 6),
        "time_in_profit": round(_safe_float(path_metrics.get("time_in_profit_seconds")), 6),
        "time_in_profit_seconds": round(_safe_float(path_metrics.get("time_in_profit_seconds")), 6),
        "time_in_profit_ratio": round(_safe_float(path_metrics.get("time_in_profit_ratio")), 6),
        "holding_efficiency": round(_safe_float(path_metrics.get("holding_efficiency")), 6),
        "time_decay_score": round(_safe_float(path_metrics.get("time_decay_score")), 6),
        "thesis_status": str(path_metrics.get("thesis_status") or ""),
        "thesis_status_at_exit": str(path_metrics.get("thesis_status") or ""),
        "regime_shift": str(path_metrics.get("regime_shift") or ""),
        "regime_shift_at_exit": str(path_metrics.get("regime_shift") or ""),
        "position_path_state": path_metrics.get("position_path_state") or {},
        "path_source": str(path_metrics.get("path_source") or ""),
        "entry_score": entry_score,
        "top_weight_factor": "",
        "top_weight": 0.0,
        "top_factor": "",
        "top_factor_mc": 0.0,
        "worst_factor": "",
        "worst_factor_mc": 0.0,
        "positive_share": 0.0,
        "close_price": float(row["exec_price"] or 0.0),
        "real_pnl": real_pnl,
        "close_reason": inferred_close_reason,
        "close_reason_source": _close_reason_source(inferred_close_reason),
        "context_integrity": "full",
        "failure_tags": [outcome_label],
        "factor_contributions": {},
        "entry_quality": round(0.55 + (0.25 if pnl > 0 else -0.30) * min(abs(entry_score), 1.0), 4),
        "hold_quality": round(0.55 if pnl > 0 else 0.40, 4),
        "exit_quality": 0.55,
        "regime_fit_score": round(0.70 if pnl > 0 else (0.35 + (0.10 if outcome_label == "good_loss" else 0.0)), 4),
        "regime_fit": round(0.70 if pnl > 0 else (0.35 + (0.10 if outcome_label == "good_loss" else 0.0)), 4),
        "execution_quality": 0.60,
    }
    context_integrity = "full" if row["entry_decision_id"] else ("partial" if broker_entry_ts > 0 else "minimal")
    review_json["context_integrity"] = context_integrity
    review_json = normalize_trade_review_contract(
        review_json,
        entry_quality=review_json["entry_quality"],
        hold_quality=review_json["hold_quality"],
        exit_quality=review_json["exit_quality"],
        regime_fit_score=review_json["regime_fit_score"],
        execution_quality=review_json["execution_quality"],
    )
    review_json["phase_c_diagnosis"] = _phase_c_diagnosis(review_json)
    taxonomy = build_failure_taxonomy({**review_json, "pnl": pnl})
    review_json["failure_taxonomy"] = taxonomy
    review_json["primary_responsibility"] = taxonomy["primary_responsibility"]
    review_json["responsibility_labels"] = taxonomy["responsibility_labels"]
    failure_tags = [outcome_label]
    for label in taxonomy["responsibility_labels"]:
        if label not in failure_tags:
            failure_tags.append(label)
    return {
        "review_id": new_id("review"),
        "trade_id": trade_id,
        "position_id": position_id,
        "entry_decision_id": str(row["entry_decision_id"] or ""),
        "exit_decision_id": "",
        "entry_quality": round(0.55 + (0.25 if pnl > 0 else -0.30) * min(abs(entry_score), 1.0), 4),
        "hold_quality": 0.55 if pnl > 0 else 0.40,
        "exit_quality": 0.55,
        "regime_fit_score": 0.70 if pnl > 0 else (0.35 + (0.10 if outcome_label == "good_loss" else 0.0)),
        "execution_quality": 0.60,
        "pnl": round(pnl, 6),
        "mae": round(_safe_float(path_metrics.get("mae"), abs(min(pnl, 0.0))), 6),
        "mfe": round(_safe_float(path_metrics.get("mfe"), max(pnl, 0.0)), 6),
        "outcome_label": outcome_label,
        "failure_tags_json": json.dumps(failure_tags, ensure_ascii=False),
        "summary_text": summary,
        "review_json": json.dumps(review_json, ensure_ascii=False, default=str),
        "created_at": float(row["close_ts"] or time.time()),
    }


def insert_review(conn: sqlite3.Connection, record: dict) -> None:
    _execute(conn,
        """
        INSERT INTO trade_outcome_review
        (review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
         entry_quality, hold_quality, exit_quality, regime_fit_score,
         execution_quality, pnl, mae, mfe, outcome_label,
         failure_tags_json, summary_text, review_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["review_id"],
            record["trade_id"],
            record["position_id"],
            record["entry_decision_id"],
            record["exit_decision_id"],
            record["entry_quality"],
            record["hold_quality"],
            record["exit_quality"],
            record["regime_fit_score"],
            record["execution_quality"],
            record["pnl"],
            record["mae"],
            record["mfe"],
            record["outcome_label"],
            record["failure_tags_json"],
            record["summary_text"],
            record["review_json"],
            record["created_at"],
        ),
    )


def _ensure_experience_memory_source_columns(conn: sqlite3.Connection) -> None:
    cols = state_table_columns(conn, "experience_memory")
    migrations = {
        "source_table": "ALTER TABLE experience_memory ADD COLUMN source_table TEXT DEFAULT ''",
        "source_id": "ALTER TABLE experience_memory ADD COLUMN source_id TEXT DEFAULT ''",
        "append_source": "ALTER TABLE experience_memory ADD COLUMN append_source TEXT DEFAULT ''",
        "evolution_run_id": "ALTER TABLE experience_memory ADD COLUMN evolution_run_id TEXT DEFAULT ''",
    }
    for name, ddl in migrations.items():
        if name not in cols:
            _execute(conn, ddl)
    _execute(conn,
        """
        CREATE INDEX IF NOT EXISTS idx_experience_memory_source
        ON experience_memory(source_table, source_id, append_source)
        """
    )


def _stable_experience_id(append_source: str, source_table: str, source_id: str) -> str:
    digest = hashlib.sha1(f"{append_source}:{source_table}:{source_id}".encode("utf-8")).hexdigest()[:18]
    return f"exp_{digest}"


def rebuild_learning_state(conn: sqlite3.Connection) -> tuple[int, int]:
    _ensure_experience_memory_source_columns(conn)
    _execute(conn, "DELETE FROM experience_memory WHERE append_source='learning_backfill.v1'")
    _execute(conn, "DELETE FROM experience_pattern_stats WHERE scope_type='factor'")
    reviews = _execute(conn,
        """
        SELECT review_id, trade_id, position_id, outcome_label, pnl, failure_tags_json,
               summary_text, review_json, created_at
        FROM trade_outcome_review
        ORDER BY created_at ASC
        """
    ).fetchall()
    stats: dict[str, dict] = {}
    suggestions_created = 0
    rebuilt = 0
    now = time.time()

    for row in reviews:
        review_json = json.loads(row["review_json"] or "{}")
        failure_tags = list(json.loads(row["failure_tags_json"] or "[]"))
        outcome_label = str(row["outcome_label"] or "")
        pnl = float(row["pnl"] or 0.0)
        close_reason = str(review_json.get("close_reason") or "")
        context_integrity = str(review_json.get("context_integrity", "full") or "full")
        top_weight_factor = str(review_json.get("top_weight_factor") or "")
        top_factor = str(review_json.get("top_factor") or "")
        worst_factor = str(review_json.get("worst_factor") or "")

        def actionable(name: str) -> bool:
            return bool(name) and not name.startswith("dsl_auto_")

        if outcome_label in {"bad_loss", "good_loss"}:
            primary_factor = worst_factor if actionable(worst_factor) else (top_weight_factor or top_factor or worst_factor)
        else:
            primary_factor = top_weight_factor or top_factor or worst_factor
            if not actionable(primary_factor):
                primary_factor = top_weight_factor or top_factor or worst_factor

        reward_score = 0.0
        if pnl > 0:
            reward_score = min(1.0, pnl / max(abs(pnl), 50.0))
        elif pnl < 0:
            reward_score = -min(1.0, abs(pnl) / max(abs(pnl), 50.0))
        reward_scale = 1.0
        evidence_scale = 1.0
        if context_integrity != "full":
            reward_scale *= 0.5
            evidence_scale *= 0.35
        if close_reason in {"emergency_close", "restart_replay"}:
            reward_scale *= 0.6
            evidence_scale *= 0.5
        reward_score *= reward_scale

        if context_integrity != "full" and "partial_context" not in failure_tags:
            failure_tags.append("partial_context")
        if close_reason == "emergency_close" and "manual_intervention" not in failure_tags:
            failure_tags.append("manual_intervention")
        if close_reason == "restart_replay" and "restart_replay" not in failure_tags:
            failure_tags.append("restart_replay")

        recommended_action = "downweight" if outcome_label == "bad_loss" else "watch"
        if context_integrity != "full" or close_reason in {"emergency_close", "restart_replay"}:
            recommended_action = "watch"
        evidence_strength = min(1.0, max(0.15, abs(reward_score) + 0.20 * len(failure_tags)))
        evidence_strength = max(0.05, evidence_strength * evidence_scale)

        setup_hash = hashlib.sha1(f"|{primary_factor}|{outcome_label}".encode("utf-8")).hexdigest()[:16]
        source_table = "trade_outcome_review"
        source_id = str(row["review_id"] or row["trade_id"] or row["position_id"] or "")
        append_source = "learning_backfill.v1"
        experience_id = _stable_experience_id(append_source, source_table, source_id)
        context = {
            "position_id": str(row["position_id"] or ""),
            "trade_id": str(row["trade_id"] or ""),
            "experience_source": {
                "source_table": source_table,
                "source_id": source_id,
                "append_source": append_source,
            },
            "primary_factor": primary_factor,
            "failure_tags": failure_tags,
            "close_reason": close_reason,
            "context_integrity": context_integrity,
            "summary_text": str(row["summary_text"] or ""),
            "review_json": review_json,
        }
        _execute(conn,
            """
            INSERT INTO experience_memory
            (experience_id, trade_id, source_table, source_id, append_source,
             regime_id, setup_hash, decision_context_json,
             outcome_label, reward_score, failure_tags_json, recommended_action,
             evidence_strength, artifact_version, evolution_run_id, created_at)
            VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, 'v1', '', ?)
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
                str(row["trade_id"] or ""),
                source_table,
                source_id,
                append_source,
                setup_hash,
                json.dumps(context, ensure_ascii=False),
                outcome_label,
                round(reward_score, 6),
                json.dumps(failure_tags, ensure_ascii=False),
                recommended_action,
                round(evidence_strength, 6),
                now,
            ),
        )
        rebuilt += 1

        if not primary_factor:
            continue

        stat = stats.get(primary_factor, {"sample_count": 0, "win_count": 0, "bad_loss_count": 0, "avg_reward": 0.0})
        stat["sample_count"] += 1
        stat["win_count"] += 1 if reward_score > 0 else 0
        stat["bad_loss_count"] += 1 if outcome_label == "bad_loss" else 0
        prev_avg = stat["avg_reward"]
        stat["avg_reward"] = prev_avg + (reward_score - prev_avg) / stat["sample_count"]
        stats[primary_factor] = stat

        sample_count = stat["sample_count"]
        avg_reward = stat["avg_reward"]
        bad_loss_count = stat["bad_loss_count"]
        win_count = stat["win_count"]
        if sample_count >= 3 and avg_reward <= -0.20:
            action = "downweight"
            confidence = min(0.95, 0.45 + 0.08 * sample_count + 0.10 * bad_loss_count)
            reason = f"factor {primary_factor} shows repeated negative outcomes ({sample_count} samples)"
        elif sample_count >= 4 and win_count >= 3 and avg_reward >= 0.22:
            action = "boost_small"
            confidence = min(0.85, 0.40 + 0.05 * sample_count)
            reason = f"factor {primary_factor} shows stable positive outcomes ({sample_count} samples)"
        else:
            action = "watch"
            confidence = 0.0
            reason = f"factor {primary_factor} still accumulating evidence"

        _execute(conn,
            """
            INSERT INTO experience_pattern_stats
            (scope_type, scope_key, sample_count, win_count, bad_loss_count,
             avg_reward, last_outcome_label, recommended_action, updated_at)
            VALUES ('factor', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_type, scope_key) DO UPDATE SET
                sample_count=excluded.sample_count,
                win_count=excluded.win_count,
                bad_loss_count=excluded.bad_loss_count,
                avg_reward=excluded.avg_reward,
                last_outcome_label=excluded.last_outcome_label,
                recommended_action=excluded.recommended_action,
                updated_at=excluded.updated_at
            """,
            (
                primary_factor,
                sample_count,
                win_count,
                bad_loss_count,
                round(avg_reward, 6),
                outcome_label,
                action,
                now,
            ),
        )

        if action != "watch":
            existing = _execute(conn,
                """
                SELECT suggestion_id
                FROM policy_suggestion
                WHERE scope_type='factor' AND scope_key=? AND action=? AND status='proposed'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (primary_factor, action),
            ).fetchone()
            evidence = {
                "source": "learning_backfill.v1",
                "source_table": source_table,
                "source_id": source_id,
                "append_source": append_source,
                "sample_count": sample_count,
                "win_count": win_count,
                "bad_loss_count": bad_loss_count,
                "avg_reward": round(avg_reward, 6),
                "experience_id": experience_id,
                "failure_tags": failure_tags,
            }
            if existing:
                _execute(conn,
                    """
                    UPDATE policy_suggestion
                    SET confidence=?, reason=?, evidence_json=?, created_at=?
                    WHERE suggestion_id=?
                    """,
                    (
                        round(confidence, 6),
                        reason,
                        json.dumps(evidence, ensure_ascii=False),
                        now,
                        str(existing["suggestion_id"]),
                    ),
                )
            else:
                _execute(conn,
                    """
                    INSERT INTO policy_suggestion
                    (suggestion_id, scope_type, scope_key, action, confidence, reason,
                     evidence_json, status, created_at)
                    VALUES (?, 'factor', ?, ?, ?, ?, ?, 'proposed', ?)
                    """,
                    (
                        new_id("psg"),
                        primary_factor,
                        action,
                        round(confidence, 6),
                        reason,
                        json.dumps(evidence, ensure_ascii=False),
                        now,
                    ),
                )
                suggestions_created += 1

    return rebuilt, suggestions_created


def run_learning_backfill(
    *,
    limit: int = _DEFAULT_LIMIT,
    allow_partial: bool = False,
    rebuild_learning: bool = True,
) -> dict:
    conn = _connect_state()
    try:
        rows = fetch_missing_positions(conn, limit=limit, require_decision=not allow_partial)
        inserted = []
        for row in rows:
            record = build_review_record(row)
            insert_review(conn, record)
            inserted.append(
                {
                    "position_id": record["position_id"],
                    "trade_id": record["trade_id"],
                    "outcome_label": record["outcome_label"],
                    "pnl": record["pnl"],
                }
            )
        rebuilt = 0
        suggestions = 0
        if rebuild_learning and inserted:
            rebuilt, suggestions = rebuild_learning_state(conn)
        conn.commit()
        result = {
            "inserted_reviews": inserted,
            "inserted_count": len(inserted),
            "rebuild_reviews": rebuilt,
            "rebuild_suggestions": suggestions,
            "require_decision": not allow_partial,
        }
        if inserted:
            logger.info("[learning_backfill] inserted %d missing reviews", len(inserted))
        return result
    finally:
        conn.close()


def schedule_learning_backfill(
    *,
    delay_sec: float = _DEFAULT_DELAY_SEC,
    limit: int = _DEFAULT_LIMIT,
    allow_partial: bool = False,
    rebuild_learning: bool = True,
) -> bool:
    global _backfill_thread
    if _backfill_thread is not None and _backfill_thread.is_alive():
        return False

    def _worker() -> None:
        time.sleep(max(0.0, delay_sec))
        try:
            result = run_learning_backfill(
                limit=limit,
                allow_partial=allow_partial,
                rebuild_learning=rebuild_learning,
            )
            logger.info("[learning_backfill] scheduled run completed: %s", result)
        except Exception as exc:
            logger.warning("[learning_backfill] scheduled run failed: %s", exc)

    _backfill_thread = threading.Thread(
        target=_worker,
        name="learning_backfill_startup",
        daemon=True,
    )
    _backfill_thread.start()
    return True
