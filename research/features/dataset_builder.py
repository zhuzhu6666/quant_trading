from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from backend.core.db import DATA_DIR
from research.features.feature_provider import (
    DECISION_SCHEMA_VERSION,
    SCHEMA_VERSION,
    LearningFeatureProvider,
)
from research.features.readiness import (
    DECISION_REQUIRED_FIELDS,
    TRADE_REQUIRED_FIELDS,
    LearningDatasetReadiness,
)


def _json_line(item: dict) -> str:
    return json.dumps(item, ensure_ascii=False, sort_keys=True, default=str) + "\n"


def _write_jsonl(path: Path, items: list[dict]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for item in items:
            line = _json_line(item)
            digest.update(line.encode("utf-8"))
            f.write(line)
    tmp.replace(path)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)
    return digest


def _quality_summary(items: list[dict]) -> dict[str, Any]:
    ready = [item for item in items if item.get("quality", {}).get("model_ready")]
    missing: dict[str, int] = {}
    for item in items:
        for key in item.get("quality", {}).get("missing", []) or []:
            missing[str(key)] = missing.get(str(key), 0) + 1
    return {
        "count": len(items),
        "model_ready": len(ready),
        "needs_attention": len(items) - len(ready),
        "ready_ratio": round(len(ready) / max(len(items), 1), 6),
        "missing": missing,
    }


def _snapshot_readiness(
    *,
    trade_samples: list[dict],
    decision_samples: list[dict],
    min_ready_trades: int,
    min_ready_decisions: int,
    max_schema_issues: int = 0,
) -> dict[str, Any]:
    trade_quality = _quality_summary(trade_samples)
    decision_quality = _quality_summary(decision_samples)
    schema_issues: list[dict] = []
    for item in trade_samples:
        schema_issues.extend(
            LearningDatasetReadiness._validate_item(
                item,
                expected_schema=SCHEMA_VERSION,
                required_fields=TRADE_REQUIRED_FIELDS,
                kind="trade",
            )
        )
    for item in decision_samples:
        schema_issues.extend(
            LearningDatasetReadiness._validate_item(
                item,
                expected_schema=DECISION_SCHEMA_VERSION,
                required_fields=DECISION_REQUIRED_FIELDS,
                kind="decision",
            )
        )

    blockers = []
    if trade_quality["model_ready"] < min_ready_trades:
        blockers.append(
            {
                "code": "insufficient_model_ready_trades",
                "required": int(min_ready_trades),
                "actual": int(trade_quality["model_ready"]),
            }
        )
    if decision_quality["model_ready"] < min_ready_decisions:
        blockers.append(
            {
                "code": "insufficient_model_ready_decisions",
                "required": int(min_ready_decisions),
                "actual": int(decision_quality["model_ready"]),
            }
        )
    if len(schema_issues) > max_schema_issues:
        blockers.append(
            {
                "code": "schema_contract_issues",
                "required": int(max_schema_issues),
                "actual": len(schema_issues),
            }
        )

    ready = not blockers
    has_any_ready = trade_quality["model_ready"] > 0 or decision_quality["model_ready"] > 0
    level = "ready" if ready else "warming_up" if has_any_ready and len(schema_issues) <= max_schema_issues else "not_ready"
    return {
        "ready": ready,
        "level": level,
        "thresholds": {
            "min_ready_trades": int(min_ready_trades),
            "min_ready_decisions": int(min_ready_decisions),
            "max_schema_issues": int(max_schema_issues),
        },
        "schema_issue_count": len(schema_issues),
        "schema_issues": schema_issues[:50],
        "blockers": blockers,
    }


class LearningDatasetBuilder:
    """Persist model-ready learning data snapshots for offline training/review."""

    def __init__(
        self,
        db_path: str | None = None,
        output_dir: str | Path | None = None,
    ):
        self.provider = LearningFeatureProvider(db_path)
        self.output_dir = Path(output_dir) if output_dir else DATA_DIR / "model_datasets"

    def build_snapshot(
        self,
        *,
        name: str | None = None,
        trade_limit: int = 1000,
        decision_limit: int = 5000,
        model_ready_only: bool = False,
        decision_event_types: list[str] | None = None,
        min_ready_trades: int = 50,
        min_ready_decisions: int = 200,
    ) -> dict:
        created_at = time.time()
        dataset_id = name or time.strftime("learning_%Y%m%d_%H%M%S", time.gmtime(created_at))
        dataset_dir = self.output_dir / dataset_id

        trade_samples = self.provider.build_training_samples(
            limit=trade_limit,
            model_ready_only=model_ready_only,
        )
        decision_samples = self.provider.build_decision_samples(
            limit=decision_limit,
            event_types=decision_event_types,
            model_ready_only=model_ready_only,
        )

        trade_path = dataset_dir / "trade_samples.jsonl"
        decision_path = dataset_dir / "decision_samples.jsonl"
        trade_sha = _write_jsonl(trade_path, trade_samples)
        decision_sha = _write_jsonl(decision_path, decision_samples)
        readiness = _snapshot_readiness(
            trade_samples=trade_samples,
            decision_samples=decision_samples,
            min_ready_trades=int(min_ready_trades),
            min_ready_decisions=int(min_ready_decisions),
        )

        manifest = {
            "dataset_id": dataset_id,
            "dataset_ref": str(dataset_dir),
            "created_at": created_at,
            "schemas": {
                "trade": SCHEMA_VERSION,
                "decision": DECISION_SCHEMA_VERSION,
            },
            "contracts": {
                "trade": {
                    "target": ["outcome_label", "reward_score", "pnl", "failure_tags", "recommended_action"],
                    "features": ["decision", "factor_outcomes", "attribution_alignment", "execution_trace", "application_context", "llm_context"],
                    "quality_gate": "model_ready requires verifiable pnl, full context, entry decision, factor snapshot, factor contribution review, outcome label, and experience memory",
                },
                "decision": {
                    "target": ["event_type", "executed", "skipped", "gate_passed", "gate_reason", "skip_stage", "direction", "action_score"],
                    "features": ["decision.factor_evidence", "decision.factor_tags", "execution_trace", "llm_context", "explainability.top_factors"],
                    "quality_gate": "model_ready requires factor snapshot, action payload, action reason, symbol, and timeframe",
                },
            },
            "filters": {
                "trade_limit": int(trade_limit),
                "decision_limit": int(decision_limit),
                "model_ready_only": bool(model_ready_only),
                "decision_event_types": list(decision_event_types or []),
                "min_ready_trades": int(min_ready_trades),
                "min_ready_decisions": int(min_ready_decisions),
            },
            "files": {
                "trade_samples": {
                    "path": str(trade_path),
                    "sha256": trade_sha,
                    "count": len(trade_samples),
                },
                "decision_samples": {
                    "path": str(decision_path),
                    "sha256": decision_sha,
                    "count": len(decision_samples),
                },
            },
            "quality": {
                "trade": _quality_summary(trade_samples),
                "decision": _quality_summary(decision_samples),
            },
            "readiness": readiness,
        }
        manifest_path = dataset_dir / "manifest.json"
        manifest_sha = _write_json(manifest_path, manifest)
        manifest["files"]["manifest"] = {
            "path": str(manifest_path),
            "sha256": manifest_sha,
            "count": 1,
        }
        _write_json(manifest_path, manifest)
        return manifest
