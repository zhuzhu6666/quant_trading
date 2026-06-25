import threading
from types import SimpleNamespace

import pytest

from backend.services import live_service


class _IdleThread:
    def __init__(self, target=None, args=(), name=None, daemon=None):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.ident = 12345
        self._alive = False

    def start(self):
        self._alive = True

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self._alive = False


@pytest.fixture(autouse=True)
def _reset_loop_state():
    live_service._loop_thread = None
    live_service._loop_stop_flag = None
    live_service._loop_broker = None
    live_service._loop_started_at = None
    live_service._loop_strategy_name = None
    live_service._last_loop_end = None
    live_service._live_state_update(
        broker=None,
        loop_running=False,
        loop_strategy=None,
        loop_started_at=None,
        account=None,
        account_updated_at=None,
    )
    live_service._reset_session_state_for_new_day()
    yield
    live_service._loop_thread = None
    live_service._loop_stop_flag = None
    live_service._loop_broker = None
    live_service._loop_started_at = None
    live_service._loop_strategy_name = None
    live_service._last_loop_end = None
    live_service._live_state_update(
        broker=None,
        loop_running=False,
        loop_strategy=None,
        loop_started_at=None,
        account=None,
        account_updated_at=None,
    )
    live_service._reset_session_state_for_new_day()


def test_prime_live_loop_state_sets_loop_and_session_fields():
    live_service._live_state_update(
        session_pnl=88.0,
        session_trades=3,
        session_winning=2,
        session_losing=1,
        session_consecutive_loss=1,
        session_max_drawdown_pct=4.1,
    )

    live_service._prime_live_loop_state(
        broker="ctrader",
        strategy_name="test_strategy",
        started_at=123.0,
        account={"ok": True, "broker": "ctrader", "balance": 1000.0, "equity": 1000.0},
    )

    assert live_service._live_state_get("broker") == "ctrader"
    assert live_service._live_state_get("loop_running") is True
    assert live_service._live_state_get("loop_strategy") == "test_strategy"
    assert live_service._live_state_get("loop_started_at") == 123.0
    assert live_service._live_state_get("account", clone=True)["balance"] == 1000.0
    assert live_service._live_state_get("session_pnl") == 0.0
    assert live_service._live_state_get("session_trades") == 0
    assert live_service._live_state_get("session_max_drawdown_pct") == 0.0


def test_start_loop_primes_shared_state_and_scheduler(monkeypatch):
    scheduler_calls = []

    monkeypatch.setattr(live_service, "_start_live_scheduler", lambda: scheduler_calls.append("started"))
    monkeypatch.setattr(live_service.threading, "Thread", _IdleThread)

    result = live_service.start_loop("ctrader", strategy_name="smoke")

    assert result["ok"] is True
    assert result["broker"] == "ctrader"
    assert result["strategy_name"] == "smoke"
    assert result["thread_id"] == 12345
    assert scheduler_calls == ["started"]
    assert isinstance(live_service._loop_stop_flag, threading.Event)
    assert live_service._live_state_get("loop_running") is True
    assert live_service._live_state_get("broker") == "ctrader"
    assert live_service._live_state_get("loop_strategy") == "smoke"

    acct = live_service._live_state_get("account", clone=True)
    assert acct["ok"] is True
    assert acct["broker"] == "ctrader"
    assert acct["balance"] == 0
    assert live_service._live_state_get("session_trades") == 0
    assert live_service._live_state_get("session_pnl") == 0.0


def test_mark_loop_stopped_for_display_preserves_cached_data():
    live_service._live_state_update(
        broker="ctrader",
        loop_running=True,
        loop_strategy="carry",
        account={"ok": True, "balance": 999.0},
    )

    live_service._mark_loop_stopped_for_display()

    assert live_service._live_state_get("loop_running") is False
    assert live_service._live_state_get("loop_strategy") is None
    assert live_service._live_state_get("broker") == "ctrader"
    assert live_service._live_state_get("account", clone=True)["balance"] == 999.0


def test_protection_prices_from_reference_use_direction_and_digits():
    assert live_service._protection_prices_from_reference(1, 4000.123, 10.0, 15.0, 2) == (3990.12, 4015.12)
    assert live_service._protection_prices_from_reference(-1, 4000.123, 10.0, 15.0, 2) == (4010.12, 3985.12)


def test_position_open_price_accepts_dict_and_object_payloads():
    assert live_service._position_open_price({"entry_price": 4008.5}) == 4008.5
    assert live_service._position_open_price(SimpleNamespace(open_price=4010.25)) == 4010.25
    assert live_service._position_open_price({"entry_price": None, "price": 3999.0}) == 3999.0


def test_record_filled_open_context_persists_even_before_amend_success(monkeypatch):
    calls = {"orders": [], "positions": [], "upserts": []}

    class _Ledger:
        def log_composite_decision(self, **kwargs):
            calls["decision"] = kwargs
            return "dec_open"

        def log_order_event(self, **kwargs):
            calls["orders"].append(kwargs)

        def log_position_event(self, **kwargs):
            calls["positions"].append(kwargs)

    class _Attr:
        def record_open(self, pid, trade_attr):
            calls["attr"] = (pid, trade_attr)

    composite = SimpleNamespace(
        direction=-1,
        score=-0.7,
        tactical_score=-0.8,
        macro_score=0.0,
        factor_signals={"rsi": -0.5},
        factor_values={"rsi": 70.0},
        active_weights={"rsi": 0.5},
        tags_breakdown={},
        n_active_factors=1,
        n_abstain_factors=0,
    )
    gate = SimpleNamespace(passed=True, reason="passed")
    cfg = SimpleNamespace(timeframe="M5")

    monkeypatch.setattr(live_service, "_LEDGER", _Ledger())
    monkeypatch.setattr(
        live_service,
        "_upsert_recovery_position_state",
        lambda raw, **kwargs: calls["upserts"].append((raw, kwargs)),
    )

    decision_id = live_service._record_filled_position_open_context(
        attr_engine=_Attr(),
        broker="ctrader",
        cfg=cfg,
        bar={"time": 123.0},
        tick=7,
        pid=268,
        actual_api_volume=100.0,
        requested_volume=100.0,
        fill_price=4008.5,
        current_price=4008.4,
        sl_price=4012.5,
        tp_price=3994.5,
        acct={"balance": 10000, "equity": 10001},
        pos=[],
        composite=composite,
        gate_result=gate,
    )

    assert decision_id == "dec_open"
    assert calls["decision"]["event_type"] == "open"
    assert [item["event_type"] for item in calls["orders"]] == ["submitted", "filled"]
    assert calls["positions"][0]["event_type"] == "opened"
    assert calls["upserts"][0][0]["entry_decision_id"] == "dec_open"
    assert calls["attr"][0] == 268
