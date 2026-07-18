from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from backend.core.db import EXPERIMENTS_DB, prepare_experiments_store
from research.model_promotion import ModelPromotionGate
from research.offline_trainer import MODEL_TYPE


@dataclass
class ShadowModelCandidate:
    candidate_id: str
    model_type: str
    artifact_path: str
    artifact_sha256: str
    symbol: str
    timeframe: str
    status: str
    gate_decision: str
    gate_json: str
    registry_version_json: str
    note: str
    created_at: float
    updated_at: float

    @property
    def gate(self) -> dict:
        return json.loads(self.gate_json or "{}")

    @property
    def registry_version(self) -> dict | None:
        value = json.loads(self.registry_version_json or "null")
        return value if isinstance(value, dict) else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "model_type": self.model_type,
            "artifact_path": self.artifact_path,
            "artifact_sha256": self.artifact_sha256,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "status": self.status,
            "gate_decision": self.gate_decision,
            "gate": self.gate,
            "registry_version": self.registry_version,
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ModelShadowQueue:
    """Persistent queue for offline model artifacts approved for shadow validation."""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or str(EXPERIMENTS_DB)
        self._ensure_table()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_table(self) -> None:
        prepare_experiments_store(self._db_path)

    def queue_from_gate(
        self,
        *,
        gate_result: dict,
        note: str = "",
    ) -> dict:
        if not gate_result.get("ok") or gate_result.get("decision") != "shadow_candidate":
            return {
                "ok": False,
                "queued": False,
                "error": "gate result is not eligible for shadow validation",
                "gate": gate_result,
            }

        now = time.time()
        model_type = str(gate_result.get("model_type") or MODEL_TYPE)
        artifact_path = str(gate_result.get("artifact_path") or "")
        artifact_sha256 = str(gate_result.get("artifact_sha256") or "")
        registry_version = gate_result.get("registry_version") or None
        symbol = str((registry_version or {}).get("symbol") or "XAUUSD+")
        timeframe = str((registry_version or {}).get("timeframe") or "M5")
        candidate_id = f"{model_type}:{symbol}:{timeframe}:{artifact_sha256[:16]}"
        gate_json = json.dumps(gate_result, ensure_ascii=False, sort_keys=True)
        registry_json = json.dumps(registry_version, ensure_ascii=False, sort_keys=True)

        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO model_shadow_candidate
                (candidate_id, model_type, artifact_path, artifact_sha256, symbol, timeframe,
                 status, gate_decision, gate_json, registry_version_json, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(model_type, artifact_sha256, symbol, timeframe) DO UPDATE SET
                    status=CASE
                        WHEN model_shadow_candidate.status IN ('queued', 'running') THEN model_shadow_candidate.status
                        ELSE 'queued'
                    END,
                    gate_decision=excluded.gate_decision,
                    gate_json=excluded.gate_json,
                    registry_version_json=excluded.registry_version_json,
                    note=excluded.note,
                    updated_at=excluded.updated_at
                """,
                (
                    candidate_id,
                    model_type,
                    artifact_path,
                    artifact_sha256,
                    symbol,
                    timeframe,
                    str(gate_result.get("decision") or ""),
                    gate_json,
                    registry_json,
                    note,
                    now,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM model_shadow_candidate WHERE model_type=? AND artifact_sha256=? AND symbol=? AND timeframe=?",
                (model_type, artifact_sha256, symbol, timeframe),
            ).fetchone()
            candidate = self._row(row)
            return {"ok": True, "queued": True, "candidate": candidate.to_dict()}
        finally:
            conn.close()

    def queue(
        self,
        *,
        model_type: str = MODEL_TYPE,
        artifact_path: str | None = None,
        version: int | None = None,
        registry_db_path: str | None = None,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
        min_samples: int = 20,
        min_holdout_samples: int = 5,
        min_oos_acc: float = 0.52,
        min_features: int = 1,
        require_snapshot_ready: bool = True,
        note: str = "",
    ) -> dict:
        gate = ModelPromotionGate().evaluate(
            model_type=model_type,
            artifact_path=artifact_path,
            version=version,
            registry_db_path=registry_db_path,
            symbol=symbol,
            timeframe=timeframe,
            min_samples=min_samples,
            min_holdout_samples=min_holdout_samples,
            min_oos_acc=min_oos_acc,
            min_features=min_features,
            require_snapshot_ready=require_snapshot_ready,
        )
        result = self.queue_from_gate(gate_result=gate, note=note)
        if result.get("ok"):
            result["gate"] = gate
        return result

    def list_candidates(
        self,
        *,
        status: str | None = None,
        model_type: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if model_type:
            clauses.append("model_type=?")
            params.append(model_type)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        conn = self._conn()
        try:
            rows = conn.execute(
                f"""
                SELECT *
                FROM model_shadow_candidate
                {where}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (*params, int(limit)),
            ).fetchall()
            return [self._row(row).to_dict() for row in rows]
        finally:
            conn.close()

    def get_candidate(self, candidate_id: str) -> dict | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM model_shadow_candidate WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            return self._row(row).to_dict() if row else None
        finally:
            conn.close()

    def update_status(self, candidate_id: str, status: str, note: str = "") -> dict:
        allowed = {
            "queued",
            "running",
            "passed",
            "failed",
            "cancelled",
            "canary_ready",
            "canary_rejected",
            "canary_running",
            "canary_passed",
            "canary_failed",
        }
        if status not in allowed:
            return {"ok": False, "error": f"invalid status: {status}", "allowed": sorted(allowed)}
        now = time.time()
        conn = self._conn()
        try:
            conn.execute(
                """
                UPDATE model_shadow_candidate
                SET status=?, note=?, updated_at=?
                WHERE candidate_id=?
                """,
                (status, note, now, candidate_id),
            )
            conn.commit()
            if conn.execute("SELECT changes()").fetchone()[0] <= 0:
                return {"ok": False, "error": "candidate not found", "candidate_id": candidate_id}
            row = conn.execute(
                "SELECT * FROM model_shadow_candidate WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            return {"ok": True, "candidate": self._row(row).to_dict()}
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> ShadowModelCandidate:
        return ShadowModelCandidate(
            candidate_id=str(row["candidate_id"] or ""),
            model_type=str(row["model_type"] or ""),
            artifact_path=str(row["artifact_path"] or ""),
            artifact_sha256=str(row["artifact_sha256"] or ""),
            symbol=str(row["symbol"] or ""),
            timeframe=str(row["timeframe"] or ""),
            status=str(row["status"] or ""),
            gate_decision=str(row["gate_decision"] or ""),
            gate_json=str(row["gate_json"] or "{}"),
            registry_version_json=str(row["registry_version_json"] or "null"),
            note=str(row["note"] or ""),
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
        )
