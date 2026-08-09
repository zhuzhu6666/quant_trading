"""Heavy research jobs owned by the learning worker, without live imports."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from backend.core import db as state_db
from backend.core.db import connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.core.state_store import validate_runtime_state_schema
from backend.services.runtime_health_projection import RuntimeHealthProjectionService


logger = logging.getLogger(__name__)


def run_feature_engineering_job() -> dict[str, Any]:
    """Derive/select research features and register PCA candidates."""
    try:
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
) -> dict[str, Any]:
    db_path = Path(db_path or state_db.STATE_DB)
    now = time.time()
    started = float(started_at or now)
    row = {
        "audit_id": f"{job_name}:{int(started * 1000)}",
        "job_name": job_name,
        "status": status,
        "session_status": str((session or {}).get("status") or ""),
        "high_load_profile": str((session or {}).get("high_load_profile") or "disabled"),
        "payload": payload or {},
        "result": result or {},
        "error": str(error or ""),
        "started_at": started,
        "finished_at": now,
    }
    conn = _connect(db_path)
    try:
        table_declaration = """
            CREATE TABLE IF NOT EXISTS offmarket_high_load_job_audit (
                audit_id TEXT PRIMARY KEY, job_name TEXT NOT NULL, status TEXT NOT NULL,
                session_status TEXT DEFAULT '', high_load_profile TEXT DEFAULT '',
                payload_json TEXT DEFAULT '{}', result_json TEXT DEFAULT '{}', error TEXT DEFAULT '',
                started_at REAL NOT NULL, finished_at REAL NOT NULL
            )
        """
        if is_state_db_path(db_path):
            validate_runtime_state_schema(conn, table_declaration)
        else:
            conn.execute(table_declaration)
        conn.execute(
            _sql(
                db_path,
                """
                INSERT INTO offmarket_high_load_job_audit
                (audit_id, job_name, status, session_status, high_load_profile,
                 payload_json, result_json, error, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(audit_id) DO UPDATE SET
                    status=excluded.status, payload_json=excluded.payload_json,
                    result_json=excluded.result_json, error=excluded.error,
                    finished_at=excluded.finished_at
                """,
            ),
            (
                row["audit_id"], row["job_name"], row["status"], row["session_status"],
                row["high_load_profile"], json.dumps(row["payload"], ensure_ascii=False),
                json.dumps(row["result"], ensure_ascii=False), row["error"],
                row["started_at"], row["finished_at"],
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
) -> dict[str, Any]:
    """Keep one model's training failure isolated from the other models.

    The off-market job is a suite, not a single model transaction: a sparse
    position-quality window must not hide a usable factor-governance result.
    Each item therefore owns its stable status and its optional shadow score.
    """
    try:
        trained = dict(model_service.train(**dict(train_kwargs)) or {})
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
) -> dict[str, Any]:
    """Train the PIT-v2 suite and keep every model's shadow output current."""
    job_name = "offmarket_position_quality_lightgbm"
    started_at = time.time()
    db_path = Path(db_path or state_db.STATE_DB)
    if session is None:
        projection = RuntimeHealthProjectionService(db_path).latest(max_age_seconds=300.0)
        session = dict(projection.get("market_session") or {}) if projection.get("ok") else {}
    allowed, reason = offmarket_high_load_allowed(session or {})
    profile = str((session or {}).get("high_load_profile") or "disabled")
    payload = {
        "job_name": job_name,
        "market_session": session or {},
        "limit": 4000 if profile == "full" else 250,
        "shadow_limit": 100 if profile == "full" else 30,
        "min_samples": 20,
        "profile": profile,
    }
    training_window_key = _offmarket_training_window_key(session or {}, profile)
    payload["training_window_key"] = training_window_key
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
        audit = _record_offmarket_audit(
            job_name=job_name,
            status="skipped",
            session=session or {},
            payload=payload,
            result=result,
            started_at=started_at,
            db_path=db_path,
        )
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
            "audit": audit,
        }
    try:
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
            )
            result["models"][model_type] = item

        position_item = result["models"].get("position_quality_lightgbm") or {}
        result["train"] = dict(position_item.get("train") or {})
        # Preserve the existing top-level position shadow field for API callers.
        result["shadow"] = dict(position_item.get("shadow") or {})

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
        status = "done" if any(trained_ok) else "failed"
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
        )
        return {"ok": status == "done", "status": status, "audit": audit, "result": result}
    except Exception as exc:
        audit = _record_offmarket_audit(
            job_name=job_name, status="error", session=session or {}, payload=payload,
            error=f"{type(exc).__name__}: {exc}"[:500], started_at=started_at, db_path=db_path,
        )
        logger.warning("[offmarket_high_load] %s error: %s", job_name, exc)
        return {"ok": False, "status": "error", "audit": audit, "error": str(exc)}
