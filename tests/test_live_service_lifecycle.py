import threading
import sqlite3
import time
from types import SimpleNamespace

import pytest

from backend.ledger.service import DecisionLedger
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
    live_service._pending_close_reasons.clear()
    live_service._pending_close_verdicts.clear()
    live_service._recovery_zero_confirmations.clear()
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
    live_service._pending_close_reasons.clear()
    live_service._pending_close_verdicts.clear()
    live_service._recovery_zero_confirmations.clear()
    live_service._live_state_update(
        broker=None,
        loop_running=False,
        loop_strategy=None,
        loop_started_at=None,
        account=None,
        account_updated_at=None,
    )
    live_service._reset_session_state_for_new_day()


def _patch_live_state_conn(monkeypatch, conn_factory):
    monkeypatch.setattr(live_service, "_get_state_pg_conn", conn_factory)
    monkeypatch.setattr(live_service, "_get_state_read_conn", conn_factory)


def test_closed_position_handler_preserves_close_source_mapping(monkeypatch):
    close_source = {
        "close_reason_source": "supervisor_tighten_stopout",
        "inferred_close_supervisor": {"event_type": "supervisor_tighten"},
    }
    captured = {}

    monkeypatch.setattr(
        live_service,
        "_collect_closed_position_attribution",
        lambda **kwargs: {
            "close_reason": "broker_close",
            "close_verdict": {"allowed": True},
            "close_ts": 1234.0,
            "attribution_integrity": "full",
            "factor_contributions": {"trend": -1.0},
            "close_source": close_source,
            "total_pnl": -1.0,
        },
    )
    monkeypatch.setattr(live_service, "_write_close_decision_log_after_tick", lambda **kwargs: None)
    monkeypatch.setattr(live_service, "_lookup_recovery_context_integrity", lambda *args: "full")

    def _capture_ledger(**kwargs):
        captured["ledger_close_source"] = kwargs["close_source"]
        return "exit_decision_1", kwargs["context_integrity"]

    def _capture_learning(**kwargs):
        captured["learning_close_source"] = kwargs["close_source"]

    monkeypatch.setattr(live_service, "_log_closed_position_ledger_after_tick", _capture_ledger)
    monkeypatch.setattr(live_service, "_run_closed_position_learning_after_tick", _capture_learning)
    monkeypatch.setattr(live_service, "_cleanup_closed_position_after_tick", lambda **kwargs: None)

    live_service._handle_closed_positions_after_tick(
        closed_pids={123},
        real_pnls={123: {"net": -1.0}},
        attr_engine=None,
        current_price=3330.0,
        bar={"time": 1230.0},
        cfg=SimpleNamespace(timeframe="M5"),
        acct={},
        broker="ctrader",
        tick=7,
        log=lambda message: None,
    )

    assert captured["ledger_close_source"] == close_source
    assert captured["learning_close_source"] == close_source


def test_prime_live_loop_state_sets_loop_and_resets_session_when_no_snapshot(monkeypatch):
    monkeypatch.setattr(live_service, "_restore_session_state_for_day", lambda trade_date=None: False)

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


def test_prime_live_loop_state_restores_existing_session_snapshot(monkeypatch):
    reset_called = False

    def _restore(trade_date=None):
        live_service._live_state_update(
            session_pnl=2.75,
            session_trades=29,
            session_winning=8,
            session_losing=21,
            session_consecutive_loss=1,
        )
        return True

    def _reset():
        nonlocal reset_called
        reset_called = True

    monkeypatch.setattr(live_service, "_restore_session_state_for_day", _restore)
    monkeypatch.setattr(live_service, "_reset_session_state_for_new_day", _reset)

    live_service._prime_live_loop_state(
        broker="ctrader",
        strategy_name="test_strategy",
        started_at=123.0,
        account={"ok": True, "broker": "ctrader", "balance": 1000.0, "equity": 1000.0},
    )

    assert reset_called is False
    assert live_service._live_state_get("loop_running") is True
    assert live_service._live_state_get("session_pnl") == pytest.approx(2.75)
    assert live_service._live_state_get("session_trades") == 29


def test_floor_api_volume_to_step_skips_untradeable_partial_reduce():
    meta = {"api_min_volume": 100, "api_step_volume": 100}

    assert live_service._floor_api_volume_to_step(50.0, meta) == 0.0
    assert live_service._floor_api_volume_to_step(150.0, meta) == 100.0
    assert live_service._floor_api_volume_to_step(200.0, meta) == 200.0


def test_kelly_sizing_outputs_api_volume_tiers():
    live_service._live_state_update(risk={"kelly": {"kelly_fraction": 1.0}})
    cfg = SimpleNamespace(
        kelly_enabled=True,
        kelly_fraction=1.0,
        kelly_risk_per_trade_pct=0.02,
        kelly_max_pct=0.25,
        max_position_api_volume=1000.0,
        dynamic_sizing_enabled=True,
        dynamic_sizing_max_api_volume=300.0,
        dynamic_sizing_api_units_per_display_unit=100.0,
    )
    meta = {"api_min_volume": 100, "api_step_volume": 100}

    result = live_service._risk_kelly_sizing(
        cfg, 1, current_price=4000.0, sl_price=3990.0,
        bridge_meta=meta, acct={"equity": 1000.0},
    )

    assert result["volume"] == 200.0
    assert result["trace"]["raw_api_volume"] == pytest.approx(200.0)
    assert result["trace"]["base_api_volume"] == 200.0


def test_kelly_sizing_respects_initial_dynamic_cap():
    live_service._live_state_update(risk={"kelly": {"kelly_fraction": 1.0}})
    cfg = SimpleNamespace(
        kelly_enabled=True,
        kelly_fraction=1.0,
        kelly_risk_per_trade_pct=0.10,
        kelly_max_pct=0.25,
        max_position_api_volume=1000.0,
        dynamic_sizing_enabled=True,
        dynamic_sizing_max_api_volume=300.0,
        dynamic_sizing_api_units_per_display_unit=100.0,
    )
    meta = {"api_min_volume": 100, "api_step_volume": 100}

    result = live_service._risk_kelly_sizing(
        cfg, 1, current_price=4000.0, sl_price=3990.0,
        bridge_meta=meta, acct={"equity": 1000.0},
    )

    assert result["volume"] == 300.0
    assert result["trace"]["capped_raw_api_volume"] == 300.0


def test_event_sizing_below_min_skips_instead_of_lifting_to_min():
    meta = {"api_min_volume": 100, "api_step_volume": 100}

    result = live_service._apply_entry_event_sizing(
        base_volume=100.0,
        event_multiplier=0.2,
        bridge_meta=meta,
        sizing_trace={"base_api_volume": 100.0},
    )

    assert result["volume"] == 0.0
    assert result["blocked_reason"].startswith("event_sizing_below_min")
    assert result["trace"]["final_api_volume"] == 0.0


def test_event_sizing_floors_tradeable_reduced_tier():
    meta = {"api_min_volume": 100, "api_step_volume": 100}

    result = live_service._apply_entry_event_sizing(
        base_volume=300.0,
        event_multiplier=0.5,
        bridge_meta=meta,
    )

    assert result["volume"] == 100.0
    assert result["blocked_reason"] == ""


def test_untradeable_min_position_reduce_upgrades_to_close_when_thesis_broken():
    should_close, reason = live_service._should_full_close_untradeable_reduce(
        current_volume=100.0,
        raw_reduce_volume=50.0,
        reduce_volume=0.0,
        min_volume=100.0,
        verdict={
            "summary_reason": "profit_giveback_after_mfe",
            "evidence": {
                "thesis_status": "broken",
                "thesis_break_confirmed": True,
                "giveback_ratio": 1.0,
                "current_pnl": -1.08,
                "trigger_tags": ["profit_giveback_after_mfe"],
            },
            "recommended_controls": {"reduce_fraction": 0.5},
        },
    )

    assert should_close is True
    assert reason == "minimum_position_thesis_broken"


def test_untradeable_min_position_reduce_stays_skipped_without_strong_risk_evidence():
    should_close, reason = live_service._should_full_close_untradeable_reduce(
        current_volume=100.0,
        raw_reduce_volume=50.0,
        reduce_volume=0.0,
        min_volume=100.0,
        verdict={
            "summary_reason": "profit_giveback_after_mfe",
            "evidence": {
                "thesis_status": "weakening",
                "giveback_ratio": 0.75,
                "current_pnl": 8.0,
                "trigger_tags": ["profit_giveback_after_mfe"],
            },
            "recommended_controls": {"reduce_fraction": 0.5},
        },
    )

    assert should_close is False
    assert reason == "risk_evidence_not_strong_enough"


def test_untradeable_reduce_does_not_upgrade_above_minimum_position():
    should_close, reason = live_service._should_full_close_untradeable_reduce(
        current_volume=150.0,
        raw_reduce_volume=75.0,
        reduce_volume=0.0,
        min_volume=100.0,
        verdict={
            "evidence": {
                "thesis_status": "broken",
                "giveback_ratio": 1.0,
                "current_pnl": -1.0,
            },
            "recommended_controls": {"reduce_fraction": 0.5},
        },
    )

    assert should_close is False
    assert reason == "not_minimum_position"


def test_start_loop_primes_shared_state_and_scheduler(monkeypatch):
    scheduler_calls = []

    monkeypatch.setattr(live_service, "_start_live_scheduler", lambda: scheduler_calls.append("started"))
    monkeypatch.setattr(live_service.threading, "Thread", _IdleThread)

    result = live_service.start_loop("ctrader", strategy_name="smoke", persist_desired=False)

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
    risk_verdict = SimpleNamespace(
        to_dict=lambda: {
            "allowed": True,
            "reason": "ok",
            "audit_payload": {"action": "open_trade"},
        }
    )

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
        risk_verdict=risk_verdict,
    )

    assert decision_id == "dec_open"
    assert calls["decision"]["event_type"] == "open"
    assert calls["decision"]["risk_state"]["policy_verdict"]["allowed"] is True
    assert calls["decision"]["action_json"]["risk_verdict"]["audit_payload"]["action"] == "open_trade"
    assert [item["event_type"] for item in calls["orders"]] == ["submitted", "filled"]
    assert calls["positions"][0]["event_type"] == "opened"
    assert calls["upserts"][0][0]["entry_decision_id"] == "dec_open"
    protection_plan = calls["upserts"][0][1]["meta"]["entry_protection_plan"]
    assert protection_plan["schema_version"] == live_service._ENTRY_PROTECTION_PLAN_SCHEMA
    assert protection_plan["status"] == "pending"
    assert protection_plan["target_stop_loss"] == 4012.5
    assert protection_plan["target_take_profit"] == 3994.5
    assert calls["attr"][0] == 268


def test_record_amend_failure_after_fill_records_context_status_and_ledger(monkeypatch):
    calls = {"open_context": [], "status": [], "decisions": [], "orders": []}

    class _Ledger:
        def log_composite_decision(self, **kwargs):
            calls["decisions"].append(kwargs)
            return "dec_amend_failed"

        def log_order_event(self, **kwargs):
            calls["orders"].append(kwargs)

    monkeypatch.setattr(live_service, "_LEDGER", _Ledger())
    monkeypatch.setattr(
        live_service,
        "_record_filled_position_open_context",
        lambda **kwargs: calls["open_context"].append(kwargs),
    )
    monkeypatch.setattr(
        live_service,
        "_update_entry_protection_plan_status",
        lambda *args, **kwargs: calls["status"].append((args, kwargs)),
    )
    monkeypatch.setattr(
        live_service,
        "_live_state_get",
        lambda key, *args, **kwargs: {"risk": "state"} if key == "risk" else 0,
    )

    logs: list[str] = []
    composite = SimpleNamespace(direction=1)
    gate = SimpleNamespace(passed=True, reason="passed")

    live_service._record_amend_failure_after_fill(
        attr_engine=SimpleNamespace(),
        bridge=SimpleNamespace(),
        broker="ctrader",
        cfg=SimpleNamespace(timeframe="M5"),
        bar={"time": 123.0},
        tick=9,
        pid=268,
        actual_api_volume=120.0,
        requested_volume=100.0,
        base_requested_volume=90.0,
        fill_price=4008.5,
        current_price=4008.4,
        sl_price=3998.0,
        tp_price=4028.0,
        sl_dist=10.0,
        tp_dist=20.0,
        acct={"balance": 10000, "equity": 10001},
        pos=[],
        composite=composite,
        gate_result=gate,
        risk_verdict=SimpleNamespace(to_dict=lambda: {"allowed": True}),
        market_session={"status": "open"},
        event_sizing_context={"multiplier": 1.0},
        sizing_trace={"source": "test"},
        status_error="bad stops",
        ledger_action_reason="bad stops",
        ledger_comment="bad stops",
        failure_log="tick 9: v4 LONG AMEND FAILED pos=268: bad stops",
        log=logs.append,
    )

    assert logs == ["tick 9: v4 LONG AMEND FAILED pos=268: bad stops"]
    assert calls["open_context"][0]["pid"] == 268
    assert calls["open_context"][0]["market_session"] == {"status": "open"}
    assert calls["status"] == [((268,), {"status": "failed", "error": "bad stops", "attempted": True})]
    assert calls["decisions"][0]["event_type"] == "amend_failed"
    assert calls["decisions"][0]["action_reason"] == "bad stops"
    assert calls["orders"][0]["decision_id"] == "dec_amend_failed"
    assert calls["orders"][0]["event_type"] == "amend_failed"


def test_record_amended_open_success_records_all_contexts(monkeypatch):
    calls = {
        "track": [],
        "status": [],
        "quality": [],
        "attr": [],
        "decisions": [],
        "orders": [],
        "positions": [],
        "upserts": [],
        "decision_logs": [],
    }

    class _Ledger:
        def log_composite_decision(self, **kwargs):
            calls["decisions"].append(kwargs)
            return "dec_open_amended"

        def log_order_event(self, **kwargs):
            calls["orders"].append(kwargs)

        def log_position_event(self, **kwargs):
            calls["positions"].append(kwargs)

    class _Attr:
        def record_open(self, pid, trade_attr):
            calls["attr"].append((pid, trade_attr))

    class _ExecQuality:
        def record(self, trade):
            calls["quality"].append(trade)

    composite = SimpleNamespace(
        direction=1,
        score=0.82,
        tactical_score=0.7,
        macro_score=0.1,
        factor_signals={"rsi": 0.5},
        factor_values={"rsi": 42.0},
        active_weights={"rsi": 1.0},
        tags_breakdown={},
        n_active_factors=1,
        n_abstain_factors=0,
    )
    risk_verdict = SimpleNamespace(to_dict=lambda: {"allowed": True, "reason": "ok"})

    monkeypatch.setattr(live_service, "_LEDGER", _Ledger())
    monkeypatch.setattr(live_service, "_DECISION_LOG", object())
    monkeypatch.setattr(live_service, "_DECISION_LOG_RUN_ID", 88)
    monkeypatch.setattr(live_service, "_exec_quality", _ExecQuality())
    monkeypatch.setattr(live_service, "_track_local_sl_tp", lambda *args, **kwargs: calls["track"].append((args, kwargs)))
    monkeypatch.setattr(
        live_service,
        "_update_entry_protection_plan_status",
        lambda *args, **kwargs: calls["status"].append((args, kwargs)),
    )
    monkeypatch.setattr(
        live_service,
        "_open_learning_context_payload",
        lambda **kwargs: {"learning": "ctx", "sizing_trace": kwargs.get("sizing_trace") or {}},
    )
    monkeypatch.setattr(
        live_service,
        "_live_state_get",
        lambda key, *args, **kwargs: {"risk": "state"} if key == "risk" else 3.5 if key == "session_pnl" else None,
    )
    monkeypatch.setattr(
        live_service,
        "_upsert_recovery_position_state",
        lambda raw, **kwargs: calls["upserts"].append((raw, kwargs)),
    )
    monkeypatch.setattr(
        live_service,
        "_safe_decision_log",
        lambda log_store, **kwargs: calls["decision_logs"].append((log_store, kwargs)),
    )

    logs: list[str] = []
    live_service._record_amended_open_success_context(
        attr_engine=_Attr(),
        bridge=SimpleNamespace(),
        broker="ctrader",
        cfg=SimpleNamespace(timeframe="M5"),
        bar={"time": 1783209600.0},
        tick=10,
        pid=268,
        actual_api_volume=120.0,
        requested_volume=100.0,
        base_requested_volume=90.0,
        fill_price=4008.5,
        current_price=4008.4,
        sl_price=3998.0,
        tp_price=4028.0,
        sl_dist=10.0,
        tp_dist=20.0,
        acct={"balance": 10000, "equity": 10001},
        pos=[],
        composite=composite,
        gate_result=SimpleNamespace(passed=True, reason="passed"),
        risk_verdict=risk_verdict,
        market_session={"status": "open"},
        event_sizing_context={"multiplier": 1.2},
        sizing_trace={"source": "event"},
        entry_protection_plan={"schema_version": "entry_protection_plan.v1", "status": "pending"},
        direction_name="LONG",
        log=logs.append,
    )

    assert calls["track"][0][1] == {"sl": 3998.0, "tp": 4028.0}
    assert calls["status"][0] == ((268,), {"status": "applied", "attempted": True, "applied_sl": 3998.0, "applied_tp": 4028.0})
    assert calls["quality"][0].order_id == 268
    assert calls["attr"][0][0] == 268
    assert "ORDER+AMEND OK" in logs[0]
    assert calls["decisions"][0]["event_type"] == "open"
    assert calls["decisions"][0]["portfolio_state"]["session_pnl"] == 3.5
    assert [item["event_type"] for item in calls["orders"]] == ["submitted", "filled"]
    assert calls["orders"][0]["decision_id"] == "dec_open_amended"
    assert calls["positions"][0]["event_type"] == "opened"
    assert calls["upserts"][0][0]["entry_decision_id"] == "dec_open_amended"
    assert calls["upserts"][0][1]["meta"]["entry_protection_plan"]["status"] == "applied"
    assert calls["upserts"][0][1]["meta"]["entry_protection_plan"]["applied_stop_loss"] == 3998.0
    assert calls["decision_logs"][0][1]["run_id"] == 88
    assert calls["decision_logs"][0][1]["bar_date"] == "2026-07-05"
    assert '"position_id": 268' in calls["decision_logs"][0][1]["meta"]


def test_delegate_timeout_supervisor_close_logs_timeout_trace(monkeypatch):
    traces: list[dict] = []
    monkeypatch.setattr(
        live_service,
        "_build_close_position_risk_context",
        lambda **kwargs: {
            **kwargs,
            "holding_seconds": 7200.0,
            "max_holding_seconds": 3600.0,
        },
    )
    monkeypatch.setattr(
        live_service,
        "_log_supervisor_trace",
        lambda **kwargs: traces.append(kwargs),
    )

    delegated = live_service._delegate_timeout_supervisor_close(
        position={"position_id": 268, "symbol": "XAUUSD+"},
        verdict={"action": "close", "summary_reason": "holding_timeout_exceeded"},
        cfg=SimpleNamespace(),
        tick=11,
        acct={"equity": 10000.0},
    )

    assert delegated is True
    assert traces[0]["stage"] == "timeout_delegated"
    assert traces[0]["execution_status"] == "delegated"
    assert traces[0]["execution_reason"] == "main_timeout_path"
    assert traces[0]["execution"]["timeout_context"]["position_id"] == 268


def test_run_live_loop_tick_body_returns_wait_when_market_closed(monkeypatch):
    diagnostics: list[tuple] = []
    logs: list[str] = []

    monkeypatch.setattr(
        live_service,
        "_market_session_snapshot",
        lambda *_args, **_kwargs: {
            "status": "closed_confirmed",
            "reason": "weekend",
            "high_load_allowed": False,
        },
    )
    monkeypatch.setattr(
        live_service,
        "_get_ctrader",
        lambda: (SimpleNamespace(is_connected=False), None, True),
    )
    monkeypatch.setattr(
        live_service,
        "_set_loop_diagnostic",
        lambda *args, **kwargs: diagnostics.append((args, kwargs)),
    )

    result = live_service._run_live_loop_tick_body(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(risk_require_l2_depth=False, l2_collection_enabled=True),
        timeframe="M5",
        tick=12,
        recovery_bootstrapped=False,
        stop_requested=lambda: False,
        log=logs.append,
    )

    assert result == {"recovery_bootstrapped": False, "wait_seconds": 300.0, "break_loop": False}
    assert diagnostics[0][0][:2] == (12, "market_closed")
    assert "market closed confirmed (weekend)" in logs[0]


def test_closed_decision_bar_frame_drops_current_partial_bar():
    import pandas as pd

    now_ts = 1_783_396_219.0  # 2026-07-07 11:50:19 Asia/Shanghai
    df = pd.DataFrame(
        [
            {"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1},
            {"open": 2.0, "high": 2.1, "low": 1.9, "close": 2.0, "volume": 2},
            {"open": 3.0, "high": 3.1, "low": 2.9, "close": 3.0, "volume": 3},
        ],
        index=pd.to_datetime(
            [
                "2026-07-07T03:40:00Z",
                "2026-07-07T03:45:00Z",
                "2026-07-07T03:50:00Z",
            ]
        ),
    )

    closed = live_service._closed_decision_bar_frame(df, timeframe="M5", now_ts=now_ts)

    assert list(closed.index) == list(pd.to_datetime(["2026-07-07T03:40:00Z", "2026-07-07T03:45:00Z"]))


def test_ensure_live_decision_bars_repairs_from_primary_bridge(monkeypatch):
    import pandas as pd

    now_ts = 1_783_396_219.0  # 2026-07-07 11:50:19 Asia/Shanghai
    stale_df = pd.DataFrame(
        [{"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1}],
        index=pd.to_datetime(["2026-07-07T03:40:00Z"]),
    )
    fetched_df = pd.DataFrame(
        [
            {"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1},
            {"open": 2.0, "high": 2.1, "low": 1.9, "close": 2.0, "volume": 2},
            {"open": 3.0, "high": 3.1, "low": 2.9, "close": 3.0, "volume": 3},
        ],
        index=pd.to_datetime(
            [
                "2026-07-07T03:40:00Z",
                "2026-07-07T03:45:00Z",
                "2026-07-07T03:50:00Z",
            ]
        ),
    )
    inserted = []

    class _Store:
        def insert_bars(self, bars, symbol, timeframe):
            inserted.append((bars, symbol, timeframe))

    class _Bridge:
        is_connected = True

        def fetch_bars(self, timeframe, n_bars):
            return fetched_df

    monkeypatch.setattr(live_service.time, "time", lambda: now_ts)
    monkeypatch.setattr("data.store.DataStore", lambda: _Store())
    monkeypatch.setattr(live_service, "_warmup_from_local_db", lambda *_args, **_kwargs: fetched_df)

    logs: list[str] = []
    repaired = live_service._ensure_live_decision_bars_fresh(
        bridge=_Bridge(),
        symbol="XAUUSD+",
        timeframe="M5",
        df_new=stale_df,
        tick=99,
        log=logs.append,
    )

    assert repaired.index[-1] == pd.Timestamp("2026-07-07T03:45:00Z")
    assert inserted[0][1:] == ("XAUUSD+", "M5")
    assert inserted[0][0][-1]["time"] == 1_783_395_900
    assert all(bar["time"] <= 1_783_395_900 for bar in inserted[0][0])
    snapshot = live_service._live_state_get("decision_bar_freshness", {}, clone=True)
    assert snapshot["fresh"] is True
    assert snapshot["repair_attempted"] is True
    assert snapshot["repair_status"] == "inserted"


def test_ensure_live_decision_bars_does_not_fallback_to_current_partial(monkeypatch):
    import pandas as pd

    now_ts = 1_783_396_219.0  # 2026-07-07 11:50:19 Asia/Shanghai
    partial_only = pd.DataFrame(
        [{"open": 3.0, "high": 3.1, "low": 2.9, "close": 3.0, "volume": 3}],
        index=pd.to_datetime(["2026-07-07T03:50:00Z"]),
    )

    monkeypatch.setattr(live_service.time, "time", lambda: now_ts)

    repaired = live_service._ensure_live_decision_bars_fresh(
        bridge=None,
        symbol="XAUUSD+",
        timeframe="M5",
        df_new=partial_only,
        tick=100,
        log=lambda _msg: None,
    )

    snapshot = live_service._live_state_get("decision_bar_freshness", {}, clone=True)
    assert len(repaired) == 0
    assert snapshot["fresh"] is False
    assert snapshot["latest_bar_ts"] == 0.0
    assert snapshot["repair_attempted"] is False


def test_emergency_close_evaluates_and_remembers_close_verdict(monkeypatch):
    calls = []
    close_calls = []

    class _Policy:
        def evaluate(self, action, context):
            calls.append((action, context))
            return SimpleNamespace(
                allowed=True,
                reason="risk_reducing_action",
                to_dict=lambda: {
                    "allowed": True,
                    "reason": "risk_reducing_action",
                    "audit_payload": {
                        "action": action,
                        "position_id": context["position_id"],
                        "close_reason": context["close_reason"],
                    },
                },
            )

    class _Bridge:
        is_connected = True

        def refresh_positions(self, *, force=False, allow_cache_fallback=True):
            self.refresh_args = {
                "force": force,
                "allow_cache_fallback": allow_cache_fallback,
            }
            return [{"position_id": 268, "symbol": "XAUUSD+", "volume": 100.0}]

        def close_position(self, pid, volume=0.0):
            close_calls.append((pid, volume))
            return SimpleNamespace(success=True, position_id=pid)

    bridge = _Bridge()
    monkeypatch.setattr(live_service, "_RISK_POLICY", _Policy())
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (bridge, None, False))

    result = live_service.emergency_close("ctrader", "XAUUSD+")

    assert result["ok"] is True
    assert result["attempted"] == 1
    assert result["closed"] == 1
    assert result["failed"] == 0
    assert bridge.refresh_args == {"force": True, "allow_cache_fallback": False}
    assert close_calls == [(268, 100.0)]
    assert calls[0][0] == "close_position"
    assert calls[0][1]["close_reason"] == "emergency_close"
    assert live_service._consume_close_reason(268) == "emergency_close"
    verdict = live_service._consume_close_verdict(268, "emergency_close")
    assert verdict["allowed"] is True
    assert verdict["audit_payload"]["action"] == "close_position"


def test_emergency_close_reports_close_failures(monkeypatch):
    class _Policy:
        def evaluate(self, action, context):
            return SimpleNamespace(
                allowed=True,
                reason="risk_reducing_action",
                to_dict=lambda: {"allowed": True, "reason": "risk_reducing_action"},
            )

    class _Bridge:
        is_connected = True

        def refresh_positions(self, *, force=False, allow_cache_fallback=True):
            return [{"position_id": 269, "symbol": "XAUUSD+", "volume": 100.0}]

        def close_position(self, pid, volume=0.0):
            return SimpleNamespace(
                success=False,
                position_id=pid,
                error_code="TRADING_BAD_VOLUME",
                comment="close rejected",
            )

    monkeypatch.setattr(live_service, "_RISK_POLICY", _Policy())
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (_Bridge(), None, False))

    result = live_service.emergency_close("ctrader", "XAUUSD+")

    assert result["ok"] is False
    assert result["attempted"] == 1
    assert result["closed"] == 0
    assert result["failed"] == 1
    assert result["failures"][0]["position_id"] == 269
    assert result["failures"][0]["error_code"] == "TRADING_BAD_VOLUME"


def test_upsert_recovery_position_state_preserves_valid_volume_on_zero_snapshot(monkeypatch, tmp_path):
    from backend.core import db as db_module

    db_path = tmp_path / "state.db"

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    conn = _conn()
    try:
        conn.executescript(db_module.STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    _patch_live_state_conn(monkeypatch, _conn)
    monkeypatch.setattr(live_service, "_lookup_entry_decision_id", lambda position_id: "dec_open")

    live_service._upsert_recovery_position_state(
        {"position_id": 270, "symbol": "XAUUSD+", "direction": 1, "open_price": 4050.0, "volume": 100.0},
        broker="ctrader",
        strategy_name="factor_v4",
        status="open",
    )
    live_service._upsert_recovery_position_state(
        {"position_id": 270, "symbol": "XAUUSD+", "direction": 1, "open_price": 4051.0, "volume": 0.0},
        broker="ctrader",
        strategy_name="factor_v4",
        status="open",
    )

    conn = _conn()
    try:
        row = conn.execute("SELECT volume, open_price FROM recovery_position_state WHERE position_id=270").fetchone()
    finally:
        conn.close()

    assert row["volume"] == pytest.approx(100.0)
    assert row["open_price"] == pytest.approx(4051.0)


def test_pending_close_intent_survives_memory_loss_via_recovery_meta(monkeypatch, tmp_path):
    from backend.core import db as db_module

    db_path = tmp_path / "state.db"

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    conn = _conn()
    try:
        conn.executescript(db_module.STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO recovery_position_state
            (position_id, broker, symbol, direction, open_price, volume,
             first_seen_at, last_seen_at, status, strategy_name,
             entry_decision_id, context_integrity, recovery_meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (271, "ctrader", "XAUUSD+", 1, 4050.0, 100.0, 10.0, 20.0, "open", "factor_v4", "dec_open", "full", "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    _patch_live_state_conn(monkeypatch, _conn)

    live_service._remember_close_reason(271, "holding_timeout")
    live_service._remember_close_verdict(271, SimpleNamespace(to_dict=lambda: {"allowed": True, "reason": "ok"}))
    live_service._pending_close_reasons.clear()
    live_service._pending_close_verdicts.clear()

    assert live_service._consume_close_reason(271) == "holding_timeout"
    assert live_service._consume_close_verdict(271, "holding_timeout")["allowed"] is True


def test_session_risk_state_persists_and_restores(monkeypatch, tmp_path):
    from backend.core import db as db_module

    db_path = tmp_path / "state.db"

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    conn = _conn()
    try:
        conn.executescript(db_module.STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()

    _patch_live_state_conn(monkeypatch, _conn)
    live_service._live_state_update(
        session_pnl=-18.5,
        session_trades=2,
        session_winning=0,
        session_losing=2,
        session_consecutive_loss=2,
        session_max_drawdown_pct=1.85,
        session_start_balance=1000.0,
        session_last_trade_ts=123.0,
        circuit_breaker=True,
        circuit_reason="daily drawdown 5.1%",
        trade_equity_history=[1000.0, 981.5],
    )
    live_service._persist_session_state("2026-06-29")
    live_service._reset_session_state_for_new_day()

    assert live_service._restore_session_state_for_day("2026-06-29") is True
    assert live_service._live_state_get("session_pnl") == pytest.approx(-18.5)
    assert live_service._live_state_get("session_consecutive_loss") == 2
    assert live_service._live_state_get("circuit_breaker") is True
    assert live_service._live_state_get("trade_equity_history", clone=True) == [1000.0, 981.5]


def test_close_pnl_fallback_reads_recovery_when_memory_cache_missing(monkeypatch, tmp_path):
    from backend.core import db as db_module

    db_path = tmp_path / "state.db"

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    conn = _conn()
    try:
        conn.executescript(db_module.STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO recovery_position_state
            (position_id, broker, symbol, direction, open_price, volume,
             first_seen_at, last_seen_at, status, strategy_name,
             entry_decision_id, context_integrity, recovery_meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (272, "ctrader", "XAUUSD+", -1, 4050.0, 100.0, 10.0, 20.0, "open", "factor_v4", "dec_open", "full", "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    _patch_live_state_conn(monkeypatch, _conn)
    live_service._pos_open_prices.pop(272, None)
    live_service._pos_open_api_volume.pop(272, None)

    assert live_service._estimate_close_pnl_from_cached_state(272, 4040.0) == pytest.approx(1000.0)


def test_recovery_bootstrap_reconciles_persisted_positions_after_confirmed_broker_zero(monkeypatch, tmp_path):
    from backend.core import db as db_module
    import execution.deal_sync as deal_sync_module

    db_path = tmp_path / "state.db"

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    conn = _conn()
    try:
        conn.executescript(db_module.STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO recovery_position_state
            (position_id, broker, symbol, direction, open_price, volume,
             first_seen_at, last_seen_at, status, strategy_name,
             entry_decision_id, context_integrity, recovery_meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                301,
                "ctrader",
                "XAUUSD+",
                1,
                4060.0,
                100.0,
                1000.0,
                2000.0,
                "open",
                "factor_v4",
                "dec_open",
                "full",
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    class _Bridge:
        is_connected = True

        def __init__(self):
            self._last_reconcile_at = 0.0
            self.calls = []

        def refresh_positions(self, *, force=False, allow_cache_fallback=True):
            self.calls.append((force, allow_cache_fallback))
            self._last_reconcile_at += 1.0
            return []

    bridge = _Bridge()
    logs = []
    live_service._live_state_update(positions=[{"position_id": 301, "volume": 0.0}], positions_updated_at=time.time())
    _patch_live_state_conn(monkeypatch, _conn)
    monkeypatch.setattr(live_service, "_LEDGER", None)
    monkeypatch.setattr(deal_sync_module, "sync_close_deals_batch", lambda *args, **kwargs: {})

    first = live_service._bootstrap_position_recovery(
        bridge,
        broker="ctrader",
        strategy_name="factor_v4",
        log=logs.append,
    )
    second = live_service._bootstrap_position_recovery(
        bridge,
        broker="ctrader",
        strategy_name="factor_v4",
        log=logs.append,
    )

    conn = _conn()
    try:
        row = conn.execute("SELECT status, close_reason FROM recovery_position_state WHERE position_id=301").fetchone()
    finally:
        conn.close()

    assert first is False
    assert second is True
    assert bridge.calls == [(True, False), (True, False)]
    assert live_service._live_state_get("positions", clone=True) == []
    assert row["status"] == "closed_replayed"
    assert row["close_reason"] == "restart_replay"
    assert any("confirmation 1/2" in item for item in logs)
    assert any("reconciled 1 persisted positions as closed" in item for item in logs)


def test_build_open_trade_risk_context_includes_runtime_health(monkeypatch):
    class _SyncHealth:
        def snapshot(self):
            return {"fresh": False, "stale": True, "degraded": True}

        def last_bar_age_seconds(self, timeframe):
            assert timeframe == "M5"
            return 321.0

    class _Bridge:
        is_connected = False

    class _Component:
        def __init__(self, status):
            self.status = status

    class _SystemHealth:
        def get_last_report(self):
            return SimpleNamespace(
                overall="critical",
                overall_score=0.8,
                components={
                    "l2_depth": _Component("critical"),
                    "disk_space": _Component("degraded"),
                },
            )

    now = time.time()
    live_service._live_state_update(
        loop_running=True,
        account_updated_at=now - 12,
        positions_updated_at=now - 34,
    )

    import data.live_sync.health as sync_health_module

    monkeypatch.setattr(sync_health_module.SyncHealth, "shared", staticmethod(lambda: _SyncHealth()))
    import monitor.system_health as system_health_module

    monkeypatch.setattr(system_health_module, "shared", staticmethod(lambda: _SystemHealth()))

    ctx = live_service._build_open_trade_risk_context(
        cfg=SimpleNamespace(
            timeframe="M5",
            var_enabled=True,
            var_cvar_threshold=0.02,
            risk_loss_cooldown_after_losses=2,
            risk_loss_cooldown_bars=3,
            risk_block_on_disk_critical=True,
            risk_require_l2_depth=False,
            max_position_count=3,
            max_position_api_volume=1000.0,
            pyramid_enabled=True,
        ),
        bridge=_Bridge(),
        acct={"balance": 10000, "equity": 10000},
        positions=[],
        requested_api_volume=100.0,
        signal_score=0.6,
    )

    assert ctx["bridge_connected"] is False
    assert ctx["loop_running"] is True
    assert ctx["data_lag_seconds"] == 321.0
    assert ctx["loss_cooldown_after_losses"] == 2
    assert ctx["loss_cooldown_bars"] == 3
    assert ctx["temporal_context"]["timeframe"] == "M5"
    assert "session_label" in ctx["temporal_context"]
    assert ctx["runtime_health"]["sync_health"]["degraded"] is True
    assert ctx["runtime_health"]["system_health"]["overall"] == "critical"
    assert "l2_depth" in ctx["runtime_health"]["system_health"]["critical_components"]
    assert ctx["runtime_health"]["account_cache_age_seconds"] >= 10.0
    assert ctx["runtime_health"]["positions_cache_age_seconds"] >= 30.0


def test_open_trade_risk_context_separates_market_and_runtime_time(monkeypatch):
    class _Bridge:
        is_connected = True

    market_ts = 1_782_979_200.0
    evaluated_at = market_ts + 900.0
    monkeypatch.setattr(live_service.time, "time", lambda: evaluated_at)
    live_service._live_state_update(
        loop_running=True,
        session_last_trade_ts=evaluated_at - 600.0,
        loop_started_at=evaluated_at - 3600.0,
    )

    ctx = live_service._build_open_trade_risk_context(
        cfg=SimpleNamespace(
            timeframe="M5",
            var_enabled=False,
            risk_loss_cooldown_after_losses=2,
            risk_loss_cooldown_bars=3,
            risk_block_on_disk_critical=True,
            risk_require_l2_depth=False,
            max_position_count=3,
            max_position_api_volume=1000.0,
            pyramid_enabled=True,
        ),
        bridge=_Bridge(),
        acct={"balance": 10000, "equity": 10000},
        positions=[],
        requested_api_volume=100.0,
        signal_score=0.6,
        decision_ts=market_ts,
    )

    temporal = ctx["temporal_context"]
    assert temporal["decision_ts"] == pytest.approx(market_ts)
    assert temporal["evaluated_at"] == pytest.approx(evaluated_at)
    assert temporal["time_basis"] == "market_epoch_seconds_utc"
    assert temporal["runtime_basis"] == "system_epoch_seconds_utc"
    assert temporal["hour_utc"] == 8
    assert temporal["session_label"] == "europe"
    assert temporal["seconds_since_last_trade"] == pytest.approx(600.0)
    assert temporal["bars_since_last_trade"] == pytest.approx(2.0)
    assert temporal["loop_uptime_seconds"] == pytest.approx(3600.0)


def test_recovered_close_repairs_missing_open_ledger(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    _patch_live_state_conn(monkeypatch, _conn)
    monkeypatch.setattr(live_service, "_LEDGER", ledger)

    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO recovery_position_state
            (position_id, broker, symbol, direction, open_price, volume,
             first_seen_at, last_seen_at, status, strategy_name,
             entry_decision_id, context_integrity, recovery_meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                268046003,
                "ctrader",
                "XAUUSD+",
                1,
                4015.92,
                100.0,
                1_782_373_400.0,
                1_782_373_500.0,
                "open",
                "smoke",
                "",
                "partial",
                "{}",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    decision_id = live_service._ensure_open_ledger_for_recovered_close(
        268046003,
        broker="ctrader",
        close_ts=1_782_373_646.154,
        close_price=3980.89,
        real_pnl={"net": 36.52, "entry_price": 4015.92},
        close_reason="broker_close",
    )

    rows = []
    conn = _conn()
    try:
        rows = list(
            conn.execute(
                "SELECT * FROM decision_ledger WHERE position_id='268046003' AND event_type='open'"
            )
        )
        recovery = conn.execute(
            "SELECT entry_decision_id, context_integrity FROM recovery_position_state WHERE position_id=268046003"
        ).fetchone()
    finally:
        conn.close()

    assert decision_id
    assert len(rows) == 1
    assert rows[0]["action_reason"] == "live_close_open_repair"
    assert recovery["entry_decision_id"] == decision_id
    assert recovery["context_integrity"] == "partial"


def test_build_close_position_risk_context_marks_timeout(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    _patch_live_state_conn(monkeypatch, _conn)

    open_ts = time.time() - 3900.0
    ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="268",
        position_id="268",
        decision_ts=open_ts,
        portfolio_state={},
        risk_state={},
        action_score=0.0,
        action_reason="test_open",
        action_json={},
    )

    ctx = live_service._build_close_position_risk_context(
        position_id=268,
        close_reason="holding_timeout",
        cfg=SimpleNamespace(timeframe="M5", risk_max_holding_bars=12),
        decision_ts=open_ts + 3900.0,
    )

    assert ctx["entry_ts_source"] == "decision_ledger"
    assert ctx["holding_seconds"] == pytest.approx(3900.0)
    assert ctx["max_holding_seconds"] == pytest.approx(3600.0)


def test_classify_close_source_infers_supervisor_tighten_stopout(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    _patch_live_state_conn(monkeypatch, _conn)

    close_ts = time.time()
    decision_id = ledger.log_decision(
        event_type="supervisor_tighten",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="7001",
        position_id="7001",
        decision_ts=close_ts - 20.0,
        portfolio_state={},
        risk_state={"policy_verdict": {"allowed": True, "reason": "ok"}},
        action_score=0.8,
        action_reason="profit_giveback_after_mfe",
        action_json={
            "supervisor_verdict": {
                "action": "tighten",
                "summary_reason": "profit_giveback_after_mfe",
                "evidence": {"giveback_ratio": 0.8},
                "recommended_controls": {"target_stop_loss": 4000.0},
            }
        },
    )

    result = live_service._classify_close_source(7001, "broker_close", close_ts)

    assert decision_id
    assert result["close_reason_source"] == "supervisor_tighten_stopout"
    assert result["inferred_close_supervisor"]["event_type"] == "supervisor_tighten"
    assert result["inferred_close_supervisor"]["seconds_before_close"] == pytest.approx(20.0)


def test_classify_close_source_infers_legacy_awe_trailing_stopout_from_trace(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    _patch_live_state_conn(monkeypatch, _conn)

    close_ts = time.time()
    ledger.log_position_supervisor_trace(
        position_id="7101",
        trade_id="7101",
        symbol="XAUUSD+",
        timeframe="M5",
        event_ts=close_ts - 10.0,
        action="tighten",
        summary_reason="legacy_awe_trailing",
        confidence=0.8,
        stage="protection_arbitrated",
        outcome="applied",
        risk_action="tighten_position",
        risk_allowed=True,
        risk_reason="risk_reducing_action",
        execution_status="applied",
        execution_reason="amend_position_sltp_success",
        verdict={
            "action": "tighten",
            "summary_reason": "legacy_awe_trailing",
            "evidence": {"protection_source": "legacy_awe_trailing"},
            "recommended_controls": {"target_stop_loss": 4000.0},
        },
        risk_verdict={"allowed": True, "reason": "risk_reducing_action"},
        execution={"target_stop_loss_sent": 4000.0},
    )

    result = live_service._classify_close_source(7101, "broker_close", close_ts)

    assert result["close_reason_source"] == "legacy_awe_trailing_stopout"
    assert result["inferred_close_supervisor"]["event_type"] == "legacy_awe_trailing"
    assert result["inferred_close_supervisor"]["seconds_before_close"] == pytest.approx(10.0)


def test_holding_summary_for_position_reports_watch_status(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    _patch_live_state_conn(monkeypatch, _conn)

    open_ts = time.time() - 3000.0
    ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="9001",
        position_id="9001",
        decision_ts=open_ts,
        portfolio_state={},
        risk_state={},
        action_score=0.0,
        action_reason="test_open",
        action_json={},
    )

    summary = live_service._holding_summary_for_position(
        {"position_id": 9001, "symbol": "XAUUSD+", "open_time": open_ts},
        cfg=SimpleNamespace(timeframe="M5", risk_max_holding_bars=12),
        now_ts=open_ts + 3000.0,
    )

    assert summary["timeout_enabled"] is True
    assert summary["holding_timeout_status"] == "watch"
    assert summary["holding_timeout_exceeded"] is False
    assert summary["holding_timeout_remaining_seconds"] == pytest.approx(600.0)


def test_position_path_metrics_tracks_mfe_giveback_and_time_in_profit(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    ledger = DecisionLedger(str(db_path))

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    _patch_live_state_conn(monkeypatch, _conn)

    open_ts = time.time() - 1200.0
    ledger.log_decision(
        event_type="open",
        symbol="XAUUSD+",
        timeframe="M5",
        trade_id="9002",
        position_id="9002",
        decision_ts=open_ts,
        portfolio_state={},
        risk_state={},
        action_score=0.0,
        action_reason="test_open",
        action_json={},
    )

    first = live_service._position_path_metrics_for_position(
        {"position_id": 9002, "symbol": "XAUUSD+", "open_time": open_ts, "profit": 80.0},
        cfg=SimpleNamespace(timeframe="M5", risk_max_holding_bars=12),
        now_ts=open_ts + 600.0,
        persist=True,
        broker="ctrader",
        strategy_name="factor_v4",
    )
    second = live_service._position_path_metrics_for_position(
        {"position_id": 9002, "symbol": "XAUUSD+", "open_time": open_ts, "profit": 20.0},
        cfg=SimpleNamespace(timeframe="M5", risk_max_holding_bars=12),
        now_ts=open_ts + 1200.0,
        persist=True,
        broker="ctrader",
        strategy_name="factor_v4",
    )

    assert first["mfe"] == pytest.approx(80.0)
    assert second["mfe"] == pytest.approx(80.0)
    assert second["giveback_ratio"] == pytest.approx(0.75)
    assert second["profit_capture_ratio"] == pytest.approx(0.25)
    assert second["time_in_profit"] == pytest.approx(600.0)
    assert second["thesis_status"] == "weakening"


def test_position_path_metrics_keeps_flat_new_position_intact(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    DecisionLedger(str(db_path))

    def _conn():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    _patch_live_state_conn(monkeypatch, _conn)

    open_ts = time.time() - 120.0
    metrics = live_service._position_path_metrics_for_position(
        {"position_id": 9003, "symbol": "XAUUSD+", "open_time": open_ts, "profit": 0.0},
        cfg=SimpleNamespace(timeframe="M5", risk_max_holding_bars=288),
        now_ts=open_ts + 120.0,
        persist=True,
        broker="ctrader",
        strategy_name="factor_v4",
    )

    assert metrics["mfe"] == 0.0
    assert metrics["mae"] == 0.0
    assert metrics["holding_efficiency"] == 0.0
    assert metrics["thesis_status"] == "intact"


def test_awe_trailing_builds_candidate_without_direct_broker_amend():
    class _Awe:
        def composite_conviction(self):
            return 0.8

    class _Bridge:
        def amend_position_sltp(self, *args, **kwargs):
            raise AssertionError("trailing must not directly amend broker state")

    live_service._trailing_state.clear()
    candidates = live_service._update_trailing_stops(
        _Bridge(),
        [
            {
                "position_id": 701,
                "symbol": "XAUUSD+",
                "direction": 1,
                "entry_price": 4000.0,
                "current_price": 4012.0,
                "sl": 3990.0,
                "tp": 4030.0,
                "volume": 100.0,
            }
        ],
        current_price=4012.0,
        pipeline={"awe": _Awe()},
        atr_price=5.0,
        tick=1,
        log=lambda msg: None,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source == "legacy_awe_trailing"
    assert candidate.action == "tighten"
    assert candidate.risk_action == "tighten_position"
    assert candidate.controls["target_stop_loss"] == pytest.approx(4004.5)


def test_supervisor_tighten_trace_keeps_decision_id(monkeypatch):
    traces = []
    decisions = []
    events = []

    class _Ledger:
        def log_decision(self, **kwargs):
            decisions.append(kwargs)
            return "dec_supervisor_tighten"

        def log_position_supervisor_trace(self, **kwargs):
            traces.append(kwargs)
            return "trace1"

        def log_position_event(self, **kwargs):
            events.append(kwargs)

    class _Policy:
        def evaluate(self, action, context):
            return SimpleNamespace(
                allowed=True,
                reason="risk_reducing_action",
                to_dict=lambda: {"allowed": True, "reason": "risk_reducing_action"},
            )

    class _Bridge:
        is_connected = True

        def get_spot_quote(self):
            return {"bid": 4010.0, "ask": 4010.1, "mid": 4010.05}

        def amend_position_sltp(self, pid, sl=0.0, tp=0.0):
            return SimpleNamespace(success=True, position_id=pid, sl=sl, tp=tp)

    verdict = {
        "position_id": "702",
        "decision_ts": time.time(),
        "action": "tighten",
        "confidence": 0.75,
        "summary_reason": "profit_giveback_after_mfe",
        "evidence": {"giveback_ratio": 0.7},
        "recommended_controls": {
            "target_stop_loss": 4005.0,
            "target_take_profit": 4030.0,
            "close_reason": "supervisor_tighten",
            "protection_mode": "tightened_stop",
        },
        "supervisor_template": {
            "template_id": "position_supervisor:default.v1",
            "template_version": "default.v1",
        },
    }

    monkeypatch.setattr(live_service, "_LEDGER", _Ledger())
    monkeypatch.setattr(live_service, "_RISK_POLICY", _Policy())
    monkeypatch.setattr(live_service, "_evaluate_position_supervisor_for_position", lambda *args, **kwargs: verdict)
    monkeypatch.setattr(live_service, "_supervisor_recently_applied", lambda *args, **kwargs: False)
    monkeypatch.setattr(live_service, "_remember_supervisor_state", lambda *args, **kwargs: None)

    handled = live_service._run_position_supervision(
        _Bridge(),
        [
            {
                "position_id": 702,
                "symbol": "XAUUSD+",
                "direction": 1,
                "entry_price": 4000.0,
                "current_price": 4010.0,
                "sl": 3990.0,
                "tp": 4030.0,
                "volume": 100.0,
            }
        ],
        cfg=SimpleNamespace(timeframe="M5"),
        acct={"balance": 10000.0, "equity": 10000.0},
        tick=3,
        log=lambda msg: None,
    )

    assert handled == {702}
    assert decisions[0]["event_type"] == "supervisor_tighten"
    applied_traces = [item for item in traces if item["outcome"] == "applied"]
    assert applied_traces
    assert applied_traces[0]["decision_id"] == "dec_supervisor_tighten"
    assert events[0]["event_type"] == "tightened"


def test_supervisor_dynamic_tpsl_sends_extended_take_profit(monkeypatch):
    amend_calls = []
    traces = []

    class _Ledger:
        def log_decision(self, **kwargs):
            return "dec_supervisor_dynamic_tpsl"

        def log_position_supervisor_trace(self, **kwargs):
            traces.append(kwargs)
            return "trace_dynamic_tpsl"

        def log_position_event(self, **kwargs):
            pass

    class _Policy:
        def evaluate(self, action, context):
            return SimpleNamespace(
                allowed=True,
                reason="risk_reducing_action",
                to_dict=lambda: {"allowed": True, "reason": "risk_reducing_action"},
            )

    class _Bridge:
        is_connected = True

        def get_spot_quote(self):
            return {"bid": 4028.0, "ask": 4028.1, "mid": 4028.05}

        def amend_position_sltp(self, pid, sl=0.0, tp=0.0):
            amend_calls.append((pid, sl, tp))
            return SimpleNamespace(success=True, position_id=pid, sl=sl, tp=tp)

    verdict = {
        "position_id": "705",
        "decision_ts": time.time(),
        "action": "tighten",
        "confidence": 0.82,
        "summary_reason": "near_take_profit_protect",
        "evidence": {"take_profit_progress": 0.93, "tp_extension_candidate": True},
        "recommended_controls": {
            "target_stop_loss": 4018.0,
            "target_take_profit": 4038.0,
            "close_reason": "supervisor_tighten",
            "protection_mode": "dynamic_tpsl",
        },
        "supervisor_template": {
            "template_id": "position_supervisor:profit_protection.v1",
            "template_version": "profit_protection.v1",
        },
    }

    monkeypatch.setattr(live_service, "_LEDGER", _Ledger())
    monkeypatch.setattr(live_service, "_RISK_POLICY", _Policy())
    monkeypatch.setattr(live_service, "_evaluate_position_supervisor_for_position", lambda *args, **kwargs: verdict)
    monkeypatch.setattr(live_service, "_supervisor_recently_applied", lambda *args, **kwargs: False)
    monkeypatch.setattr(live_service, "_remember_supervisor_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(live_service, "_remember_supervisor_reentry_block", lambda *args, **kwargs: None)

    handled = live_service._run_position_supervision(
        _Bridge(),
        [
            {
                "position_id": 705,
                "symbol": "XAUUSD+",
                "direction": 1,
                "entry_price": 4000.0,
                "current_price": 4028.0,
                "sl": 3990.0,
                "tp": 4030.0,
                "volume": 100.0,
            }
        ],
        cfg=SimpleNamespace(timeframe="M5"),
        acct={"balance": 10000.0, "equity": 10000.0},
        tick=5,
        log=lambda msg: None,
    )

    assert handled == {705}
    assert amend_calls == [(705, 4018.0, 4038.0)]
    applied = [item for item in traces if item["outcome"] == "applied"][0]
    assert applied["execution"]["target_take_profit_sent"] == 4038.0
    assert applied["execution"]["target_take_profit_changed"] is True


def test_protection_cycle_supersedes_trailing_when_supervisor_handles_position(monkeypatch):
    superseded = []
    candidate = live_service.ProtectionCandidate(
        source="legacy_awe_trailing",
        action="tighten",
        priority=50,
        position_id=703,
        risk_action="tighten_position",
        controls={"target_stop_loss": 4005.0},
        reason="legacy_awe_trailing",
        position={"position_id": 703, "symbol": "XAUUSD+", "direction": 1},
    )

    monkeypatch.setattr(live_service, "_update_trailing_stops", lambda *args, **kwargs: [candidate])
    monkeypatch.setattr(live_service, "_enforce_holding_timeout", lambda *args, **kwargs: set())
    monkeypatch.setattr(live_service, "_run_position_supervision", lambda *args, **kwargs: {703})
    monkeypatch.setattr(
        live_service,
        "_log_protection_candidate_superseded",
        lambda item, **kwargs: superseded.append((item.position_id, kwargs["reason"])),
    )
    monkeypatch.setattr(
        live_service,
        "_execute_trailing_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("superseded candidate must not execute")),
    )

    result = live_service._run_position_protection_cycle(
        SimpleNamespace(is_connected=True),
        [{"position_id": 703}],
        cfg=SimpleNamespace(timeframe="M5"),
        acct={},
        pipeline={},
        current_price=4010.0,
        atr_price=5.0,
        tick=4,
        log=lambda msg: None,
    )

    assert result["supervisor"] == [703]
    assert result["trailing_superseded"] == [703]
    assert superseded == [(703, "position_supervisor")]


def test_legacy_awe_trailing_records_protection_state_not_supervisor_cooldown(monkeypatch):
    traces = []
    decisions = []
    protection_states = []

    class _Ledger:
        def log_decision(self, **kwargs):
            decisions.append(kwargs)
            return "dec_legacy_awe"

        def log_position_supervisor_trace(self, **kwargs):
            traces.append(kwargs)
            return "trace_legacy_awe"

        def log_position_event(self, **kwargs):
            pass

    class _Policy:
        def evaluate(self, action, context):
            return SimpleNamespace(
                allowed=True,
                reason="risk_reducing_action",
                to_dict=lambda: {"allowed": True, "reason": "risk_reducing_action"},
            )

    class _Bridge:
        is_connected = True

        def get_spot_quote(self):
            return {"bid": 4010.0, "ask": 4010.1, "mid": 4010.05}

        def amend_position_sltp(self, pid, sl=0.0, tp=0.0):
            return SimpleNamespace(success=True, position_id=pid, sl=sl, tp=tp)

    candidate = live_service.ProtectionCandidate(
        source="legacy_awe_trailing",
        action="tighten",
        priority=50,
        position_id=704,
        risk_action="tighten_position",
        controls={"target_stop_loss": 4005.0, "target_take_profit": 4030.0},
        evidence={"confidence": 0.4},
        reason="legacy_awe_trailing",
        position={
            "position_id": 704,
            "symbol": "XAUUSD+",
            "direction": 1,
            "entry_price": 4000.0,
            "current_price": 4010.0,
            "sl": 3990.0,
            "tp": 4030.0,
            "volume": 100.0,
        },
    )

    monkeypatch.setattr(live_service, "_LEDGER", _Ledger())
    monkeypatch.setattr(live_service, "_RISK_POLICY", _Policy())
    monkeypatch.setattr(
        live_service,
        "_remember_supervisor_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy AWE must not update supervisor cooldown")),
    )
    monkeypatch.setattr(
        live_service,
        "_remember_protection_state",
        lambda *args, **kwargs: protection_states.append(kwargs),
    )

    handled = live_service._execute_trailing_candidate(
        candidate,
        bridge=_Bridge(),
        cfg=SimpleNamespace(timeframe="M5"),
        tick=5,
        log=lambda msg: None,
        acct={},
    )

    assert handled is True
    assert decisions[0]["event_type"] == "legacy_awe_trailing"
    assert protection_states[0]["source"] == "legacy_awe_trailing"
    assert protection_states[0]["action_applied"] == "tighten"
    assert traces[0]["decision_id"] == "dec_legacy_awe"


def test_trailing_candidate_risk_rejected_logs_trace_without_amend(monkeypatch):
    traces: list[dict] = []
    events: list[dict] = []

    class _Policy:
        def evaluate(self, action, context):
            return SimpleNamespace(to_dict=lambda: {"allowed": False, "reason": "blocked"})

    class _Bridge:
        is_connected = True

        def amend_position_sltp(self, *args, **kwargs):
            raise AssertionError("risk rejected candidate must not amend")

    candidate = live_service.ProtectionCandidate(
        source="legacy_awe_trailing",
        action="tighten",
        priority=50,
        position_id=704,
        risk_action="tighten_position",
        controls={"target_stop_loss": 4005.0, "target_take_profit": 4030.0},
        evidence={"confidence": 0.4},
        reason="legacy_awe_trailing",
        position={"position_id": 704, "symbol": "XAUUSD+", "direction": 1, "current_price": 4010.0},
    )

    monkeypatch.setattr(live_service, "_RISK_POLICY", _Policy())
    monkeypatch.setattr(live_service, "_log_supervisor_decision", lambda **kwargs: "dec_blocked")
    monkeypatch.setattr(live_service, "_log_supervisor_trace", lambda **kwargs: traces.append(kwargs))
    monkeypatch.setattr(live_service, "_log_supervisor_position_event", lambda **kwargs: events.append(kwargs))

    handled = live_service._execute_trailing_candidate(
        candidate,
        bridge=_Bridge(),
        cfg=SimpleNamespace(timeframe="M5"),
        tick=6,
        log=lambda msg: None,
        acct={},
    )

    assert handled is True
    assert traces[0]["stage"] == "risk_rejected"
    assert traces[0]["execution_status"] == "blocked"
    assert events == []


def test_trailing_candidate_amend_failed_logs_event_and_trace(monkeypatch):
    traces: list[dict] = []
    events: list[dict] = []
    logs: list[str] = []

    class _Policy:
        def evaluate(self, action, context):
            return SimpleNamespace(to_dict=lambda: {"allowed": True, "reason": "ok"})

    class _Bridge:
        is_connected = True

        def get_spot_quote(self):
            return {"bid": 4010.0, "ask": 4010.1, "mid": 4010.05}

        def amend_position_sltp(self, pid, sl=0.0, tp=0.0):
            return SimpleNamespace(success=False, comment="bad_stops")

    candidate = live_service.ProtectionCandidate(
        source="legacy_awe_trailing",
        action="tighten",
        priority=50,
        position_id=704,
        risk_action="tighten_position",
        controls={"target_stop_loss": 4005.0, "target_take_profit": 4030.0},
        evidence={"confidence": 0.4},
        reason="legacy_awe_trailing",
        position={
            "position_id": 704,
            "symbol": "XAUUSD+",
            "direction": 1,
            "current_price": 4010.0,
            "sl": 3990.0,
            "tp": 4030.0,
        },
    )

    monkeypatch.setattr(live_service, "_RISK_POLICY", _Policy())
    monkeypatch.setattr(live_service, "_log_supervisor_decision", lambda **kwargs: "dec_failed")
    monkeypatch.setattr(live_service, "_log_supervisor_trace", lambda **kwargs: traces.append(kwargs))
    monkeypatch.setattr(live_service, "_log_supervisor_position_event", lambda **kwargs: events.append(kwargs))

    handled = live_service._execute_trailing_candidate(
        candidate,
        bridge=_Bridge(),
        cfg=SimpleNamespace(timeframe="M5"),
        tick=7,
        log=logs.append,
        acct={},
    )

    assert handled is True
    assert events[0]["event_type"] == "amend_failed"
    assert traces[0]["stage"] == "execution_failed"
    assert traces[0]["execution_reason"] == "bad_stops"
    assert "AMEND FAILED" in logs[0]
