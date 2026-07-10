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
from alpha.registry import factor_registry
from alpha.registry_adapter import RegistryAdapter
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
    live_service._live_state["last_processed_decision_bar_ts"] = 0.0
    live_service._pos_open_api_volume.clear()
    live_service._loop_stop_flag = None
    live_service._process_shutdown_requested = False
    rc.reset_for_tests()
    yield
    live_service._local_positions.clear()
    live_service._live_state["account"] = None
    live_service._live_state["positions"] = []
    live_service._live_state["market_session"] = None
    live_service._live_state["spot_quote"] = None
    live_service._live_state["last_processed_decision_bar_ts"] = 0.0
    live_service._pos_open_api_volume.clear()
    live_service._loop_stop_flag = None
    live_service._process_shutdown_requested = False


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


def test_merge_portfolio_configs_includes_active_discovered_factor(monkeypatch):
    discovered = "_test_live_discovered_factor"
    shadow = "_test_live_shadow_factor"

    def _one(df):
        import numpy as np

        return np.full(len(df), 1.0)

    factor_registry._factors[discovered] = _one
    factor_registry._factors[shadow] = _one

    class _Adapter:
        def __init__(self):
            self._meta = {
                discovered: {"source": "discovered"},
                shadow: {"source": "shadow"},
            }

        def list_by_source(self, source):
            return [name for name, meta in self._meta.items() if meta["source"] == source]

        def dead_names(self):
            return []

        def get_meta(self, name):
            return dict(self._meta.get(name, {"source": "builtin"}))

    monkeypatch.setattr(RegistryAdapter, "shared", staticmethod(lambda: _Adapter()))

    try:
        merged = live_service._merge_portfolio_configs(
            {"rsi_14": {"mode": "zscore_tanh", "tags": ["技术"]}},
            {"rsi_14": 1.0},
            0.7,
            0.3,
        )
    finally:
        factor_registry._factors.pop(discovered, None)
        factor_registry._factors.pop(shadow, None)

    assert discovered in merged
    assert shadow not in merged
    assert merged[discovered]["weight"] == 0.3
    assert merged[discovered]["source"] == "discovered"
    assert merged[discovered]["tags"] == ["GP发现"]
    assert merged[discovered]["role"] == "alpha"


def test_merge_portfolio_configs_does_not_activate_cold_weight_only_factor(monkeypatch):
    monkeypatch.setattr(
        "alpha.runtime_factor_selection.active_discovered_factor_ids",
        lambda config: [],
    )
    merged = live_service._merge_portfolio_configs(
        {"rsi_14": {"enabled": True}},
        {"rsi_14": 0.5, "dsl_auto_cold": 0.01},
        0.7,
        0.3,
    )
    assert "rsi_14" in merged
    assert "dsl_auto_cold" not in merged


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


def test_live_autonomy_budget_breach_tightens_incident(monkeypatch):
    calls = []

    class _IncidentControl:
        def status(self):
            return {"mode": "normal"}

        def set_mode(self, mode, *, reason, actor, confirm_thaw):
            calls.append(
                {
                    "mode": mode,
                    "reason": reason,
                    "actor": actor,
                    "confirm_thaw": confirm_thaw,
                }
            )
            return {"ok": True, "status": "applied", "target_mode": mode}

    monkeypatch.setattr(live_service, "RuntimeIncidentControlService", lambda: _IncidentControl())
    logs = []
    verdict = SimpleNamespace(
        to_dict=lambda: {
            "allowed": False,
            "reason": "live_autonomy_budget_breach",
            "audit_payload": {
                "source": "live_autonomy_budget",
                "recommended_incident_mode": "no_new_risk",
            },
        }
    )

    result = live_service._maybe_tighten_incident_for_live_autonomy_budget_breach(
        verdict,
        tick=7,
        log=logs.append,
    )

    assert result["status"] == "applied"
    assert calls == [
        {
            "mode": "no_new_risk",
            "reason": "live_autonomy_budget_breach",
            "actor": "system:live_autonomy_budget",
            "confirm_thaw": False,
        }
    ]
    assert "incident tighten" in logs[0]


def test_live_autonomy_budget_breach_does_not_relax_stricter_incident(monkeypatch):
    calls = []

    class _IncidentControl:
        def status(self):
            return {"mode": "only_close"}

        def set_mode(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": True, "status": "applied"}

    monkeypatch.setattr(live_service, "RuntimeIncidentControlService", lambda: _IncidentControl())
    verdict = SimpleNamespace(
        to_dict=lambda: {
            "allowed": False,
            "reason": "live_autonomy_budget_breach",
            "audit_payload": {"recommended_incident_mode": "no_new_risk"},
        }
    )

    result = live_service._maybe_tighten_incident_for_live_autonomy_budget_breach(
        verdict,
        tick=7,
        log=lambda _: None,
    )

    assert result["status"] == "already_strict"
    assert result["current_mode"] == "only_close"
    assert calls == []


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


def test_open_trade_context_sizing_floors_to_broker_step():
    volume, trace = live_service._apply_context_position_sizing(
        volume=123.0,
        sizing_trace={"schema_version": "position_sizing_trace.v1"},
        composite=SimpleNamespace(
            context_policy={
                "applied": True,
                "position_multiplier": 0.5,
                "reason": "thin_liquidity",
            }
        ),
        bridge_meta={"api_min_volume": 10.0, "api_step_volume": 10.0},
    )

    assert volume == 60.0
    assert trace["context_policy"]["raw_api_volume"] == 61.5
    assert trace["context_policy"]["adjusted_api_volume"] == 60.0
    assert trace["context_policy"]["reason"] == "thin_liquidity"


def test_open_trade_context_sizing_rounds_reduction_to_nearest_broker_step():
    volume, trace = live_service._apply_context_position_sizing(
        volume=300.0,
        sizing_trace={"schema_version": "position_sizing_trace.v1"},
        composite=SimpleNamespace(
            context_policy={
                "applied": True,
                "position_multiplier": 0.57375,
                "reason": "event_window_near;low_volatility;asia_session",
            }
        ),
        bridge_meta={"api_min_volume": 100.0, "api_step_volume": 100.0},
    )

    assert volume == 200.0
    assert trace["context_policy"]["raw_api_volume"] == 172.125
    assert trace["context_policy"]["adjusted_api_volume"] == 200.0


def test_open_trade_context_sizing_blocks_when_reduction_below_broker_min():
    volume, trace = live_service._apply_context_position_sizing(
        volume=100.0,
        sizing_trace={"schema_version": "position_sizing_trace.v1"},
        composite=SimpleNamespace(
            context_policy={
                "applied": True,
                "position_multiplier": 0.5,
                "reason": "low_quality_context",
            }
        ),
        bridge_meta={"api_min_volume": 100.0, "api_step_volume": 100.0},
    )

    assert volume == 0.0
    assert trace["context_policy"]["raw_api_volume"] == 50.0
    assert trace["context_policy"]["adjusted_api_volume"] == 0.0
    assert trace["context_policy"]["blocked_reason"].startswith("context_sizing_below_min")
    assert trace["context_policy_candidate_api_volume"] == 100.0


def test_open_trade_context_sizing_preserves_demo_nursery_exploration_min_volume():
    volume, trace = live_service._apply_context_position_sizing(
        volume=100.0,
        sizing_trace={
            "schema_version": "position_sizing_trace.v1",
            "demo_nursery_exploration": True,
        },
        composite=SimpleNamespace(
            context_policy={
                "applied": True,
                "position_multiplier": 0.5,
                "reason": "low_quality_context",
            }
        ),
        bridge_meta={"api_min_volume": 100.0, "api_step_volume": 100.0},
    )

    assert volume == 100.0
    assert trace["context_policy_demo_nursery_min_preserved"] is True
    assert trace["context_policy"]["raw_api_volume"] == 50.0
    assert trace["context_policy"]["adjusted_api_volume"] == 100.0
    assert trace["context_policy"]["blocked_reason"] == ""


def test_open_trade_context_sizing_does_not_lift_non_positive_upstream_size():
    volume, trace = live_service._apply_context_position_sizing(
        volume=0.0,
        sizing_trace={
            "schema_version": "position_sizing_trace.v1",
            "blocked_reason": "kelly_fraction_non_positive",
        },
        composite=SimpleNamespace(
            context_policy={
                "applied": True,
                "position_multiplier": 0.75,
                "reason": "low_quality_context",
            }
        ),
        bridge_meta={"api_min_volume": 100.0, "api_step_volume": 100.0},
    )

    assert volume == 0.0
    assert trace["blocked_reason"] == "kelly_fraction_non_positive"
    assert trace["context_policy"]["raw_api_volume"] == 0.0
    assert trace["context_policy"]["adjusted_api_volume"] == 0.0
    assert trace["context_policy"]["blocked_reason"] == "kelly_fraction_non_positive"


def test_open_trade_pipeline_stops_before_broker_order_when_attach_pending():
    bridge = _fake_bridge()
    logs: list[str] = []
    gate_result = SimpleNamespace(passed=True, reason="pass")

    returned_gate = live_service._run_open_trade_pipeline(
        bridge=bridge,
        pipeline={},
        broker="ctrader",
        cfg=SimpleNamespace(),
        bar={"time": time.time()},
        factor_values={},
        composite=SimpleNamespace(direction=1, score=0.8),
        gate_result=gate_result,
        account={"balance": 10000.0, "equity": 10000.0},
        positions=[],
        attr_engine=None,
        current_price=4000.0,
        atr_price=4.0,
        pending_open_attach_ids=[12345],
        send=True,
        tick=7,
        log=logs.append,
    )

    assert returned_gate is gate_result
    assert any("pending_open_attach" in message for message in logs)
    bridge.market_buy.assert_not_called()
    bridge.market_sell.assert_not_called()


def _open_pipeline_kwargs(bridge, logs, *, stop_requested=None):
    return {
        "bridge": bridge,
        "pipeline": {},
        "broker": "ctrader",
        "cfg": SimpleNamespace(),
        "bar": {"time": time.time()},
        "factor_values": {},
        "composite": SimpleNamespace(direction=1, score=0.8),
        "gate_result": SimpleNamespace(passed=True, reason="pass"),
        "account": {"balance": 10000.0, "equity": 10000.0},
        "positions": [],
        "attr_engine": None,
        "current_price": 4000.0,
        "atr_price": 4.0,
        "pending_open_attach_ids": [],
        "send": True,
        "tick": 8,
        "log": logs.append,
        "stop_requested": stop_requested,
    }


def test_open_trade_pipeline_blocks_draining_before_candidate(monkeypatch):
    bridge = _fake_bridge()
    logs = []
    stop_flag = threading.Event()
    stop_flag.set()
    prepare = MagicMock()
    monkeypatch.setattr(live_service, "_prepare_open_trade_candidate", prepare)

    gate = live_service._run_open_trade_pipeline(
        **_open_pipeline_kwargs(bridge, logs, stop_requested=stop_flag.is_set)
    )

    assert gate.passed is False
    assert gate.reason == "loop_draining"
    assert any("loop_draining stage=before_candidate" in item for item in logs)
    prepare.assert_not_called()
    bridge.market_buy.assert_not_called()
    bridge.market_sell.assert_not_called()


def test_open_trade_pipeline_blocks_draining_after_candidate_before_submit(monkeypatch):
    bridge = _fake_bridge()
    logs = []
    stop_flag = threading.Event()
    candidate = SimpleNamespace(
        direction_name="LONG",
        volume=100.0,
        order_block={"order_blocked": False},
    )

    def _prepare(**_kwargs):
        stop_flag.set()
        return candidate

    monkeypatch.setattr(live_service, "_prepare_open_trade_candidate", _prepare)

    gate = live_service._run_open_trade_pipeline(
        **_open_pipeline_kwargs(bridge, logs, stop_requested=stop_flag.is_set)
    )

    assert gate.passed is False
    assert gate.reason == "loop_draining"
    assert any("loop_draining stage=broker_submit" in item for item in logs)
    bridge.market_buy.assert_not_called()
    bridge.market_sell.assert_not_called()


def test_process_shutdown_waits_for_admitted_order_post_fill(monkeypatch):
    logs = []
    stop_flag = threading.Event()
    rpc_entered = threading.Event()
    allow_rpc_return = threading.Event()
    post_fill_entered = threading.Event()
    allow_post_fill_return = threading.Event()
    worker_finished = threading.Event()
    shutdown_finished = threading.Event()
    runtime_writes = []
    candidate = SimpleNamespace(direction_name="LONG", volume=100.0)
    result = SimpleNamespace(success=True)

    def _order(_bridge, _composite, _volume):
        rpc_entered.set()
        assert allow_rpc_return.wait(2.0)
        return result

    def _post_fill(**_kwargs):
        assert stop_flag.wait(2.0)
        post_fill_entered.set()
        assert allow_post_fill_return.wait(2.0)

    monkeypatch.setattr(live_service, "_submit_open_trade_order", _order)
    monkeypatch.setattr(live_service, "_handle_open_trade_order_success", _post_fill)
    monkeypatch.setattr(
        live_service,
        "_runtime_kv_set",
        lambda key, value: runtime_writes.append((key, value)),
    )

    def _run_order():
        try:
            live_service._submit_open_trade_candidate(
                bridge=object(),
                attr_engine=None,
                broker="ctrader",
                cfg=SimpleNamespace(),
                bar={"time": time.time()},
                tick=9,
                account={},
                positions=[],
                composite=SimpleNamespace(direction=1),
                gate_result=SimpleNamespace(passed=True, reason="pass"),
                candidate=candidate,
                current_price=4000.0,
                log=logs.append,
                stop_requested=stop_flag.is_set,
            )
        finally:
            worker_finished.set()

    worker = threading.Thread(target=_run_order)
    live_service._loop_thread = worker
    live_service._loop_stop_flag = stop_flag
    live_service._loop_broker = "ctrader"
    live_service._loop_started_at = time.time()
    live_service._loop_strategy_name = "factor_v4"
    worker.start()
    assert rpc_entered.wait(1.0)

    shutdown_result = {}

    def _shutdown():
        shutdown_result.update(
            live_service.stop_loop_for_process_shutdown(timeout_sec=2.0)
        )
        shutdown_finished.set()

    shutdown = threading.Thread(target=_shutdown)
    shutdown.start()
    allow_rpc_return.set()
    assert post_fill_entered.wait(1.0)
    assert live_service._live_state_get("loop_shutdown")["status"] == "draining"
    assert live_service._live_state_get("accepting_new_risk") is False
    assert shutdown_finished.is_set() is False
    assert runtime_writes == []

    allow_post_fill_return.set()
    worker.join(timeout=2.0)
    shutdown.join(timeout=2.0)

    assert worker_finished.is_set() is True
    assert shutdown_finished.is_set() is True
    assert shutdown_result["status"] == "completed"
    assert runtime_writes == [
        (live_service._RUNTIME_KV_LAST_SHUTDOWN, shutdown_result)
    ]


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


def test_process_tick_duplicate_decision_bar_skips_open_decision(monkeypatch):
    """Same closed decision bar should not be fed into the signal/open path twice."""
    bridge = _fake_bridge()
    df_new = _make_df()
    last_bar = df_new.iloc[-1]
    bar_ts = float(df_new.index[-1].timestamp())
    live_service._live_state["account"] = {"ok": True, "balance": 10000.0, "equity": 10000.0}
    live_service._live_state["positions"] = []
    live_service._live_state["last_processed_decision_bar_ts"] = bar_ts
    logs: list[str] = []

    def _fail_decision_pipeline(**_kwargs):
        raise AssertionError("duplicate bar must not run live decision pipeline")

    previous_pipeline = live_service._factor_pipeline
    live_service._factor_pipeline = {
        "last_factor_values": {"atr_ratio": 0.001},
        "attribution": None,
    }
    try:
        monkeypatch.setattr(live_service, "_decision_run_live_decision_pipeline", _fail_decision_pipeline)
        monkeypatch.setattr(live_service, "_write_live_trade_log_factor", lambda *args, **kwargs: None)
        monkeypatch.setattr(live_service, "_check_business_alerts", lambda *args, **kwargs: None)
        live_service._process_tick(
            bridge,
            None,
            df_new,
            last_bar,
            "ctrader",
            tick=2,
            log=logs.append,
        )
    finally:
        live_service._factor_pipeline = previous_pipeline

    bridge.market_buy.assert_not_called()
    bridge.market_sell.assert_not_called()
    assert any("decision bar already processed" in item for item in logs)


def test_should_send_orders_respects_system_mode(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("system:\n  mode: backtest\nctrader:\n  send_orders: true\n", encoding="utf-8")
    monkeypatch.setattr(config_service, "SETTINGS_PATH", path)
    rc.replace(rc.RuntimeConfig(ctrader_send_orders=True, factor_dry_run=False))

    assert live_service._should_send_orders("ctrader") is False

    path.write_text("system:\n  mode: live\nctrader:\n  send_orders: true\n", encoding="utf-8")
    assert live_service._should_send_orders("ctrader") is True
