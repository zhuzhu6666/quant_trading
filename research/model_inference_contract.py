from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core.db import EXPERIMENTS_DB
from research.features.evidence_contract import stable_hash
from research.model_shadow_queue import ModelShadowQueue
from research.offline_trainer import _factor_features, _predict_score


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


@dataclass
class ModelInferenceAudit:
    inference_id: str
    candidate_id: str
    model_type: str
    mode: str
    score: float
    prediction: int
    payload_json: str
    result_json: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "inference_id": self.inference_id,
            "candidate_id": self.candidate_id,
            "model_type": self.model_type,
            "mode": self.mode,
            "score": self.score,
            "prediction": self.prediction,
            "payload": json.loads(self.payload_json or "{}"),
            "result": json.loads(self.result_json or "{}"),
            "created_at": self.created_at,
        }


class ModelInferenceContract:
    """Read-only advisory inference contract for canary-ready learning models."""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or str(EXPERIMENTS_DB)
        self.queue = ModelShadowQueue(self._db_path)
        self._ensure_table()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_table(self) -> None:
        conn = self._conn()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_inference_audit (
                    inference_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    model_type TEXT NOT NULL,
                    mode TEXT DEFAULT 'advisory',
                    score REAL DEFAULT 0.0,
                    prediction INTEGER DEFAULT 0,
                    payload_json TEXT DEFAULT '{}',
                    result_json TEXT DEFAULT '{}',
                    created_at REAL DEFAULT 0.0
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_model_inference_candidate
                ON model_inference_audit(candidate_id, created_at)
                """
            )
            conn.commit()
        finally:
            conn.close()

    def score(
        self,
        *,
        candidate_id: str,
        sample: dict | None = None,
        factor_signals: dict[str, float | None] | None = None,
        factor_values: dict[str, float | None] | None = None,
        composite_score: float | None = None,
        mode: str = "advisory",
    ) -> dict:
        candidate = self.queue.get_candidate(candidate_id)
        if not candidate:
            return {"ok": False, "error": "candidate not found", "candidate_id": candidate_id}
        if candidate.get("status") != "canary_ready":
            return {
                "ok": False,
                "error": "candidate must be canary_ready before advisory inference",
                "candidate": candidate,
                "capabilities": {"live_trading": False, "advisory_only": True},
            }
        artifact_path = Path(candidate.get("artifact_path") or "")
        if not artifact_path.exists():
            return {"ok": False, "error": "artifact missing", "candidate": candidate}
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        if (artifact.get("capabilities") or {}).get("live_trading"):
            return {"ok": False, "error": "unsafe artifact live_trading=true", "candidate": candidate}

        params = artifact.get("parameters") or {}
        weights = params.get("weights") or {}
        bias = float(params.get("bias") or 0.0)
        payload = {
            "sample": sample,
            "factor_signals": factor_signals,
            "factor_values": factor_values,
            "composite_score": composite_score,
        }
        features = _factor_features(sample) if sample else self._features_from_live_context(
            factor_signals=factor_signals or {},
            factor_values=factor_values or {},
            composite_score=composite_score,
        )
        if not features:
            return {
                "ok": False,
                "error": "no compatible inference features",
                "candidate": candidate,
                "capabilities": {"live_trading": False, "advisory_only": True},
            }
        score = _predict_score(features, weights, bias)
        prediction = 1 if score >= 0.5 else 0
        top_terms = sorted(
            (
                {
                    "feature": key,
                    "value": value,
                    "weight": float(weights.get(key, 0.0) or 0.0),
                    "contribution": round(value * float(weights.get(key, 0.0) or 0.0), 8),
                }
                for key, value in features.items()
                if weights.get(key) is not None
            ),
            key=lambda item: -abs(item["contribution"]),
        )[:12]
        result = {
            "ok": True,
            "candidate_id": candidate_id,
            "model_type": candidate.get("model_type"),
            "mode": mode,
            "traceability": {
                "sample_id": str((sample or {}).get("sample_id") or ""),
                "artifact_path": str(artifact_path),
                "artifact_sha256": stable_hash(artifact),
                "features_sha256": stable_hash(features),
                "input_evidence_contract": (sample or {}).get("evidence_contract") or {},
            },
            "score": round(score, 8),
            "prediction": prediction,
            "prediction_label": "positive_outcome_hint" if prediction == 1 else "negative_outcome_hint",
            "advice": "review_only",
            "features": features,
            "explainability": {
                "top_terms": top_terms,
                "summary": "Advisory-only model score; not an order, not a weight update, not live execution.",
                "evidence_bullets": list(((sample or {}).get("llm_context") or {}).get("evidence_bullets") or [])[:8],
            },
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "canary_required_before_live": True,
            },
            "guardrails": [
                "MUST NOT place orders",
                "MUST NOT change factor weights",
                "MUST be logged before any downstream canary runner consumes it",
            ],
        }
        audit = self._persist(candidate, mode, score, prediction, payload, result)
        result["audit"] = audit
        return result

    def list_audits(self, *, candidate_id: str | None = None, limit: int = 50) -> list[dict]:
        clauses = []
        params: list[Any] = []
        if candidate_id:
            clauses.append("candidate_id=?")
            params.append(candidate_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        conn = self._conn()
        try:
            rows = conn.execute(
                f"""
                SELECT *
                FROM model_inference_audit
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (*params, int(limit)),
            ).fetchall()
            return [self._row(row).to_dict() for row in rows]
        finally:
            conn.close()

    def _persist(
        self,
        candidate: dict,
        mode: str,
        score: float,
        prediction: int,
        payload: dict,
        result: dict,
    ) -> dict:
        now = time.time()
        inference_id = f"{candidate['candidate_id']}:infer:{int(now * 1000)}"
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO model_inference_audit
                (inference_id, candidate_id, model_type, mode, score, prediction,
                 payload_json, result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inference_id,
                    candidate["candidate_id"],
                    candidate["model_type"],
                    mode,
                    float(score),
                    int(prediction),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(result, ensure_ascii=False, sort_keys=True, default=str),
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM model_inference_audit WHERE inference_id=?",
                (inference_id,),
            ).fetchone()
            return self._row(row).to_dict()
        finally:
            conn.close()

    @staticmethod
    def _features_from_live_context(
        *,
        factor_signals: dict[str, float | None],
        factor_values: dict[str, float | None],
        composite_score: float | None,
    ) -> dict[str, float]:
        features: dict[str, float] = {}
        names = set(factor_signals) | set(factor_values)
        for name in names:
            key = str(name or "").strip()
            if not key:
                continue
            sig = factor_signals.get(key)
            val = factor_values.get(key)
            if sig is not None:
                features[f"factor:{key}:signal"] = _safe_float(sig)
            if val is not None:
                features[f"factor:{key}:normalized"] = _safe_float(val)
        if composite_score is not None:
            features["decision:action_score"] = abs(_safe_float(composite_score))
        features["quality:score"] = 1.0
        return {k: v for k, v in features.items() if v != 0.0}

    @staticmethod
    def _row(row: sqlite3.Row) -> ModelInferenceAudit:
        return ModelInferenceAudit(
            inference_id=str(row["inference_id"] or ""),
            candidate_id=str(row["candidate_id"] or ""),
            model_type=str(row["model_type"] or ""),
            mode=str(row["mode"] or ""),
            score=float(row["score"] or 0.0),
            prediction=int(row["prediction"] or 0),
            payload_json=str(row["payload_json"] or "{}"),
            result_json=str(row["result_json"] or "{}"),
            created_at=float(row["created_at"] or 0.0),
        )
