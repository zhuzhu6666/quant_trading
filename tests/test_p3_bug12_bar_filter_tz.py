"""
tests/test_p3_bug12_bar_filter_tz.py — P3 fix: bar_filter 时区

引自 framework_audit_20260604.md BUG-12:
data/live_sync/bar_filter.py:77-80 用 _time.time() (本地 epoch) 判当前 bar,
但 bar.time 是 broker server epoch。两者差 3h 时,
判'bar 是否在画' 会错 3h, 可能在 broker 视角的当前 bar 被入库,
frozen close 写入 db。

修复: BarFilter 接受 mt5_puller 注入, 用 puller.get_server_time_epoch()
替代 _time.time()。

本文件 3 个 case:
  - 有 puller 时用 server time
  - puller 返回 None 时 fallback 本地 epoch
  - 无 puller 参数时 fallback 本地 epoch
"""
import time

from data.live_sync.bar_filter import BarFilter


class _MockPuller:
    """mock MT5Puller, 返回固定 server time epoch (模拟 broker 偏差)"""
    def __init__(self, server_epoch: float | None):
        self._server_epoch = server_epoch

    def get_server_time_epoch(self) -> float | None:
        return self._server_epoch


def test_now_uses_server_time_when_puller_available():
    """P3: 有 puller 时, _now_epoch() 用 broker server epoch 而非本地"""
    local_now = time.time()
    server_now = local_now + 10800  # broker 慢 3h, 或本地 PC 快了 3h

    puller = _MockPuller(server_epoch=server_now)
    f = BarFilter(mt5_puller=puller)

    got = f._now_epoch()
    # 修复后: 拿到的是 server epoch
    assert abs(got - server_now) < 0.01, (
        f"BUG-12 复发: 用了本地 epoch {got}, 应是 server epoch {server_now}"
    )
    # 不应是本地
    assert abs(got - local_now) > 100, (
        f"BUG-12 修复未生效: got={got} 看起来还是本地"
    )


def test_now_falls_back_to_local_when_puller_returns_none():
    """P3: puller 拿不到 server time (None), fallback 本地 epoch"""
    local_now = time.time()

    puller = _MockPuller(server_epoch=None)
    f = BarFilter(mt5_puller=puller)

    got = f._now_epoch()
    # 拿不到时 fallback 本地
    assert abs(got - local_now) < 1.0


def test_now_falls_back_to_local_when_puller_not_provided():
    """P3: BarFilter() 无 puller 参数, fallback 本地 epoch (向后兼容)"""
    local_now = time.time()

    f = BarFilter()  # 不传 puller
    got = f._now_epoch()
    assert abs(got - local_now) < 1.0


def test_now_falls_back_when_puller_raises():
    """P3: puller 抛异常, BarFilter 不崩, fallback 本地"""
    class _BrokenPuller:
        def get_server_time_epoch(self):
            raise ConnectionError("MT5 not connected")

    f = BarFilter(mt5_puller=_BrokenPuller())
    got = f._now_epoch()
    local_now = time.time()
    assert abs(got - local_now) < 1.0  # 不崩, fallback
