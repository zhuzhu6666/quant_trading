"""SyncHealth — 跟踪数据同步健康度。

设计:
- 跟踪每次 sync 的 last_attempt_utc / last_success_utc / consecutive_failures
- 提供 is_degraded() / is_stale() / is_fresh() 判定
- 不持有 IO 状态(纯内存 + 可选 JSONL 持久化)
- 与 metrics.py 联动:状态变化 emit metric
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class SyncHealthRecord:
    last_attempt_ts: Optional[float] = None
    last_success_ts: Optional[float] = None
    consecutive_failures: int = 0
    last_error: str = ""
    total_attempts: int = 0
    total_successes: int = 0
    last_bar_ts_by_tf: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SyncHealth:
    """进程内单例 + JSONL 持久化。

    state_path 默认 data/charts/sync_health.json
    """

    _instance: Optional["SyncHealth"] = None
    _lock = threading.Lock()

    def __init__(self, state_path: Optional[str] = None) -> None:
        self._path = state_path or "data/charts/sync_health.json"
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._record = SyncHealthRecord()
        self._load()

    @classmethod
    def shared(cls, state_path: Optional[str] = None) -> "SyncHealth":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(state_path)
        return cls._instance

    @classmethod
    def reset_singleton(cls) -> None:
        with cls._lock:
            cls._instance = None

    @property
    def record(self) -> SyncHealthRecord:
        return self._record

    # ----- 写入 -----
    def record_attempt(self) -> None:
        self._record.last_attempt_ts = time.time()
        self._record.total_attempts += 1
        self._save()

    def record_success(self, last_bar_ts_by_tf: Optional[Dict[str, float]] = None) -> None:
        self._record.last_success_ts = time.time()
        self._record.last_attempt_ts = self._record.last_success_ts
        self._record.consecutive_failures = 0
        self._record.last_error = ""
        self._record.total_successes += 1
        if last_bar_ts_by_tf:
            self._record.last_bar_ts_by_tf.update(last_bar_ts_by_tf)
        self._save()
        self._emit_metrics()

    def record_failure(self, error: str) -> None:
        self._record.last_attempt_ts = time.time()
        self._record.consecutive_failures += 1
        self._record.last_error = error
        self._save()
        self._emit_metrics()

    # ----- 判定 -----
    def is_fresh(self, max_age_sec: float = 600.0) -> bool:
        """最近一次成功同步距今不超过 max_age_sec 视为 fresh。"""
        if self._record.last_success_ts is None:
            return False
        return (time.time() - self._record.last_success_ts) <= max_age_sec

    def is_stale(self, max_age_sec: float = 1800.0) -> bool:
        """最近一次成功同步距今超过 max_age_sec 视为 stale(默认 30 分钟)。"""
        return not self.is_fresh(max_age_sec)

    def is_degraded(self, max_consecutive_failures: int = 3) -> bool:
        return self._record.consecutive_failures >= max_consecutive_failures

    def last_bar_age_seconds(self, timeframe: str) -> Optional[float]:
        ts = self._record.last_bar_ts_by_tf.get(timeframe)
        if ts is None:
            return None
        return time.time() - ts

    def snapshot(self) -> Dict[str, Any]:
        rec = self._record.to_dict()
        rec["fresh"] = self.is_fresh()
        rec["stale"] = self.is_stale()
        rec["degraded"] = self.is_degraded()
        return rec

    def summary(self) -> Dict[str, Any]:
        """返回轻量摘要，与 snapshot 相同但去掉了内部字段。"""
        return self.snapshot()

    def check_and_log(self) -> None:
        """检查健康状态 + 数据库每个周期数据新鲜度。供 sync_health cron job 调用。
        使用 DataStore (DuckDB) 查询 bars 表。"""
        if self.is_degraded():
            logger.warning(
                "[SyncHealth] DEGRADED: %d consecutive failures, last error: %s",
                self._record.consecutive_failures, self._record.last_error,
            )
        elif self.is_stale():
            last_ok = self._record.last_success_ts
            age = (time.time() - last_ok) if last_ok else float("inf")
            logger.warning("[SyncHealth] STALE: last success %.0fs ago", age)
        else:
            logger.debug("[SyncHealth] healthy: fresh=%s", self.is_fresh())
        try:
            from data.store import DataStore
            store = DataStore("data/ctrader_data.duckdb")
            now = time.time()
            thresholds = {"M5": 900, "M15": 1800, "M30": 3600, "H1": 7200, "D1": 172800}
            stale_tfs = []
            for tf in ["M5", "M15", "M30", "H1", "D1"]:
                df = store.load_bars("XAUUSD+", tf, limit=1)
                if df is not None and len(df) > 0:
                    ts = df.index[-1]
                    ts_epoch = ts.timestamp() if hasattr(ts, 'timestamp') else float(ts)
                    age = now - ts_epoch
                    threshold = thresholds.get(tf, 3600)
                    if age > threshold:
                        stale_tfs.append(f"{tf}({age/3600:.1f}h)")
            if stale_tfs:
                logger.warning("[SyncHealth] data gap: %s", ", ".join(stale_tfs))
        except Exception:
            pass  # 数据库不可用时静默跳过

    # ----- 持久化 -----
    def _load(self) -> None:
        p = Path(self._path)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            self._record = SyncHealthRecord(
                last_attempt_ts=data.get("last_attempt_ts"),
                last_success_ts=data.get("last_success_ts"),
                consecutive_failures=int(data.get("consecutive_failures", 0)),
                last_error=str(data.get("last_error", "")),
                total_attempts=int(data.get("total_attempts", 0)),
                total_successes=int(data.get("total_successes", 0)),
                last_bar_ts_by_tf=dict(data.get("last_bar_ts_by_tf", {})),
            )
        except Exception:  # noqa: BLE001
            logger.exception("SyncHealth._load failed, starting fresh")
            self._record = SyncHealthRecord()

    def _save(self) -> None:
        try:
            Path(self._path).write_text(
                json.dumps(self._record.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.exception("SyncHealth._save failed for %s", self._path)
        # ★ 写入 state.db
        try:
            from backend.core.db import get_state_conn
            import time as _t
            conn = get_state_conn()
            try:
                conn.execute(
                    "INSERT INTO sync_health (status_json, updated_at) VALUES (?, ?)",
                    (json.dumps(self._record.to_dict(), ensure_ascii=False), _t.time())
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    def _emit_metrics(self) -> None:
        try:
            from backend.runtime.runtime_state import RuntimeState

            state = RuntimeState.shared()
            state.emit_metric("loop_status", {"kind": "sync", "value": 1 if self._record.last_success_ts else 0})
            for tf, ts in self._record.last_bar_ts_by_tf.items():
                age = time.time() - ts
                state.emit_metric("data_sync_last_bar_age_seconds", {"symbol": "XAUUSD+", "timeframe": tf, "value": age})
        except Exception:  # noqa: BLE001
            pass
