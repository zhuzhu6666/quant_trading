from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, STATE_DB_DDL, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists
from backend.ledger.service import DecisionLedger
from backend.services.policy_suggestion_context import attach_policy_suggestion_agent_context
from research.meta_model_lightgbm import MODEL_TYPE, MetaModelLightGBMService


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str, sort_keys=True)


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


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn, sql: str, params: Any = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), params)


class MetaGovernanceService:
    """Advisory-only bridge from meta shadow reports into human/governor review."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)
        self._ensure_tables()

    def _conn(self) -> sqlite3.Connection:
        conn = get_state_pg_conn() if _use_pg(self.db_path) else connect_sqlite(self.db_path)
        if not _use_pg(self.db_path):
            conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self) -> None:
        with self._conn() as conn:
            if _conn_is_pg(conn):
                if not state_table_exists(conn, "meta_shadow_report_snapshot"):
                    raise RuntimeError("missing state table: meta_shadow_report_snapshot")
                return
            conn.executescript(STATE_DB_DDL)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta_shadow_report_snapshot (
                    report_id TEXT PRIMARY KEY,
                    model_type TEXT NOT NULL,
                    model_version TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    accuracy REAL DEFAULT 0.0,
                    evaluated_count INTEGER DEFAULT 0,
                    audit_count INTEGER DEFAULT 0,
                    artifact_path TEXT DEFAULT '',
                    payload_json TEXT DEFAULT '{}',
                    created_at REAL NOT NULL DEFAULT 0.0
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_meta_shadow_report_snapshot_created
                ON meta_shadow_report_snapshot(created_at)
                """
            )
            conn.commit()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

    def create_shadow_report_snapshot(
        self,
        *,
        report: dict[str, Any] | None = None,
        limit: int = 200,
        include_samples: bool = False,
        source: str = "manual",
    ) -> dict[str, Any]:
        report = report or MetaModelLightGBMService(db_path=self.db_path).build_shadow_report(
            limit=limit,
            include_samples=include_samples,
        )
        artifact = dict(report.get("artifact_summary") or {})
        report_id = self._new_id("msr")
        now = time.time()
        with self._conn() as conn:
            _execute(
                conn,
                """
                INSERT INTO meta_shadow_report_snapshot
                (report_id, model_type, model_version, source, accuracy,
                 evaluated_count, audit_count, artifact_path, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    str(report.get("model_type") or MODEL_TYPE),
                    str(artifact.get("model_version") or ""),
                    str(source or "manual"),
                    _safe_float(report.get("accuracy")),
                    int(report.get("evaluated_count") or 0),
                    int(report.get("audit_count") or 0),
                    str(artifact.get("artifact_path") or ""),
                    _dumps(report),
                    now,
                ),
            )
            conn.commit()
        return {
            "ok": True,
            "schema_version": "meta_shadow_report_snapshot.v1",
            "report_id": report_id,
            "created_at": now,
            "report": report,
        }

    def list_shadow_report_snapshots(self, *, limit: int = 20) -> dict[str, Any]:
        with self._conn() as conn:
            rows = _execute(
                conn,
                """
                SELECT *
                FROM meta_shadow_report_snapshot
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        items = []
        for row in rows:
            items.append(
                {
                    "report_id": str(row["report_id"] or ""),
                    "model_type": str(row["model_type"] or ""),
                    "model_version": str(row["model_version"] or ""),
                    "source": str(row["source"] or ""),
                    "accuracy": _safe_float(row["accuracy"]),
                    "evaluated_count": int(row["evaluated_count"] or 0),
                    "audit_count": int(row["audit_count"] or 0),
                    "artifact_path": str(row["artifact_path"] or ""),
                    "report": _loads(row["payload_json"], {}),
                    "created_at": _safe_float(row["created_at"]),
                }
            )
        return {"items": items, "count": len(items)}

    def materialize_meta_governance_suggestion(
        self,
        *,
        report: dict[str, Any] | None = None,
        limit: int = 200,
        snapshot: bool = True,
        source: str = "meta_shadow_report",
    ) -> dict[str, Any]:
        report = report or MetaModelLightGBMService(db_path=self.db_path).build_shadow_report(
            limit=limit,
            include_samples=False,
        )
        snapshot_result = self.create_shadow_report_snapshot(
            report=report,
            include_samples=False,
            source=source,
        ) if snapshot else {}
        suggestion = self._suggestion_from_report(report, snapshot_result=snapshot_result, db_path=self.db_path)
        now = time.time()
        with self._conn() as conn:
            existing = _execute(
                conn,
                """
                SELECT suggestion_id, status
                FROM policy_suggestion
                WHERE scope_type='meta_model'
                  AND scope_key=?
                  AND action=?
                  AND status IN ('proposed', 'pending_review')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (MODEL_TYPE, suggestion["action"]),
            ).fetchone()
            if existing:
                suggestion_id = str(existing["suggestion_id"] or "")
                _execute(
                    conn,
                    """
                    UPDATE policy_suggestion
                    SET confidence=?, reason=?, evidence_json=?, reviewed_at=0, review_note=''
                    WHERE suggestion_id=?
                    """,
                    (
                        suggestion["confidence"],
                        suggestion["reason"],
                        _dumps(suggestion["evidence"]),
                        suggestion_id,
                    ),
                )
            else:
                suggestion_id = self._new_id("psg")
                _execute(
                    conn,
                    """
                    INSERT INTO policy_suggestion
                    (suggestion_id, scope_type, scope_key, action, confidence, reason,
                     evidence_json, status, reviewed_at, review_note, created_at)
                    VALUES (?, 'meta_model', ?, ?, ?, ?, ?, 'proposed', 0, '', ?)
                    """,
                    (
                        suggestion_id,
                        MODEL_TYPE,
                        suggestion["action"],
                        suggestion["confidence"],
                        suggestion["reason"],
                        _dumps(suggestion["evidence"]),
                        now,
                    ),
                )
            conn.commit()

        ledger_id = DecisionLedger(str(self.db_path)).log_decision(
            event_type="meta_model_governance_suggestion",
            symbol="XAUUSD+",
            timeframe="M5",
            decision_ts=now,
            portfolio_state={"source": source, "snapshot": snapshot_result},
            risk_state={
                "advisory_only": True,
                "live_trading": False,
                "approval_required": True,
            },
            action_score=float(suggestion["confidence"]),
            action_reason=suggestion["action"],
            action_json={
                "schema_version": "meta_model_governance_suggestion.v1",
                "suggestion_id": suggestion_id,
                "suggestion": suggestion,
                "report_summary": self._compact_report(report),
                "advisory_only": True,
                "forbidden_actions": [
                    "place_orders",
                    "close_positions",
                    "change_hard_risk_limits",
                    "change_factor_weights_without_review",
                    "bypass_risk_policy",
                ],
            },
        )
        return {
            "ok": True,
            "schema_version": "meta_model_governance_materialization.v1",
            "suggestion_id": suggestion_id,
            "ledger_decision_id": ledger_id,
            "suggestion": suggestion,
            "snapshot": snapshot_result,
            "report_summary": self._compact_report(report),
            "advisory_only": True,
            "requires_review": True,
            "capabilities": {
                "live_trading": False,
                "can_place_orders": False,
                "can_close_positions": False,
                "can_change_risk_limits": False,
            },
        }

    @staticmethod
    def _suggestion_from_report(report: dict[str, Any], *, snapshot_result: dict[str, Any], db_path: str | Path = STATE_DB) -> dict[str, Any]:
        accuracy = _safe_float(report.get("accuracy"))
        evaluated = int(report.get("evaluated_count") or 0)
        posture_distribution = dict(report.get("posture_distribution") or {})
        contract_rate = int(posture_distribution.get("contract") or 0) / max(evaluated, 1)
        artifact = dict(report.get("artifact_summary") or {})
        metrics = dict(artifact.get("metrics") or {})
        holdout = dict(metrics.get("holdout") or {})
        holdout_accuracy = _safe_float(holdout.get("accuracy"))
        action = "observe_meta_model_shadow"
        reason = "meta model shadow report requires continued observation"
        confidence = min(0.65, max(0.2, accuracy))
        if holdout_accuracy < 0.55:
            action = "block_meta_model_promotion"
            reason = f"meta model holdout accuracy too low for promotion: {holdout_accuracy:.3f}"
            confidence = min(0.85, max(0.5, 1.0 - holdout_accuracy))
        elif contract_rate >= 0.4:
            action = "review_meta_contract_posture"
            reason = f"meta model predicts elevated contract posture rate={contract_rate:.3f}"
            confidence = min(0.75, max(0.45, contract_rate))
        evidence = {
            "schema_version": "meta_model_governance_advisory.v1",
            "report_id": str(snapshot_result.get("report_id") or ""),
            "model_type": MODEL_TYPE,
            "accuracy": accuracy,
            "evaluated_count": evaluated,
            "holdout_accuracy": holdout_accuracy,
            "posture_distribution": posture_distribution,
            "rule_comparison": dict(report.get("rule_comparison") or {}),
            "artifact_path": str(artifact.get("artifact_path") or ""),
            "advisory_only": True,
            "approval_path": "human_or_governor_review_only",
            "safe_for_live_trading": False,
        }
        evidence = attach_policy_suggestion_agent_context(
            evidence,
            source_agent="lightgbm_shadow_models",
            scope_type="meta_model",
            action=action,
            requested_writes=[],
            status="proposed",
            impact_level="shadow",
            db_path=db_path,
        )
        return {
            "action": action,
            "confidence": round(confidence, 6),
            "reason": reason,
            "evidence": evidence,
        }

    @staticmethod
    def _compact_report(report: dict[str, Any]) -> dict[str, Any]:
        artifact = dict(report.get("artifact_summary") or {})
        metrics = dict(artifact.get("metrics") or {})
        return {
            "model_type": str(report.get("model_type") or MODEL_TYPE),
            "accuracy": _safe_float(report.get("accuracy")),
            "evaluated_count": int(report.get("evaluated_count") or 0),
            "confusion_matrix": dict(report.get("confusion_matrix") or {}),
            "posture_distribution": dict(report.get("posture_distribution") or {}),
            "rule_comparison": {
                key: value
                for key, value in dict(report.get("rule_comparison") or {}).items()
                if key != "disagreements"
            },
            "artifact": {
                "artifact_path": str(artifact.get("artifact_path") or ""),
                "model_version": str(artifact.get("model_version") or ""),
                "metrics": metrics,
            },
        }
