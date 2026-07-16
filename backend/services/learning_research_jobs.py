"""Heavy research jobs owned by the learning worker, without live imports."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from backend.core import db as state_db
from backend.core.db import connect_sqlite, get_state_pg_conn, is_state_db_path
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
        conn.execute(
            _sql(
                db_path,
                """
                CREATE TABLE IF NOT EXISTS offmarket_high_load_job_audit (
                    audit_id TEXT PRIMARY KEY, job_name TEXT NOT NULL, status TEXT NOT NULL,
                    session_status TEXT DEFAULT '', high_load_profile TEXT DEFAULT '',
                    payload_json TEXT DEFAULT '{}', result_json TEXT DEFAULT '{}', error TEXT DEFAULT '',
                    started_at REAL NOT NULL, finished_at REAL NOT NULL
                )
                """,
            )
        )
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


def run_offmarket_position_quality_job(
    *,
    session: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Train the PIT-v2 model suite and promote only evidence-ready artifacts."""
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
    if not allowed:
        result = {"ok": False, "skipped": True, "reason": reason}
        audit = _record_offmarket_audit(
            job_name=job_name, status="skipped", session=session or {}, payload=payload,
            result=result, started_at=started_at, db_path=db_path,
        )
        logger.info("[offmarket_high_load] %s skipped: %s", job_name, reason)
        return {"ok": True, "skipped": True, "reason": reason, "audit": audit}
    try:
        from backend.services.model_influence_governance import ModelInfluenceGovernanceService
        from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService
        from research.factor_governance_lightgbm import FactorGovernanceLightGBMService
        from research.meta_model_lightgbm import MetaModelLightGBMService
        from research.open_quality_lightgbm import OpenQualityLightGBMService
        from research.position_quality_lightgbm import PositionQualityLightGBMService

        service = PositionQualityLightGBMService(db_path=db_path)
        train = service.train(
            limit=int(payload["limit"]), holdout_ratio=0.2,
            min_samples=int(payload["min_samples"]), register=True,
            symbol="XAUUSD+", timeframe="M5",
        )
        result: dict[str, Any] = {"train": train, "models": {"position_quality_lightgbm": {"train": train}}}
        if train.get("ok"):
            result["shadow"] = service.score_samples(
                artifact_path=train.get("artifact_path"),
                limit=int(payload["shadow_limit"]),
                mode="offmarket_shadow_after_train",
            )
        suite = [
            ("open_quality_lightgbm", OpenQualityLightGBMService(db_path=db_path), {
                "limit": 3000, "holdout_ratio": 0.25, "min_samples": 100, "register": True,
            }),
            ("factor_governance_lightgbm", FactorGovernanceLightGBMService(db_path=db_path), {
                "limit": 5000, "holdout_ratio": 0.25, "min_samples": 100, "register": True,
            }),
            ("meta_model_lightgbm", MetaModelLightGBMService(db_path=db_path), {
                "limit": 3000, "window": 12, "horizon": 3, "holdout_ratio": 0.25,
                "min_samples": 100, "register": True,
            }),
        ]
        if profile == "full":
            for model_type, model_service, train_kwargs in suite:
                result["models"][model_type] = {"train": model_service.train(**train_kwargs)}

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
        audit = _record_offmarket_audit(
            job_name=job_name, status=status, session=session or {}, payload=payload,
            result=result, error=str(train.get("error") or ""),
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
