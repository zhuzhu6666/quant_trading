"""业务告警边沿触发测试 (2026-08-25)。

背景缺陷: session_max_drawdown_pct 是当日只涨不降的水位、consec 在持仓
未平前不变, 旧实现 `tick % 10 == 0` 会把同一条告警每 10 tick (约5分钟)
重发一遍, 直到日切。修复: 全部规则改为边沿触发——状态恶化跨过阈值那一刻
发一次, 回落后重新武装。
"""
import pytest

from backend.services import live_service


@pytest.fixture(autouse=True)
def _reset_alert_state():
    live_service._reset_business_alert_armed()
    yield
    live_service._reset_business_alert_armed()


class _FakeAlerter:
    def __init__(self):
        self.sent = []

    def send(self, level, title, message, **kwargs):
        self.sent.append((level, title))


@pytest.fixture
def fake_alerter(monkeypatch):
    holder = {"alerter": _FakeAlerter()}

    def _factory(_cfg):
        return holder["alerter"]

    monkeypatch.setattr(
        "monitor.alerter.Alerter", _factory, raising=True
    )
    return holder


def _run(acct=None, log=lambda _msg: None, tick=1):
    live_service._check_business_alerts(
        tick, acct or {"balance": 500.0}, [], log
    )


def test_drawdown_sent_once_not_every_10_ticks(fake_alerter, monkeypatch):
    """回撤 4.9% 水位持续存在时只发一次, 不再每 5 分钟刷屏."""
    monkeypatch.setattr(
        live_service, "_live_state_get",
        lambda key, default=None, clone=False: {
            "session_max_drawdown_pct": 4.95,
            "session_consecutive_loss": 0,
            "session_pnl": -20.0,
        }.get(key, default),
    )
    for tick in range(1, 31):  # 覆盖旧逻辑的多个 %10==0 触发点
        _run(tick=tick)
    sent = [s for s in fake_alerter["alerter"].sent if "回撤" in s[1]]
    assert len(sent) == 1
    assert sent[0][0] == "WARNING"


def test_drawdown_escalates_to_error_exactly_once(fake_alerter, monkeypatch):
    """水位从 WARNING 区抬到 ERROR 区: 升级补发一次, 之后不再重复."""
    state = {"session_max_drawdown_pct": 3.5, "session_consecutive_loss": 0}
    monkeypatch.setattr(
        live_service, "_live_state_get",
        lambda key, default=None, clone=False: state.get(key, default),
    )
    _run(tick=1)
    _run(tick=2)
    assert len(fake_alerter["alerter"].sent) == 1  # WARNING once

    state["session_max_drawdown_pct"] = 6.2
    _run(tick=3)
    _run(tick=4)
    _run(tick=12)  # 旧逻辑的 %10 触发点也不许再发
    levels = [s[0] for s in fake_alerter["alerter"].sent]
    assert levels.count("ERROR") == 1
    assert levels.count("WARNING") == 1


def test_consecutive_loss_rearms_when_streak_resets(fake_alerter, monkeypatch):
    """连亏告警: 进入状态发一次, 加深(3→4)每档再发一次, 回落重新武装."""
    state = {"session_consecutive_loss": 4, "session_max_drawdown_pct": 0.0}
    monkeypatch.setattr(
        live_service, "_live_state_get",
        lambda key, default=None, clone=False: state.get(key, default),
    )
    _run(tick=1)
    _run(tick=11)
    _run(tick=21)
    assert len(fake_alerter["alerter"].sent) == 1  # 同一笔数不重复

    state["session_consecutive_loss"] = 5  # 连亏加深: 升级一次
    _run(tick=31)

    state["session_consecutive_loss"] = 0  # 赢一笔, 回落重新武装
    _run(tick=41)

    state["session_consecutive_loss"] = 3  # 新一轮连亏
    _run(tick=51)
    titles = [s[1] for s in fake_alerter["alerter"].sent]
    assert titles.count("⚠️ 连续亏损 4 笔") == 1
    assert titles.count("⚠️ 连续亏损 5 笔") == 1
    assert titles.count("⚠️ 连续亏损 3 笔") == 1
    assert len(titles) == 3


def test_circuit_breaker_edge_triggered_once(fake_alerter, monkeypatch):
    state = {"circuit_breaker": True, "circuit_reason": "daily_loss",
             "session_consecutive_loss": 0}
    monkeypatch.setattr(
        live_service, "_live_state_get",
        lambda key, default=None, clone=False: state.get(key, default),
    )
    for tick in (1, 11, 21):
        _run(tick=tick)
    critical = [s for s in fake_alerter["alerter"].sent if s[0] == "CRITICAL"]
    assert len(critical) == 1


def test_reset_business_alert_armed_clears_edges():
    live_service._business_alert_should_send("dd_warn", True)
    live_service._reset_business_alert_armed()
    # 重置后同一状态应能再次触发 (新交易日语义)
    assert live_service._business_alert_should_send("dd_warn", True) is True
