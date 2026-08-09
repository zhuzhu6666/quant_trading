from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from backend.core.db import DATA_DIR
from research.features.evidence_contract import stable_hash
from research.features.snapshot_validator import LearningDatasetValidator
from research.model_shadow_queue import ModelShadowQueue
from research.offline_trainer import (
    _factor_features,
    _label_from_trade,
    _predict_score,
    _read_jsonl,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_artifact(path: str | Path) -> dict:
    root = Path(path)
    return json.loads(root.read_text(encoding="utf-8"))


class ModelShadowRunner:
    """Offline shadow validator for queued learning model candidates.

    It scores verified learning snapshots using an offline artifact and records
    whether the candidate is ready for the next canary step. It never executes
    orders or changes live factor weights.
    """

    def __init__(
        self,
        *,
        registry_db_path: str | None = None,
        report_dir: str | Path | None = None,
    ):
        self.queue = ModelShadowQueue(registry_db_path)
        self.report_dir = Path(report_dir) if report_dir else DATA_DIR / "model_shadow_reports"

    def run_candidate(
        self,
        candidate_id: str,
        *,
        dataset_ref: str | Path | None = None,
        min_shadow_samples: int = 20,
        min_shadow_accuracy: float = 0.52,
        mark_status: bool = True,
    ) -> dict:
        candidate = self.queue.get_candidate(candidate_id)
        if not candidate:
            return {"ok": False, "error": "candidate not found", "candidate_id": candidate_id}
        if candidate.get("status") not in {"queued", "running"}:
            return {
                "ok": False,
                "error": "candidate status is not runnable",
                "candidate": candidate,
            }
        if mark_status:
            self.queue.update_status(candidate_id, "running", "offline shadow validation running")

        artifact_path = Path(candidate.get("artifact_path") or "")
        if not artifact_path.exists():
            if mark_status:
                self.queue.update_status(candidate_id, "failed", "artifact missing")
            return {"ok": False, "error": "artifact missing", "candidate": candidate}
        artifact = _load_artifact(artifact_path)
        if (artifact.get("capabilities") or {}).get("live_trading"):
            if mark_status:
                self.queue.update_status(candidate_id, "failed", "artifact declares live trading capability")
            return {"ok": False, "error": "unsafe artifact live_trading=true", "candidate": candidate}

        params = artifact.get("parameters") or {}
        weights = params.get("weights") or {}
        bias = float(params.get("bias") or 0.0)
        root = Path(dataset_ref or artifact.get("dataset_ref") or "")
        validation = LearningDatasetValidator().validate(root)
        if not validation.get("valid"):
            if mark_status:
                self.queue.update_status(candidate_id, "failed", "shadow dataset validation failed")
            return {
                "ok": False,
                "error": "shadow dataset validation failed",
                "candidate": candidate,
                "validation": validation,
            }

        trade_items = [
            item for item in _read_jsonl(root / "trade_samples.jsonl")
            if (item.get("quality") or {}).get("model_ready")
            and "supervised_training" in ((item.get("evidence_contract") or {}).get("allowed_uses") or [])
        ]
        scored = []
        skipped = 0
        correct = 0
        positives = 0
        for item in trade_items:
            label = _label_from_trade(item)
            features = _factor_features(item)
            if label is None or not features:
                skipped += 1
                continue
            score = _predict_score(features, weights, bias)
            pred = 1 if score >= 0.5 else 0
            correct += 1 if pred == label else 0
            positives += 1 if label == 1 else 0
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
            )[:8]
            scored.append(
                {
                    "sample_id": item.get("sample_id"),
                    "evidence_contract": item.get("evidence_contract") or {},
                    "features_sha256": stable_hash(features),
                    "label": label,
                    "prediction": pred,
                    "score": round(score, 8),
                    "correct": pred == label,
                    "top_terms": top_terms,
                    "evidence_bullets": list(((item.get("llm_context") or {}).get("evidence_bullets") or []))[:6],
                }
            )

        sample_count = len(scored)
        accuracy = round(correct / sample_count, 6) if sample_count else None
        metrics = {
            "sample_count": sample_count,
            "skipped_count": skipped,
            "accuracy": accuracy,
            "positive_rate": round(positives / sample_count, 6) if sample_count else None,
            "min_shadow_samples": int(min_shadow_samples),
            "min_shadow_accuracy": float(min_shadow_accuracy),
            "safe_for_live_trading": False,
        }
        passed = sample_count >= int(min_shadow_samples) and accuracy is not None and accuracy >= float(min_shadow_accuracy)
        report = {
            "candidate_id": candidate_id,
            "created_at": time.time(),
            "artifact_path": str(artifact_path),
            "artifact_sha256": _sha256(artifact_path),
            "dataset_ref": str(root),
            "dataset_validation": validation,
            "dataset_evidence": ((artifact.get("explainability") or {}).get("evidence_contract") or {}).get("dataset_evidence") or {},
            "metrics": metrics,
            "decision": "passed" if passed else "failed",
            "capabilities": {
                "live_trading": False,
                "demo_canary_required_before_influence": True,
            },
            "explainability": {
                "summary": (
                    "Offline shadow validation passed; candidate may proceed to canary review."
                    if passed
                    else "Offline shadow validation failed; inspect metrics and per-sample terms."
                ),
                "top_model_weights": list(((artifact.get("explainability") or {}).get("top_weights") or []))[:15],
                "sample_explanations": scored[:50],
            },
        }
        self.report_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.report_dir / f"{candidate_id.replace(':', '_')}_shadow_report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        report["report_path"] = str(report_path)
        report["report_sha256"] = _sha256(report_path)
        if mark_status:
            self.queue.update_status(
                candidate_id,
                "passed" if passed else "failed",
                f"shadow_accuracy={accuracy} samples={sample_count}",
            )
        return {"ok": True, "passed": passed, "report": report}

    def run_next(
        self,
        *,
        min_shadow_samples: int = 20,
        min_shadow_accuracy: float = 0.52,
    ) -> dict:
        items = self.queue.list_candidates(status="queued", limit=1)
        if not items:
            return {"ok": False, "error": "no queued candidates"}
        return self.run_candidate(
            items[0]["candidate_id"],
            min_shadow_samples=min_shadow_samples,
            min_shadow_accuracy=min_shadow_accuracy,
        )
