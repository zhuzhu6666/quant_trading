"""test_loop_host — LoopHost 的生命周期 + 互斥测试。"""
from __future__ import annotations

import asyncio
import pytest

from backend.runtime.loop_host import LoopHost
from backend.runtime.runtime_state import RuntimeState


@pytest.fixture(autouse=True)
def _reset_state():
    """每个测试前 reset RuntimeState 单例,避免相互污染。"""
    RuntimeState.reset_singleton()
    yield
    RuntimeState.reset_singleton()


async def _dummy_runner(state: RuntimeState) -> None:
    """短跑 50ms 然后退出的假 loop。"""
    await asyncio.sleep(0.05)


async def _long_runner(state: RuntimeState) -> None:
    await asyncio.sleep(10)


async def test_spawn_creates_running_loop_status() -> None:
    host = LoopHost()
    await host.spawn("paper", _dummy_runner)
    status = host.status()
    assert "paper" in status
    assert status["paper"]["running"] is True


async def test_double_spawn_raises() -> None:
    host = LoopHost()
    await host.spawn("paper", _long_runner)
    with pytest.raises(RuntimeError):
        await host.spawn("paper", _long_runner)
    await host.stop("paper")


async def test_stop_sets_running_false() -> None:
    host = LoopHost()
    await host.spawn("paper", _dummy_runner)
    await host.stop("paper")
    status = host.status()
    assert status["paper"]["running"] is False


async def test_is_running_reflects_lifecycle() -> None:
    host = LoopHost()
    assert host.is_running("paper") is False
    await host.spawn("paper", _dummy_runner)
    # dummy 在 sleep 50ms 期间应是 running
    assert host.is_running("paper") is True
    await host.stop("paper")
    assert host.is_running("paper") is False


async def test_stop_unknown_loop_returns_false() -> None:
    host = LoopHost()
    ok = await host.stop("never_spawned")
    assert ok is False


async def test_loop_status_records_error_on_exception() -> None:
    async def _bad_runner(state):
        raise ValueError("intentional")

    host = LoopHost()
    await host.spawn("paper", _bad_runner)
    # 等任务退出
    for _ in range(50):
        if not host.is_running("paper"):
            break
        await asyncio.sleep(0.01)
    s = host.status()["paper"]
    assert s["running"] is False
    assert "ValueError" in (s.get("last_error") or "")


async def test_stop_all_stops_every_loop() -> None:
    host = LoopHost()
    await host.spawn("a", _long_runner)
    await host.spawn("b", _long_runner)
    await host.stop_all()
    assert not host.is_running("a")
    assert not host.is_running("b")
