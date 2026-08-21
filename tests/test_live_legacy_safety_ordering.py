from __future__ import annotations

import time
from types import SimpleNamespace

from backend.services import live_service
from backend.services.live_loop_controller import LiveLoopController


def test_off_mode_executes_governed_supervisor_cycle_once_per_due_cycle(monkeypatch):
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

    assert first["supervisor_executor_authoritative"] is True
    assert first["protection"]["status"] == "completed"
    assert second["protection"]["status"] == "not_due"
    assert executions == [901]


def test_running_loop_enables_safety_watchdog(monkeypatch):
    monkeypatch.setattr(live_service, "_LIVE_LOOP_CONTROLLER", LiveLoopController())
    generation = live_service._LIVE_LOOP_CONTROLLER.begin_start(
        broker="ctrader",
        strategy_name="factor_v4",
    )
    live_service._LIVE_LOOP_CONTROLLER.bind_thread(
        generation.generation_id,
        SimpleNamespace(is_alive=lambda: True, ident=902),
    )
    live_service._live_state_update(loop_running=True)

    snapshot = live_service._live_safety_watchdog_probe()

    assert snapshot["enabled"] is True
    assert snapshot["running"] is True
