#!/usr/bin/env python3
"""Offline validation report for the open-quality entry model (no deployment).

Re-trains OpenQualityLightGBMService against the current matured
shadow_open_decision samples with the service's own time-ordered purged
holdout, then compares holdout metrics against the rule baseline
(abs_action_score >= 0.55 and no crowded pyramid) evaluated on the same
temporal tail. Writes artifacts only to a throwaway directory.

Pre-registered read (approved 2026-09-05): with ~150 matured samples the
result is a pipeline-health check, NOT an enforce decision. Enforce stays
off; live shadow accrual decides next.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research.open_quality_lightgbm import (  # noqa: E402
    FEATURE_NAMES,
    OpenQualityLightGBMService,
    _rule_baseline_label,
)


def _rule_metrics(tail: list[dict[str, Any]]) -> dict[str, Any]:
    y = [int(item["label"]) for item in tail]
    preds = [int(_rule_baseline_label(item["features"])) for item in tail]
    positives = sum(y)
    hits = sum(1 for p, t in zip(preds, y) if p == 1 and t == 1)
    predicted_pos = sum(preds)
    accuracy = sum(1 for p, t in zip(preds, y) if p == t) / len(y) if y else 0.0
    return {
        "n": len(y),
        "positives": positives,
        "precision": round(hits / predicted_pos, 4) if predicted_pos else None,
        "recall": round(hits / positives, 4) if positives else None,
        "accuracy": round(accuracy, 4),
        "kept_fraction": round(predicted_pos / len(y), 4) if y else 0.0,
    }


def main() -> int:
    out_dir = PROJECT_ROOT / "run_artifacts" / "open_quality_validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    service = OpenQualityLightGBMService(
        artifact_dir=out_dir / f"artifact_{stamp}"
    )
    train_result = service.train(
        limit=3000,
        holdout_ratio=0.25,
        min_samples=60,
        register=False,
    )
    samples = service.load_samples(limit=3000)
    holdout_ratio = 0.25
    holdout_count = min(max(1, int(round(len(samples) * holdout_ratio))), max(0, len(samples) - 1))
    tail = samples[len(samples) - holdout_count:] if holdout_count else []

    metrics = dict(train_result.get("metrics") or {})
    report = {
        "schema_version": "open_quality_validation.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "train_ok": bool(train_result.get("ok")),
        "train_error": train_result.get("error"),
        "data_quality": dict(service.last_data_quality),
        "model_metrics": {
            "holdout": metrics.get("holdout"),
            "train": metrics.get("train"),
            "split": metrics.get("split"),
            "sample_count": metrics.get("sample_count"),
            "real_holdout_count": metrics.get("real_holdout_count"),
            "label_distribution": metrics.get("label_distribution"),
            "training_sources": metrics.get("training_sources"),
            "augmentation_comparison": metrics.get("augmentation_comparison"),
        },
        "feature_count": len(FEATURE_NAMES),
        "rule_baseline_on_same_tail": _rule_metrics(tail),
        "caveats": [
            "~150 matured samples: pipeline-health check only, not an enforce decision.",
            "Rule baseline is the 'primary-signal-only' proxy; it is not a full walk-forward.",
            "Model holdout is the service's own time-ordered purged split (last 25%).",
            "Enforce stays off regardless; the live shadow accrual (>=50 fresh samples) decides.",
        ],
        "verdict_rule": "pipeline ok if holdout metrics computed; enforce requires live-shadow uplift",
    }
    out_path = out_dir / f"validation_{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwritten: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
