from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol

from backend.core.db import DATA_DIR
from research.features.snapshot_validator import LearningDatasetValidator


class ModelAdapter(Protocol):
    name: str
    version: str

    def fit(self, dataset_ref: str, **kwargs) -> dict:
        ...

    def predict(self, features: dict) -> dict:
        ...

    def explain(self, features: dict, prediction: dict) -> dict:
        ...


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def _count(values: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        key = str(value or "")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


class DatasetSummaryAdapter:
    """Safe baseline adapter for future model/LLM integration.

    It does not produce live trading decisions. It validates an exported dataset
    and creates a compact model card that downstream trainers or LLM review
    jobs can consume through the same adapter-shaped interface.
    """

    name = "dataset_summary_adapter"
    version = "0.1"

    def __init__(self, artifact_dir: str | Path | None = None):
        self.artifact_dir = Path(artifact_dir) if artifact_dir else DATA_DIR / "model_artifacts"

    def fit(self, dataset_ref: str, **kwargs) -> dict:
        root = Path(dataset_ref)
        validation = LearningDatasetValidator().validate(root)
        if not validation.get("valid"):
            return {
                "ok": False,
                "adapter": self.name,
                "version": self.version,
                "dataset_ref": str(root),
                "validation": validation,
                "error": "dataset validation failed",
            }

        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        trade_items = _read_jsonl(root / "trade_samples.jsonl")
        decision_items = _read_jsonl(root / "decision_samples.jsonl")
        trade_labels = _count([str((item.get("target") or {}).get("outcome_label") or "") for item in trade_items])
        recommended_actions = _count([str((item.get("target") or {}).get("recommended_action") or "") for item in trade_items])
        decision_events = _count([str((item.get("target") or {}).get("event_type") or "") for item in decision_items])
        failed_decisions = sum(1 for item in decision_items if (item.get("target") or {}).get("failed_execution"))

        model_card = {
            "adapter": self.name,
            "adapter_version": self.version,
            "created_at": time.time(),
            "dataset_id": manifest.get("dataset_id"),
            "dataset_ref": str(root),
            "schemas": manifest.get("schemas") or {},
            "readiness": manifest.get("readiness") or {},
            "quality": manifest.get("quality") or {},
            "evidence": manifest.get("evidence") or {},
            "label_distribution": {
                "trade_outcome": trade_labels,
                "recommended_action": recommended_actions,
                "decision_event": decision_events,
                "failed_decision_count": failed_decisions,
            },
            "capabilities": {
                "live_trading": False,
                "offline_review": True,
                "llm_context": True,
                "prediction_kind": "review_hint",
            },
            "notes": [
                "Baseline adapter summarizes verified datasets only.",
                "Evidence contract statistics are preserved for downstream model and audit consumers.",
                "It must not be wired directly into live execution.",
            ],
        }

        output_dir = Path(kwargs.get("artifact_dir") or self.artifact_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = output_dir / f"{manifest.get('dataset_id') or root.name}_model_card.json"
        artifact_path.write_text(
            json.dumps(model_card, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        registry_version = None
        if kwargs.get("register", False):
            from research.model_registry import ModelRegistry

            registry = ModelRegistry(db_path=kwargs.get("registry_db_path"))
            registry_version = registry.register(
                self.name,
                artifact_path=str(artifact_path),
                params={
                    "dataset_ref": str(root),
                    "dataset_id": manifest.get("dataset_id"),
                    "adapter_version": self.version,
                    "safe_for_live_trading": False,
                },
                metrics={
                    "ready": bool((manifest.get("readiness") or {}).get("ready")),
                    "trade_model_ready": int(((manifest.get("quality") or {}).get("trade") or {}).get("model_ready") or 0),
                    "decision_model_ready": int(((manifest.get("quality") or {}).get("decision") or {}).get("model_ready") or 0),
                    "failed_decision_count": int(failed_decisions),
                    "safe_for_live_trading": False,
                },
                symbol=str(kwargs.get("symbol") or "XAUUSD+"),
                timeframe=str(kwargs.get("timeframe") or "M5"),
            ).to_dict()

        result = {
            "ok": True,
            "adapter": self.name,
            "version": self.version,
            "dataset_ref": str(root),
            "artifact_path": str(artifact_path),
            "validation": validation,
            "model_card": model_card,
        }
        if registry_version is not None:
            result["registry_version"] = registry_version
        return result

    def predict(self, features: dict) -> dict:
        quality = features.get("quality") or {}
        target = features.get("target") or {}
        llm_context = features.get("llm_context") or {}
        label_summary = llm_context.get("label_summary") or {}
        return {
            "prediction_type": "review_hint",
            "safe_for_live_trading": False,
            "confidence": float(quality.get("quality_score") or 0.0),
            "label_hint": label_summary or target,
            "evidence_contract": features.get("evidence_contract") or {},
            "prompt_card": llm_context.get("prompt_card", ""),
        }

    def explain(self, features: dict, prediction: dict) -> dict:
        llm_context = features.get("llm_context") or {}
        return {
            "adapter": self.name,
            "safe_for_live_trading": False,
            "prediction_type": prediction.get("prediction_type", "review_hint"),
            "summary": llm_context.get("prompt_card", ""),
            "evidence_bullets": list(llm_context.get("evidence_bullets") or []),
            "label_summary": llm_context.get("label_summary") or prediction.get("label_hint") or {},
            "evidence_contract": features.get("evidence_contract") or prediction.get("evidence_contract") or {},
        }
