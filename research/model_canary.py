from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core.db import DATA_DIR, EXPERIMENTS_DB, prepare_experiments_store
from research.model_shadow_queue import ModelShadowQueue


@dataclass
class ModelCanaryReview:
    review_id: str
    candidate_id: str
    model_type: str
    decision: str
    report_path: str
    metrics_json: str
    thresholds_json: str
    issues_json: str
    note: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "candidate_id": self.candidate_id,
            "model_type": self.model_type,
            "decision": self.decision,
            "report_path": self.report_path,
            "metrics": json.loads(self.metrics_json or "{}"),
            "thresholds": json.loads(self.thresholds_json or "{}"),
            "issues": json.loads(self.issues_json or "[]"),
            "note": self.note,
            "created_at": self.created_at,
        }


class ModelCanaryReviewer:
    """Review shadow-passed model candidates for Demo Canary readiness.

    A ``canary_ready`` decision means the model has enough offline shadow
    evidence to enter a controlled Demo runner. It never grants broker
    execution or account-level permissions.
    """

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
        prepare_experiments_store(self._db_path)

    def review_candidate(
        self,
        candidate_id: str,
        *,
        report_path: str | Path | None = None,
        min_shadow_samples: int = 20,
        min_shadow_accuracy: float = 0.55,
        min_positive_rate: float = 0.05,
        max_positive_rate: float = 0.95,
        note: str = "",
    ) -> dict:
        candidate = self.queue.get_candidate(candidate_id)
        if not candidate:
            return {"ok": False, "error": "candidate not found", "candidate_id": candidate_id}
        if candidate.get("status") != "passed":
            return {
                "ok": False,
                "error": "candidate must pass shadow validation before canary review",
                "candidate": candidate,
            }
        path = Path(report_path) if report_path else self._default_report_path(candidate_id)
        if not path.exists():
            return {
                "ok": False,
                "error": "shadow report not found",
                "candidate": candidate,
                "report_path": str(path),
            }
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "ok": False,
                "error": f"invalid shadow report: {exc}",
                "candidate": candidate,
                "report_path": str(path),
            }

        metrics = report.get("metrics") or {}
        capabilities = report.get("capabilities") or {}
        thresholds = {
            "min_shadow_samples": int(min_shadow_samples),
            "min_shadow_accuracy": float(min_shadow_accuracy),
            "min_positive_rate": float(min_positive_rate),
            "max_positive_rate": float(max_positive_rate),
        }
        issues = []
        sample_count = int(metrics.get("sample_count") or 0)
        accuracy = metrics.get("accuracy")
        positive_rate = metrics.get("positive_rate")
        if report.get("decision") != "passed":
            issues.append({"code": "shadow_report_not_passed", "actual": report.get("decision")})
        if capabilities.get("live_trading"):
            issues.append({"code": "live_trading_not_allowed"})
        if sample_count < thresholds["min_shadow_samples"]:
            issues.append({"code": "insufficient_shadow_samples", "required": thresholds["min_shadow_samples"], "actual": sample_count})
        if accuracy is None or float(accuracy) < thresholds["min_shadow_accuracy"]:
            issues.append({"code": "shadow_accuracy_below_threshold", "required": thresholds["min_shadow_accuracy"], "actual": accuracy})
        if positive_rate is None:
            issues.append({"code": "missing_positive_rate"})
        elif not (thresholds["min_positive_rate"] <= float(positive_rate) <= thresholds["max_positive_rate"]):
            issues.append({
                "code": "positive_rate_out_of_bounds",
                "min": thresholds["min_positive_rate"],
                "max": thresholds["max_positive_rate"],
                "actual": float(positive_rate),
            })

        decision = "canary_ready" if not issues else "canary_rejected"
        review = self._persist_review(
            candidate=candidate,
            decision=decision,
            report_path=str(path),
            metrics=metrics,
            thresholds=thresholds,
            issues=issues,
            note=note,
        )
        self.queue.update_status(
            candidate_id,
            decision,
            f"{decision}: accuracy={accuracy} samples={sample_count}",
        )
        return {
            "ok": True,
            "decision": decision,
            "candidate_id": candidate_id,
            "review": review,
            "candidate": self.queue.get_candidate(candidate_id),
            "capabilities": {
                "live_trading": False,
                "requires_demo_inference_contract": True,
                "requires_controlled_canary_runner": True,
            },
        }

    def list_reviews(self, *, candidate_id: str | None = None, limit: int = 50) -> list[dict]:
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
                FROM model_canary_review
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (*params, int(limit)),
            ).fetchall()
            return [self._row(row).to_dict() for row in rows]
        finally:
            conn.close()

    def _persist_review(
        self,
        *,
        candidate: dict,
        decision: str,
        report_path: str,
        metrics: dict,
        thresholds: dict,
        issues: list[dict],
        note: str,
    ) -> dict:
        now = time.time()
        review_id = f"{candidate['candidate_id']}:canary:{int(now * 1000)}"
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT INTO model_canary_review
                (review_id, candidate_id, model_type, decision, report_path,
                 metrics_json, thresholds_json, issues_json, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    candidate["candidate_id"],
                    candidate["model_type"],
                    decision,
                    report_path,
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    json.dumps(thresholds, ensure_ascii=False, sort_keys=True),
                    json.dumps(issues, ensure_ascii=False, sort_keys=True),
                    note,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM model_canary_review WHERE review_id=?",
                (review_id,),
            ).fetchone()
            return self._row(row).to_dict()
        finally:
            conn.close()

    @staticmethod
    def _default_report_path(candidate_id: str) -> Path:
        return DATA_DIR / "model_shadow_reports" / f"{candidate_id.replace(':', '_')}_shadow_report.json"

    @staticmethod
    def _row(row: sqlite3.Row) -> ModelCanaryReview:
        return ModelCanaryReview(
            review_id=str(row["review_id"] or ""),
            candidate_id=str(row["candidate_id"] or ""),
            model_type=str(row["model_type"] or ""),
            decision=str(row["decision"] or ""),
            report_path=str(row["report_path"] or ""),
            metrics_json=str(row["metrics_json"] or "{}"),
            thresholds_json=str(row["thresholds_json"] or "{}"),
            issues_json=str(row["issues_json"] or "[]"),
            note=str(row["note"] or ""),
            created_at=float(row["created_at"] or 0.0),
        )
