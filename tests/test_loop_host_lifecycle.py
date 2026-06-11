"""test_loop_host_lifecycle — 真实 asyncio 下 spawn → status → stop 端到端。"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.runtime.loop_host import LoopHost
from backend.runtime.runtime_state import RuntimeState


@pytest.fixture(autouse=True)
def _reset():
    RuntimeState.reset_singleton()
    yield
    RuntimeState.reset_singleton()


async def test_full_lifecycle() -> None:
    """spawn 后 is_running=True,stop 后是 False,中间能拿到正确 started_at。"""
    started_holder: dict = {}

    async def _r(state: RuntimeState) -> None:
        started_holder["now"] = time.time()
        await asyncio.sleep(0.2)

    host = LoopHost()
    t0 = time.time()
    await host.spawn("sync", _r)

    # spawn 完立刻 running
    assert host.is_running("sync")
    s = host.status()["sync"]
    assert s["started_at"] is not None
    assert s["started_at"] >= t0

    await host.stop("sync")
    assert not host.is_running("sync")
    s2 = host.status()["sync"]
    assert s2["stopped_at"] is not None
    assert s2["stopped_at"] >= s["started_at"]


async def test_two_independent_loops() -> None:
    async def _a(state):
        await asyncio.sleep(0.1)

    async def _b(state):
        await asyncio.sleep(0.1)

    host = LoopHost()
    await host.spawn("a", _a)
    await host.spawn("b", _b)
    assert host.is_running("a")
    assert host.is_running("b")
    await host.stop_all()
    assert not host.is_running("a")
    assert not host.is_running("b")


async def test_stop_during_long_sleep_returns_within_grace() -> None:
    """stop 应在 grace_sec 内返回(取消 task)。"""
    async def _slow(state):
        await asyncio.sleep(60)

    host = LoopHost()
    host._grace_sec = 0.5  # 缩短 grace
    await host.spawn("slow", _slow)
    t0 = time.time()
    await host.stop("slow")
    elapsed = time.time() - t0
    assert elapsed < 1.0  # 远小于 sleep 60
    assert not host.is_running("slow")
