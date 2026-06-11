"""RuntimeState — 进程内单例,集中所有长生命周期共享状态。

设计原则:
- 单例,任何地方通过 RuntimeState.shared() 拿到同一实例。
- 持有 loop 任务表、配置版本号、订阅者列表、metrics 注入点。
- 不持有任何 IO 资源(数据库/网络由各自的 service 负责)。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class LoopStatus:
    """单个 loop 的运行状态。"""

    kind: str
    started_at: Optional[float] = None
    stopped_at: Optional[float] = None
    last_error: Optional[str] = None
    pid: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def is_running(self) -> bool:
        return self.started_at is not None and self.stopped_at is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "last_error": self.last_error,
            "pid": self.pid,
            "running": self.is_running(),
            "extra": self.extra,
        }


ConfigSubscriber = Callable[["RuntimeState"], None]


class RuntimeState:
    """进程内单例。

    使用方式::

        state = RuntimeState.shared()
        state.set_config(new_cfg)
        state.subscribe(my_callback)
        state.start_loop("paper", coro_factory)
    """

    _instance: Optional["RuntimeState"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._run_id: str = uuid.uuid4().hex[:12]
        self._started_at: float = time.time()
        self._config_version: int = 0
        self._config: Dict[str, Any] = {}
        self._loops: Dict[str, LoopStatus] = {}
        self._loop_tasks: Dict[str, asyncio.Task] = {}
        self._subscribers: List[ConfigSubscriber] = []
        self._metrics_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None

    # ----- 单例 -----
    @classmethod
    def shared(cls) -> "RuntimeState":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_singleton(cls) -> None:
        """仅供测试使用。"""
        with cls._lock:
            cls._instance = None

    # ----- 基础属性 -----
    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def config_version(self) -> int:
        return self._config_version

    @property
    def config(self) -> Dict[str, Any]:
        return dict(self._config)

    # ----- 配置管理 -----
    def set_config(self, new_config: Dict[str, Any]) -> int:
        """原子替换配置,version 单调递增,广播给所有订阅者。"""
        self._config = dict(new_config)
        self._config_version += 1
        logger.info(
            "RuntimeConfig updated version=%d keys=%d",
            self._config_version,
            len(self._config),
        )
        self._notify_subscribers()
        return self._config_version

    def update_config(self, patch: Dict[str, Any]) -> int:
        """增量更新配置,key 存在则覆盖,不存在则新增。"""
        self._config.update(patch)
        return self.set_config(self._config)

    def subscribe(self, cb: ConfigSubscriber) -> None:
        self._subscribers.append(cb)

    def unsubscribe(self, cb: ConfigSubscriber) -> None:
        if cb in self._subscribers:
            self._subscribers.remove(cb)

    def _notify_subscribers(self) -> None:
        for cb in list(self._subscribers):
            try:
                cb(self)
            except Exception:  # noqa: BLE001
                logger.exception("config subscriber raised: %r", cb)

    # ----- loop 状态 -----
    def register_loop(self, kind: str) -> LoopStatus:
        if kind not in self._loops:
            self._loops[kind] = LoopStatus(kind=kind)
        return self._loops[kind]

    def get_loop(self, kind: str) -> Optional[LoopStatus]:
        return self._loops.get(kind)

    def all_loops(self) -> Dict[str, LoopStatus]:
        return dict(self._loops)

    def set_loop_task(self, kind: str, task: Optional[asyncio.Task]) -> None:
        if task is None:
            self._loop_tasks.pop(kind, None)
        else:
            self._loop_tasks[kind] = task

    def get_loop_task(self, kind: str) -> Optional[asyncio.Task]:
        return self._loop_tasks.get(kind)

    # ----- metrics 注入 -----
    def set_metrics_hook(self, hook: Optional[Callable[[str, Dict[str, Any]], None]]) -> None:
        """设置 metric 注入点。Phase 1.3 的 Metrics 单例会注册到这里。"""
        self._metrics_hook = hook

    def emit_metric(self, name: str, fields: Optional[Dict[str, Any]] = None) -> None:
        if self._metrics_hook is None:
            return
        try:
            self._metrics_hook(name, fields or {})
        except Exception:  # noqa: BLE001
            logger.exception("metrics_hook raised for %s", name)

    # ----- 序列化 -----
    def snapshot(self) -> Dict[str, Any]:
        return {
            "run_id": self._run_id,
            "started_at": self._started_at,
            "config_version": self._config_version,
            "config_keys": sorted(self._config.keys()),
            "loops": {k: v.to_dict() for k, v in self._loops.items()},
        }
