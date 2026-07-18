from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from backend.core.db import EXPERIMENTS_DB, prepare_experiments_store
from research.model_inference_contract import ModelInferenceContract
from research.model_shadow_queue import ModelShadowQueue


@dataclass
class ModelCanaryTrial:
    trial_id: str
    candidate_id: str
    status: str
    metrics_json: str
    thresholds_json: str
    details_json: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "candidate_id": self.candidate_id,
            "status": self.status,
            "metrics": json.loads(self.metrics_json or "{}"),
            "thresholds": json.loads(self.thresholds_json or "{}"),
            "details": json.loads(self.details_json or "{}"),
            "created_at": self.created_at,
        }


class ModelCanaryExecutor:
    """Controlled canary trial executor for advisory-only model inference.

    This executor runs model scoring in a controlled batch and records trial
    evidence. It does not send orders, change weights, or modify live strategy
    decisions.
    """

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or str(EXPERIMENTS_DB)
        self.queue = ModelShadowQueue(self._db_path)
        self.contract = ModelInferenceContract(self._db_path)
        self._ensure_table()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_table(self) -> None:
        prepare_experiments_store(self._db_path)

    def run_trial(
        self,
        *,
        candidate_id: str,
        samples: list[dict] | None = None,
        contexts: list[dict] | None = None,
        min_items: int = 1,
        min_success_rate: float = 1.0,
        min_decision_coverage: float = 0.0,
        note: str = "",
    ) -> dict:
        candidate = self.queue.get_candidate(candidate_id)
        if not candidate:
            return {"ok": False, "error": "candidate not found", "candidate_id": candidate_id}
        if candidate.get("status") != "canary_ready":
            return {
                "ok": False,
                "error": "candidate must be canary_ready before controlled canary trial",
                "candidate": candidate,
                "capabilities": {"live_trading": False, "advisory_only": True},
            }

        payloads = []
        for item in samples or []:
            payloads.append({"kind": "sample", "sample": item})
        for item in contexts or []:
            payloads.append(
                {
                    "kind": "context",
                    "factor_signals": item.get("factor_signals") or {},
                    "factor_values": item.get("factor_values") or {},
                    "composite_score": item.get("composite_score"),
                }
            )
        if len(payloads) < int(min_items):
            return {
                "ok": False,
                "error": "insufficient canary trial items",
                "required": int(min_items),
                "actual": len(payloads),
            }

        results = []
        success_count = 0
        actionable_count = 0
        for payload in payloads:
            if payload["kind"] == "sample":
                result = self.contract.score(
                    candidate_id=candidate_id,
                    sample=payload["sample"],
                    mode="controlled_canary",
                )
            else:
                result = self.contract.score(
                    candidate_id=candidate_id,
                    factor_signals=payload["factor_signals"],
                    factor_values=payload["factor_values"],
                    composite_score=payload.get("composite_score"),
                    mode="controlled_canary",
                )
            success = bool(result.get("ok"))
            success_count += 1 if success else 0
            if success and result.get("score") is not None:
                actionable_count += 1
            results.append(
                {
                    "kind": payload["kind"],
                    "ok": success,
                    "score": result.get("score"),
                    "prediction": result.get("prediction"),
                    "audit_id": (result.get("audit") or {}).get("inference_id"),
                    "error": result.get("error", ""),
                    "top_terms": list(((result.get("explainability") or {}).get("top_terms") or []))[:5],
                }
            )

        total = len(payloads)
        success_rate = success_count / max(total, 1)
        coverage = actionable_count / max(total, 1)
        thresholds = {
            "min_items": int(min_items),
            "min_success_rate": float(min_success_rate),
            "min_decision_coverage": float(min_decision_coverage),
        }
        metrics = {
            "item_count": total,
            "success_count": success_count,
            "success_rate": round(success_rate, 6),
            "decision_coverage": round(coverage, 6),
            "safe_for_live_trading": False,
        }
        passed = (
            total >= thresholds["min_items"]
            and success_rate >= thresholds["min_success_rate"]
            and coverage >= thresholds["min_decision_coverage"]
        )
        status = "canary_passed" if passed else "canary_failed"
        details = {
            "note": note,
            "results": results[:100],
            "guardrails": [
                "controlled canary trial is advisory-only",
                "no orders were placed",
                "no factor weights were changed",
            ],
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
            },
        }
        trial = self._persist_trial(
            candidate_id=candidate_id,
            status=status,
            metrics=metrics,
            thresholds=thresholds,
            details=details,
        )
        self.queue.update_status(
            candidate_id,
            status,
            f"{status}: success_rate={metrics['success_rate']} coverage={metrics['decision_coverage']}",
        )
        return {
            "ok": True,
            "passed": passed,
            "trial": trial,
            "candidate": self.queue.get_candidate(candidate_id),
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "requires_manual_promotion_before_live": True,
            },
        }

    def list_trials(self, *, candidate_id: str | None = None, limit: int = 50) -> list[dict]:
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
                FROM model_canary_trial
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (*params, int(limit)),
            ).fetchall()
            return [self._row(row).to_dict() for row in rows]
        finally:
            conn.close()

    def _persist_trial(
        self,
        *,
        candidate_id: str,
        status: str,
        metrics: dict,
        thresholds: dict,
        details: dict,
    ) -> dict:
        now = time.time()
        trial_id = f"{candidate_id}:trial:{int(now * 1000)}"
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO model_canary_trial
                (trial_id, candidate_id, status, metrics_json, thresholds_json, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trial_id,
                    candidate_id,
                    status,
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    json.dumps(thresholds, ensure_ascii=False, sort_keys=True),
                    json.dumps(details, ensure_ascii=False, sort_keys=True, default=str),
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM model_canary_trial WHERE trial_id=?",
                (trial_id,),
            ).fetchone()
            return self._row(row).to_dict()
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> ModelCanaryTrial:
        return ModelCanaryTrial(
            trial_id=str(row["trial_id"] or ""),
            candidate_id=str(row["candidate_id"] or ""),
            status=str(row["status"] or ""),
            metrics_json=str(row["metrics_json"] or "{}"),
            thresholds_json=str(row["thresholds_json"] or "{}"),
            details_json=str(row["details_json"] or "{}"),
            created_at=float(row["created_at"] or 0.0),
        )
