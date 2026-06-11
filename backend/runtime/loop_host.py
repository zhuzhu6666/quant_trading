"""LoopHost — 在 FastAPI 进程内管理 paper/sync/live 长循环。

设计:
- 单一入口 spawn(kind, factory),factory 接收 RuntimeState 并返回 awaitable。
- stop(kind) 取消对应 task,等待 grace 时间内退出。
- status() 返回所有 loop 的 LoopStatus。
- live 仍走线程,所以本 Host 主要服务 paper/sync(都是 asyncio 协程);
  live 有自己的 thread manager,见 backend.services.live_service。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from .runtime_state import RuntimeState

logger = logging.getLogger(__name__)


RunnerFactory = Callable[[RuntimeState], Awaitable[None]]


class LoopHost:
    """FastAPI 进程内长循环宿主。

    单例,所有 loop 共享同一个 asyncio event loop(uvicorn 提供的那个)。
    """

    def __init__(self, state: Optional[RuntimeState] = None) -> None:
        self._state = state or RuntimeState.shared()
        self._grace_sec: float = 5.0

    @property
    def state(self) -> RuntimeState:
        return self._state

    async def spawn(self, kind: str, factory: RunnerFactory, extra: Optional[Dict[str, Any]] = None) -> None:
        """启动一个长循环,kind 唯一,重复启动会抛 RuntimeError。"""
        existing = self._state.get_loop_task(kind)
        if existing is not None and not existing.done():
            raise RuntimeError(f"loop '{kind}' already running")

        status = self._state.register_loop(kind)
        status.started_at = time.time()
        status.stopped_at = None
        status.last_error = None
        status.pid = os.getpid()
        if extra:
            status.extra.update(extra)

        coro = factory(self._state)
        task = asyncio.create_task(coro, name=f"loop-{kind}")
        self._state.set_loop_task(kind, task)

        def _done_callback(t: asyncio.Task) -> None:
            s = self._state.get_loop(kind)
            if s is None:
                return
            s.stopped_at = time.time()
            if t.cancelled():
                s.last_error = "cancelled"
            elif t.exception() is not None:
                s.last_error = repr(t.exception())
                logger.error("loop '%s' exited with error: %s", kind, s.last_error)
            else:
                logger.info("loop '%s' exited cleanly", kind)

        task.add_done_callback(_done_callback)
        logger.info("spawned loop '%s' task=%s", kind, task.get_name())

    async def stop(self, kind: str, grace_sec: Optional[float] = None) -> bool:
        task = self._state.get_loop_task(kind)
        if task is None or task.done():
            return False
        task.cancel()
        timeout = grace_sec if grace_sec is not None else self._grace_sec
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("error while stopping loop '%s'", kind)
        self._state.set_loop_task(kind, None)
        logger.info("stopped loop '%s'", kind)
        return True

    async def stop_all(self) -> None:
        kinds = list(self._state.all_loops().keys())
        for kind in kinds:
            await self.stop(kind)

    def status(self) -> Dict[str, Any]:
        return {kind: s.to_dict() for kind, s in self._state.all_loops().items()}

    def is_running(self, kind: str) -> bool:
        task = self._state.get_loop_task(kind)
        return task is not None and not task.done()
