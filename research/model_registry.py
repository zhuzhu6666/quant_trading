"""research/model_registry.py — ML 模型版本注册器.

跟踪每个模型 (meta_model_lightgbm, position_quality_lightgbm 等) 的版本: 训练参数、性能指标、artifact 路径。
支持版本回滚: list_versions() → load_version().

用法:
    reg = ModelRegistry()

    # 训练后注册新版本
    version = reg.register(
        model_type="example_model",
        artifact_path="data/model_artifacts/example_model/example_model.json",
        params={"n_estimators": 200, "max_depth": 4},
        metrics={"oos_acc": 0.685, "oos_sharpe": 1.2},
        symbol="XAUUSD+",
        timeframe="M5",
    )

    # 查看历史
    versions = reg.list_versions("example_model", symbol="XAUUSD+")
    best = reg.best_version("example_model", metric="oos_acc")
    rollback = reg.load_version("example_model", version=best)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ModelVersion:
    """单个模型版本记录."""
    id: int = 0
    model_type: str = ""            # meta_model_lightgbm / position_quality_lightgbm / etc.
    symbol: str = "XAUUSD+"
    timeframe: str = "M5"
    version: int = 0                 # 自增版本号
    artifact_path: str = ""          # 模型文件路径
    params_json: str = "{}"          # 训练参数
    metrics_json: str = "{}"         # 评估指标
    status: str = "active"           # active / archived / rolled_back
    created_at: float = 0.0
    timestamp: float = 0.0

    @property
    def params(self) -> dict:
        return json.loads(self.params_json) if self.params_json else {}

    @property
    def metrics(self) -> dict:
        return json.loads(self.metrics_json) if self.metrics_json else {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model_type": self.model_type,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "version": self.version,
            "artifact_path": self.artifact_path,
            "params": self.params,
            "metrics": self.metrics,
            "status": self.status,
            "created_at": self.created_at,
        }


class ModelRegistry:
    """ML 模型版本注册器.

    存储到 experiments.db (model_registry 表).
    """

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path
        self._ensure_table()

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
        return conn

    def _ensure_table(self) -> None:
        conn = self._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS model_registry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_type TEXT NOT NULL,
                    symbol TEXT DEFAULT 'XAUUSD+',
                    timeframe TEXT DEFAULT 'M5',
                    version INTEGER NOT NULL,
                    artifact_path TEXT DEFAULT '',
                    params_json TEXT DEFAULT '{}',
                    metrics_json TEXT DEFAULT '{}',
                    status TEXT DEFAULT 'active',
                    created_at REAL DEFAULT 0.0,
                    UNIQUE(model_type, symbol, timeframe, version)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_model_registry_lookup
                ON model_registry(model_type, symbol, timeframe)
            """)
            conn.commit()
        finally:
            conn.close()

    def register(
        self,
        model_type: str,
        *,
        artifact_path: str = "",
        params: dict | None = None,
        metrics: dict | None = None,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
    ) -> ModelVersion:
        """注册一个新模型版本. 返回 ModelVersion."""
        conn = self._get_conn()
        try:
            # 获取当前最大版本号
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) as max_ver FROM model_registry "
                "WHERE model_type=? AND symbol=? AND timeframe=?",
                (model_type, symbol, timeframe),
            ).fetchone()
            next_ver = (row["max_ver"] if row else 0) + 1

            now = time.time()
            conn.execute("""
                INSERT INTO model_registry
                (model_type, symbol, timeframe, version, artifact_path,
                 params_json, metrics_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """, (
                model_type, symbol, timeframe, next_ver,
                artifact_path,
                json.dumps(params or {}, ensure_ascii=False),
                json.dumps(metrics or {}, ensure_ascii=False),
                now,
            ))
            conn.commit()

            version = ModelVersion(
                id=conn.execute("SELECT last_insert_rowid()").fetchone()[0],
                model_type=model_type,
                symbol=symbol,
                timeframe=timeframe,
                version=next_ver,
                artifact_path=artifact_path,
                params_json=json.dumps(params or {}),
                metrics_json=json.dumps(metrics or {}),
                created_at=now,
                timestamp=now,
            )
            logger.info("[ModelRegistry] %s %s/%s v%d registered: %s",
                        model_type, symbol, timeframe, next_ver, artifact_path)
            return version
        finally:
            conn.close()

    def list_versions(
        self,
        model_type: str,
        *,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
        limit: int = 20,
    ) -> list[ModelVersion]:
        """列出模型的所有版本 (按版本号降序)."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM model_registry "
                "WHERE model_type=? AND symbol=? AND timeframe=? "
                "ORDER BY version DESC LIMIT ?",
                (model_type, symbol, timeframe, limit),
            ).fetchall()
            return [self._row_to_version(r) for r in rows]
        finally:
            conn.close()

    def get_version(
        self,
        model_type: str,
        *,
        version: int,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
    ) -> ModelVersion | None:
        """获取指定版本."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM model_registry "
                "WHERE model_type=? AND symbol=? AND timeframe=? AND version=?",
                (model_type, symbol, timeframe, version),
            ).fetchone()
            return self._row_to_version(row) if row else None
        finally:
            conn.close()

    def best_version(
        self,
        model_type: str,
        *,
        metric: str = "oos_acc",
        higher_is_better: bool = True,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
    ) -> ModelVersion | None:
        """根据指标返回最佳版本."""
        versions = self.list_versions(model_type, symbol=symbol, timeframe=timeframe)
        if not versions:
            return None
        versions = [v for v in versions if v.metrics.get(metric) is not None]
        if not versions:
            return None
        return max(versions, key=lambda v: v.metrics.get(metric, 0 if higher_is_better else float("inf")))

    def archive_version(
        self,
        model_type: str,
        *,
        version: int,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
    ) -> bool:
        """标记版本为 archived."""
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE model_registry SET status='archived' "
                "WHERE model_type=? AND symbol=? AND timeframe=? AND version=?",
                (model_type, symbol, timeframe, version),
            )
            conn.commit()
            return conn.execute("SELECT changes()").fetchone()[0] > 0
        finally:
            conn.close()

    def load_version(
        self,
        model_type: str,
        *,
        version: int | None = None,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
    ) -> str | None:
        """返回最佳或指定版本的 artifact_path.

        Args:
            version: 指定版本号. None 则自动选 best_version('oos_acc').
        Returns:
            artifact_path 或 None.
        """
        if version is not None:
            v = self.get_version(model_type, version=version, symbol=symbol, timeframe=timeframe)
        else:
            v = self.best_version(model_type, symbol=symbol, timeframe=timeframe)
        return v.artifact_path if v else None

    def summary(self) -> dict[str, Any]:
        """所有模型的版本摘要."""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT model_type, symbol, timeframe,
                       COUNT(*) as n_versions,
                       MAX(version) as latest_version,
                       MAX(created_at) as last_updated
                FROM model_registry
                GROUP BY model_type, symbol, timeframe
                ORDER BY model_type, symbol
            """).fetchall()
            return {
                f"{r['model_type']}/{r['symbol']}/{r['timeframe']}": {
                    "n_versions": r["n_versions"],
                    "latest_version": r["latest_version"],
                    "last_updated": r["last_updated"],
                }
                for r in rows
            }
        finally:
            conn.close()

    @staticmethod
    def _row_to_version(row) -> ModelVersion:
        return ModelVersion(
            id=row["id"],
            model_type=row["model_type"],
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            version=row["version"],
            artifact_path=row["artifact_path"],
            params_json=row["params_json"] or "{}",
            metrics_json=row["metrics_json"] or "{}",
            status=row["status"],
            created_at=row["created_at"],
        )
