from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from backend.core.db import DATA_DIR
from research.features.snapshot_validator import LearningDatasetValidator


MODEL_TYPE = "learning_statistical_baseline"
MODEL_VERSION = "0.1"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


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


def _label_from_trade(item: dict) -> int | None:
    target = item.get("target") or {}
    outcome = str(target.get("outcome_label") or "")
    if outcome in {"good_win", "small_win"}:
        return 1
    if outcome in {"bad_loss", "small_loss"}:
        return 0
    pnl = target.get("pnl")
    if pnl is not None:
        return 1 if _safe_float(pnl) > 0 else 0
    reward = target.get("reward_score")
    if reward is not None:
        return 1 if _safe_float(reward) >= 0.5 else 0
    return None


def _factor_features(item: dict) -> dict[str, float]:
    features: dict[str, float] = {}
    contract = item.get("evidence_contract") or {}
    for factor in item.get("factor_outcomes") or []:
        name = str(factor.get("factor") or "").strip()
        if not name:
            continue
        contribution = factor.get("outcome_contribution") or {}
        signal = _safe_float(factor.get("contribution_score"))
        normalized = _safe_float(factor.get("normalized_value"))
        net = _safe_float(contribution.get("net_contribution"))
        delta = _safe_float(contribution.get("contribution_delta"))
        role = str(contribution.get("outcome_role") or "")
        role_score = 1.0 if role == "helpful" else -1.0 if role == "harmful" else 0.0
        features[f"factor:{name}:signal"] = signal
        features[f"factor:{name}:normalized"] = normalized
        features[f"factor:{name}:net"] = net
        features[f"factor:{name}:delta"] = delta
        features[f"factor:{name}:role"] = role_score

    alignment = item.get("attribution_alignment") or {}
    labels = alignment.get("labels") or {}
    features["alignment:confirmed"] = _safe_float(labels.get("confirmed"))
    features["alignment:contradicted"] = _safe_float(labels.get("contradicted"))
    features["alignment:weakened"] = _safe_float(labels.get("weakened"))
    features["alignment:abs_net_contribution"] = _safe_float(alignment.get("total_abs_net_contribution"))
    features["alignment:abs_contribution_delta"] = _safe_float(alignment.get("total_abs_contribution_delta"))

    decision = item.get("decision") or {}
    features["decision:action_score"] = _safe_float(decision.get("action_score"))
    features["quality:score"] = _safe_float((item.get("quality") or {}).get("quality_score"))
    features["evidence:train_weight"] = _safe_float(contract.get("train_weight"), 1.0)
    execution = ((item.get("execution_trace") or {}).get("summary") or {})
    features["execution:failed_order"] = 1.0 if execution.get("has_failed_order") else 0.0
    features["execution:order_events"] = _safe_float(execution.get("order_event_count"))
    return {k: v for k, v in features.items() if v != 0.0}


def _split(items: list[tuple[dict[str, float], int]], holdout_ratio: float) -> tuple[list[tuple[dict[str, float], int]], list[tuple[dict[str, float], int]]]:
    if len(items) < 3 or holdout_ratio <= 0:
        return items, []
    holdout_count = max(1, int(round(len(items) * holdout_ratio)))
    holdout_count = min(holdout_count, len(items) - 1)
    return items[:-holdout_count], items[-holdout_count:]


def _train_weights(rows: list[tuple[dict[str, float], int]], min_feature_count: int) -> dict[str, float]:
    sums: dict[str, dict[str, float]] = {}
    for features, label in rows:
        bucket = "pos" if label == 1 else "neg"
        for key, value in features.items():
            stats = sums.setdefault(key, {"pos": 0.0, "neg": 0.0, "pos_count": 0.0, "neg_count": 0.0})
            stats[bucket] += value
            stats[f"{bucket}_count"] += 1.0

    weights = {}
    for key, stats in sums.items():
        count = int(stats["pos_count"] + stats["neg_count"])
        if count < min_feature_count:
            continue
        pos_mean = stats["pos"] / max(stats["pos_count"], 1.0)
        neg_mean = stats["neg"] / max(stats["neg_count"], 1.0)
        weights[key] = round(pos_mean - neg_mean, 8)
    return dict(sorted(weights.items(), key=lambda kv: (-abs(kv[1]), kv[0])))


def _predict_score(features: dict[str, float], weights: dict[str, float], bias: float) -> float:
    raw = bias
    for key, value in features.items():
        raw += value * weights.get(key, 0.0)
    raw = max(-30.0, min(30.0, raw))
    return 1.0 / (1.0 + math.exp(-raw))


def _evaluate(rows: list[tuple[dict[str, float], int]], weights: dict[str, float], bias: float) -> dict[str, Any]:
    if not rows:
        return {"count": 0, "accuracy": None, "positive_rate": None}
    correct = 0
    positives = 0
    for features, label in rows:
        score = _predict_score(features, weights, bias)
        pred = 1 if score >= 0.5 else 0
        correct += 1 if pred == label else 0
        positives += 1 if label == 1 else 0
    return {
        "count": len(rows),
        "accuracy": round(correct / len(rows), 6),
        "positive_rate": round(positives / len(rows), 6),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LearningStatisticalTrainer:
    """Offline, explainable statistical baseline for verified learning datasets."""

    def __init__(self, artifact_dir: str | Path | None = None):
        self.artifact_dir = Path(artifact_dir) if artifact_dir else DATA_DIR / "model_artifacts"

    def train(
        self,
        dataset_ref: str | Path,
        *,
        holdout_ratio: float = 0.25,
        min_samples: int = 4,
        min_feature_count: int = 1,
        register: bool = False,
        registry_db_path: str | None = None,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
    ) -> dict:
        root = Path(dataset_ref)
        validation = LearningDatasetValidator().validate(root)
        if not validation.get("valid"):
            return {
                "ok": False,
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "dataset_ref": str(root),
                "validation": validation,
                "error": "dataset validation failed",
            }

        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        trade_items = [
            item for item in _read_jsonl(root / "trade_samples.jsonl")
            if (item.get("quality") or {}).get("model_ready")
            and "supervised_training" in ((item.get("evidence_contract") or {}).get("allowed_uses") or [])
        ]
        rows: list[tuple[dict[str, float], int]] = []
        skipped = 0
        for item in trade_items:
            label = _label_from_trade(item)
            features = _factor_features(item)
            if label is None or not features:
                skipped += 1
                continue
            rows.append((features, label))

        if len(rows) < int(min_samples):
            return {
                "ok": False,
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "dataset_ref": str(root),
                "validation": validation,
                "sample_count": len(rows),
                "skipped": skipped,
                "error": "insufficient model-ready labeled trade samples",
            }

        train_rows, holdout_rows = _split(rows, max(0.0, min(float(holdout_ratio), 0.8)))
        positives = sum(label for _, label in train_rows)
        positive_rate = positives / max(len(train_rows), 1)
        bias = math.log((positive_rate + 1e-6) / (1.0 - positive_rate + 1e-6))
        weights = _train_weights(train_rows, max(1, int(min_feature_count)))
        train_metrics = _evaluate(train_rows, weights, bias)
        holdout_metrics = _evaluate(holdout_rows, weights, bias)
        metrics = {
            "train": train_metrics,
            "holdout": holdout_metrics,
            "oos_acc": holdout_metrics.get("accuracy"),
            "feature_count": len(weights),
            "sample_count": len(rows),
            "skipped_count": skipped,
            "safe_for_live_trading": False,
        }
        top_weights = [
            {"feature": key, "weight": value, "direction": "positive" if value > 0 else "negative"}
            for key, value in list(weights.items())[:30]
        ]
        artifact = {
            "model_type": MODEL_TYPE,
            "model_version": MODEL_VERSION,
            "created_at": time.time(),
            "dataset_id": manifest.get("dataset_id"),
            "dataset_ref": str(root),
            "dataset_validation": validation,
            "schemas": manifest.get("schemas") or {},
            "readiness": manifest.get("readiness") or {},
            "evidence": manifest.get("evidence") or {},
            "feature_schema": {
                "kind": "sparse_factor_statistics",
                "sources": [
                    "evidence_contract.train_weight",
                    "factor_outcomes",
                    "attribution_alignment",
                    "decision.action_score",
                    "execution_trace.summary",
                    "quality.quality_score",
                ],
                "label": "positive trade outcome",
            },
            "parameters": {
                "holdout_ratio": max(0.0, min(float(holdout_ratio), 0.8)),
                "min_samples": int(min_samples),
                "min_feature_count": int(min_feature_count),
                "bias": round(bias, 8),
                "weights": weights,
            },
            "metrics": metrics,
            "explainability": {
                "top_weights": top_weights,
                "evidence_contract": {
                    "dataset_evidence": manifest.get("evidence") or {},
                    "training_rule": "only samples with quality.model_ready=true and supervised_training in evidence_contract.allowed_uses are used",
                },
                "evidence_summary": [
                    "Model trained only from validator-approved snapshot files.",
                    "Evidence contract controls train_weight and blocks weak or non-matured labels from supervised training.",
                    "Weights are mean feature differences between positive and negative trade outcomes.",
                    "Artifact is offline-only and is not eligible for direct broker execution.",
                ],
            },
            "promotion": {
                "eligible_for_demo_influence": False,
                "reason": "offline statistical baseline requires separate shadow/Demo Canary validation before Demo influence",
            },
            "capabilities": {
                "live_trading": False,
                "offline_scoring": True,
                "explainable_weights": True,
            },
        }

        output_dir = self.artifact_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = output_dir / f"{manifest.get('dataset_id') or root.name}_{MODEL_TYPE}.json"
        artifact_path.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        artifact["artifact_path"] = str(artifact_path)
        artifact["artifact_sha256"] = _sha256(artifact_path)

        registry_version = None
        if register:
            from research.model_registry import ModelRegistry

            registry_version = ModelRegistry(db_path=registry_db_path).register(
                MODEL_TYPE,
                artifact_path=str(artifact_path),
                params={
                    "dataset_ref": str(root),
                    "dataset_id": manifest.get("dataset_id"),
                    "model_version": MODEL_VERSION,
                    "safe_for_live_trading": False,
                    "eligible_for_demo_influence": False,
                },
                metrics={
                    "sample_count": len(rows),
                    "feature_count": len(weights),
                    "oos_acc": metrics["oos_acc"],
                    "safe_for_live_trading": False,
                    "eligible_for_demo_influence": False,
                },
                symbol=symbol,
                timeframe=timeframe,
            ).to_dict()

        result = {
            "ok": True,
            "model_type": MODEL_TYPE,
            "model_version": MODEL_VERSION,
            "dataset_ref": str(root),
            "artifact_path": str(artifact_path),
            "artifact_sha256": artifact["artifact_sha256"],
            "metrics": metrics,
            "explainability": artifact["explainability"],
            "promotion": artifact["promotion"],
            "validation": validation,
        }
        if registry_version is not None:
            result["registry_version"] = registry_version
        return result
