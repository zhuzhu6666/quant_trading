"""Heavy research jobs owned by the learning worker, without live imports."""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import socket
import time
import uuid
from pathlib import Path
from typing import Any

from backend.core import db as state_db
from backend.core.db import connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.core.state_store import validate_runtime_state_schema
from backend.services.runtime_health_projection import RuntimeHealthProjectionService
from research.position_quality_lightgbm import TrainingMemoryBudgetExceeded


logger = logging.getLogger(__name__)

_WORKER_INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
_TRAINING_WINDOW_STALE_SECONDS = 300.0
_TRAINING_TERMINAL_STATUSES = {
    "done",
    "blocked_memory_budget",
    "aborted_process_loss",
    "failed",
}


def run_feature_engineering_job() -> dict[str, Any]:
    """Derive/select research features and register PCA candidates."""
    try:
        from backend.services.learning_workload_gate import (
            RUN_PENDING_GOVERNANCE,
            SKIP_CLOSED_NO_NEW_FACTS,
            evaluate_learning_workload,
        )

        workload_gate = evaluate_learning_workload()
        if str(workload_gate.get("status") or "") in {
            SKIP_CLOSED_NO_NEW_FACTS,
            RUN_PENDING_GOVERNANCE,
        }:
            logger.info(
                "[fe] skipped: learning workload gate=%s",
                workload_gate.get("reason_code"),
            )
            return {
                "ok": True,
                "skipped": True,
                "status": str(workload_gate.get("status") or SKIP_CLOSED_NO_NEW_FACTS),
                "reason": workload_gate.get("reason_code"),
                "workload_gate": workload_gate,
            }
        import numpy as np

        from alpha.features.selector import run_feature_selection
        from alpha.registry import factor_registry
        from data.store import DataStore
        from monitor.evolution_story.report import EvolutionStory

        df = DataStore().load_bars("XAUUSD+", "M5", limit=20000)
        if df.empty or len(df) < 1000:
            logger.info("[fe] insufficient bars: %d", len(df))
            return {"ok": False, "status": "insufficient_bars", "bars": len(df)}
        factor_values: dict[str, np.ndarray] = {}
        errors = 0
        for name in factor_registry.list():
            try:
                fn = factor_registry.get(name)
                if fn is None:
                    continue
                values = np.asarray(fn(df), dtype=float)
                values[np.isinf(values)] = np.nan
                factor_values[name] = values
            except Exception:
                errors += 1
        close = df["close"].values.astype(float)
        forward_returns = np.full(len(close), np.nan)
        forward_returns[:-1] = (close[1:] - close[:-1]) / close[:-1]
        result = run_feature_selection(df, forward_returns, factor_values)
        result = {**dict(result or {}), "ok": True, "factor_errors": errors}
        logger.info(
            "[fe] done: %d derived -> %d pca -> %d selected / %d candidates",
            result.get("n_derived", 0),
            result.get("pca_n_components", 0),
            result.get("n_selected", 0),
            result.get("n_candidates", 0),
        )
        try:
            story = EvolutionStory.shared() if hasattr(EvolutionStory, "shared") else None
            if story:
                story.append(
                    event_type="feature_engineering",
                    payload={
                        "n_selected": result.get("n_selected"),
                        "n_candidates": result.get("n_candidates"),
                        "pca_n": result.get("pca_n_components"),
                        "pca_var": result.get("pca_variance"),
                    },
                )
        except Exception as exc:
            logger.debug("[fe] EvolutionStory.append failed: %s", exc)
        return result
    except Exception as exc:
        logger.warning("[fe] failed: %s", exc, exc_info=True)
        return {"ok": False, "status": "error", "error": f"{type(exc).__name__}: {exc}"}


def offmarket_high_load_allowed(session: dict[str, Any]) -> tuple[bool, str]:
    status = str((session or {}).get("status") or "")
    if status not in {"closed_confirmed", "closed_pending_positions"}:
        return False, f"market_session_not_offmarket:{status or 'unknown'}"
    if not bool((session or {}).get("high_load_allowed", False)):
        return False, "high_load_not_allowed"
    return True, "ok"


def _connect(db_path: str | Path):
    if is_state_db_path(db_path):
        return get_state_pg_conn()
    conn = connect_sqlite(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _sql(db_path: str | Path, sql: str) -> str:
    return sql.replace("?", "%s") if is_state_db_path(db_path) else sql


def _ensure_offmarket_audit_table(conn, db_path: str | Path) -> None:
    table_declaration = """
        CREATE TABLE IF NOT EXISTS offmarket_high_load_job_audit (
            audit_id TEXT PRIMARY KEY, job_name TEXT NOT NULL, status TEXT NOT NULL,
            session_status TEXT DEFAULT '', high_load_profile TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{}', result_json TEXT DEFAULT '{}', error TEXT DEFAULT '',
            started_at REAL NOT NULL, finished_at REAL NOT NULL,
            training_window_key TEXT NOT NULL DEFAULT '',
            phase TEXT NOT NULL DEFAULT '',
            worker_instance_id TEXT NOT NULL DEFAULT '',
            heartbeat_at REAL NOT NULL DEFAULT 0.0,
            input_bytes_estimate INTEGER NOT NULL DEFAULT 0
        )
    """
    if is_state_db_path(db_path):
        validate_runtime_state_schema(conn, table_declaration)
        return
    conn.execute(table_declaration)
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(offmarket_high_load_job_audit)").fetchall()
    }
    for name, declaration in (
        ("training_window_key", "TEXT NOT NULL DEFAULT ''"),
        ("phase", "TEXT NOT NULL DEFAULT ''"),
        ("worker_instance_id", "TEXT NOT NULL DEFAULT ''"),
        ("heartbeat_at", "REAL NOT NULL DEFAULT 0.0"),
        ("input_bytes_estimate", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE offmarket_high_load_job_audit ADD COLUMN {name} {declaration}")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_offmarket_training_window_unique
        ON offmarket_high_load_job_audit(job_name, training_window_key)
        WHERE training_window_key <> ''
        """
    )


def _training_audit_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    try:
        return {key: row[key] for key in row.keys()}
    except (AttributeError, KeyError, TypeError):
        return {
            "audit_id": row[0],
            "status": row[1],
            "phase": row[2],
            "heartbeat_at": row[3],
            "finished_at": row[4],
        }


def _claim_training_window(
    *,
    db_path: str | Path,
    job_name: str,
    window_key: str,
    session: dict[str, Any],
    payload: dict[str, Any],
    started_at: float,
    allow_retry_terminal: bool = False,
) -> dict[str, Any] | None:
    """Claim one durable window, or return the reason it must not run again."""
    if not window_key:
        return None
    conn = _connect(db_path)
    now = time.time()
    try:
        _ensure_offmarket_audit_table(conn, db_path)
        row = conn.execute(
            _sql(
                db_path,
                """
                SELECT audit_id, status, phase, heartbeat_at, finished_at,
                       payload_json, result_json, error
                FROM offmarket_high_load_job_audit
                WHERE job_name=? AND training_window_key=?
                FOR UPDATE
                """,
            )
            if is_state_db_path(db_path)
            else _sql(
                db_path,
                """
                SELECT audit_id, status, phase, heartbeat_at, finished_at,
                       payload_json, result_json, error
                FROM offmarket_high_load_job_audit
                WHERE job_name=? AND training_window_key=?
                """,
            ),
            (job_name, window_key),
        ).fetchone()
        if row is not None:
            existing = _training_audit_row(row)
            status = str(existing.get("status") or "")
            audit_id = str(existing.get("audit_id") or "")
            if status in _TRAINING_TERMINAL_STATUSES:
                try:
                    previous_payload = json.loads(existing.get("payload_json") or "{}")
                except (TypeError, ValueError):
                    previous_payload = {}
                retry_count = int(previous_payload.get("retry_count") or 0)
                retryable_status = status in {"aborted_process_loss", "blocked_memory_budget"}
                if allow_retry_terminal and retryable_status and retry_count < 1:
                    retry_payload = {
                        **dict(payload or {}),
                        "retry_count": retry_count + 1,
                        "retry_of_audit_id": audit_id,
                        "previous_terminal_status": status,
                    }
                    conn.execute(
                        _sql(
                            db_path,
                            """
                            UPDATE offmarket_high_load_job_audit
                            SET status='running', phase='retry_claim',
                                payload_json=?, result_json=?, error='',
                                started_at=?, finished_at=?, heartbeat_at=?,
                                worker_instance_id=?
                            WHERE audit_id=? AND status=?
                            """,
                        ),
                        (
                            json.dumps(retry_payload, ensure_ascii=False, default=str),
                            json.dumps(
                                {
                                    "previous_attempt": {
                                        "status": status,
                                        "result_json": existing.get("result_json") or "{}",
                                        "error": existing.get("error") or "",
                                    }
                                },
                                ensure_ascii=False,
                                default=str,
                            ),
                            started_at,
                            now,
                            now,
                            _WORKER_INSTANCE_ID,
                            audit_id,
                            status,
                        ),
                    )
                    conn.commit()
                    return {
                        "claimed": True,
                        "audit_id": audit_id,
                        "status": "running",
                        "training_window_key": window_key,
                        "retry_count": retry_count + 1,
                    }
                conn.commit()
                return {
                    "claimed": False,
                    "reason": "training_window_already_terminal",
                    "audit": existing,
                }
            heartbeat_at = float(existing.get("heartbeat_at") or 0.0)
            if status == "running" and now - heartbeat_at <= _TRAINING_WINDOW_STALE_SECONDS:
                conn.commit()
                return {
                    "claimed": False,
                    "reason": "training_window_running",
                    "audit": existing,
                }
            if status == "running":
                conn.execute(
                    _sql(
                        db_path,
                        """
                        UPDATE offmarket_high_load_job_audit
                        SET status='aborted_process_loss', phase='recovered_process_loss',
                            error=?, heartbeat_at=?, finished_at=?
                        WHERE audit_id=?
                        """,
                    ),
                    (
                        "stale running audit recovered after worker process loss",
                        now,
                        now,
                        audit_id,
                    ),
                )
            conn.commit()
            return {
                "claimed": False,
                "reason": "training_window_aborted_process_loss",
                "audit": {
                    **existing,
                    "status": "aborted_process_loss",
                    "phase": "recovered_process_loss",
                },
            }

        audit_id = f"{job_name}:window:{uuid.uuid5(uuid.NAMESPACE_URL, window_key).hex[:24]}"
        insert_result = conn.execute(
            _sql(
                db_path,
                """
                INSERT INTO offmarket_high_load_job_audit
                (audit_id, job_name, status, session_status, high_load_profile,
                 payload_json, result_json, error, started_at, finished_at,
                 training_window_key, phase, worker_instance_id, heartbeat_at,
                 input_bytes_estimate)
                VALUES (?, ?, 'running', ?, ?, ?, '{}', '', ?, ?, ?, 'claim', ?, ?, 0)
                ON CONFLICT DO NOTHING
                """,
            ),
            (
                audit_id,
                job_name,
                str((session or {}).get("status") or ""),
                str((session or {}).get("high_load_profile") or "disabled"),
                json.dumps(payload, ensure_ascii=False, default=str),
                started_at,
                now,
                window_key,
                _WORKER_INSTANCE_ID,
                now,
            ),
        )
        if int(getattr(insert_result, "rowcount", 1) or 0) == 0:
            existing_row = conn.execute(
                _sql(
                    db_path,
                    """
                    SELECT audit_id, status, phase, heartbeat_at, finished_at
                    FROM offmarket_high_load_job_audit
                    WHERE job_name=? AND training_window_key=?
                    """,
                ),
                (job_name, window_key),
            ).fetchone()
            conn.commit()
            existing = _training_audit_row(existing_row)
            return {
                "claimed": False,
                "reason": "training_window_running",
                "audit": existing,
            }
        conn.commit()
        return {
            "claimed": True,
            "audit_id": audit_id,
            "status": "running",
            "training_window_key": window_key,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _heartbeat_training_window(
    *,
    db_path: str | Path,
    audit_id: str,
    phase: str,
    input_bytes_estimate: int = 0,
) -> None:
    if not audit_id:
        return
    conn = _connect(db_path)
    try:
        _ensure_offmarket_audit_table(conn, db_path)
        now = time.time()
        conn.execute(
            _sql(
                db_path,
                """
                UPDATE offmarket_high_load_job_audit
                SET phase=?, heartbeat_at=?, input_bytes_estimate=?
                WHERE audit_id=? AND status='running'
                """,
            ),
            (str(phase or ""), now, max(0, int(input_bytes_estimate or 0)), audit_id),
        )
        conn.commit()
    finally:
        conn.close()


def _record_offmarket_audit(
    *,
    job_name: str,
    status: str,
    session: dict[str, Any],
    payload: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error: str = "",
    started_at: float | None = None,
    db_path: str | Path | None = None,
    training_window_key: str = "",
    phase: str = "",
    worker_instance_id: str = "",
    heartbeat_at: float | None = None,
    input_bytes_estimate: int = 0,
    audit_id: str = "",
) -> dict[str, Any]:
    db_path = Path(db_path or state_db.STATE_DB)
    now = time.time()
    started = float(started_at or now)
    row = {
        "audit_id": audit_id or (
            f"{job_name}:window:{uuid.uuid5(uuid.NAMESPACE_URL, training_window_key).hex[:24]}"
            if training_window_key
            else f"{job_name}:{int(started * 1000)}"
        ),
        "job_name": job_name,
        "status": status,
        "session_status": str((session or {}).get("status") or ""),
        "high_load_profile": str((session or {}).get("high_load_profile") or "disabled"),
        "payload": payload or {},
        "result": result or {},
        "error": str(error or ""),
        "started_at": started,
        "finished_at": now,
        "training_window_key": str(training_window_key or ""),
        "phase": str(phase or ""),
        "worker_instance_id": str(worker_instance_id or _WORKER_INSTANCE_ID),
        "heartbeat_at": float(heartbeat_at or now),
        "input_bytes_estimate": max(0, int(input_bytes_estimate or 0)),
    }
    conn = _connect(db_path)
    try:
        _ensure_offmarket_audit_table(conn, db_path)
        conn.execute(
            _sql(
                db_path,
                """
                INSERT INTO offmarket_high_load_job_audit
                (audit_id, job_name, status, session_status, high_load_profile,
                 payload_json, result_json, error, started_at, finished_at,
                 training_window_key, phase, worker_instance_id,
                  heartbeat_at, input_bytes_estimate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(audit_id) DO UPDATE SET
                    status=excluded.status, payload_json=excluded.payload_json,
                    result_json=excluded.result_json, error=excluded.error,
                    finished_at=excluded.finished_at,
                    phase=excluded.phase, worker_instance_id=excluded.worker_instance_id,
                    heartbeat_at=excluded.heartbeat_at,
                    input_bytes_estimate=excluded.input_bytes_estimate
                """,
            ),
            (
                row["audit_id"], row["job_name"], row["status"], row["session_status"],
                row["high_load_profile"], json.dumps(row["payload"], ensure_ascii=False, default=str),
                json.dumps(row["result"], ensure_ascii=False, default=str), row["error"],
                row["started_at"], row["finished_at"], row["training_window_key"],
                row["phase"], row["worker_instance_id"], row["heartbeat_at"],
                row["input_bytes_estimate"],
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return row


def _build_shadow_model_suite(db_path: str | Path):
    from research.factor_governance_lightgbm import FactorGovernanceLightGBMService
    from research.open_quality_lightgbm import OpenQualityLightGBMService
    from research.position_quality_lightgbm import PositionQualityLightGBMService

    return [
        (
            "position_quality_lightgbm",
            PositionQualityLightGBMService(db_path=db_path),
            {
                "limit": 4000,
                "holdout_ratio": 0.2,
                "min_samples": 20,
                "register": True,
                "symbol": "XAUUSD+",
                "timeframe": "M5",
            },
        ),
        (
            "open_quality_lightgbm",
            OpenQualityLightGBMService(db_path=db_path),
            {
                "limit": 3000,
                "holdout_ratio": 0.25,
                "min_samples": 100,
                "register": True,
            },
        ),
        (
            "factor_governance_lightgbm",
            FactorGovernanceLightGBMService(db_path=db_path),
            {
                "limit": 5000,
                "holdout_ratio": 0.25,
                "min_samples": 100,
                "register": True,
            },
        ),
    ]


def _score_shadow_model(
    *,
    model_type: str,
    model_service: Any,
    limit: int,
    mode: str,
    artifact_path: str | None = None,
    skip_existing: bool = False,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "limit": int(limit),
        "mode": mode,
        "skip_existing": bool(skip_existing),
    }
    if artifact_path:
        kwargs["artifact_path"] = artifact_path
    try:
        result = dict(model_service.score_samples(**kwargs) or {})
    except Exception as exc:
        logger.warning("[offmarket_high_load] %s shadow score failed: %s", model_type, exc)
        return {
            "ok": False,
            "model_type": model_type,
            "error": "shadow_score_error",
            "detail": f"{type(exc).__name__}: {exc}",
        }
    result.setdefault("model_type", model_type)
    return result


def _train_and_score_model(
    *,
    model_type: str,
    model_service: Any,
    train_kwargs: dict[str, Any],
    shadow_limit: int,
    mode: str,
    score_shadow: bool = True,
) -> dict[str, Any]:
    """Keep one model's training failure isolated from the other models.

    The off-market job is a suite, not a single model transaction: a sparse
    position-quality window must not hide a usable factor-governance result.
    Each item therefore owns its stable status and its optional shadow score.
    """
    try:
        trained = dict(model_service.train(**dict(train_kwargs)) or {})
    except TrainingMemoryBudgetExceeded as exc:
        trained = {
            "ok": False,
            "status": "blocked_memory_budget",
            "reason_codes": ["blocked_memory_budget"],
            "error": "blocked_memory_budget",
            "detail": str(exc),
            "data_quality": dict(getattr(exc, "data_quality", {}) or {}),
        }
        logger.warning(
            "[offmarket_high_load] %s blocked by memory budget: %s",
            model_type,
            trained["detail"],
        )
    except Exception as exc:  # noqa: BLE001 - report per-model failure
        trained = {
            "ok": False,
            "status": "failed",
            "reason_codes": ["model_training_error"],
            "error": "model_training_error",
            "detail": f"{type(exc).__name__}: {exc}",
        }
        logger.warning(
            "[offmarket_high_load] %s training failed: %s",
            model_type,
            trained["detail"],
        )
    trained.setdefault(
        "status",
        "trained" if bool(trained.get("ok")) else "failed",
    )
    trained.setdefault("reason_codes", [])
    item: dict[str, Any] = {"train": trained}
    if not trained.get("ok"):
        item["shadow"] = {
            "ok": False,
            "skipped": True,
            "model_type": model_type,
            "reason": str(
                trained.get("reason")
                or (trained.get("reason_codes") or ["training_not_ready"])[0]
            ),
        }
        return item
    if not score_shadow:
        item["shadow"] = {
            "ok": False,
            "skipped": True,
            "model_type": model_type,
            "reason": "training_only",
        }
        return item
    item["shadow"] = _score_shadow_model(
        model_type=model_type,
        model_service=model_service,
        artifact_path=str(trained.get("artifact_path") or "") or None,
        limit=int(shadow_limit),
        mode=mode,
    )
    return item


def _offmarket_training_window_key(session: dict[str, Any], profile: str) -> str:
    """Build a stable key for one closed window from the shared session state."""
    try:
        now_ts = float(session.get("now_ts") or 0.0)
        seconds_to_open = float(session.get("seconds_to_open"))
        next_open_ts = now_ts + seconds_to_open
    except (AttributeError, TypeError, ValueError):
        return ""
    if now_ts <= 0.0 or seconds_to_open < 0.0 or not math.isfinite(next_open_ts):
        return ""
    # The evaluator derives seconds_to_open from the same session boundary on
    # every snapshot; minute rounding removes scheduler-second jitter.
    boundary_minute = int(round(next_open_ts / 60.0)) * 60
    return f"{profile}:next_open:{boundary_minute}"


def _completed_offmarket_training_window(
    *,
    db_path: str | Path,
    job_name: str,
    window_key: str,
) -> dict[str, Any] | None:
    if not window_key:
        return None
    conn = None
    try:
        conn = _connect(db_path)
        rows = conn.execute(
            _sql(
                db_path,
                """
                SELECT audit_id, payload_json, finished_at
                FROM offmarket_high_load_job_audit
                WHERE job_name=? AND status='done'
                ORDER BY finished_at DESC
                LIMIT 500
                """,
            ),
            (job_name,),
        ).fetchall()
        for row in rows:
            try:
                payload_raw = row["payload_json"]
                audit_id = row["audit_id"]
                finished_at = row["finished_at"]
            except (KeyError, IndexError, TypeError):
                payload_raw = row[1]
                audit_id = row[0]
                finished_at = row[2]
            try:
                payload = json.loads(payload_raw or "{}")
            except (TypeError, ValueError):
                continue
            if str(payload.get("training_window_key") or "") == window_key:
                return {
                    "audit_id": str(audit_id or ""),
                    "finished_at": float(finished_at or 0.0),
                }
    except Exception as exc:
        # A first run may legitimately have no audit table yet. The normal
        # audit write below remains the source of truth and will surface a
        # real database failure instead of silently claiming completion.
        logger.debug("[offmarket_high_load] window lookup unavailable: %s", exc)
    finally:
        if conn is not None:
            conn.close()
    return None


def run_offmarket_position_quality_job(
    *,
    session: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
    execution_mode: str = "scheduled_suite",
) -> dict[str, Any]:
    """Run the scheduled suite or one explicitly isolated training-only job."""
    if execution_mode not in {"scheduled_suite", "training_only"}:
        raise ValueError(f"unsupported execution_mode: {execution_mode}")
    training_only = execution_mode == "training_only"
    job_name = "offmarket_position_quality_lightgbm"
    started_at = time.time()
    db_path = Path(db_path or state_db.STATE_DB)
    from backend.services.learning_workload_gate import (
        RUN_PENDING_GOVERNANCE,
        SKIP_CLOSED_NO_NEW_FACTS,
        evaluate_learning_workload,
    )

    workload_gate = evaluate_learning_workload(db_path)
    if str(workload_gate.get("status") or "") in {
        SKIP_CLOSED_NO_NEW_FACTS,
        RUN_PENDING_GOVERNANCE,
    }:
        logger.info(
            "[offmarket_high_load] %s skipped: learning workload gate=%s",
            job_name,
            workload_gate.get("reason_code"),
        )
        return {
            "ok": True,
            "skipped": True,
            "reason": workload_gate.get("reason_code"),
            "status": str(workload_gate.get("status") or SKIP_CLOSED_NO_NEW_FACTS),
            "workload_gate": workload_gate,
        }
    if session is None:
        projection = RuntimeHealthProjectionService(db_path).latest(max_age_seconds=300.0)
        session = dict(projection.get("market_session") or {}) if projection.get("ok") else {}
    allowed, reason = offmarket_high_load_allowed(session or {})
    profile = str((session or {}).get("high_load_profile") or "disabled")
    if training_only and allowed and profile != "full":
        allowed = False
        reason = "training_only_requires_full_profile"
    payload = {
        "job_name": job_name,
        "execution_mode": execution_mode,
        "market_session": session or {},
        "limit": 4000 if training_only or profile == "full" else 250,
        "shadow_limit": 0 if training_only else (100 if profile == "full" else 30),
        "min_samples": 20,
        "profile": profile,
    }
    training_window_key = _offmarket_training_window_key(session or {}, profile)
    payload["training_window_key"] = training_window_key
    payload["worker_instance_id"] = _WORKER_INSTANCE_ID
    if not allowed:
        shadow_refresh: dict[str, Any] = {
            "ok": False,
            "skipped": True,
            "reason": "market_session_not_safe_for_shadow_refresh",
            "models": {},
        }
        result = {
            "ok": False,
            "skipped": True,
            "reason": reason,
            "shadow_refresh": shadow_refresh,
        }
        audit = _record_offmarket_audit(
            job_name=job_name, status="skipped", session=session or {}, payload=payload,
            result=result, started_at=started_at, db_path=db_path,
        )
        logger.info("[offmarket_high_load] %s skipped: %s", job_name, reason)
        return {
            "ok": True,
            "skipped": True,
            "reason": reason,
            "shadow_refresh": shadow_refresh,
            "audit": audit,
        }
    completed_window = _completed_offmarket_training_window(
        db_path=db_path,
        job_name=job_name,
        window_key=training_window_key,
    ) if allowed else None
    if completed_window is not None:
        result = {
            "ok": False,
            "skipped": True,
            "reason": "training_window_already_completed",
            "training_window_key": training_window_key,
            "completed_audit": completed_window,
        }
        logger.info(
            "[offmarket_high_load] %s skipped: training window already completed (%s)",
            job_name,
            training_window_key,
        )
        return {
            "ok": True,
            "skipped": True,
            "reason": result["reason"],
            "training_window_key": training_window_key,
            "completed_audit": completed_window,
            "audit": {**completed_window, "status": "done"},
        }

    window_claim = _claim_training_window(
        db_path=db_path,
        job_name=job_name,
        window_key=training_window_key,
        session=session or {},
        payload=payload,
        started_at=started_at,
        allow_retry_terminal=training_only,
    )
    window_audit_id = ""
    if window_claim is not None and not bool(window_claim.get("claimed")):
        guard_reason = str(window_claim.get("reason") or "training_window_not_claimed")
        existing = dict(window_claim.get("audit") or {})
        status = str(existing.get("status") or "")
        if status == "done":
            guard_reason = "training_window_already_completed"
        elif status == "blocked_memory_budget":
            guard_reason = "training_window_already_blocked_memory_budget"
        result = {
            "ok": False,
            "skipped": True,
            "reason": guard_reason,
            "training_window_key": training_window_key,
            "audit_status": status,
        }
        logger.info(
            "[offmarket_high_load] %s skipped: %s (%s)",
            job_name,
            guard_reason,
            training_window_key,
        )
        return {
            "ok": True,
            "skipped": True,
            "reason": guard_reason,
            "training_window_key": training_window_key,
            "audit": existing,
        }
    if window_claim is not None:
        window_audit_id = str(window_claim.get("audit_id") or "")
    try:
        if training_only:
            from research.position_quality_lightgbm import PositionQualityLightGBMService

            suite = [
                (
                    "position_quality_lightgbm",
                    PositionQualityLightGBMService(db_path=db_path),
                    {
                        "limit": 4000,
                        "holdout_ratio": 0.2,
                        "min_samples": 20,
                        "register": False,
                        "horizon_minutes": 30,
                        "pnl_tolerance": 0.25,
                        "symbol": "XAUUSD+",
                        "timeframe": "M5",
                    },
                )
            ]
        else:
            from backend.services.model_influence_governance import ModelInfluenceGovernanceService
            from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService

            suite = _build_shadow_model_suite(db_path)
        result: dict[str, Any] = {"models": {}}
        for model_type, model_service, train_kwargs in suite:
            if model_type != "position_quality_lightgbm" and (
                profile != "full" and model_type != "factor_governance_lightgbm"
            ):
                result["models"][model_type] = {
                    "train": {
                        "ok": False,
                        "status": "skipped",
                        "skipped": True,
                        "reason": "profile_not_full",
                        "reason_codes": ["profile_not_full"],
                    },
                    "shadow": {
                        "ok": False,
                        "skipped": True,
                        "model_type": model_type,
                        "reason": "profile_not_full",
                    },
                }
                continue
            _heartbeat_training_window(
                db_path=db_path,
                audit_id=window_audit_id,
                phase=f"train:{model_type}",
            )
            item = _train_and_score_model(
                model_type=model_type,
                model_service=model_service,
                train_kwargs=(
                    {
                        **train_kwargs,
                        "limit": int(payload["limit"]),
                        "min_samples": int(payload["min_samples"]),
                    }
                    if model_type == "position_quality_lightgbm"
                    else train_kwargs
                ),
                shadow_limit=int(payload["shadow_limit"]),
                mode="offmarket_shadow_after_train",
                score_shadow=not training_only,
            )
            result["models"][model_type] = item
            quality = dict(getattr(model_service, "last_data_quality", {}) or {})
            estimated_bytes = int(
                quality.get("input_bytes_estimate")
                or (
                    int(quality.get("unique_review_bytes") or 0)
                    + int(quality.get("selected_verdict_bytes") or 0)
                )
            )
            _heartbeat_training_window(
                db_path=db_path,
                audit_id=window_audit_id,
                phase=f"completed:{model_type}",
                input_bytes_estimate=estimated_bytes,
            )

        position_item = result["models"].get("position_quality_lightgbm") or {}
        result["train"] = dict(position_item.get("train") or {})
        # Preserve the existing top-level position shadow field for API callers.
        result["shadow"] = dict(position_item.get("shadow") or {})

        if training_only:
            result["governance"] = {
                "ok": True,
                "skipped": True,
                "reason": "training_only",
                "promotion": "skipped",
                "model_registration": "disabled",
                "v16_delegate": "skipped",
            }
            result["promoted_models"] = []
        else:
            governance = ModelInfluenceGovernanceService(db_path)
            result["reconcile_before_training"] = governance.reconcile_active_models()
            v16 = V16BrainOrchestratorService(db_path)
            promoted = []
            for model_type, item in result["models"].items():
                trained = dict(item.get("train") or {})
                if not trained.get("ok"):
                    continue
                gate = governance.evaluate_artifact(str(trained.get("artifact_path") or ""))
                item["promotion_gate"] = gate
                if not gate.get("passed"):
                    continue
                delegation = v16.delegate_model_promotion(gate, persist=True)
                item["v16_delegation"] = delegation
                command_id = str((delegation.get("command") or {}).get("command_id") or "")
                promotion = governance.promote(
                    str(trained.get("artifact_path") or ""),
                    stage="demo_canary",
                    v16_command_id=command_id,
                )
                item["promotion"] = promotion
                if promotion.get("ok"):
                    promoted.append(model_type)
            result["promoted_models"] = promoted
        trained_ok = [bool((item.get("train") or {}).get("ok")) for item in result["models"].values()]
        position_train_status = str((position_item.get("train") or {}).get("status") or "")
        status = (
            "blocked_memory_budget"
            if position_train_status == "blocked_memory_budget"
            else "done" if any(trained_ok) else "failed"
        )
        model_errors = [
            f"{model_type}:{str((item.get('train') or {}).get('error') or (item.get('train') or {}).get('reason') or '')}"
            for model_type, item in result["models"].items()
            if not bool((item.get("train") or {}).get("ok"))
            and str((item.get("train") or {}).get("error") or (item.get("train") or {}).get("reason") or "")
        ]
        audit = _record_offmarket_audit(
            job_name=job_name, status=status, session=session or {}, payload=payload,
            result=result, error=";".join(model_errors),
            started_at=started_at, db_path=db_path,
            training_window_key=training_window_key,
            phase="finished",
            worker_instance_id=_WORKER_INSTANCE_ID,
            audit_id=window_audit_id,
            input_bytes_estimate=int(
                ((position_item.get("train") or {}).get("data_quality") or {}).get("unique_review_bytes") or 0
            ) + int(
                ((position_item.get("train") or {}).get("data_quality") or {}).get("selected_verdict_bytes") or 0
            ),
        )
        return {"ok": status == "done", "status": status, "audit": audit, "result": result}
    except Exception as exc:
        audit = _record_offmarket_audit(
            job_name=job_name, status="error", session=session or {}, payload=payload,
            error=f"{type(exc).__name__}: {exc}"[:500], started_at=started_at, db_path=db_path,
            training_window_key=training_window_key,
            phase="error",
            worker_instance_id=_WORKER_INSTANCE_ID,
            audit_id=window_audit_id,
        )
        logger.warning("[offmarket_high_load] %s error: %s", job_name, exc)
        return {"ok": False, "status": "error", "audit": audit, "error": str(exc)}
