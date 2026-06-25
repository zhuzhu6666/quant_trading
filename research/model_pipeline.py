from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research.model_canary import ModelCanaryReviewer
from research.model_canary_executor import ModelCanaryExecutor
from research.model_promotion import ModelPromotionGate
from research.model_shadow_queue import ModelShadowQueue
from research.model_shadow_runner import ModelShadowRunner
from research.offline_trainer import LearningStatisticalTrainer, _read_jsonl


class LearningModelPipeline:
    """End-to-end offline learning model workflow orchestrator."""

    def __init__(
        self,
        *,
        registry_db_path: str | None = None,
        artifact_dir: str | Path | None = None,
        shadow_report_dir: str | Path | None = None,
    ):
        self.registry_db_path = registry_db_path
        self.artifact_dir = artifact_dir
        self.shadow_report_dir = shadow_report_dir

    def run(
        self,
        *,
        dataset_ref: str,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
        holdout_ratio: float = 0.25,
        min_train_samples: int = 4,
        min_feature_count: int = 1,
        min_gate_samples: int = 4,
        min_gate_holdout_samples: int = 1,
        min_gate_oos_acc: float = 0.0,
        min_shadow_samples: int = 4,
        min_shadow_accuracy: float = 0.0,
        min_canary_positive_rate: float = 0.0,
        max_canary_positive_rate: float = 1.0,
        min_trial_items: int = 1,
        min_trial_success_rate: float = 1.0,
        min_trial_coverage: float = 1.0,
    ) -> dict[str, Any]:
        train = LearningStatisticalTrainer(self.artifact_dir).train(
            dataset_ref,
            holdout_ratio=holdout_ratio,
            min_samples=min_train_samples,
            min_feature_count=min_feature_count,
            register=True,
            registry_db_path=self.registry_db_path,
            symbol=symbol,
            timeframe=timeframe,
        )
        if not train.get("ok"):
            return {"ok": False, "stage": "train", "train": train}

        gate = ModelPromotionGate().evaluate(
            artifact_path=train["artifact_path"],
            registry_db_path=self.registry_db_path,
            min_samples=min_gate_samples,
            min_holdout_samples=min_gate_holdout_samples,
            min_oos_acc=min_gate_oos_acc,
            min_features=1,
        )
        if not gate.get("ok"):
            return {"ok": False, "stage": "promotion_gate", "train": train, "gate": gate}

        queue = ModelShadowQueue(self.registry_db_path).queue_from_gate(
            gate_result=gate,
            note="pipeline queued",
        )
        if not queue.get("ok"):
            return {"ok": False, "stage": "shadow_queue", "train": train, "gate": gate, "queue": queue}
        candidate_id = queue["candidate"]["candidate_id"]

        shadow = ModelShadowRunner(
            registry_db_path=self.registry_db_path,
            report_dir=self.shadow_report_dir,
        ).run_candidate(
            candidate_id,
            min_shadow_samples=min_shadow_samples,
            min_shadow_accuracy=min_shadow_accuracy,
        )
        if not shadow.get("ok") or not shadow.get("passed"):
            return {
                "ok": False,
                "stage": "shadow_run",
                "train": train,
                "gate": gate,
                "queue": queue,
                "shadow": shadow,
            }

        canary = ModelCanaryReviewer(self.registry_db_path).review_candidate(
            candidate_id,
            report_path=shadow["report"]["report_path"],
            min_shadow_samples=min_shadow_samples,
            min_shadow_accuracy=min_shadow_accuracy,
            min_positive_rate=min_canary_positive_rate,
            max_positive_rate=max_canary_positive_rate,
            note="pipeline canary review",
        )
        if not canary.get("ok") or canary.get("decision") != "canary_ready":
            return {
                "ok": False,
                "stage": "canary_review",
                "train": train,
                "gate": gate,
                "queue": queue,
                "shadow": shadow,
                "canary": canary,
            }

        trial_samples = _read_jsonl(Path(dataset_ref) / "trade_samples.jsonl")[: max(1, int(min_trial_items))]
        trial = ModelCanaryExecutor(self.registry_db_path).run_trial(
            candidate_id=candidate_id,
            samples=trial_samples,
            min_items=min_trial_items,
            min_success_rate=min_trial_success_rate,
            min_decision_coverage=min_trial_coverage,
            note="pipeline controlled canary trial",
        )
        return {
            "ok": bool(trial.get("ok") and trial.get("passed")),
            "stage": "complete" if trial.get("ok") and trial.get("passed") else "canary_trial",
            "candidate_id": candidate_id,
            "train": train,
            "gate": gate,
            "queue": queue,
            "shadow": shadow,
            "canary": canary,
            "trial": trial,
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "manual_enablement_required": True,
            },
        }
