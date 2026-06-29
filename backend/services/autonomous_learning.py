from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger

from backend.core.db import STATE_DB, connect_sqlite
from backend.services.evolution_ledger import (
    ensure_evolution_columns,
    ensure_evolution_ledger_tables,
    finish_evolution_run,
    record_evolution_decision,
    start_evolution_run,
)
from research.features.evidence_contract import build_evidence_contract

_scheduler_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _sample_id(sample_type: str, source_table: str, source_id: str) -> str:
    digest = hashlib.sha1(f"{sample_type}:{source_table}:{source_id}".encode("utf-8")).hexdigest()[:18]
    return f"als_{digest}"


def _sample_causal_level(sample_type: str, label_status: str, requested: Any = None) -> str:
    if label_status != "matured" and sample_type in {"supervisor_trajectory", "supervisor_execution_trace"}:
        return "observational"
    if requested:
        return str(requested)
    if sample_type == "post_close_counterfactual":
        return "counterfactual"
    return "intervention_observed"


def _sample_integrity_level(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"full", "recovered", "partial", "missing"}:
        return text
    return "missing"


def ensure_autonomous_learning_tables(db_path: str | Path = STATE_DB) -> None:
    ensure_evolution_ledger_tables(db_path)
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evolution_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS autonomous_learning_sample (
                sample_id TEXT PRIMARY KEY,
                sample_type TEXT NOT NULL,
                source_table TEXT DEFAULT '',
                source_id TEXT DEFAULT '',
                decision_id TEXT DEFAULT '',
                trade_id TEXT DEFAULT '',
                position_id TEXT DEFAULT '',
                symbol TEXT DEFAULT '',
                timeframe TEXT DEFAULT '',
                event_ts REAL NOT NULL DEFAULT 0.0,
                label_status TEXT DEFAULT 'pending',
                integrity TEXT DEFAULT 'full',
                train_weight REAL DEFAULT 1.0,
                features_json TEXT DEFAULT '{}',
                verdict_json TEXT DEFAULT '{}',
                label_json TEXT DEFAULT '{}',
                trace_json TEXT DEFAULT '{}',
                evidence_contract_json TEXT DEFAULT '{}',
                config_version INTEGER DEFAULT 0,
                config_hash TEXT DEFAULT '',
                evolution_run_id TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS position_supervisor_trace (
                trace_id TEXT PRIMARY KEY,
                decision_id TEXT DEFAULT '',
                position_id TEXT NOT NULL,
                trade_id TEXT DEFAULT '',
                symbol TEXT DEFAULT '',
                timeframe TEXT DEFAULT '',
                tick INTEGER DEFAULT 0,
                event_ts REAL NOT NULL DEFAULT 0.0,
                action TEXT DEFAULT '',
                summary_reason TEXT DEFAULT '',
                confidence REAL DEFAULT 0.0,
                template_id TEXT DEFAULT '',
                template_version TEXT DEFAULT '',
                stage TEXT DEFAULT '',
                outcome TEXT DEFAULT '',
                risk_action TEXT DEFAULT '',
                risk_allowed INTEGER DEFAULT 0,
                risk_reason TEXT DEFAULT '',
                execution_status TEXT DEFAULT '',
                execution_reason TEXT DEFAULT '',
                context_json TEXT DEFAULT '{}',
                verdict_json TEXT DEFAULT '{}',
                risk_verdict_json TEXT DEFAULT '{}',
                execution_json TEXT DEFAULT '{}',
                trace_integrity TEXT DEFAULT 'full',
                config_version INTEGER DEFAULT 0,
                config_hash TEXT DEFAULT '',
                evolution_run_id TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(autonomous_learning_sample)").fetchall()}
        if "evidence_contract_json" not in cols:
            conn.execute("ALTER TABLE autonomous_learning_sample ADD COLUMN evidence_contract_json TEXT DEFAULT '{}'")
        if "config_version" not in cols:
            conn.execute("ALTER TABLE autonomous_learning_sample ADD COLUMN config_version INTEGER DEFAULT 0")
        if "config_hash" not in cols:
            conn.execute("ALTER TABLE autonomous_learning_sample ADD COLUMN config_hash TEXT DEFAULT ''")
        if "evolution_run_id" not in cols:
            conn.execute("ALTER TABLE autonomous_learning_sample ADD COLUMN evolution_run_id TEXT DEFAULT ''")
        trace_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(position_supervisor_trace)").fetchall()}
        if "trace_integrity" not in trace_cols:
            conn.execute("ALTER TABLE position_supervisor_trace ADD COLUMN trace_integrity TEXT DEFAULT 'full'")
        if "config_version" not in trace_cols:
            conn.execute("ALTER TABLE position_supervisor_trace ADD COLUMN config_version INTEGER DEFAULT 0")
        if "config_hash" not in trace_cols:
            conn.execute("ALTER TABLE position_supervisor_trace ADD COLUMN config_hash TEXT DEFAULT ''")
        if "evolution_run_id" not in trace_cols:
            conn.execute("ALTER TABLE position_supervisor_trace ADD COLUMN evolution_run_id TEXT DEFAULT ''")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_autonomous_learning_sample_type
            ON autonomous_learning_sample(sample_type, label_status, event_ts)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_autonomous_learning_sample_source
            ON autonomous_learning_sample(sample_type, source_table, source_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_position_supervisor_trace_position_ts
            ON position_supervisor_trace(position_id, event_ts)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_position_supervisor_trace_action_outcome
            ON position_supervisor_trace(action, outcome, event_ts)
            """
        )
        conn.commit()
    finally:
        conn.close()
    ensure_evolution_columns(db_path)


def _upsert_sample(conn, item: dict[str, Any]) -> bool:
    now = time.time()
    sample_type = str(item.get("sample_type") or "")
    source_table = str(item.get("source_table") or "")
    source_id = str(item.get("source_id") or "")
    if not sample_type or not source_table or not source_id:
        return False
    sample_id = str(item.get("sample_id") or _sample_id(sample_type, source_table, source_id))
    features = item.get("features") or {}
    verdict = item.get("verdict") or {}
    label = item.get("label") or {}
    trace = item.get("trace") or {}
    integrity = _sample_integrity_level(item.get("integrity") or "full")
    label_status = str(item.get("label_status") or "pending")
    train_weight = float(item.get("train_weight") if item.get("train_weight") is not None else 1.0)
    causal_level = _sample_causal_level(sample_type, label_status, item.get("causal_level"))
    snapshot = item.get("runtime_config") or {}
    config_version = int(item.get("config_version") or (snapshot or {}).get("config_version") or 0)
    config_hash = str(item.get("config_hash") or (snapshot or {}).get("config_hash") or "")
    evolution_run_id = str(item.get("evolution_run_id") or "")
    existing = conn.execute(
        """
        SELECT label_status
        FROM autonomous_learning_sample
        WHERE sample_id=?
        LIMIT 1
        """,
        (sample_id,),
    ).fetchone()
    if existing is not None:
        try:
            existing_label_status = str(existing["label_status"] or "")
        except Exception:
            existing_label_status = str(existing[0] or "")
        if existing_label_status == "matured" and label_status != "matured":
            return False
    evidence_contract = build_evidence_contract(
        sample_id=sample_id,
        sample_kind=sample_type,
        source={"table": source_table, "source_id": source_id},
        features=features,
        label=label,
        trace=trace,
        quality={
            "quality_score": max(0.0, min(1.0, train_weight)),
            "model_ready": (
                label_status == "matured"
                and integrity in {"full", "recovered"}
                and bool(features)
                and bool(label)
                and bool(trace)
            ),
            "missing": [],
        },
        integrity=integrity,
        causal_level=causal_level,
        label_status=label_status,
        explanation={"verdict": verdict},
    )
    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO autonomous_learning_sample
        (sample_id, sample_type, source_table, source_id, decision_id, trade_id,
         position_id, symbol, timeframe, event_ts, label_status, integrity,
         train_weight, features_json, verdict_json, label_json, trace_json,
         evidence_contract_json, config_version, config_hash, evolution_run_id,
         created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sample_id) DO UPDATE SET
            decision_id=excluded.decision_id,
            trade_id=excluded.trade_id,
            position_id=excluded.position_id,
            symbol=excluded.symbol,
            timeframe=excluded.timeframe,
            event_ts=excluded.event_ts,
            label_status=excluded.label_status,
            integrity=excluded.integrity,
            train_weight=excluded.train_weight,
            features_json=excluded.features_json,
            verdict_json=excluded.verdict_json,
            label_json=excluded.label_json,
            trace_json=excluded.trace_json,
            evidence_contract_json=excluded.evidence_contract_json,
            config_version=excluded.config_version,
            config_hash=excluded.config_hash,
            evolution_run_id=excluded.evolution_run_id,
            updated_at=excluded.updated_at
        """,
        (
            sample_id,
            sample_type,
            source_table,
            source_id,
            str(item.get("decision_id") or ""),
            str(item.get("trade_id") or ""),
            str(item.get("position_id") or ""),
            str(item.get("symbol") or ""),
            str(item.get("timeframe") or ""),
            float(item.get("event_ts") or 0.0),
            label_status,
            integrity,
            train_weight,
            _dumps(features),
            _dumps(verdict),
            _dumps(label),
            _dumps(trace),
            _dumps(evidence_contract),
            config_version,
            config_hash,
            evolution_run_id,
            now,
            now,
        ),
    )
    return conn.total_changes > before


def _insert_evolution_event(conn, event_type: str, payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO evolution_events (timestamp, event_type, payload_json)
        VALUES (?, ?, ?)
        """,
        (time.time(), event_type, _dumps(payload)),
    )


def _autonomy_mode() -> str:
    try:
        from config.runtime_config import shared as runtime_config

        cfg = runtime_config()
        if not bool(getattr(cfg, "autonomy_demo_auto_apply", True)):
            return "manual"
        return str(getattr(cfg, "autonomy_mode", "") or "manual")
    except Exception:
        return "manual"


def _demo_autonomous_enabled() -> bool:
    return _autonomy_mode() == "demo_autonomous"


def _new_experiment_id(prefix: str = "demoauto") -> str:
    return f"{prefix}_{int(time.time())}_{hashlib.sha1(str(time.time()).encode('utf-8')).hexdigest()[:8]}"


def _risk_rejection_label(action_json: dict[str, Any]) -> tuple[str, float]:
    skip_stage = str(action_json.get("skip_stage") or "")
    market_session = action_json.get("market_session") or {}
    session_status = str(market_session.get("status") or "")
    if skip_stage == "market_session":
        if session_status in {"closed_confirmed", "closed_pending_confirmation", "quote_stale"}:
            return "invalid", 0.0
        return "matured", 0.25
    return "matured", 1.0


def _sample_from_decision(row: Any, sample_type: str) -> dict[str, Any]:
    action_json = _loads(row["action_json"], {})
    risk_state = _loads(row["risk_state_json"], {})
    portfolio = _loads(row["portfolio_state_json"], {})
    risk_verdict = (
        risk_state.get("policy_verdict")
        or action_json.get("risk_verdict")
        or {}
    )
    label_status = "pending"
    train_weight = 0.5
    label = {
        "event_type": str(row["event_type"] or ""),
        "action_reason": str(row["action_reason"] or ""),
    }
    if sample_type == "risk_rejection":
        label_status, train_weight = _risk_rejection_label(action_json)
        label.update(
            {
                "label": "rejected_open",
                "skip_stage": str(action_json.get("skip_stage") or ""),
                "allowed": False,
            }
        )
    elif sample_type == "shadow_open_decision":
        if str(row["event_type"] or "") == "open":
            label["label"] = "opened"
            train_weight = 0.7
        else:
            label["label"] = "not_opened"
            train_weight = 0.35
    elif sample_type == "supervisor_trajectory":
        verdict = action_json.get("supervisor_verdict") or {}
        label["label"] = str(verdict.get("action") or row["event_type"] or "")
        label["summary_reason"] = str(verdict.get("summary_reason") or row["action_reason"] or "")
        train_weight = 0.6
    return {
        "sample_type": sample_type,
        "source_table": "decision_ledger",
        "source_id": str(row["decision_id"] or ""),
        "decision_id": str(row["decision_id"] or ""),
        "trade_id": str(row["trade_id"] or ""),
        "position_id": str(row["position_id"] or ""),
        "symbol": str(row["symbol"] or ""),
        "timeframe": str(row["timeframe"] or ""),
        "event_ts": float(row["decision_ts"] or row["created_at"] or 0.0),
        "label_status": label_status,
        "integrity": "full" if risk_verdict or action_json else "partial",
        "train_weight": train_weight,
        "causal_level": "intervention_observed",
        "features": {
            "portfolio_state": portfolio,
            "risk_state": risk_state,
            "action": action_json,
            "regime_id": str(row["regime_id"] or ""),
            "regime_confidence": float(row["regime_confidence"] or 0.0),
            "action_score": float(row["action_score"] or 0.0),
        },
        "verdict": {
            "risk_verdict": risk_verdict,
            "event_type": str(row["event_type"] or ""),
        },
        "label": label,
        "trace": {
            "decision_id": str(row["decision_id"] or ""),
            "position_id": str(row["position_id"] or ""),
            "trade_id": str(row["trade_id"] or ""),
        },
    }


def _sample_from_review(row: Any) -> dict[str, Any]:
    review = _loads(row["review_json"], {})
    integrity = _sample_integrity_level(review.get("attribution_integrity") or review.get("context_integrity") or "missing")
    train_weight = 1.0
    if integrity == "missing":
        train_weight = 0.0
    elif integrity in {"partial", "recovered"}:
        train_weight = 0.5
    return {
        "sample_type": "trade_review_outcome",
        "source_table": "trade_outcome_review",
        "source_id": str(row["review_id"] or ""),
        "decision_id": str(row["exit_decision_id"] or row["entry_decision_id"] or ""),
        "trade_id": str(row["trade_id"] or ""),
        "position_id": str(row["position_id"] or ""),
        "symbol": str(review.get("symbol") or ""),
        "timeframe": str(review.get("timeframe") or ""),
        "event_ts": float(review.get("close_ts") or row["created_at"] or 0.0),
        "label_status": "matured",
        "integrity": integrity,
        "train_weight": train_weight,
        "causal_level": "intervention_observed",
        "features": {
            "entry_quality": float(row["entry_quality"] or 0.0),
            "hold_quality": float(row["hold_quality"] or 0.0),
            "exit_quality": float(row["exit_quality"] or 0.0),
            "regime_fit_score": float(row["regime_fit_score"] or 0.0),
            "execution_quality": float(row["execution_quality"] or 0.0),
            "mae": float(row["mae"] or 0.0),
            "mfe": float(row["mfe"] or 0.0),
            "review": review,
        },
        "verdict": {
            "close_reason_source": review.get("close_reason_source") or "",
            "inferred_close_supervisor": review.get("inferred_close_supervisor") or {},
        },
        "label": {
            "outcome_label": str(row["outcome_label"] or ""),
            "pnl": float(row["pnl"] or 0.0),
            "failure_tags": _loads(row["failure_tags_json"], []),
        },
        "trace": {
            "review_id": str(row["review_id"] or ""),
            "entry_decision_id": str(row["entry_decision_id"] or ""),
            "exit_decision_id": str(row["exit_decision_id"] or ""),
            "position_id": str(row["position_id"] or ""),
        },
    }


def _sample_from_counterfactual(row: Any) -> dict[str, Any]:
    label = str(row["label"] or "")
    confidence = float(row["confidence"] or 0.0)
    label_status = "pending" if label == "insufficient_future_data" else "matured"
    if label in {"", "insufficient_future_data"} and confidence <= 0.25:
        label_status = "invalid"
    return {
        "sample_type": "post_close_counterfactual",
        "source_table": "supervisor_counterfactual_review",
        "source_id": str(row["counterfactual_id"] or ""),
        "trade_id": str(row["trade_id"] or ""),
        "position_id": str(row["position_id"] or ""),
        "event_ts": float(row["close_ts"] or row["updated_at"] or 0.0),
        "label_status": label_status,
        "integrity": "full" if label_status == "matured" else "partial",
        "train_weight": max(0.0, min(1.0, confidence)),
        "causal_level": "counterfactual",
        "features": {
            "close_reason": str(row["close_reason"] or ""),
            "supervisor_event_type": str(row["supervisor_event_type"] or ""),
            "supervisor_reason": str(row["supervisor_reason"] or ""),
            "horizons": _loads(row["horizons_json"], []),
            "evidence": _loads(row["evidence_json"], {}),
        },
        "verdict": {
            "counterfactual_label": label,
            "confidence": confidence,
        },
        "label": {
            "label": label,
            "confidence": confidence,
        },
        "trace": {
            "counterfactual_id": str(row["counterfactual_id"] or ""),
            "review_id": str(row["review_id"] or ""),
            "position_id": str(row["position_id"] or ""),
        },
    }


def _sample_from_supervisor_trace(row: Any) -> dict[str, Any]:
    verdict = _loads(row["verdict_json"], {})
    context = _loads(row["context_json"], {})
    risk_verdict = _loads(row["risk_verdict_json"], {})
    execution = _loads(row["execution_json"], {})
    outcome = str(row["outcome"] or "")
    execution_status = str(row["execution_status"] or "")
    label_status = "pending"
    train_weight = 0.35
    if outcome in {"blocked", "skipped", "failed"}:
        train_weight = 0.45
    if outcome == "hold":
        train_weight = 0.25
    return {
        "sample_type": "supervisor_execution_trace",
        "source_table": "position_supervisor_trace",
        "source_id": str(row["trace_id"] or ""),
        "decision_id": str(row["decision_id"] or ""),
        "trade_id": str(row["trade_id"] or ""),
        "position_id": str(row["position_id"] or ""),
        "symbol": str(row["symbol"] or ""),
        "timeframe": str(row["timeframe"] or ""),
        "event_ts": float(row["event_ts"] or row["created_at"] or 0.0),
        "label_status": label_status,
        "integrity": "full" if verdict else "partial",
        "train_weight": train_weight,
        "causal_level": "intervention_observed",
        "features": {
            "context": context,
            "verdict": verdict,
            "risk_verdict": risk_verdict,
            "execution": execution,
            "action": str(row["action"] or ""),
            "summary_reason": str(row["summary_reason"] or ""),
            "confidence": float(row["confidence"] or 0.0),
            "template_id": str(row["template_id"] or ""),
            "template_version": str(row["template_version"] or ""),
            "stage": str(row["stage"] or ""),
            "outcome": outcome,
            "risk_action": str(row["risk_action"] or ""),
            "risk_allowed": bool(row["risk_allowed"]),
            "risk_reason": str(row["risk_reason"] or ""),
            "execution_status": execution_status,
            "execution_reason": str(row["execution_reason"] or ""),
        },
        "verdict": {
            "supervisor_action": str(row["action"] or ""),
            "summary_reason": str(row["summary_reason"] or ""),
            "risk_allowed": bool(row["risk_allowed"]),
            "execution_status": execution_status,
        },
        "label": {
            "label": outcome or execution_status or str(row["action"] or ""),
            "stage": str(row["stage"] or ""),
            "execution_status": execution_status,
        },
        "trace": {
            "trace_id": str(row["trace_id"] or ""),
            "decision_id": str(row["decision_id"] or ""),
            "position_id": str(row["position_id"] or ""),
            "trade_id": str(row["trade_id"] or ""),
        },
    }


def _supervisor_label_from_counterfactual(label: str) -> tuple[str, str, str, float]:
    key = str(label or "").strip()
    if key in {"protection_too_tight", "premature_tighten", "noise_stopout"}:
        return "matured", "over_protected", "less_tighten", 0.85
    if key == "correct_stop":
        return "matured", "correct_action", "close", 0.9
    if key in {"entry_failure_or_correct_stop"}:
        return "matured", "correct_action", "hold", 0.65
    if key in {"missed_protection"}:
        return "matured", "missed_protection", "tighten", 0.8
    return "pending", "inconclusive", "hold", 0.2


def _matured_sample_from_supervisor_trace(row: Any, cf_row: Any | None, *, run_context: dict[str, Any]) -> dict[str, Any]:
    base = _sample_from_supervisor_trace(row)
    cf_label = str(cf_row["label"] or "") if cf_row is not None else ""
    label_status, unified_label, recommended_action, weight = _supervisor_label_from_counterfactual(cf_label)
    confidence = float(cf_row["confidence"] or 0.0) if cf_row is not None else 0.0
    integrity = str(row["trace_integrity"] or base["integrity"] or "partial")
    if integrity == "missing":
        weight = 0.0
    elif integrity in {"partial", "recovered"}:
        weight *= 0.5
    base.update(
        {
            "label_status": label_status,
            "integrity": integrity,
            "train_weight": round(max(0.0, min(1.0, weight * max(confidence, 0.5))), 6),
            "causal_level": "intervention_observed" if label_status == "matured" else "observational",
            "label": {
                "label": unified_label,
                "recommended_action": recommended_action,
                "counterfactual_label": cf_label,
                "counterfactual_confidence": confidence,
                "source": "supervisor_counterfactual_review" if cf_row is not None else "pending_future_evidence",
            },
            "verdict": {
                **(base.get("verdict") or {}),
                "learning_label": unified_label,
                "recommended_action": recommended_action,
                "counterfactual_label": cf_label,
            },
            "trace": {
                **(base.get("trace") or {}),
                "counterfactual_id": str(cf_row["counterfactual_id"] or "") if cf_row is not None else "",
                "trace_integrity": integrity,
            },
            **run_context,
        }
    )
    return base


def backfill_position_supervisor_traces(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 1000,
) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(run_type="position_supervisor_trace_backfill", trigger_source="decision_ledger", db_path=db_path)
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    inserted = 0
    skipped = 0
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM decision_ledger
            WHERE event_type IN ('supervisor_close', 'supervisor_reduce', 'supervisor_tighten')
              AND NOT EXISTS (
                  SELECT 1 FROM position_supervisor_trace t
                  WHERE t.decision_id = decision_ledger.decision_id
              )
            ORDER BY decision_ts DESC, created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        for row in rows:
            action_json = _loads(row["action_json"], {})
            verdict = action_json.get("supervisor_verdict") or action_json
            event_type = str(row["event_type"] or "")
            action = str(verdict.get("action") or event_type.replace("supervisor_", "") or "")
            trace_id = "psvtrace_legacy_" + hashlib.sha1(str(row["decision_id"] or "").encode("utf-8")).hexdigest()[:16]
            integrity = "recovered" if verdict else "partial"
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO position_supervisor_trace
                (trace_id, decision_id, position_id, trade_id, symbol, timeframe,
                 tick, event_ts, action, summary_reason, confidence, template_id,
                 template_version, stage, outcome, risk_action, risk_allowed,
                 risk_reason, execution_status, execution_reason, context_json,
                 verdict_json, risk_verdict_json, execution_json, trace_integrity,
                 config_version, config_hash, evolution_run_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 'legacy_backfill',
                        'legacy_recovered', '', 0, '', 'unknown', 'legacy decision_ledger backfill',
                        ?, ?, '{}', '{}', ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    str(row["decision_id"] or ""),
                    str(row["position_id"] or ""),
                    str(row["trade_id"] or ""),
                    str(row["symbol"] or ""),
                    str(row["timeframe"] or ""),
                    float(row["decision_ts"] or row["created_at"] or 0.0),
                    action,
                    str(verdict.get("summary_reason") or row["action_reason"] or ""),
                    float(verdict.get("confidence", row["action_score"] or 0.0) or 0.0),
                    str((verdict.get("supervisor_template") or {}).get("template_id") or ""),
                    str((verdict.get("supervisor_template") or {}).get("template_version") or ""),
                    _dumps({"legacy_action": action_json, "event_type": event_type}),
                    _dumps(verdict),
                    integrity,
                    int(run.get("config_version") or 0),
                    str(run.get("config_hash") or ""),
                    str(run.get("run_id") or ""),
                    time.time(),
                ),
            )
            if conn.total_changes > before:
                inserted += 1
            else:
                skipped += 1
        payload = {
            "schema_version": "position_supervisor_trace_backfill.v1",
            "evolution_run_id": str(run.get("run_id") or ""),
            "inserted": inserted,
            "skipped": skipped,
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "position_supervisor_trace_backfill", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="backfill_traces",
            scope_type="position_supervisor_trace",
            action="legacy_backfill",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    finally:
        conn.close()


def mature_position_supervisor_traces(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 500,
) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(run_type="position_supervisor_trace_maturation", trigger_source="counterfactual_review", db_path=db_path)
    run_context = {
        "config_version": int(run.get("config_version") or 0),
        "config_hash": str(run.get("config_hash") or ""),
        "evolution_run_id": str(run.get("run_id") or ""),
    }
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    matured = 0
    pending = 0
    try:
        traces = conn.execute(
            """
            SELECT *
            FROM position_supervisor_trace
            WHERE action IN ('close', 'reduce', 'tighten')
               OR action LIKE 'supervisor_%'
               OR stage LIKE '%execut%'
               OR outcome IN ('executed', 'legacy_recovered')
            ORDER BY event_ts DESC, created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        for trace in traces:
            cf = conn.execute(
                """
                SELECT *
                FROM supervisor_counterfactual_review
                WHERE position_id=?
                  AND close_ts >= ?
                ORDER BY updated_at DESC, close_ts ASC
                LIMIT 1
                """,
                (str(trace["position_id"] or ""), float(trace["event_ts"] or 0.0)),
            ).fetchone()
            item = _matured_sample_from_supervisor_trace(trace, cf, run_context=run_context)
            if _upsert_sample(conn, item):
                if item["label_status"] == "matured":
                    matured += 1
                else:
                    pending += 1
        payload = {
            "schema_version": "position_supervisor_trace_maturation.v1",
            "evolution_run_id": str(run.get("run_id") or ""),
            "matured": matured,
            "pending": pending,
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "position_supervisor_trace_maturation", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="mature_traces",
            scope_type="supervisor_execution_trace",
            action="materialize_labels",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    finally:
        conn.close()


def materialize_autonomous_learning_samples(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 500,
) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(run_type="autonomous_learning_samples", trigger_source="materialize", db_path=db_path)
    sample_context = {
        "config_version": int(run.get("config_version") or 0),
        "config_hash": str(run.get("config_hash") or ""),
        "evolution_run_id": str(run.get("run_id") or ""),
    }
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    counts = {
        "shadow_open_decision": 0,
        "risk_rejection": 0,
        "supervisor_trajectory": 0,
        "supervisor_execution_trace": 0,
        "trade_review_outcome": 0,
        "post_close_counterfactual": 0,
    }
    try:
        decisions = conn.execute(
            """
            SELECT *
            FROM decision_ledger
            WHERE event_type IN ('open', 'skip') OR event_type LIKE 'supervisor_%'
            ORDER BY decision_ts DESC, created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        for row in decisions:
            event_type = str(row["event_type"] or "")
            if event_type in {"open", "skip"}:
                if _upsert_sample(conn, {**_sample_from_decision(row, "shadow_open_decision"), **sample_context}):
                    counts["shadow_open_decision"] += 1
            if event_type == "skip":
                action_json = _loads(row["action_json"], {})
                if str(action_json.get("skip_stage") or "") in {"risk_policy", "market_session"}:
                    if _upsert_sample(conn, {**_sample_from_decision(row, "risk_rejection"), **sample_context}):
                        counts["risk_rejection"] += 1
            if event_type.startswith("supervisor_"):
                if _upsert_sample(conn, {**_sample_from_decision(row, "supervisor_trajectory"), **sample_context}):
                    counts["supervisor_trajectory"] += 1

        trace_exists = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='position_supervisor_trace'
            """
        ).fetchone()
        if trace_exists:
            traces = conn.execute(
                """
                SELECT *
                FROM position_supervisor_trace
                ORDER BY event_ts DESC, created_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            for row in traces:
                if _upsert_sample(conn, {**_sample_from_supervisor_trace(row), **sample_context}):
                    counts["supervisor_execution_trace"] += 1

        reviews = conn.execute(
            """
            SELECT *
            FROM trade_outcome_review
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        for row in reviews:
            if _upsert_sample(conn, {**_sample_from_review(row), **sample_context}):
                counts["trade_review_outcome"] += 1

        exists = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='supervisor_counterfactual_review'
            """
        ).fetchone()
        if exists:
            cfs = conn.execute(
                """
                SELECT *
                FROM supervisor_counterfactual_review
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            for row in cfs:
                if _upsert_sample(conn, {**_sample_from_counterfactual(row), **sample_context}):
                    counts["post_close_counterfactual"] += 1

        payload = {
            "schema_version": "autonomous_learning_samples.v1",
            "evolution_run_id": str(run.get("run_id") or ""),
            "config_version": int(run.get("config_version") or 0),
            "config_hash": str(run.get("config_hash") or ""),
            "counts": counts,
            "total_changed": sum(counts.values()),
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "autonomous_learning_samples", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="materialize_samples",
            scope_type="autonomous_learning_sample",
            action="upsert_samples",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    finally:
        conn.close()


def list_autonomous_learning_samples(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 100,
    sample_type: str | None = None,
    label_status: str | None = None,
    position_id: str | None = None,
) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    clauses = []
    params: list[Any] = []
    if sample_type:
        clauses.append("sample_type=?")
        params.append(str(sample_type))
    if label_status:
        clauses.append("label_status=?")
        params.append(str(label_status))
    if position_id:
        clauses.append("position_id=?")
        params.append(str(position_id))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    try:
        rows = conn.execute(
            f"""
            SELECT *
            FROM autonomous_learning_sample
            {where}
            ORDER BY event_ts DESC, updated_at DESC
            LIMIT ?
            """,
            (*params, int(limit)),
        ).fetchall()
        items = []
        for row in rows:
            items.append(
                {
                    "sample_id": str(row["sample_id"] or ""),
                    "sample_type": str(row["sample_type"] or ""),
                    "source_table": str(row["source_table"] or ""),
                    "source_id": str(row["source_id"] or ""),
                    "decision_id": str(row["decision_id"] or ""),
                    "trade_id": str(row["trade_id"] or ""),
                    "position_id": str(row["position_id"] or ""),
                    "symbol": str(row["symbol"] or ""),
                    "timeframe": str(row["timeframe"] or ""),
                    "event_ts": float(row["event_ts"] or 0.0),
                    "label_status": str(row["label_status"] or ""),
                    "integrity": str(row["integrity"] or ""),
                    "train_weight": float(row["train_weight"] or 0.0),
                    "config_version": int(row["config_version"] or 0) if "config_version" in row.keys() else 0,
                    "config_hash": str(row["config_hash"] or "") if "config_hash" in row.keys() else "",
                    "evolution_run_id": str(row["evolution_run_id"] or "") if "evolution_run_id" in row.keys() else "",
                    "features": _loads(row["features_json"], {}),
                    "verdict": _loads(row["verdict_json"], {}),
                    "label": _loads(row["label_json"], {}),
                    "trace": _loads(row["trace_json"], {}),
                    "evidence_contract": _loads(row["evidence_contract_json"], {}),
                    "created_at": float(row["created_at"] or 0.0),
                    "updated_at": float(row["updated_at"] or 0.0),
                }
            )
        return {"items": items, "count": len(items)}
    finally:
        conn.close()


def _rebuilt_evidence_contract_from_sample(row: Any) -> dict[str, Any]:
    features = _loads(row["features_json"], {})
    label = _loads(row["label_json"], {})
    trace = _loads(row["trace_json"], {})
    verdict = _loads(row["verdict_json"], {})
    label_status = str(row["label_status"] or "pending")
    integrity = _sample_integrity_level(row["integrity"] or "missing")
    train_weight = float(row["train_weight"] if row["train_weight"] is not None else 0.0)
    sample_type = str(row["sample_type"] or "")
    causal_level = _sample_causal_level(sample_type, label_status)
    return build_evidence_contract(
        sample_id=str(row["sample_id"] or ""),
        sample_kind=sample_type,
        source={"table": str(row["source_table"] or ""), "source_id": str(row["source_id"] or "")},
        features=features,
        label=label,
        trace=trace,
        quality={
            "quality_score": max(0.0, min(1.0, train_weight)),
            "model_ready": (
                label_status == "matured"
                and integrity in {"full", "recovered"}
                and bool(features)
                and bool(label)
                and bool(trace)
            ),
            "missing": [],
        },
        integrity=integrity,
        causal_level=causal_level,
        label_status=label_status,
        explanation={"verdict": verdict},
    )


def validate_evidence_contract_health(*, db_path: str | Path = STATE_DB, limit: int = 10000) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    counts = {
        "checked": 0,
        "non_matured_allows_supervised_training": 0,
        "model_ready_without_supervised_training": 0,
        "model_ready_non_matured": 0,
        "model_ready_missing_or_incomplete": 0,
        "parse_errors": 0,
    }
    examples: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            """
            SELECT sample_id, sample_type, label_status, integrity,
                   features_json, label_json, trace_json, evidence_contract_json
            FROM autonomous_learning_sample
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        for row in rows:
            counts["checked"] += 1
            try:
                contract = json.loads(row["evidence_contract_json"] or "{}")
            except Exception:
                contract = {}
                counts["parse_errors"] += 1
            allowed = set(contract.get("allowed_uses") or [])
            model_ready = bool(contract.get("model_ready"))
            label_status = str(row["label_status"] or "")
            integrity = str(row["integrity"] or "")
            complete = bool(_loads(row["features_json"], {})) and bool(_loads(row["label_json"], {})) and bool(_loads(row["trace_json"], {}))
            bad_codes = []
            if label_status != "matured" and "supervised_training" in allowed:
                counts["non_matured_allows_supervised_training"] += 1
                bad_codes.append("non_matured_allows_supervised_training")
            if model_ready and "supervised_training" not in allowed:
                counts["model_ready_without_supervised_training"] += 1
                bad_codes.append("model_ready_without_supervised_training")
            if model_ready and label_status != "matured":
                counts["model_ready_non_matured"] += 1
                bad_codes.append("model_ready_non_matured")
            if model_ready and (integrity == "missing" or not complete):
                counts["model_ready_missing_or_incomplete"] += 1
                bad_codes.append("model_ready_missing_or_incomplete")
            if bad_codes and len(examples) < 10:
                examples.append(
                    {
                        "sample_id": str(row["sample_id"] or ""),
                        "sample_type": str(row["sample_type"] or ""),
                        "label_status": label_status,
                        "integrity": integrity,
                        "codes": bad_codes,
                    }
                )
        counts["bad_total"] = sum(
            counts[key]
            for key in (
                "non_matured_allows_supervised_training",
                "model_ready_without_supervised_training",
                "model_ready_non_matured",
                "model_ready_missing_or_incomplete",
                "parse_errors",
            )
        )
        return {"schema_version": "evidence_contract_health.v1", "counts": counts, "examples": examples}
    finally:
        conn.close()


def repair_evidence_contracts(*, db_path: str | Path = STATE_DB, limit: int = 10000) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(run_type="evidence_contract_repair", trigger_source="contract_health", db_path=db_path)
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    checked = 0
    repaired = 0
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM autonomous_learning_sample
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        now = time.time()
        for row in rows:
            checked += 1
            rebuilt = _rebuilt_evidence_contract_from_sample(row)
            rebuilt_json = _dumps(rebuilt)
            if rebuilt_json == str(row["evidence_contract_json"] or "{}"):
                continue
            conn.execute(
                """
                UPDATE autonomous_learning_sample
                SET evidence_contract_json=?, updated_at=?
                WHERE sample_id=?
                """,
                (rebuilt_json, now, str(row["sample_id"] or "")),
            )
            repaired += 1
        payload = {
            "schema_version": "evidence_contract_repair.v1",
            "evolution_run_id": str(run.get("run_id") or ""),
            "checked": checked,
            "repaired": repaired,
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "evidence_contract_repair", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="repair_evidence_contracts",
            scope_type="autonomous_learning_sample",
            action="rebuild_contract_json",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    finally:
        conn.close()


def _latest_protection_evidence_before_close(
    conn: Any,
    *,
    position_id: str,
    close_ts: float,
    lookback_sec: float = 3600.0,
) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    lower = float(close_ts or time.time()) - max(1.0, float(lookback_sec or 0.0))
    upper = float(close_ts or time.time())
    try:
        row = conn.execute(
            """
            SELECT decision_id, event_type, action_reason, action_json, risk_state_json, decision_ts
            FROM decision_ledger
            WHERE position_id=?
              AND (
                  event_type LIKE 'supervisor_%'
                  OR event_type IN ('legacy_awe_trailing', 'holding_timeout')
              )
              AND decision_ts <= ?
              AND decision_ts >= ?
            ORDER BY decision_ts DESC
            LIMIT 1
            """,
            (str(position_id), upper, lower),
        ).fetchone()
    except Exception:
        row = None
    if row:
        action_json = _loads(row["action_json"], {})
        risk_state = _loads(row["risk_state_json"], {})
        verdict = action_json.get("supervisor_verdict") or {}
        latest = {
            "decision_id": str(row["decision_id"] or ""),
            "event_type": str(row["event_type"] or ""),
            "action_reason": str(row["action_reason"] or ""),
            "decision_ts": float(row["decision_ts"] or 0.0),
            "seconds_before_close": round(max(0.0, upper - float(row["decision_ts"] or 0.0)), 3),
            "action": str(verdict.get("action") or "").strip(),
            "summary_reason": str(verdict.get("summary_reason") or row["action_reason"] or ""),
            "evidence": verdict.get("evidence") or {},
            "recommended_controls": verdict.get("recommended_controls") or {},
            "risk_state": risk_state,
            "source_table": "decision_ledger",
        }
    try:
        trace = conn.execute(
            """
            SELECT trace_id, decision_id, action, summary_reason, event_ts,
                   verdict_json, risk_verdict_json, execution_json, stage, outcome
            FROM position_supervisor_trace
            WHERE position_id=?
              AND event_ts <= ?
              AND event_ts >= ?
              AND action IN ('tighten', 'reduce', 'close')
            ORDER BY event_ts DESC
            LIMIT 1
            """,
            (str(position_id), upper, lower),
        ).fetchone()
    except Exception:
        trace = None
    if trace and (not latest or float(trace["event_ts"] or 0.0) > float(latest.get("decision_ts") or 0.0)):
        verdict = _loads(trace["verdict_json"], {})
        risk_state = _loads(trace["risk_verdict_json"], {})
        execution = _loads(trace["execution_json"], {})
        evidence = verdict.get("evidence") or {}
        source = str(evidence.get("protection_source") or "")
        action = str(trace["action"] or "")
        if source == "legacy_awe_trailing":
            event_type = "legacy_awe_trailing"
        elif source == "holding_timeout":
            event_type = "holding_timeout"
        else:
            event_type = f"supervisor_{action}" if action else "position_supervisor_trace"
        latest = {
            "decision_id": str(trace["decision_id"] or ""),
            "trace_id": str(trace["trace_id"] or ""),
            "event_type": event_type,
            "action_reason": str(trace["summary_reason"] or ""),
            "decision_ts": float(trace["event_ts"] or 0.0),
            "seconds_before_close": round(max(0.0, upper - float(trace["event_ts"] or 0.0)), 3),
            "action": action,
            "summary_reason": str(trace["summary_reason"] or ""),
            "evidence": evidence,
            "recommended_controls": verdict.get("recommended_controls") or {},
            "risk_state": risk_state,
            "execution": execution,
            "stage": str(trace["stage"] or ""),
            "outcome": str(trace["outcome"] or ""),
            "source_table": "position_supervisor_trace",
        }
    return latest


def _classify_review_close_source_from_evidence(close_reason: str, latest: dict[str, Any]) -> str:
    reason = str(close_reason or "")
    if reason == "restart_replay":
        return "restart_replay"
    if latest:
        event_type = str(latest.get("event_type") or "")
        if reason not in {"broker_close", "restart_replay"} and event_type == "supervisor_close":
            return "supervisor_direct_close"
        if reason == "broker_close" and event_type == "supervisor_tighten":
            return "supervisor_tighten_stopout"
        if reason == "broker_close" and event_type == "supervisor_reduce":
            return "supervisor_reduce_partial_or_stopout"
        if reason == "broker_close" and event_type == "supervisor_close":
            return "supervisor_direct_close"
        if reason == "broker_close" and event_type == "legacy_awe_trailing":
            return "legacy_awe_trailing_stopout"
        if reason == "broker_close" and event_type == "holding_timeout":
            return "holding_timeout"
    if reason == "broker_close":
        return "external_broker_close"
    return "unknown_legacy"


def backfill_trade_review_close_sources(*, db_path: str | Path = STATE_DB, limit: int = 10000) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(run_type="trade_review_close_source_backfill", trigger_source="review_contract_health", db_path=db_path)
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    checked = 0
    updated = 0
    by_source: dict[str, int] = {}
    try:
        rows = conn.execute(
            """
            SELECT review_id, position_id, review_json, created_at
            FROM trade_outcome_review
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        now = time.time()
        for row in rows:
            checked += 1
            review = _loads(row["review_json"], {})
            if str(review.get("close_reason_source") or "").strip():
                continue
            position_id = str(row["position_id"] or review.get("position_id") or "")
            if not position_id:
                continue
            close_ts = float(review.get("close_ts") or row["created_at"] or 0.0)
            close_reason = str(review.get("close_reason") or "broker_close")
            latest = _latest_protection_evidence_before_close(conn, position_id=position_id, close_ts=close_ts)
            source = _classify_review_close_source_from_evidence(close_reason, latest)
            review["close_reason_source"] = source
            review["inferred_close_supervisor"] = latest
            review["close_reason_source_backfill"] = {
                "schema_version": "close_reason_source_backfill.v1",
                "backfilled_at": now,
                "method": "decision_ledger_or_position_supervisor_trace" if latest else "conservative_no_system_evidence",
            }
            conn.execute(
                """
                UPDATE trade_outcome_review
                SET review_json=?
                WHERE review_id=?
                """,
                (_dumps(review), str(row["review_id"] or "")),
            )
            updated += 1
            by_source[source] = by_source.get(source, 0) + 1
        payload = {
            "schema_version": "trade_review_close_source_backfill.v1",
            "evolution_run_id": str(run.get("run_id") or ""),
            "checked": checked,
            "updated": updated,
            "by_source": by_source,
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "trade_review_close_source_backfill", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="backfill_close_sources",
            scope_type="trade_outcome_review",
            action="infer_missing_close_reason_source",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    finally:
        conn.close()


def backfill_trade_review_integrity_markers(*, db_path: str | Path = STATE_DB, limit: int = 10000) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    run = start_evolution_run(run_type="trade_review_integrity_backfill", trigger_source="review_contract_health", db_path=db_path)
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    checked = 0
    updated = 0
    try:
        rows = conn.execute(
            """
            SELECT review_id, review_json
            FROM trade_outcome_review
            WHERE COALESCE(json_extract(review_json, '$.attribution_integrity'), json_extract(review_json, '$.context_integrity'), '') = ''
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        now = time.time()
        for row in rows:
            checked += 1
            review = _loads(row["review_json"], {})
            review["attribution_integrity"] = "missing"
            review["context_integrity"] = "missing"
            review["integrity_backfill"] = {
                "schema_version": "trade_review_integrity_backfill.v1",
                "backfilled_at": now,
                "reason": "legacy_review_missing_integrity_marker",
            }
            conn.execute(
                """
                UPDATE trade_outcome_review
                SET review_json=?
                WHERE review_id=?
                """,
                (_dumps(review), str(row["review_id"] or "")),
            )
            updated += 1
        payload = {
            "schema_version": "trade_review_integrity_backfill.v1",
            "evolution_run_id": str(run.get("run_id") or ""),
            "checked": checked,
            "updated": updated,
            "limit": int(limit),
        }
        _insert_evolution_event(conn, "trade_review_integrity_backfill", payload)
        conn.commit()
        record_evolution_decision(
            run_id=str(run.get("run_id") or ""),
            decision_type="backfill_review_integrity",
            scope_type="trade_outcome_review",
            action="mark_missing_legacy_integrity",
            status="completed",
            result=payload,
            db_path=db_path,
        )
        finish_evolution_run(str(run.get("run_id") or ""), status="completed", summary=payload, db_path=db_path)
        return payload
    finally:
        conn.close()


def _recommendation_already_materialized(conn, recommendation_id: str) -> bool:
    needle = f"%{recommendation_id}%"
    checks = [
        (
            """
            SELECT 1 FROM policy_suggestion
            WHERE evidence_json LIKE ?
            LIMIT 1
            """,
            (needle,),
        ),
        (
            """
            SELECT 1 FROM parameter_template_release_candidate
            WHERE validation_summary_json LIKE ?
            LIMIT 1
            """,
            (needle,),
        ),
        (
            """
            SELECT 1 FROM jobs
            WHERE kind='parameter_template_validation'
              AND params_json LIKE ?
              AND status IN ('pending','running','done')
            LIMIT 1
            """,
            (needle,),
        ),
    ]
    for sql, params in checks:
        try:
            if conn.execute(sql, params).fetchone():
                return True
        except Exception:
            continue
    return False


def _offline_deep_auto_submit_allowed() -> tuple[bool, str]:
    try:
        from backend.services import live_service

        session = live_service._live_state_get("market_session", {}, clone=True) or {}
        if not session:
            session = live_service._market_session_snapshot(None)
        allowed, reason = live_service._offmarket_high_load_allowed(session)
        return bool(allowed), str(reason or "")
    except Exception as exc:
        return False, f"market_session_unavailable:{exc}"


def materialize_parameter_template_recommendations(
    *,
    db_path: str | Path = STATE_DB,
    limit: int = 20,
    submit_offline_deep: bool = True,
) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    from backend.jobs import get_job_manager
    from backend.services.parameter_template_validation import run_parameter_template_offline_validation
    from backend.services.parameter_templates import ParameterTemplateService

    service = ParameterTemplateService(str(db_path))
    recommendations = service.list_recommendations(limit=limit)
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    counts = {"suggested": 0, "offline_jobs": 0, "skipped_existing": 0, "skipped_offmarket": 0, "errors": 0}
    items: list[dict[str, Any]] = []
    try:
        for recommendation in recommendations:
            recommendation_id = str(recommendation.get("recommendation_id") or "")
            if not recommendation_id:
                continue
            if _recommendation_already_materialized(conn, recommendation_id):
                counts["skipped_existing"] += 1
                continue
            try:
                action = str(recommendation.get("recommended_action") or "")
                if action == "offline_validate":
                    if not submit_offline_deep:
                        counts["skipped_offmarket"] += 1
                        items.append({"recommendation_id": recommendation_id, "mode": "offline_validate", "skipped": "offline_deep_disabled"})
                        continue
                    allowed, reason = _offline_deep_auto_submit_allowed()
                    if not allowed:
                        counts["skipped_offmarket"] += 1
                        items.append({"recommendation_id": recommendation_id, "mode": "offline_validate", "skipped": reason})
                        continue
                    boundary = dict(recommendation.get("boundary") or {})
                    params = {
                        "factor_id": str(recommendation.get("factor_id") or ""),
                        "template_id": str(recommendation.get("target_template_id") or ""),
                        "regime_key": str(recommendation.get("regime_key") or ""),
                        "recommended_scope": boundary.get("recommended_scope"),
                        "boundary_reasons": list(boundary.get("reasons") or []),
                        "recommendation_context": {
                            "source": "autonomous_learning",
                            "recommendation_id": recommendation_id,
                            "reason": recommendation.get("reason", ""),
                            "responsibility": dict(recommendation.get("responsibility") or {}),
                            "approval_path": recommendation.get("approval_path", ""),
                        },
                    }
                    fn = lambda cb, _params=params: run_parameter_template_offline_validation(_params, cb)
                    js = get_job_manager().submit("parameter_template_validation", params, fn)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO jobs
                        (id, kind, status, params_json, result_json, progress, error, created_at, updated_at)
                        VALUES (?, 'parameter_template_validation', 'pending', ?, '{}', 0.0, '', ?, ?)
                        """,
                        (js.id, _dumps(params), time.time(), time.time()),
                    )
                    counts["offline_jobs"] += 1
                    items.append({"recommendation_id": recommendation_id, "mode": "offline_validate", "job_id": js.id})
                else:
                    result = service.create_suggestion_from_recommendation(
                        recommendation_id=recommendation_id,
                        note="autonomous materialize from parameter template recommendation",
                    )
                    counts["suggested"] += 1
                    items.append(
                        {
                            "recommendation_id": recommendation_id,
                            "mode": "suggest_switch",
                            "suggestion_id": ((result.get("item") or {}).get("suggestion_id") or ""),
                        }
                    )
            except Exception as exc:
                counts["errors"] += 1
                items.append({"recommendation_id": recommendation_id, "error": str(exc)})
        payload = {
            "schema_version": "parameter_template_auto_materialize.v1",
            "counts": counts,
            "items": items,
        }
        _insert_evolution_event(conn, "parameter_template_auto_materialize", payload)
        conn.commit()
        return payload
    finally:
        conn.close()


def _approve_demo_policy_suggestions(
    conn,
    *,
    experiment_id: str,
    limit: int = 200,
    db_path: str | Path = STATE_DB,
    run_id: str = "",
) -> dict[str, Any]:
    allowed_scopes = {"factor", "parameter_template", "position_supervisor_template"}
    allowed_actions = {
        "boost_small",
        "downweight",
        "switch_parameter_template",
        "relax_thesis_break",
        "tighten_profit_protection",
        "increase_min_hold_window",
        "fix_stop_legality",
        "switch_position_supervisor_template",
    }
    rows = conn.execute(
        """
        SELECT *
        FROM policy_suggestion
        WHERE status='proposed'
        ORDER BY created_at ASC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    approved = []
    skipped = []
    now = time.time()
    for row in rows:
        scope_type = str(row["scope_type"] or "")
        action = str(row["action"] or "")
        suggestion_id = str(row["suggestion_id"] or "")
        if scope_type not in allowed_scopes or action not in allowed_actions:
            skipped.append({"suggestion_id": suggestion_id, "reason": "not_demo_autonomy_whitelisted"})
            continue
        evidence = _loads(row["evidence_json"], {})
        if scope_type == "position_supervisor_template":
            has_replay = bool(evidence.get("replay_summary") or evidence.get("replay") or evidence.get("day"))
            has_counterfactual = bool(evidence.get("counterfactual_summary") or evidence.get("counterfactual"))
            if not (has_replay and has_counterfactual):
                skipped.append({"suggestion_id": suggestion_id, "reason": "missing_supervisor_switch_evidence"})
                continue
        conn.execute(
            """
            UPDATE policy_suggestion
            SET status='approved', reviewed_at=?, review_note=?
            WHERE suggestion_id=? AND status='proposed'
            """,
            (
                now,
                f"auto-approved by demo_autonomous experiment {experiment_id}",
                suggestion_id,
            ),
        )
        conn.commit()
        record_evolution_decision(
            run_id=run_id,
            decision_type="demo_auto_approve",
            scope_type=scope_type,
            scope_key=str(row["scope_key"] or ""),
            action=action,
            status="approved",
            evidence=evidence,
            before={"status": "proposed", "suggestion_id": suggestion_id},
            after={"status": "approved", "suggestion_id": suggestion_id},
            result={"experiment_id": experiment_id},
            db_path=db_path,
        )
        approved.append(
            {
                "suggestion_id": suggestion_id,
                "scope_type": scope_type,
                "scope_key": str(row["scope_key"] or ""),
                "action": action,
            }
        )
    return {"approved": approved, "skipped": skipped}


def _sync_factor_weights_for_demo(*, experiment_id: str) -> dict[str, Any]:
    try:
        verdict = __import__("risk.policy_service", fromlist=["RiskPolicyService"]).RiskPolicyService.shared().evaluate(
            "update_weight",
            {
                "required_mode": "governed",
                "governance": {
                    "experiment_id": experiment_id,
                    "autonomy_mode": "demo_autonomous",
                },
            },
        ).to_dict()
        if not verdict.get("allowed", False):
            return {"synced": False, "blocked": True, "risk_verdict": verdict}
        from backend.runtime.evolution_orchestrator import _update_weights

        return {"synced": bool(_update_weights()), "blocked": False, "risk_verdict": verdict}
    except Exception as exc:
        return {"synced": False, "blocked": False, "error": str(exc)}


def _auto_apply_parameter_template_suggestions(
    *,
    db_path: str | Path,
    experiment_id: str,
    limit: int = 100,
) -> dict[str, Any]:
    from backend.services.parameter_templates import ParameterTemplateService

    service = ParameterTemplateService(str(db_path))
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    applied = []
    skipped = []
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM policy_suggestion
            WHERE status='approved'
              AND scope_type='parameter_template'
              AND action='switch_parameter_template'
            ORDER BY reviewed_at DESC, created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        suggestion_id = str(row["suggestion_id"] or "")
        evidence = _loads(row["evidence_json"], {})
        target_template_id = str(evidence.get("target_template_id") or "")
        factor_id = str(evidence.get("factor_id") or "")
        regime_key = str(evidence.get("regime_key") or "")
        boundary = evidence.get("boundary") or {}
        if not target_template_id or not factor_id:
            skipped.append({"suggestion_id": suggestion_id, "reason": "missing_target_template"})
            continue
        if str(boundary.get("recommended_scope") or "") != "online_light":
            skipped.append({"suggestion_id": suggestion_id, "reason": "offline_deep_requires_candidate_release"})
            continue
        current = service.get_active_template(factor_id=factor_id, regime_key=regime_key) or {}
        if str(current.get("template_id") or "") == target_template_id:
            skipped.append({"suggestion_id": suggestion_id, "reason": "already_active"})
            continue
        result = service.activate_template(
            factor_id=factor_id,
            template_id=target_template_id,
            regime_key=regime_key,
            suggestion_id=suggestion_id,
            note=f"demo_autonomous apply experiment {experiment_id}",
        )
        if result.get("blocked"):
            skipped.append({"suggestion_id": suggestion_id, "reason": "risk_blocked", "result": result})
        else:
            applied.append({"suggestion_id": suggestion_id, "result": result})
    return {"applied": applied, "skipped": skipped}


def _auto_release_parameter_template_candidates(
    *,
    db_path: str | Path,
    experiment_id: str,
    limit: int = 50,
) -> dict[str, Any]:
    from backend.services.parameter_template_validation import ParameterTemplateValidationService
    from backend.services.parameter_templates import ParameterTemplateService

    service = ParameterTemplateValidationService(str(db_path))
    template_service = ParameterTemplateService(str(db_path))
    candidates = service.list_release_candidates(limit=limit)
    approved = []
    released = []
    rejected = []
    skipped = []
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        status = str(candidate.get("status") or "")
        summary = candidate.get("validation_summary") or {}
        factor_id = str(candidate.get("factor_id") or "")
        regime_key = str(candidate.get("regime_key") or "")
        template_id = str(candidate.get("template_id") or "")
        if template_id and not template_service.get_template(template_id=template_id):
            if status in {"pending_review", "approved"}:
                try:
                    service.review_release_candidate(
                        candidate_id=candidate_id,
                        status="rejected",
                        note=f"demo_autonomous rejected orphan candidate experiment {experiment_id}",
                    )
                    rejected.append({"candidate_id": candidate_id, "reason": "orphan_template"})
                except Exception as exc:
                    skipped.append({"candidate_id": candidate_id, "reason": f"orphan_reject_failed:{exc}"})
            else:
                skipped.append({"candidate_id": candidate_id, "reason": "orphan_template"})
            continue
        active = template_service.get_active_template(factor_id=factor_id, regime_key=regime_key) or {}
        if template_id and str(active.get("template_id") or "") == template_id:
            skipped.append({"candidate_id": candidate_id, "reason": "already_active"})
            continue
        if status == "pending_review":
            if not bool(summary.get("walk_forward_passed", False)):
                skipped.append({"candidate_id": candidate_id, "reason": "walk_forward_not_passed"})
                continue
            candidate = service.review_release_candidate(
                candidate_id=candidate_id,
                status="approved",
                note=f"auto-approved by demo_autonomous experiment {experiment_id}",
            )
            approved.append(candidate_id)
            status = str(candidate.get("status") or "")
        if status == "approved":
            try:
                result = service.deploy_release_candidate(
                    candidate_id=candidate_id,
                    note=f"demo_autonomous release experiment {experiment_id}",
                )
                if result.get("blocked"):
                    skipped.append({"candidate_id": candidate_id, "reason": "risk_blocked", "result": result})
                else:
                    released.append({"candidate_id": candidate_id, "result": result})
            except Exception as exc:
                skipped.append({"candidate_id": candidate_id, "reason": str(exc)})
    return {"approved": approved, "released": released, "rejected": rejected, "skipped": skipped}


def _auto_apply_position_supervisor_template_suggestions(
    *,
    db_path: str | Path,
    experiment_id: str,
    limit: int = 50,
    run_id: str = "",
) -> dict[str, Any]:
    from backend.services.position_supervisor_templates import list_position_supervisor_templates
    from config.runtime_config import patch as patch_runtime_config
    from config.runtime_config import shared as runtime_config
    from risk.policy_service import RiskPolicyService
    from backend.services.evolution_ledger import persist_runtime_config_snapshot

    valid_templates = {str(item.get("template_id") or "") for item in list_position_supervisor_templates()}
    previous_template_id = str(getattr(runtime_config(), "position_supervisor_template_id", "") or "position_supervisor:default.v1")
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    applied = []
    skipped = []
    try:
        rows = conn.execute(
            """
            SELECT *
            FROM policy_suggestion
            WHERE status='approved'
              AND scope_type='position_supervisor_template'
            ORDER BY reviewed_at DESC, created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        for row in rows:
            suggestion_id = str(row["suggestion_id"] or "")
            target_template_id = str(row["scope_key"] or "")
            if target_template_id == previous_template_id:
                skipped.append({"suggestion_id": suggestion_id, "reason": "already_active"})
                continue
            if target_template_id not in valid_templates:
                skipped.append({"suggestion_id": suggestion_id, "reason": "invalid_template"})
                continue
            evidence = _loads(row["evidence_json"], {})
            verdict = RiskPolicyService.shared().evaluate(
                "switch_position_supervisor_template",
                {
                    "suggestion_id": suggestion_id,
                    "suggestion_status": "approved",
                    "target_template_id": target_template_id,
                    "previous_template_id": previous_template_id,
                    "evidence": evidence,
                    "experiment_id": experiment_id,
                    "autonomous_apply": True,
                    "autonomy_mode": "demo_autonomous",
                },
            ).to_dict()
            if not verdict.get("allowed", False):
                record_evolution_decision(
                    run_id=run_id,
                    decision_type="apply_switch",
                    scope_type="position_supervisor_template",
                    scope_key=target_template_id,
                    action="switch_position_supervisor_template",
                    status="blocked",
                    evidence=evidence,
                    risk_verdict=verdict,
                    before={"template_id": previous_template_id},
                    after={"template_id": target_template_id},
                    result={"suggestion_id": suggestion_id},
                    db_path=db_path,
                )
                skipped.append({"suggestion_id": suggestion_id, "reason": "risk_blocked", "risk_verdict": verdict})
                continue
            patch_runtime_config({"position_supervisor_template_id": target_template_id})
            snapshot = persist_runtime_config_snapshot(
                runtime_config(),
                source="position_supervisor_template_switch",
                db_path=db_path,
                run_id=run_id,
            )
            now_ts = time.time()
            application_id = f"psv_apply_{int(now_ts)}_{suggestion_id[-8:]}"
            details = {
                "schema_version": "position_supervisor_template_switch.v1",
                "experiment_id": experiment_id,
                "autonomy_mode": "demo_autonomous",
                "suggestion_id": suggestion_id,
                "previous_template_id": previous_template_id,
                "target_template_id": target_template_id,
                "risk_verdict": verdict,
                "evidence": evidence,
                "config_version": int(snapshot.get("config_version") or 0),
                "config_hash": str(snapshot.get("config_hash") or ""),
            }
            conn.execute(
                """
                INSERT OR REPLACE INTO learning_application_log
                (application_id, cycle_ts, scope_type, scope_key, action,
                 bias_multiplier, old_weight, new_weight, suggestion_ids_json,
                 status, details_json, created_at)
                VALUES (?, ?, 'position_supervisor_template', ?, 'switch_position_supervisor_template',
                        1.0, 0.0, 0.0, ?, 'applied', ?, ?)
                """,
                (
                    application_id,
                    now_ts,
                    target_template_id,
                    _dumps([suggestion_id]),
                    _dumps(details),
                    now_ts,
                ),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO learning_application_effect
                (application_id, scope_type, scope_key, action, status,
                 decision_json, updated_at, created_at)
                VALUES (?, 'position_supervisor_template', ?, 'switch_position_supervisor_template',
                        'observing', ?, ?, COALESCE(
                            (SELECT created_at FROM learning_application_effect WHERE application_id=?),
                            ?
                        ))
                """,
                (
                    application_id,
                    target_template_id,
                    _dumps(details),
                    now_ts,
                    application_id,
                    now_ts,
                ),
            )
            conn.execute(
                """
                UPDATE policy_suggestion
                SET status='applied', reviewed_at=CASE WHEN reviewed_at > 0 THEN reviewed_at ELSE ? END,
                    review_note=?
                WHERE suggestion_id=?
                """,
                (now_ts, f"demo_autonomous applied experiment {experiment_id}", suggestion_id),
            )
            conn.commit()
            record_evolution_decision(
                run_id=run_id,
                decision_type="apply_switch",
                scope_type="position_supervisor_template",
                scope_key=target_template_id,
                action="switch_position_supervisor_template",
                status="applied",
                evidence=evidence,
                risk_verdict=verdict,
                before={"template_id": previous_template_id},
                after={"template_id": target_template_id},
                result={"suggestion_id": suggestion_id, "application_id": application_id},
                rollback={"previous_template_id": previous_template_id},
                config_version=int(snapshot.get("config_version") or 0),
                config_hash=str(snapshot.get("config_hash") or ""),
                db_path=db_path,
            )
            applied.append(
                {
                    "suggestion_id": suggestion_id,
                    "previous_template_id": previous_template_id,
                    "target_template_id": target_template_id,
                    "application_id": application_id,
                }
            )
            previous_template_id = target_template_id
        conn.commit()
        return {"applied": applied, "skipped": skipped}
    finally:
        conn.close()


def _auto_rollback_position_supervisor_template(
    *,
    db_path: str | Path,
    experiment_id: str,
    run_id: str = "",
    min_observed_trades: int = 3,
    max_delta_avg_reward: float = -0.005,
) -> dict[str, Any]:
    from config.runtime_config import patch as patch_runtime_config
    from config.runtime_config import shared as runtime_config
    from backend.services.evolution_ledger import persist_runtime_config_snapshot

    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    rolled_back = []
    skipped = []
    try:
        try:
            rows = conn.execute(
                """
                SELECT l.*, e.observed_trade_count, e.delta_avg_reward, e.status AS effect_status
                FROM learning_application_log l
                JOIN learning_application_effect e ON e.application_id = l.application_id
                WHERE l.scope_type='position_supervisor_template'
                  AND l.action='switch_position_supervisor_template'
                  AND l.status IN ('applied', 'observing')
                  AND e.status='observing'
                ORDER BY l.created_at DESC
                LIMIT 20
                """
            ).fetchall()
        except Exception as exc:
            return {"rolled_back": [], "skipped": [{"reason": "effect_schema_unavailable", "error": str(exc)}]}
        for row in rows:
            application_id = str(row["application_id"] or "")
            observed = int(row["observed_trade_count"] or 0)
            delta = float(row["delta_avg_reward"] or 0.0)
            details = _loads(row["details_json"], {})
            previous_template_id = str(details.get("previous_template_id") or "")
            target_template_id = str(details.get("target_template_id") or row["scope_key"] or "")
            current_template_id = str(getattr(runtime_config(), "position_supervisor_template_id", "") or "")
            if observed < int(min_observed_trades):
                skipped.append({"application_id": application_id, "reason": "insufficient_observations", "observed": observed})
                continue
            if delta > float(max_delta_avg_reward):
                skipped.append({"application_id": application_id, "reason": "effect_not_negative_enough", "delta_avg_reward": delta})
                continue
            if not previous_template_id:
                skipped.append({"application_id": application_id, "reason": "missing_previous_template"})
                continue
            if current_template_id != target_template_id:
                skipped.append({"application_id": application_id, "reason": "target_not_current", "current_template_id": current_template_id})
                continue
            patch_runtime_config({"position_supervisor_template_id": previous_template_id})
            snapshot = persist_runtime_config_snapshot(
                runtime_config(),
                source="position_supervisor_template_auto_rollback",
                db_path=db_path,
                run_id=run_id,
            )
            now_ts = time.time()
            rollback = {
                "schema_version": "position_supervisor_template_rollback.v1",
                "experiment_id": experiment_id,
                "application_id": application_id,
                "previous_template_id": previous_template_id,
                "rolled_back_from": target_template_id,
                "observed_trade_count": observed,
                "delta_avg_reward": delta,
                "config_version": int(snapshot.get("config_version") or 0),
                "config_hash": str(snapshot.get("config_hash") or ""),
            }
            conn.execute(
                """
                UPDATE learning_application_log
                SET status='rolled_back', details_json=?
                WHERE application_id=?
                """,
                (_dumps({**details, "rollback": rollback}), application_id),
            )
            conn.execute(
                """
                UPDATE learning_application_effect
                SET status='rolled_back', decision_json=?, updated_at=?
                WHERE application_id=?
                """,
                (_dumps(rollback), now_ts, application_id),
            )
            conn.commit()
            record_evolution_decision(
                run_id=run_id,
                decision_type="auto_rollback",
                scope_type="position_supervisor_template",
                scope_key=target_template_id,
                action="rollback_position_supervisor_template",
                status="rolled_back",
                evidence={"observed_trade_count": observed, "delta_avg_reward": delta},
                before={"template_id": target_template_id},
                after={"template_id": previous_template_id},
                result=rollback,
                rollback={"previous_template_id": previous_template_id},
                config_version=int(snapshot.get("config_version") or 0),
                config_hash=str(snapshot.get("config_hash") or ""),
                db_path=db_path,
            )
            rolled_back.append(rollback)
        conn.commit()
        return {"rolled_back": rolled_back, "skipped": skipped}
    finally:
        conn.close()


def apply_demo_autonomy(
    *,
    db_path: str | Path = STATE_DB,
    suggestion_limit: int = 200,
) -> dict[str, Any]:
    ensure_autonomous_learning_tables(db_path)
    experiment_id = _new_experiment_id()
    run = start_evolution_run(
        run_type="demo_autonomy_apply",
        trigger_source="autonomous_learning_cycle",
        db_path=db_path,
        run_id=experiment_id,
    )
    if not _demo_autonomous_enabled():
        payload = {
            "schema_version": "demo_autonomy_apply.v1",
            "enabled": False,
            "mode": _autonomy_mode(),
            "experiment_id": experiment_id,
        }
        finish_evolution_run(str(run.get("run_id") or experiment_id), status="skipped", summary=payload, db_path=db_path)
        return payload
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    try:
        approvals = _approve_demo_policy_suggestions(
            conn,
            experiment_id=experiment_id,
            limit=suggestion_limit,
            db_path=db_path,
            run_id=str(run.get("run_id") or experiment_id),
        )
        _insert_evolution_event(
            conn,
            "demo_autonomy_auto_approve",
            {"experiment_id": experiment_id, **approvals},
        )
        conn.commit()
    finally:
        conn.close()

    factor_weights = _sync_factor_weights_for_demo(experiment_id=experiment_id)
    parameter_suggestions = _auto_apply_parameter_template_suggestions(
        db_path=db_path,
        experiment_id=experiment_id,
    )
    parameter_candidates = _auto_release_parameter_template_candidates(
        db_path=db_path,
        experiment_id=experiment_id,
    )
    supervisor_templates = _auto_apply_position_supervisor_template_suggestions(
        db_path=db_path,
        experiment_id=experiment_id,
        run_id=str(run.get("run_id") or experiment_id),
    )
    supervisor_rollbacks = _auto_rollback_position_supervisor_template(
        db_path=db_path,
        experiment_id=experiment_id,
        run_id=str(run.get("run_id") or experiment_id),
    )
    payload = {
        "schema_version": "demo_autonomy_apply.v1",
        "enabled": True,
        "mode": "demo_autonomous",
        "experiment_id": experiment_id,
        "approvals": approvals,
        "factor_weights": factor_weights,
        "parameter_suggestions": parameter_suggestions,
        "parameter_candidates": parameter_candidates,
        "supervisor_templates": supervisor_templates,
        "supervisor_rollbacks": supervisor_rollbacks,
    }
    conn = connect_sqlite(db_path)
    try:
        _insert_evolution_event(conn, "demo_autonomy_apply", payload)
        conn.commit()
    finally:
        conn.close()
    finish_evolution_run(str(run.get("run_id") or experiment_id), status="completed", summary=payload, db_path=db_path)
    return payload


def run_autonomous_learning_cycle(
    *,
    db_path: str | Path = STATE_DB,
    sample_limit: int = 500,
    recommendation_limit: int = 20,
    submit_offline_deep: bool = True,
) -> dict[str, Any]:
    from research.learning.governor import RuleEvolutionGovernor
    from backend.services.supervisor_counterfactual import evaluate_counterfactuals

    counterfactuals = evaluate_counterfactuals(db_path=db_path, limit=sample_limit, materialize=True)
    trace_maturation = mature_position_supervisor_traces(db_path=db_path, limit=sample_limit)
    review_integrity_backfill = backfill_trade_review_integrity_markers(db_path=db_path, limit=sample_limit)
    close_source_backfill = backfill_trade_review_close_sources(db_path=db_path, limit=sample_limit)
    samples = materialize_autonomous_learning_samples(db_path=db_path, limit=sample_limit)
    contract_repair = repair_evidence_contracts(db_path=db_path, limit=max(sample_limit, sample_limit * 4))
    gov = RuleEvolutionGovernor(str(db_path))
    governance = {
        "review_pending": gov.review_pending(),
        "reconcile_active": gov.reconcile_active(),
        "reconcile_application_effects": gov.reconcile_application_effects(),
    }
    recommendations = materialize_parameter_template_recommendations(
        db_path=db_path,
        limit=recommendation_limit,
        submit_offline_deep=submit_offline_deep,
    )
    demo_apply = apply_demo_autonomy(db_path=db_path)
    conn = connect_sqlite(db_path)
    try:
        payload = {
            "schema_version": "autonomous_learning_cycle.v1",
            "counterfactuals": counterfactuals,
            "trace_maturation": trace_maturation,
            "review_integrity_backfill": review_integrity_backfill,
            "close_source_backfill": close_source_backfill,
            "samples": samples,
            "evidence_contract_repair": contract_repair,
            "governance": governance,
            "parameter_template_recommendations": recommendations,
            "demo_autonomy": demo_apply,
        }
        _insert_evolution_event(conn, "autonomous_learning_cycle", payload)
        conn.commit()
        return payload
    finally:
        conn.close()


def schedule_autonomous_learning(
    *,
    delay_sec: float = 420.0,
    interval_sec: float = 1800.0,
    sample_limit: int = 500,
    recommendation_limit: int = 20,
    submit_offline_deep: bool = True,
) -> bool:
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return False
    _stop_event.clear()

    def _worker() -> None:
        if _stop_event.wait(max(0.0, delay_sec)):
            return
        while not _stop_event.is_set():
            try:
                result = run_autonomous_learning_cycle(
                    sample_limit=sample_limit,
                    recommendation_limit=recommendation_limit,
                    submit_offline_deep=submit_offline_deep,
                )
                logger.info("[autonomous_learning] scheduled run completed: %s", result)
            except Exception as exc:
                logger.warning("[autonomous_learning] scheduled run failed: %s", exc)
            if _stop_event.wait(max(60.0, interval_sec)):
                return

    _scheduler_thread = threading.Thread(
        target=_worker,
        name="autonomous_learning_scheduler",
        daemon=True,
    )
    _scheduler_thread.start()
    return True


def stop_autonomous_learning() -> None:
    _stop_event.set()
