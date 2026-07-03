from __future__ import annotations

import ast
import json
import time
from pathlib import Path
from typing import Any

from backend.core.db import (
    DUCKDB_EXTERNAL,
    STATE_DB,
    connect_duckdb,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_pg_enabled,
    state_table_columns,
    state_table_exists,
)
from backend.services.meta_governance import MetaGovernanceService
from research.meta_model_lightgbm import MetaModelLightGBMService


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_PATH = PROJECT_ROOT / "logs" / "backend_uvicorn.log"

KNOWN_OBSERVATION_COMPONENTS = {
    "l2_depth": "known_l2_depth_gap",
    "disk_space": "known_disk_space_degraded",
    "bar_m1": "m1_data_feed_observation",
}
BLOCKING_COMPONENTS = {
    "ctrader_bridge",
    "live_loop",
    "db_ctrader_data",
    "db_ticks",
    "db_l2",
}


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _table_exists(conn: Any, table: str) -> bool:
    return state_table_exists(conn, table)


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect_state(db_path: str | Path = STATE_DB):
    conn = get_state_pg_conn(read_only=True) if _use_pg(db_path) else connect_sqlite(db_path, read_only=True)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


def _conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn, sql: str, params: Any = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), params)


class BackendReadinessService:
    """Aggregated backend contract for the mini-program/backend handoff."""

    def __init__(self, *, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    def build(self) -> dict[str, Any]:
        live_status = self._live_status()
        market_session = dict(live_status.get("market_session") or {})
        system_health = self._system_health()
        model_status = self._model_status()
        high_load = self._high_load_status(market_session)
        governance = self._governance_status()
        factor_data = self._factor_data_status()
        governance_freshness = self._governance_freshness_status()
        runtime_weight_integrity = self._runtime_weight_integrity_status()
        execution_semantics = self._execution_semantics_status()
        startup_status = self._startup_status()
        config_runtime_drift = self._config_runtime_drift_status()
        audit_health = self._audit_health_status()
        background_jobs = self._background_jobs_status()
        is_runtime_state_db = _use_pg(self.db_path)
        blockers = []
        blockers.extend(system_health.get("blocking_components") or [])
        execution_blockers = list(execution_semantics.get("blocking_components") or [])
        if is_runtime_state_db:
            blockers.extend(execution_blockers)
        startup_blockers = list(startup_status.get("blocking_components") or [])
        if is_runtime_state_db and execution_semantics.get("effective_send_orders"):
            blockers.extend(startup_blockers)
        model_permission_blocked = not model_status.get("permission_ok", True)
        if is_runtime_state_db and model_permission_blocked:
            blockers.append({"component": "model_permissions", "status": "blocked"})
        ready_for_frontend = not blockers
        known_observations = []
        known_observations.extend(system_health.get("known_observations") or [])
        if not is_runtime_state_db:
            known_observations.extend(
                {
                    **item,
                    "classification": "execution_semantics_offline_context",
                }
                for item in execution_blockers
            )
        known_observations.extend(startup_status.get("known_observations") or [])
        if not is_runtime_state_db or not execution_semantics.get("effective_send_orders"):
            known_observations.extend(
                {
                    **item,
                    "classification": "startup_degraded_non_live",
                }
                for item in startup_blockers
            )
        if not is_runtime_state_db and model_permission_blocked:
            known_observations.append({"component": "model_permissions", "status": "blocked", "classification": "offline_context"})
        known_observations.extend(config_runtime_drift.get("known_observations") or [])
        known_observations.extend(audit_health.get("known_observations") or [])
        return {
            "ok": True,
            "schema_version": "backend_readiness.v1",
            "generated_at": time.time(),
            "ready_for_frontend": ready_for_frontend,
            "backend_service": self._service_status(),
            "system_health": system_health,
            "market_session": market_session,
            "live": {
                "ctrader": live_status.get("ctrader") or {},
                "loop": live_status.get("loop") or {},
                "readiness": live_status.get("readiness") or {},
            },
            "high_load": high_load,
            "models": model_status,
            "governance": governance,
            "factor_data": factor_data,
            "governance_freshness": governance_freshness,
            "runtime_weight_integrity": runtime_weight_integrity,
            "execution_semantics": execution_semantics,
            "startup": startup_status,
            "config_runtime_drift": config_runtime_drift,
            "mutation_policy": self._mutation_policy_status(),
            "audit_health": audit_health,
            "background_jobs": background_jobs,
            "frontend_contract": {
                "preferred_entry": "/api/ops/backend-readiness",
                "model_shadow_report": "/api/learning/model/meta-lightgbm/shadow-report",
                "model_shadow_report_snapshots": "/api/learning/model/meta-lightgbm/shadow-report/snapshots",
                "model_governance_materialize": "/api/learning/model/meta-lightgbm/governance-suggestion",
                "offmarket_high_load_audits": "/api/learning/model/offmarket-high-load/audits",
                "must_not_call_live_mutation_from_model_pages": True,
            },
            "blockers": blockers,
            "known_observations": known_observations,
        }

    @staticmethod
    def _live_status() -> dict[str, Any]:
        try:
            from backend.services.live_service import get_status

            return get_status()
        except Exception as exc:
            return {"error": str(exc), "market_session": {}}

    @staticmethod
    def _service_status() -> dict[str, Any]:
        return {
            "service": "quant-backend.service",
            "managed_by": "systemd",
            "port": 8000,
            "status": "running",
        }

    @staticmethod
    def _execution_semantics_status() -> dict[str, Any]:
        try:
            from backend.services.execution_semantics import current_execution_semantics

            semantics = current_execution_semantics().to_dict()
        except Exception as exc:
            semantics = {
                "system_mode": "unknown",
                "ctrader_send_orders": False,
                "factor_dry_run": True,
                "effective_send_orders": False,
                "blocking_reason": f"{type(exc).__name__}: {exc}",
            }
        blocking_reason = str(semantics.get("blocking_reason") or "")
        return {
            **semantics,
            "blocking_components": (
                [{"component": "execution_semantics", "status": "critical", "reason": blocking_reason}]
                if blocking_reason
                else []
            ),
        }

    @staticmethod
    def _startup_status() -> dict[str, Any]:
        try:
            from backend.services.startup_status import startup_issues

            issues = startup_issues()
        except Exception:
            issues = []
        return {
            "issues": issues,
            "blocking_components": [
                {"component": item.get("component"), "status": item.get("status"), "message": item.get("message")}
                for item in issues
                if item.get("blocking")
            ],
            "known_observations": [
                {
                    "component": item.get("component"),
                    "status": item.get("status"),
                    "classification": "startup_degraded",
                    "message": item.get("message"),
                }
                for item in issues
                if not item.get("blocking")
            ],
        }

    @staticmethod
    def _config_runtime_drift_status() -> dict[str, Any]:
        try:
            from backend.services.config_service import config_runtime_drift

            drift = config_runtime_drift()
        except Exception as exc:
            drift = {
                "drift": True,
                "changed_keys": [],
                "changed_key_count": 0,
                "semantic_drift": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
        observations = []
        if drift.get("semantic_drift"):
            observations.append({"component": "config_runtime_drift", "status": "degraded", "reason": "semantic_drift"})
        return {**drift, "blocking_components": [], "known_observations": observations}

    @staticmethod
    def _mutation_policy_status() -> dict[str, Any]:
        try:
            from backend.services.mutation_audit import mutation_policy_contract

            return {"schema_version": "mutation_policy.v1", "classes": mutation_policy_contract()}
        except Exception as exc:
            return {"schema_version": "mutation_policy.v1", "classes": {}, "error": str(exc)}

    @staticmethod
    def _audit_health_status() -> dict[str, Any]:
        try:
            from backend.services.mutation_audit import audit_health

            health = audit_health()
        except Exception as exc:
            health = {"ok": False, "last_error": str(exc)}
        observations = []
        if not health.get("ok", True):
            observations.append({"component": "mutation_audit", "status": "critical", "reason": health.get("last_error", "")})
        return {**health, "blocking_components": [], "known_observations": observations}

    @staticmethod
    def _background_jobs_status() -> dict[str, Any]:
        try:
            from backend.jobs import get_job_manager

            jobs = [job.to_dict() for job in get_job_manager().list()]
        except Exception as exc:
            return {"ok": False, "error": str(exc), "running": 0, "failed_recent": 0, "jobs": []}
        running = [job for job in jobs if str(job.get("status") or "").lower() in {"running", "pending"}]
        failed = [job for job in jobs if str(job.get("status") or "").lower() in {"failed", "error"}]
        return {
            "ok": True,
            "running": len(running),
            "failed_recent": len(failed),
            "jobs": jobs[-20:],
        }

    def _model_status(self) -> dict[str, Any]:
        service = MetaModelLightGBMService(db_path=self.db_path)
        report = service.build_shadow_report(limit=200, include_samples=False)
        artifact = dict(report.get("artifact_summary") or {})
        metrics = dict(artifact.get("metrics") or {})
        holdout = dict(metrics.get("holdout") or {})
        holdout_accuracy = _safe_float(holdout.get("accuracy"))
        evaluated_count = int(report.get("evaluated_count") or 0)
        permission = self._latest_permission_audit("meta_model_lightgbm")
        eligible = (
            evaluated_count >= 200
            and holdout_accuracy >= 0.6
            and bool(permission.get("ok", True))
            and bool((metrics or {}).get("safe_for_live_trading", False))
        )
        return {
            "meta_lightgbm": {
                "report": report,
                "promotion_gate": {
                    "eligible_for_live": False,
                    "eligible_for_governor_review": bool(evaluated_count >= 30),
                    "computed_live_eligibility_would_be": eligible,
                    "reason": (
                        "shadow_only_artifact"
                        if not eligible
                        else "would_require_governance_contract_change_before_live"
                    ),
                    "min_holdout_accuracy": 0.6,
                    "holdout_accuracy": holdout_accuracy,
                    "min_evaluated_count": 200,
                    "evaluated_count": evaluated_count,
                },
            },
            "permission_ok": bool(permission.get("ok", True)),
            "latest_permission_audit": permission,
        }

    def _latest_permission_audit(self, model_type: str) -> dict[str, Any]:
        conn = _connect_state(self.db_path)
        try:
            if not _table_exists(conn, "model_permission_audit"):
                return {"ok": True, "status": "missing_table"}
            row = _execute(
                conn,
                """
                SELECT *
                FROM model_permission_audit
                WHERE model_type=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (model_type,),
            ).fetchone()
            if not row:
                return {"ok": True, "status": "none"}
            keys = set(row.keys())
            result = _loads(row["result_json"], {}) if "result_json" in keys else {
                "capabilities": _loads(row["capabilities_json"], {}) if "capabilities_json" in keys else {},
                "violations": _loads(row["violations_json"], []) if "violations_json" in keys else [],
                "context": _loads(row["context_json"], {}) if "context_json" in keys else {},
                "reason": str(row["reason"] or "") if "reason" in keys else "",
            }
            status = str(row["status"] or "")
            return {
                "ok": status != "blocked",
                "audit_id": str(row["audit_id"] or ""),
                "model_type": str(row["model_type"] or ""),
                "status": status,
                "result": result,
                "created_at": _safe_float(row["created_at"]),
            }
        finally:
            conn.close()

    def _governance_status(self) -> dict[str, Any]:
        conn = _connect_state(self.db_path)
        try:
            try:
                from config.runtime_config import shared as runtime_config

                cfg = runtime_config()
                autonomy_mode = str(getattr(cfg, "autonomy_mode", "") or "manual")
                demo_auto_apply = bool(getattr(cfg, "autonomy_demo_auto_apply", False))
            except Exception:
                autonomy_mode = "unknown"
                demo_auto_apply = False
            automatic_execution_enabled = autonomy_mode == "demo_autonomous" and demo_auto_apply
            counts = {}
            if _table_exists(conn, "policy_suggestion"):
                rows = _execute(
                    conn,
                    """
                    SELECT status, COUNT(*) AS n
                    FROM policy_suggestion
                    GROUP BY status
                    """
                ).fetchall()
                counts = {str(row["status"] or "unknown"): int(row["n"] or 0) for row in rows}
            snapshots = MetaGovernanceService(self.db_path).list_shadow_report_snapshots(limit=5)
            return {
                "policy_suggestion_counts": counts,
                "pending_review_count": int(counts.get("proposed", 0)) + int(counts.get("pending_review", 0)),
                "meta_shadow_report_snapshots": snapshots,
                "automatic_execution_enabled": automatic_execution_enabled,
                "autonomy_mode": autonomy_mode,
                "autonomy_demo_auto_apply": demo_auto_apply,
            }
        finally:
            conn.close()

    def _factor_data_status(self) -> dict[str, Any]:
        state_counts: dict[str, Any] = {}
        conn = _connect_state(self.db_path)
        try:
            if _table_exists(conn, "factor_health"):
                rows = _execute(
                    conn,
                    "SELECT status, COUNT(*) AS n FROM factor_health GROUP BY status"
                ).fetchall()
                state_counts["factor_health_by_status"] = {
                    str(row["status"] or "UNKNOWN"): int(row["n"] or 0) for row in rows
                }
                state_counts["factor_health_total"] = sum(state_counts["factor_health_by_status"].values())
        finally:
            conn.close()

        external_counts: dict[str, int] = {}
        try:
            dconn = connect_duckdb(DUCKDB_EXTERNAL, read_only=True)
            try:
                for table in ["cot_gold", "etf_holdings", "macro_daily", "cb_gold", "etf_daily"]:
                    try:
                        external_counts[table] = int(dconn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
                    except Exception:
                        external_counts[table] = 0
            finally:
                dconn.close()
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "state": state_counts,
                "external_counts": external_counts,
            }

        return {
            "ok": bool(state_counts.get("factor_health_total", 0) > 0 and external_counts.get("macro_daily", 0) > 0),
            "state": state_counts,
            "external_counts": external_counts,
            "canonical_frame": "data.factor_frame.FactorFrameBuilder",
            "last_enrichment": self._last_factor_frame_enrichment(),
        }

    @staticmethod
    def _last_factor_frame_enrichment() -> dict[str, Any]:
        try:
            from data.factor_frame import latest_factor_frame_status

            status = latest_factor_frame_status()
            return {
                "ok": bool(status.get("ok", True)),
                "updated_at": _safe_float(status.get("updated_at")),
                "error": str(status.get("error") or ""),
            }
        except Exception as exc:
            return {"ok": False, "updated_at": 0.0, "error": str(exc)}

    def _governance_freshness_status(self) -> dict[str, Any]:
        tables = [
            "meta_model_shadow_audit",
            "factor_governance_shadow_audit",
            "position_quality_shadow_audit",
            "shadow_factor_perf",
            "factor_health",
        ]
        now = time.time()
        freshness: dict[str, Any] = {}
        conn = _connect_state(self.db_path)
        try:
            for table in tables:
                if not _table_exists(conn, table):
                    freshness[table] = {"status": "missing_table"}
                    continue
                ts_col = "updated_at"
                cols = state_table_columns(conn, table)
                if "created_at" in cols:
                    ts_col = "created_at"
                elif "updated_at" in cols:
                    ts_col = "updated_at"
                else:
                    freshness[table] = {"status": "no_timestamp"}
                    continue
                latest = _safe_float(_execute(conn, f"SELECT MAX({ts_col}) AS ts FROM {table}").fetchone()["ts"])
                age_sec = max(0.0, now - latest) if latest > 0 else None
                freshness[table] = {
                    "latest_ts": latest,
                    "age_seconds": round(age_sec, 3) if age_sec is not None else None,
                    "status": "fresh" if age_sec is not None and age_sec <= 3 * 86400 else "stale_or_empty",
                }
        finally:
            conn.close()
        return {"tables": freshness}

    @staticmethod
    def _runtime_weight_integrity_status() -> dict[str, Any]:
        try:
            from config.runtime_config import shared as runtime_config

            cfg = runtime_config()
            weights = dict(getattr(cfg, "factor_portfolio_weights", {}) or {})
            signal_cfg = dict(getattr(cfg, "factor_signal_config", {}) or {})
            missing_weight = sorted(set(signal_cfg) - set(weights))
            orphan_weight = sorted(set(weights) - set(signal_cfg))
            return {
                "ok": bool(weights),
                "weight_count": len(weights),
                "signal_config_count": len(signal_cfg),
                "signal_without_weight": missing_weight[:50],
                "weight_without_signal_config": orphan_weight[:50],
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _high_load_status(self, market_session: dict[str, Any]) -> dict[str, Any]:
        latest = self._latest_offmarket_audit()
        return {
            "allowed_now": bool(market_session.get("high_load_allowed")),
            "profile": str(market_session.get("high_load_profile") or "disabled"),
            "session_status": str(market_session.get("status") or ""),
            "can_run_training_with_positions": str(market_session.get("high_load_profile") or "") == "limited_with_positions",
            "requires_closed_confirmation": True,
            "latest_audit": latest,
        }

    def _latest_offmarket_audit(self) -> dict[str, Any]:
        conn = _connect_state(self.db_path)
        try:
            if not _table_exists(conn, "offmarket_high_load_job_audit"):
                return {}
            row = _execute(
                conn,
                """
                SELECT *
                FROM offmarket_high_load_job_audit
                ORDER BY started_at DESC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return {}
            return {
                "audit_id": str(row["audit_id"] or ""),
                "job_name": str(row["job_name"] or ""),
                "status": str(row["status"] or ""),
                "session_status": str(row["session_status"] or ""),
                "high_load_profile": str(row["high_load_profile"] or ""),
                "error": str(row["error"] or ""),
                "started_at": _safe_float(row["started_at"]),
                "finished_at": _safe_float(row["finished_at"]),
                "result": _loads(row["result_json"], {}),
            }
        finally:
            conn.close()

    @staticmethod
    def _system_health() -> dict[str, Any]:
        line = BackendReadinessService._latest_system_health_line()
        if not line:
            return {
                "overall": "unknown",
                "display_overall": "unknown",
                "score": 0.0,
                "components": {},
                "blocking_components": [],
                "known_observations": [],
            }
        components = BackendReadinessService._parse_components(line)
        overall = BackendReadinessService._parse_token_after(line, "overall=") or "unknown"
        score = _safe_float(BackendReadinessService._parse_token_after(line, "score="))
        blocking = []
        observations = []
        for name, status in components.items():
            status_text = str(status)
            if status_text not in {"degraded", "critical"}:
                continue
            if name in BLOCKING_COMPONENTS and status_text == "critical":
                blocking.append({"component": name, "status": status_text})
            else:
                observations.append(
                    {
                        "component": name,
                        "status": status_text,
                        "classification": KNOWN_OBSERVATION_COMPONENTS.get(name, "non_blocking_observation"),
                    }
                )
        display_overall = "critical" if blocking else "degraded" if observations else overall
        return {
            "overall": overall,
            "display_overall": display_overall,
            "score": score,
            "components": components,
            "blocking_components": blocking,
            "known_observations": observations,
            "source": str(LOG_PATH),
        }

    @staticmethod
    def _latest_system_health_line() -> str:
        if not LOG_PATH.exists():
            return ""
        try:
            with LOG_PATH.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(size - 250000, 0))
                lines = f.read().decode("utf-8", errors="replace").splitlines()
        except Exception:
            return ""
        for line in reversed(lines):
            if "[system_health]" in line and "components=" in line:
                return line
        return ""

    @staticmethod
    def _parse_components(line: str) -> dict[str, str]:
        marker = "components="
        if marker not in line:
            return {}
        after = line.split(marker, 1)[1]
        raw = after.split(" errors=", 1)[0].strip()
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            return {}
        return {}

    @staticmethod
    def _parse_token_after(line: str, marker: str) -> str:
        if marker not in line:
            return ""
        after = line.split(marker, 1)[1]
        return after.split()[0].strip()
