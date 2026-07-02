"""research/evolution_experiment.py — 进化实验注册器.

封装 experiments.db 的读写, 让每次 GP 搜索、模型影子训练、参数调优都有科研记忆.
"""

from __future__ import annotations

import json
import logging
import time
import threading
from dataclasses import dataclass, field, asdict
from typing import Any

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

    def _get_conn(self):
        import sqlite3
        if self._db_path:
            path = self._db_path
        else:
            from backend.core.db import EXPERIMENTS_DB
            path = str(EXPERIMENTS_DB)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # Ensure DDL exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS experiments (
                run_id TEXT PRIMARY KEY,
                experiment_type TEXT,
                params_json TEXT DEFAULT '{}',
                metrics_json TEXT DEFAULT '{}',
                tags_json TEXT DEFAULT '[]',
                artifacts_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'running',
                timestamp REAL,
                created_at REAL
            )
        """)
        conn.commit()
        return conn

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
        now = time.time()
        record = ExperimentRecord(
            run_id=run_id,
            experiment_type=experiment_type,
            params_json=params or {},
            status="running",
            tags=tags or [],
            timestamp=now,
            created_at=now,
        )
        self._upsert(record)
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
        record = self._load(run_id)
        if record is None:
            logger.warning("[Experiment] finish: run_id %s not found", run_id)
            return False
        record.status = "accepted" if accepted else "rejected"
        if metrics_in_sample:
            record.metrics_in_sample = metrics_in_sample
        if metrics_oos:
            record.metrics_oos = metrics_oos
        if artifact_path:
            record.artifact_path = artifact_path
        self._upsert(record)
        logger.info("[Experiment] finished %s: status=%s metrics_oos=%s",
                    run_id, record.status, record.metrics_oos)
        return True

    def fail_run(self, run_id: str, error: str = "") -> bool:
        """标记实验失败."""
        record = self._load(run_id)
        if record is None:
            return False
        record.status = "failed"
        record.metrics_oos["error"] = error
        self._upsert(record)
        return True

    def get_run(self, run_id: str) -> dict | None:
        """获取单次实验详情."""
        record = self._load(run_id)
        if record is None:
            return None
        return asdict(record)

    def list_runs(
        self,
        experiment_type: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """列出实验记录."""
        conn = self._get_conn()
        try:
            if experiment_type:
                rows = conn.execute(
                    "SELECT * FROM experiments WHERE experiment_type=? ORDER BY timestamp DESC LIMIT ?",
                    (experiment_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM experiments ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            results = []
            for r in rows:
                results.append({
                    "run_id": r["run_id"],
                    "experiment_type": r["experiment_type"],
                    "status": r["status"],
                    "timestamp": r["timestamp"],
                })
            return results
        finally:
            conn.close()

    def _upsert(self, record: ExperimentRecord) -> None:
        """写入或更新实验记录."""
        row = record.to_db_row()
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO experiments
                (run_id, experiment_type, params_json, metrics_json,
                 tags_json, artifacts_json, status, timestamp, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["run_id"], row["experiment_type"],
                row["params_json"], row["metrics_json"],
                row["tags_json"], row["artifacts_json"],
                row["status"], row["timestamp"], row["created_at"],
            ))
            conn.commit()
        finally:
            conn.close()

    def _load(self, run_id: str) -> ExperimentRecord | None:
        """从 DB 加载实验记录."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM experiments WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            import json as _json
            params = _json.loads(row["params_json"]) if row["params_json"] else {}
            metrics = _json.loads(row["metrics_json"]) if row["metrics_json"] else {}
            tags = _json.loads(row["tags_json"]) if row["tags_json"] else []
            artifacts = _json.loads(row["artifacts_json"]) if row["artifacts_json"] else []
            return ExperimentRecord(
                run_id=row["run_id"],
                experiment_type=row["experiment_type"],
                params_json=params,
                metrics_in_sample=metrics.get("in_sample", {}),
                metrics_oos=metrics.get("oos", {}),
                status=row["status"],
                artifact_path=artifacts[0] if artifacts else "",
                tags=tags,
                timestamp=row["timestamp"],
                created_at=row["created_at"],
            )
        finally:
            conn.close()
