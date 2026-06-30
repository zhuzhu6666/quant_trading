from __future__ import annotations

import ast
import json
import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite
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
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


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
        blockers = []
        blockers.extend(system_health.get("blocking_components") or [])
        if not model_status.get("permission_ok", True):
            blockers.append({"component": "model_permissions", "status": "blocked"})
        ready_for_frontend = not blockers
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
            "frontend_contract": {
                "preferred_entry": "/api/ops/backend-readiness",
                "model_shadow_report": "/api/learning/model/meta-lightgbm/shadow-report",
                "model_shadow_report_snapshots": "/api/learning/model/meta-lightgbm/shadow-report/snapshots",
                "model_governance_materialize": "/api/learning/model/meta-lightgbm/governance-suggestion",
                "offmarket_high_load_audits": "/api/learning/model/offmarket-high-load/audits",
                "must_not_call_live_mutation_from_model_pages": True,
            },
            "blockers": blockers,
            "known_observations": system_health.get("known_observations") or [],
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
                        else "would_require_manual_contract_change_before_live"
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
        conn = connect_sqlite(self.db_path)
        conn.row_factory = __import__("sqlite3").Row
        try:
            if not _table_exists(conn, "model_permission_audit"):
                return {"ok": True, "status": "missing_table"}
            row = conn.execute(
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
        conn = connect_sqlite(self.db_path)
        conn.row_factory = __import__("sqlite3").Row
        try:
            counts = {}
            if _table_exists(conn, "policy_suggestion"):
                rows = conn.execute(
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
                "automatic_execution_enabled": False,
            }
        finally:
            conn.close()

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
        conn = connect_sqlite(self.db_path)
        conn.row_factory = __import__("sqlite3").Row
        try:
            if not _table_exists(conn, "offmarket_high_load_job_audit"):
                return {}
            row = conn.execute(
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
