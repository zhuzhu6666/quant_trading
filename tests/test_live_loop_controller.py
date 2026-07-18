import threading

import pytest

from backend.services.live_loop_controller import LiveLoopController, STARTUP_BARRIER_STEPS


class _Thread:
    def __init__(self, alive: bool = True):
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


def test_draining_generation_retains_ownership_and_rejects_start():
    controller = LiveLoopController()
    generation = controller.begin_start(broker="ctrader", strategy_name="factor_v4")
    controller.bind_thread(generation.generation_id, _Thread(alive=True))  # type: ignore[arg-type]

    controller.request_stop(generation.generation_id)

    assert controller.status()["phase"] == "draining"
    assert generation.stop_event.is_set()
    with pytest.raises(RuntimeError, match="live_loop_generation_busy:draining"):
        controller.begin_start(broker="ctrader", strategy_name="replacement")


def test_new_generation_allowed_only_after_old_thread_exits():
    controller = LiveLoopController()
    generation = controller.begin_start(broker="ctrader", strategy_name="factor_v4")
    thread = _Thread(alive=True)
    controller.bind_thread(generation.generation_id, thread)  # type: ignore[arg-type]
    controller.request_stop()
    controller.acknowledge_exit(generation.generation_id)

    with pytest.raises(RuntimeError, match="live_loop_generation_busy"):
        controller.begin_start(broker="ctrader", strategy_name="too_early")

    thread.alive = False
    replacement = controller.begin_start(broker="ctrader", strategy_name="replacement")
    assert replacement.generation_id != generation.generation_id


def test_startup_barrier_opens_new_risk_only_after_every_step():
    controller = LiveLoopController()
    generation = controller.begin_start(broker="ctrader", strategy_name="factor_v4")

    for step in STARTUP_BARRIER_STEPS[:-1]:
        assert controller.complete_barrier_step(generation.generation_id, step) is False
        assert controller.status()["accepting_new_risk"] is False

    assert controller.complete_barrier_step(generation.generation_id, STARTUP_BARRIER_STEPS[-1]) is True
    status = controller.status()
    assert status["phase"] == "running"
    assert status["ready"] is True
    assert status["accepting_new_risk"] is True


def test_generation_scopes_components_and_heartbeats():
    now = [100.0]
    controller = LiveLoopController(clock=lambda: now[0])
    generation = controller.begin_start(broker="ctrader", strategy_name="factor_v4")
    controller.bind_component(generation.generation_id, "scheduler")
    controller.bind_component(generation.generation_id, "factor_pipeline")
    now[0] = 105.0
    controller.heartbeat(generation.generation_id, "safety")
    now[0] = 106.0
    controller.heartbeat(generation.generation_id, "alpha")

    status = controller.status()
    assert status["components"] == {
        "scheduler": generation.generation_id,
        "factor_pipeline": generation.generation_id,
    }
    assert status["safety_heartbeat_at"] == 105.0
    assert status["alpha_heartbeat_at"] == 106.0


def test_wrong_generation_cannot_mutate_current_owner():
    controller = LiveLoopController()
    controller.begin_start(broker="ctrader", strategy_name="factor_v4")

    with pytest.raises(RuntimeError, match="generation_ownership_mismatch"):
        controller.request_stop("not-the-owner")
