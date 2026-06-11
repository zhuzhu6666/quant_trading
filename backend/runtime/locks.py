"""asyncio.Lock 池,管理运行时互斥。

使用场景:
- vote_lock:投票与下单之间
- weight_lock:调权与读权之间
- register_lock:因子注册/晋升/退役之间

设计:单例 + 命名锁。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Dict

logger = logging.getLogger(__name__)


class LockPool:
    """进程内 asyncio.Lock 池。

    单例,通过 name 拿到/创建锁。锁实例必须在事件循环里 await,
    所以在 lock 第一次使用时绑定到当前 running loop。
    """

    _instance: "LockPool | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}
        self._loop_id: int | None = None
        self._create_lock = threading.Lock()

    @classmethod
    def shared(cls) -> "LockPool":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_singleton(cls) -> None:
        with cls._lock:
            cls._instance = None

    def get(self, name: str) -> asyncio.Lock:
        loop = asyncio.get_event_loop()
        loop_id = id(loop)
        with self._create_lock:
            if self._loop_id is not None and self._loop_id != loop_id:
                logger.warning(
                    "LockPool: loop changed (old=%s new=%s), resetting locks",
                    self._loop_id,
                    loop_id,
                )
                self._locks.clear()
            self._loop_id = loop_id
            if name not in self._locks:
                self._locks[name] = asyncio.Lock()
            return self._locks[name]

    def names(self) -> list[str]:
        return sorted(self._locks.keys())
