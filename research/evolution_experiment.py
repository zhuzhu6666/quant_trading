"""research/evolution_experiment.py — 进化实验注册器.

封装 experiments.db 的读写, 让每次 GP 搜索、模型影子训练、参数调优都有科研记忆.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any

from research.experiment_tracker import Experiment, ExperimentTracker

logger = logging.getLogger(__name__)


@dataclass
class ExperimentRecord:
    """单次实验记录."""
    run_id: str
    experiment_type: str                    # gp_search / model_shadow_train / param_tune / feature_eng
    params_json: dict = field(default_factory=dict)
    metrics_in_sample: dict = field(default_factory=dict)
    metrics_oos: dict = field(default_factory=dict)
    status: str = "running"                # running / accepted / rejected / failed
    artifact_path: str = ""
    tags: list[str] = field(default_factory=list)
    timestamp: float = 0.0
    created_at: float = 0.0

    def to_db_row(self) -> dict:
        return {
            "run_id": self.run_id,
            "experiment_type": self.experiment_type,
            "params_json": json.dumps(self.params_json, ensure_ascii=False),
            "metrics_json": json.dumps({
                "in_sample": self.metrics_in_sample,
                "oos": self.metrics_oos,
            }, ensure_ascii=False),
            "tags_json": json.dumps(self.tags, ensure_ascii=False),
            "artifacts_json": json.dumps([self.artifact_path] if self.artifact_path else []),
            "status": self.status,
            "timestamp": self.timestamp or time.time(),
            "created_at": self.created_at or time.time(),
        }


class EvolutionExperimentRegistry:
    """进化实验注册器.

    用法:
        reg = EvolutionExperimentRegistry()
        run_id = reg.start_run("gp_search", params={"pop": 50, "gen": 20})
        # ... do work ...
        reg.finish_run(run_id, accepted=True, metrics_in_sample={...})
    """

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path
        if db_path is None:
            from backend.core.db import EXPERIMENTS_DB

            db_path = str(EXPERIMENTS_DB)
        self._tracker = ExperimentTracker(db_path=str(db_path))

    def start_run(
        self,
        experiment_type: str,
        *,
        params: dict | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """创建新实验记录. 返回 run_id."""
        import uuid
        run_id = f"{experiment_type}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        self._tracker.start_run(
            experiment_type,
            params=params or {},
            tags=tags or [],
            run_id=run_id,
        )
        logger.info("[Experiment] started %s: %s", experiment_type, run_id)
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        accepted: bool,
        metrics_in_sample: dict | None = None,
        metrics_oos: dict | None = None,
        artifact_path: str = "",
    ) -> bool:
        """完成实验记录."""
        exp = self._tracker.get_run(run_id)
        if exp is None:
            logger.warning("[Experiment] finish: run_id %s not found", run_id)
            return False
        metrics = dict(exp.metrics or {})
        metrics["in_sample"] = dict(metrics_in_sample or metrics.get("in_sample") or {})
        metrics["oos"] = dict(metrics_oos or metrics.get("oos") or {})
        self._tracker.update_run(
            run_id,
            status="accepted" if accepted else "rejected",
            metrics=metrics,
            artifacts=[artifact_path] if artifact_path else None,
        )
        logger.info("[Experiment] finished %s: status=%s metrics_oos=%s",
                    run_id, "accepted" if accepted else "rejected", metrics.get("oos"))
        return True

    def fail_run(self, run_id: str, error: str = "") -> bool:
        """标记实验失败."""
        exp = self._tracker.get_run(run_id)
        if exp is None:
            return False
        oos = dict((exp.metrics or {}).get("oos") or {})
        oos["error"] = error
        self._tracker.update_run(run_id, status="failed", metrics={"oos": oos})
        return True

    def get_run(self, run_id: str) -> dict | None:
        """获取单次实验详情."""
        exp = self._tracker.get_run(run_id)
        if exp is None:
            return None
        return asdict(self._to_record(exp))

    def list_runs(
        self,
        experiment_type: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """列出实验记录."""
        return [
            {
                "run_id": exp.run_id,
                "experiment_type": exp.experiment_type,
                "status": exp.status,
                "timestamp": exp.timestamp,
            }
            for exp in self._tracker.query(exp_type=experiment_type, limit=limit)
        ]

    @staticmethod
    def _to_record(exp: Experiment) -> ExperimentRecord:
        metrics = dict(exp.metrics or {})
        return ExperimentRecord(
            run_id=exp.run_id,
            experiment_type=exp.experiment_type,
            params_json=dict(exp.params or {}),
            metrics_in_sample=dict(metrics.get("in_sample") or {}),
            metrics_oos=dict(metrics.get("oos") or {}),
            status=exp.status,
            artifact_path=exp.artifacts[0] if exp.artifacts else "",
            tags=list(exp.tags or []),
            timestamp=exp.timestamp,
            created_at=exp.timestamp,
        )
