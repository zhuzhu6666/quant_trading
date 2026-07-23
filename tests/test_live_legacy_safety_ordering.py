from __future__ import annotations

import time
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.services import live_service
from backend.services.live_safety_state import (
    no_new_risk_latch_status,
    reset_safety_state_for_tests,
)


@pytest.fixture(autouse=True)
def _default_off_safety_state(monkeypatch, tmp_path):
    reset_safety_state_for_tests()
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path / "safety"))
    monkeypatch.setattr(
        live_service,
        "_phase2_feature_flags",
        lambda: SimpleNamespace(
            live_safety_plane_v2_mode="off",
            live_generation_controller_v2_enabled=False,
            ctrader_execution_outcome_v2_enabled=False,
        ),
    )
    monkeypatch.setattr(live_service, "_live_safety_plane", None)
    monkeypatch.setattr(live_service, "_live_safety_plane_owner", "")
    live_service._live_state_update(
        loop_running=True,
        accepting_new_risk=True,
        circuit_breaker=False,
        positions=[],
        positions_reconciled=[],
    )
    yield
    reset_safety_state_for_tests()


def _install_legacy_tick_boundary(
    monkeypatch,
    order: list[str],
    *,
    bars,
    session_available: bool = True,
    circuit_breaker: bool = False,
):
    now_ts = time.time()
    today = live_service.datetime.now(live_service.timezone.utc).strftime("%Y-%m-%d")
    bridge = SimpleNamespace(is_connected=True)
    reconcile = SimpleNamespace(
        status="fresh",
        reconcile_id="legacy-positions-r1",
        observed_at=now_ts,
        positions=({"position_id": 701, "current_price": 4000.0},),
    )
    live_service._live_state_update(
        trade_date=today if session_available else "",
        session_state_status="available" if session_available else "unavailable",
        circuit_breaker=circuit_breaker,
    )
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (bridge, None, False))
    monkeypatch.setattr(
        live_service,
        "_explicit_position_reconcile",
        lambda _bridge: order.append("broker_snapshot") or reconcile,
    )
    def _safety(**_kwargs):
        order.append("legacy_protection")
        live_service._live_state_update(
            account_reconciled={"ok": True, "balance": 1000.0, "equity": 1000.0},
            account_updated_at=now_ts,
            account_reconcile_id="legacy-account-r1",
            account_reconcile_failed_at=None,
            positions_reconciled=[{"position_id": 701}],
            positions_updated_at=now_ts,
            positions_reconcile_id="legacy-positions-r1",
            positions_reconcile_failed_at=None,
        )
        return {
            "position_ids": [701],
            "unknown_execution_count": 0,
            "reconciliation_state": "fresh",
            "blockers": [],
            "accepting_new_risk": True,
        }

    monkeypatch.setattr(live_service, "_run_live_safety_cycle", _safety)
    monkeypatch.setattr(
        live_service,
        "_market_session_snapshot",
        lambda *_args, **_kwargs: order.append("market_session")
        or {
            "status": "open_confirmed",
            "can_open_positions": True,
            "now_ts": now_ts,
        },
    )
    monkeypatch.setattr(live_service, "_set_loop_diagnostic", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(live_service, "_ensure_spot_subscription", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        live_service,
        "kickoff_account_refresh",
        lambda *_args, **_kwargs: order.append("account_refresh"),
    )
    monkeypatch.setattr(
        live_service,
        "_bootstrap_position_recovery",
        lambda *_args, **_kwargs: order.append("deal_recovery") or True,
    )
    monkeypatch.setattr(
        live_service,
        "_evaluate_daily_drawdown",
        lambda: {"tripped": False, "dd_pct": 0.0},
    )
    monkeypatch.setattr(
        live_service,
        "_warmup_from_local_db",
        lambda *_args, **_kwargs: order.append("bars") or bars,
    )
    return bridge, today


def test_default_off_runs_snapshot_and_protection_before_missing_bars(monkeypatch):
    order: list[str] = []
    _install_legacy_tick_boundary(monkeypatch, order, bars=None)

    result = live_service._run_live_loop_tick_body_legacy(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=31,
        recovery_bootstrapped=True,
        stop_requested=lambda: False,
        log=lambda _message: None,
    )

    assert order == [
        "broker_snapshot",
        "legacy_protection",
        "market_session",
        "account_refresh",
        "bars",
    ]
    assert result["wait_seconds"] == 5.0
    assert live_service._live_state_get("accepting_new_risk") is False


def test_default_off_pg_restore_failure_happens_after_safety_and_blocks_alpha(monkeypatch):
    order: list[str] = []
    _bridge, _today = _install_legacy_tick_boundary(
        monkeypatch,
        order,
        bars=None,
        session_available=False,
    )
    monkeypatch.setattr(
        live_service,
        "_retry_legacy_session_restore",
        lambda **_kwargs: order.append("session_pg") or False,
    )

    result = live_service._run_live_loop_tick_body_legacy(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=32,
        recovery_bootstrapped=False,
        stop_requested=lambda: False,
        log=lambda _message: None,
    )

    assert order == [
        "broker_snapshot",
        "legacy_protection",
        "market_session",
        "account_refresh",
        "session_pg",
    ]
    assert result["wait_seconds"] == 5.0
    assert live_service._live_state_get("accepting_new_risk") is False


def test_default_off_first_tick_rebuilds_session_after_safety_even_if_cache_is_available(
    monkeypatch,
):
    order: list[str] = []
    _bridge, today = _install_legacy_tick_boundary(
        monkeypatch,
        order,
        bars=None,
        session_available=True,
    )

    def _restore(**_kwargs):
        order.append("session_pg")
        live_service._live_state_update(
            trade_date=today,
            session_state_status="available",
        )
        return True

    monkeypatch.setattr(live_service, "_retry_legacy_session_restore", _restore)

    live_service._run_live_loop_tick_body_legacy(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=321,
        recovery_bootstrapped=False,
        stop_requested=lambda: False,
        log=lambda _message: None,
    )

    assert order == [
        "broker_snapshot",
        "legacy_protection",
        "market_session",
        "account_refresh",
        "session_pg",
        "bars",
    ]
    assert "deal_recovery" not in order


def test_default_off_demo_circuit_observation_does_not_skip_bars(monkeypatch):
    order: list[str] = []
    _install_legacy_tick_boundary(
        monkeypatch,
        order,
        bars=None,
        circuit_breaker=True,
    )

    result = live_service._run_live_loop_tick_body_legacy(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=33,
        recovery_bootstrapped=True,
        stop_requested=lambda: False,
        log=lambda _message: None,
    )

    assert order == [
        "broker_snapshot",
        "legacy_protection",
        "market_session",
        "account_refresh",
        "bars",
    ]
    assert result["wait_seconds"] == 5.0
    assert live_service._live_state_get("accepting_new_risk") is False


def test_default_off_marks_tick_protection_already_run_to_avoid_duplicate_mutation(
    monkeypatch,
):
    order: list[str] = []
    frame = pd.DataFrame(
        [{"open": 3999.0, "high": 4001.0, "low": 3998.0, "close": 4000.0}],
        index=pd.to_datetime(["2026-07-19T00:00:00Z"]),
    )
    bridge, _today = _install_legacy_tick_boundary(monkeypatch, order, bars=frame)
    bridge.get_spot_quote = lambda: {
        "bid": 3999.9,
        "ask": 4000.1,
        "mid": 4000.0,
        "ts": time.time(),
    }
    monkeypatch.setattr(
        live_service,
        "_ensure_live_decision_bars_fresh",
        lambda **_kwargs: frame,
    )
    monkeypatch.setattr(
        live_service,
        "_loop_compare_spot_quote_to_latest_bar",
        lambda **_kwargs: {"too_far": False},
    )
    process_calls: list[dict] = []
    monkeypatch.setattr(
        live_service,
        "_process_tick",
        lambda *_args, **kwargs: process_calls.append(kwargs),
    )

    live_service._run_live_loop_tick_body_legacy(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=34,
        recovery_bootstrapped=True,
        stop_requested=lambda: False,
        log=lambda _message: None,
    )

    assert order.count("legacy_protection") == 1
    assert len(process_calls) == 1
    assert process_calls[0]["protection_already_run"] is True
    assert live_service._live_state_get("accepting_new_risk") is True


def test_default_off_refreshes_stale_reconciles_before_alpha(
    monkeypatch,
):
    order: list[str] = []
    logs: list[str] = []
    frame = pd.DataFrame(
        [{"open": 3999.0, "high": 4001.0, "low": 3998.0, "close": 4000.0}],
        index=pd.to_datetime(["2026-07-19T00:00:00Z"]),
    )
    bridge, _today = _install_legacy_tick_boundary(monkeypatch, order, bars=frame)
    bridge.get_spot_quote = lambda: {
        "bid": 3999.9,
        "ask": 4000.1,
        "mid": 4000.0,
        "ts": time.time(),
    }

    def _bars_fresh(**_kwargs):
        stale_at = time.time() - 16.0
        live_service._live_state_update(
            account_updated_at=stale_at,
            positions_updated_at=stale_at,
        )
        return frame

    def _refresh(_bridge, _broker, **_kwargs):
        order.append("final_reconcile_refresh")
        fresh_at = time.time()
        live_service._live_state_update(
            account_updated_at=fresh_at,
            positions_updated_at=fresh_at,
        )
        return True

    monkeypatch.setattr(
        live_service,
        "_ensure_live_decision_bars_fresh",
        _bars_fresh,
    )
    monkeypatch.setattr(
        live_service,
        "_refresh_account_positions_sync",
        _refresh,
    )
    monkeypatch.setattr(
        live_service,
        "_loop_compare_spot_quote_to_latest_bar",
        lambda **_kwargs: {"too_far": False},
    )
    process_calls: list[dict] = []
    monkeypatch.setattr(
        live_service,
        "_process_tick",
        lambda *_args, **kwargs: process_calls.append(kwargs),
    )

    live_service._run_live_loop_tick_body_legacy(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=341,
        recovery_bootstrapped=True,
        stop_requested=lambda: False,
        log=logs.append,
    )

    assert "final_reconcile_refresh" in order
    assert any("final open reconcile refresh requested" in item for item in logs)
    assert len(process_calls) == 1
    assert live_service._live_state_get("accepting_new_risk") is True


def test_default_off_safety_exception_durably_blocks_before_pg_or_bars(monkeypatch):
    order: list[str] = []
    bridge = SimpleNamespace(is_connected=True)
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (bridge, None, False))
    monkeypatch.setattr(
        live_service,
        "_explicit_position_reconcile",
        lambda _bridge: order.append("broker_snapshot") or SimpleNamespace(),
    )
    monkeypatch.setattr(
        live_service,
        "_run_live_safety_cycle",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("protection exploded")),
    )
    monkeypatch.setattr(
        live_service,
        "_retry_legacy_session_restore",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("PG must not run")),
    )
    monkeypatch.setattr(
        live_service,
        "_warmup_from_local_db",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("bars must not run")),
    )

    result = live_service._run_live_loop_tick_body_legacy(
        broker="ctrader",
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        tick=35,
        recovery_bootstrapped=False,
        stop_requested=lambda: False,
        log=lambda _message: None,
    )

    assert order == ["broker_snapshot"]
    assert result["wait_seconds"] == 5.0
    assert no_new_risk_latch_status()["active"] is True
    assert live_service._live_state_get("accepting_new_risk") is False


def test_off_mode_executes_legacy_authority_once_per_due_cycle(monkeypatch):
    position = {"position_id": 901, "current_price": 4000.0}
    reconcile = SimpleNamespace(
        state="fresh",
        status="fresh",
        reconcile_id="off-r1",
        observed_at=time.time(),
        positions=(position,),
    )

    class _Bridge:
        is_connected = True

        def unresolved_execution_intent_count(self):
            return 0

    executions: list[int] = []
    monkeypatch.setattr(
        live_service,
        "_publish_fresh_position_reconcile",
        lambda _result, **_kwargs: [position],
    )
    monkeypatch.setattr(live_service, "_safety_reference_price", lambda *_args: 4000.0)
    monkeypatch.setattr(
        live_service,
        "_run_position_protection_cycle",
        lambda *_args, **_kwargs: executions.append(901)
        or {"safety_candidates": [], "safety_arbitration": []},
    )
    from config import runtime_config

    monkeypatch.setattr(runtime_config, "shared", lambda: SimpleNamespace())

    first = live_service._run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=36,
        log=lambda _message: None,
        reconcile_result=reconcile,
    )
    second = live_service._run_live_safety_cycle(
        bridge=_Bridge(),
        broker="ctrader",
        tick=37,
        log=lambda _message: None,
        reconcile_result=reconcile,
    )

    assert first["legacy_authoritative"] is True
    assert first["protection"]["status"] == "completed"
    assert second["protection"]["status"] == "not_due"
    assert executions == [901]


def test_default_off_running_loop_still_enables_safety_watchdog(monkeypatch):
    monkeypatch.setattr(
        live_service,
        "_loop_thread",
        SimpleNamespace(is_alive=lambda: True),
    )
    live_service._live_state_update(loop_running=True)

    snapshot = live_service._live_safety_watchdog_probe()

    assert live_service._phase2_v2_active() is False
    assert snapshot["enabled"] is True
    assert snapshot["running"] is True
