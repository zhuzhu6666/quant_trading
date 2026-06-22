"""EvolutionStory — 持久化"系统每天在干什么"事件流。

设计:
- append(event_type, payload) 一行一条 JSON 追加到 evolution_story.jsonl
- 提供 query(date) 接口回查(Phase 3 日报用)
- 与 metric Counter 联动:append 时自动 inc 对应 Counter

不写日志(那走 structured_log);只写"业务事件":
  - factor_birth: 因子被注册
  - factor_death: 因子被 unregister
  - canary_promote: canary stage 推进
  - canary_rollback: canary 回滚
  - retire_pending: DECAYING 触发 grace
  - pnl_attribution: 每 N 笔 trade 归因
  - sync_recovered: 数据同步自愈
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


class EvolutionStory:
    """Append-only JSONL 事件流。"""

    _instance: Optional["EvolutionStory"] = None
    _lock = threading.Lock()

    def __init__(self, path: Optional[str] = None) -> None:
        default_path = "data/charts/evolution_story.jsonl"
        self._path: str = path or default_path
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._fp_lock = threading.Lock()

    @classmethod
    def shared(cls, path: Optional[str] = None) -> "EvolutionStory":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(path)
        return cls._instance

    @classmethod
    def reset_singleton(cls) -> None:
        with cls._lock:
            cls._instance = None

    @property
    def path(self) -> str:
        return self._path

    def append(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """追加一条事件。

        自动加上 ts / ts_iso / event_type 字段。
        """
        record = {
            "ts": time.time(),
            "ts_iso": datetime.now(tz=timezone.utc).isoformat(),
            "event_type": event_type,
        }
        if payload:
            record.update(payload)
        # 文件备份
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._fp_lock:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
        # ★ 主存储: state.db
        try:
            from backend.core.db import get_state_conn
            conn = get_state_conn()
            try:
                conn.execute(
                    "INSERT INTO evolution_events (timestamp, event_type, payload_json) VALUES (?, ?, ?)",
                    (record["ts"], event_type, json.dumps(payload or {}, ensure_ascii=False))
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
        # 联动 metric
        try:
            from backend.runtime.runtime_state import RuntimeState

            RuntimeState.shared().emit_metric("factor_lifecycle_events_total", {"event": event_type, "source": (payload or {}).get("source", "unknown")})
        except Exception:  # noqa: BLE001
            pass
        return record

    def query(self, since_ts: Optional[float] = None, event_type: Optional[str] = None, limit: int = 1000) -> List[Dict[str, Any]]:
        """回查事件。since_ts 与 event_type 都可选;按时间倒序返回。"""
        if not os.path.exists(self._path):
            return []
        out: List[Dict[str, Any]] = []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if since_ts is not None and rec.get("ts", 0) < since_ts:
                        continue
                    if event_type is not None and rec.get("event_type") != event_type:
                        continue
                    out.append(rec)
        except OSError:
            logger.exception("EvolutionStory.query failed")
        out.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return out[:limit]

    def iter_all(self) -> Iterable[Dict[str, Any]]:
        """顺序迭代所有事件(给日报用)。"""
        if not os.path.exists(self._path):
            return
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
