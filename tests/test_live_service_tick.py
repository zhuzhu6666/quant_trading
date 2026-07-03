"""Tests for live_service._process_tick + _local_positions tracking.

audit 2026-06-10: SL/TP local mirror + non-blocking tick reads.
Task 1 added _local_positions / _track_local_sl_tp.
Task 2 added tick-level tests for _process_tick (no sync broker reads + amend).

2026-06-13: Removed old strategy-driven _process_tick tests.
Now _process_tick dispatches to Factor Takeover v4 pipeline.
Tests for the pipeline itself are in tests/alpha/.
"""
import threading
import time
from types import SimpleNamespace

import pytest
from unittest.mock import MagicMock

from backend.services import live_service
from backend.services import config_service
from config import runtime_config as rc


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset module-level state between tests."""
    rc.reset_for_tests()
    live_service._local_positions.clear()
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []
    live_service._live_state["market_session"] = None
    live_service._live_state["spot_quote"] = None
    live_service._pos_open_api_volume.clear()
    rc.reset_for_tests()
    yield
    live_service._local_positions.clear()
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []
    live_service._live_state["market_session"] = None
    live_service._live_state["spot_quote"] = None
    live_service._pos_open_api_volume.clear()


def _make_df():
    """Tiny M15 OHLCV dataframe for tick tests."""
    import pandas as pd
    idx = pd.date_range("2026-06-10 10:00", periods=5, freq="15min", tz="UTC")
    return pd.DataFrame({
        "open":  [4495, 4500, 4502, 4503, 4500],
        "high":  [4501, 4503, 4504, 4505, 4502],
        "low":   [4494, 4499, 4500, 4502, 4498],
        "close": [4500, 4502, 4503, 4503, 4500],
        "volume": [100, 110, 120, 130, 140],
    }, index=idx)


def _fake_bridge(position_id=12345, order_id=99):
    """Mock CTraderBridge: market_buy/sell returns ok, amend accepts."""
    bridge = MagicMock()
    result = MagicMock()
    result.success = True
    result.order_id = order_id
    result.position_id = position_id
    result.comment = "ok"
    bridge.market_buy.return_value = result
    bridge.market_sell.return_value = result
    bridge.amend_position_sltp.return_value = MagicMock(
        success=True, position_id=position_id,
    )
    return bridge


def test_resolve_position_api_volume_prefers_refreshed_position():
    """Actual filled size should come from refreshed broker positions, not the request."""
    refreshed = [{"position_id": 12345, "volume": 130.0}]
    vol = live_service._resolve_position_api_volume(12345, refreshed, 100.0)
    assert vol == 130.0


def test_entry_quality_learning_gate_interprets_factor_context_before_risk():
    gate = live_service._entry_quality_gate_from_learning_policy(
        policy={
            "active": True,
            "controls": [
                {
                    "suggestion_id": "psg_real_yield",
                    "scope_key": "real_yield_chg",
                    "action": "suppress_recent_worst_factor",
                    "suppressed_factor": "real_yield_chg",
                    "strong_signal_override": 0.78,
                }
            ],
        },
        decision_quality={
            "factor_conflict_ratio": 0.25,
            "top_contributors": [
                {"factor": "real_yield_chg", "contribution_score": -0.11},
            ],
        },
        signal_score=0.62,
    )

    assert gate["allowed"] is False
    assert gate["reason"] == "learning_recent_worst_factor_control"
    assert gate["source"] == "entry_quality_gate"
    assert gate["suggestion_id"] == "psg_real_yield"


def test_supervisor_tighten_sl_plan_clips_long_stop_below_current_price():
    plan = live_service._supervisor_tighten_sl_plan(
        {"direction": 1, "current_price": 4100.0, "sl": 4088.0},
        4102.0,
        quote={"bid": 4099.50, "ask": 4100.20, "mid": 4099.85},
    )

    assert plan["allowed"] is True
    assert plan["planned_sl"] < 4099.50
    assert plan["planned_sl"] > 4088.0
    assert plan["bid"] == 4099.50


def test_supervisor_tighten_sl_plan_clips_short_stop_above_current_price():
    plan = live_service._supervisor_tighten_sl_plan(
        {"direction": -1, "current_price": 4100.0, "sl": 4112.0},
        4098.0,
        quote={"bid": 4099.80, "ask": 4100.50, "mid": 4100.15},
    )

    assert plan["allowed"] is True
    assert plan["planned_sl"] > 4100.50
    assert plan["planned_sl"] < 4112.0
    assert plan["ask"] == 4100.50


def test_supervisor_tighten_sl_plan_skips_when_not_more_protective():
    plan = live_service._supervisor_tighten_sl_plan(
        {"direction": 1, "current_price": 4100.0, "sl": 4099.9},
        4102.0,
    )

    assert plan["allowed"] is False
    assert plan["reason"] == "not_tightening_long_stop_loss"


def test_entry_protection_repair_preserves_existing_sl_when_restoring_tp(monkeypatch):
    position = {
        "position_id": 270244024,
        "symbol": "XAUUSD+",
        "direction": -1,
        "current_price": 3976.5,
        "sl": 4014.95,
        "tp": 0.0,
    }
    protection_plan = {
        "schema_version": live_service._ENTRY_PROTECTION_PLAN_SCHEMA,
        "status": "failed",
        "direction": -1,
        "target_stop_loss": 4026.09,
        "target_take_profit": 3996.81,
        "last_attempt_ts": 0.0,
        "attempts": 1,
    }
    monkeypatch.setattr(
        live_service,
        "_load_recovery_position_row",
        lambda pid: {"recovery_meta": {"entry_protection_plan": protection_plan}},
    )

    candidates = live_service._entry_protection_repair_candidates(
        [position],
        current_price=3976.5,
        tick=12,
    )

    assert len(candidates) == 1
    assert candidates[0].source == live_service._ENTRY_PROTECTION_REPAIR_SOURCE
    assert candidates[0].controls["target_take_profit"] == 3996.81

    class _Policy:
        def evaluate(self, action, context):
            return SimpleNamespace(
                allowed=True,
                reason="risk_reducing_action",
                to_dict=lambda: {"allowed": True, "reason": "risk_reducing_action"},
            )

    class _Bridge:
        def __init__(self):
            self.amends = []

        def get_spot_quote(self):
            return {"bid": 3976.39, "ask": 3976.46, "mid": 3976.425}

        def amend_position_sltp(self, position_id, *, sl, tp):
            self.amends.append((position_id, sl, tp))
            return SimpleNamespace(success=True, position_id=position_id, comment="ok")

    updates = []
    monkeypatch.setattr(live_service, "_RISK_POLICY", _Policy())
    monkeypatch.setattr(live_service, "_log_supervisor_decision", lambda **kwargs: "dec_repair")
    monkeypatch.setattr(live_service, "_log_supervisor_trace", lambda **kwargs: None)
    monkeypatch.setattr(live_service, "_log_supervisor_position_event", lambda **kwargs: None)
    monkeypatch.setattr(live_service, "_remember_protection_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        live_service,
        "_update_entry_protection_plan_status",
        lambda position_id, **kwargs: updates.append((position_id, kwargs)),
    )

    bridge = _Bridge()
    handled = live_service._execute_trailing_candidate(
        candidates[0],
        bridge=bridge,
        cfg=SimpleNamespace(),
        tick=12,
        log=lambda msg: None,
        acct={},
    )

    assert handled is True
    assert bridge.amends == [(270244024, 4014.95, 3996.81)]
    assert updates[0][0] == 270244024
    assert updates[0][1]["status"] == "applied"
    assert updates[0][1]["applied_sl"] == 4014.95
    assert updates[0][1]["applied_tp"] == 3996.81


def test_pending_open_attach_blocks_until_position_is_confirmed():
    live_service._pending_open_attach_until.clear()

    live_service._remember_pending_open_attach(12345)

    assert live_service._active_pending_open_attach_ids(set()) == [12345]
    assert live_service._active_pending_open_attach_ids({12345}) == []


def test_entry_protection_failed_status_increments_attempt_and_remains_repairable(monkeypatch):
    meta = {
        "entry_protection_plan": {
            "schema_version": live_service._ENTRY_PROTECTION_PLAN_SCHEMA,
            "status": "pending",
            "direction": 1,
            "target_stop_loss": 3980.0,
            "target_take_profit": 4050.0,
            "attempts": 0,
            "last_attempt_ts": 0.0,
        }
    }
    merged = []
    monkeypatch.setattr(live_service, "_load_recovery_position_row", lambda pid: {"recovery_meta": meta})
    monkeypatch.setattr(live_service, "_merge_recovery_position_meta", lambda pid, next_meta: merged.append((pid, next_meta)))

    live_service._update_entry_protection_plan_status(
        12345,
        status="failed",
        error="amend_failed",
        attempted=True,
    )

    updated_plan = merged[0][1]["entry_protection_plan"]
    assert updated_plan["status"] == "failed"
    assert updated_plan["attempts"] == 1
    assert updated_plan["last_error"] == "amend_failed"
    updated_plan["last_attempt_ts"] = 0.0

    monkeypatch.setattr(
        live_service,
        "_load_recovery_position_row",
        lambda pid: {"recovery_meta": {"entry_protection_plan": updated_plan}},
    )
    candidates = live_service._entry_protection_repair_candidates(
        [{"position_id": 12345, "direction": 1, "sl": 0.0, "tp": 0.0}],
        current_price=4000.0,
        tick=13,
    )

    assert len(candidates) == 1
    assert candidates[0].source == live_service._ENTRY_PROTECTION_REPAIR_SOURCE


# ── _local_positions ─────────────────────────────────────


def test_local_positions_initially_empty():
    assert live_service._local_positions == {}


def test_track_local_position_adds_entry():
    live_service._track_local_sl_tp(position_id=42, sl=4480.0, tp=4550.0)
    assert 42 in live_service._local_positions
    entry = live_service._local_positions[42]
    assert entry.position_id == 42
    assert entry.sl == 4480.0
    assert entry.tp == 4550.0


def test_process_tick_does_not_call_account_info_or_get_positions_synchronously(monkeypatch):
    """Live tick is decision-only; it should NOT call bridge.account_info() or
    get_positions() synchronously — those live in the background refresh."""
    bridge = _fake_bridge()
    strategy = MagicMock()
    strategy.on_bar.return_value = None  # no signal
    strategy.last_atr = 7.0

    # Pre-populate cache as if the last WS push / status update wrote it
    live_service._live_state["account"] = {
        "ok": True, "broker": "ctrader", "balance": 10000.0,
        "equity": 10000.0, "currency": "USD", "leverage": 100,
    }
    live_service._live_state["positions"] = []

    df_new = _make_df()
    last_bar = df_new.iloc[-1]
    log_fn = MagicMock()

    with monkeypatch.context() as m:
        m.setattr("backend.services.live_service._should_send_orders", lambda broker: False)
        m.setattr("time.sleep", lambda s: None)
        live_service._process_tick(bridge, strategy, df_new, last_bar, "ctrader", tick=1, log=log_fn)

    bridge.account_info.assert_not_called()
    bridge.get_positions.assert_not_called()


def test_process_tick_dry_run_does_not_call_amend(monkeypatch):
    """When _factor_pipeline is None, dry-run tick should not fire orders."""
    bridge = _fake_bridge()
    strategy = MagicMock()
    strategy.on_bar.return_value = None
    strategy.last_atr = 7.0

    live_service._live_state["account"] = {"ok": True, "balance": 10000.0, "equity": 10000.0}
    live_service._live_state["positions"] = []
    live_service._factor_pipeline = None
    log_fn = MagicMock()

    with monkeypatch.context() as m:
        m.setattr("backend.services.live_service._should_send_orders", lambda broker: False)
        m.setattr("time.sleep", lambda s: None)
        live_service._process_tick(bridge, strategy, _make_df(), _make_df().iloc[-1], "ctrader", tick=1, log=log_fn)

    bridge.market_buy.assert_not_called()
    bridge.market_sell.assert_not_called()


def test_should_send_orders_respects_system_mode(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("system:\n  mode: backtest\nctrader:\n  send_orders: true\n", encoding="utf-8")
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)
    rc.replace(rc.RuntimeConfig(ctrader_send_orders=True, factor_dry_run=False))

    assert live_service._should_send_orders("ctrader") is False

    path.write_text("system:\n  mode: live\nctrader:\n  send_orders: true\n", encoding="utf-8")
    assert live_service._should_send_orders("ctrader") is True
