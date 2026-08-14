from types import SimpleNamespace

from backend.services.live_loop_runner import (
    SerialLiveTickRuntime,
    run_serial_live_ticks,
)


class _StopFlag:
    def __init__(self, wait_results=()):
        self.wait_results = list(wait_results)
        self.wait_calls = []

    def is_set(self):
        return False

    def wait(self, seconds):
        self.wait_calls.append(seconds)
        return self.wait_results.pop(0) if self.wait_results else False


def _runtime(
    *,
    run_tick,
    pipeline=None,
    diagnostics=None,
    state_updates=None,
    acknowledgements=None,
):
    diagnostics = diagnostics if diagnostics is not None else []
    state_updates = state_updates if state_updates is not None else []
    acknowledgements = (
        acknowledgements if acknowledgements is not None else []
    )
    return SerialLiveTickRuntime(
        set_loop_diagnostic=lambda *args: diagnostics.append(args),
        run_tick_body=run_tick,
        factor_pipeline=lambda: pipeline,
        acknowledge_factor_projections=lambda **kwargs: acknowledgements.append(
            kwargs
        )
        or {"acknowledged": True},
        live_state_update=lambda **kwargs: state_updates.append(kwargs),
    )


def test_tick_requested_break_preserves_recovery_and_projection_ack():
    pipeline = {"engine": SimpleNamespace(is_warm=True)}
    acknowledgements = []
    diagnostics = []
    stop_flag = _StopFlag()

    result = run_serial_live_ticks(
        broker="ctrader",
        stop_flag=stop_flag,
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        generation_id="generation-1",
        log=lambda _message: None,
        runtime=_runtime(
            run_tick=lambda **_kwargs: {
                "recovery_bootstrapped": True,
                "break_loop": True,
                "wait_seconds": None,
            },
            pipeline=pipeline,
            diagnostics=diagnostics,
            acknowledgements=acknowledgements,
        ),
    )

    assert result == {
        "tick_count": 1,
        "recovery_bootstrapped": True,
        "exit_reason": "tick_requested_break",
    }
    assert diagnostics == [(1, "checking"), (1, None)]
    assert acknowledgements[0]["generation_id"] == "generation-1"
    assert pipeline["factor_projection_ack"] == {"acknowledged": True}


def test_tick_exception_blocks_risk_and_retries_safety_in_five_seconds():
    state_updates = []
    logs = []
    diagnostics = []
    stop_flag = _StopFlag(wait_results=[True])

    def unavailable(**_kwargs):
        raise RuntimeError("alpha failed")

    result = run_serial_live_ticks(
        broker="ctrader",
        stop_flag=stop_flag,
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        generation_id="generation-2",
        log=logs.append,
            runtime=_runtime(
                run_tick=unavailable,
                state_updates=state_updates,
                diagnostics=diagnostics,
            ),
    )

    assert state_updates == [{"accepting_new_risk": False}]
    assert diagnostics == [(1, "checking"), (1, "error")]
    assert len(stop_flag.wait_calls) == 1
    assert 0.0 < stop_flag.wait_calls[0] <= 5.0
    assert result["exit_reason"] == "stop_during_safety_retry"
    assert any("alpha failed" in message for message in logs)


def test_normal_tick_waits_sixty_seconds_after_tick():
    diagnostics = []
    stop_flag = _StopFlag(wait_results=[True])

    result = run_serial_live_ticks(
        broker="ctrader",
        stop_flag=stop_flag,
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        generation_id="generation-3",
        log=lambda _message: None,
        runtime=_runtime(
            run_tick=lambda **_kwargs: {
                "recovery_bootstrapped": False,
                "break_loop": False,
                "wait_seconds": None,
            },
            diagnostics=diagnostics,
        ),
    )

    assert diagnostics == [(1, "checking"), (1, None)]
    assert stop_flag.wait_calls == [60.0]
    assert result["exit_reason"] == "stop_during_alpha_wait"


def test_tick_specific_wait_remains_owned_by_serial_runner():
    diagnostics = []
    stop_flag = _StopFlag(wait_results=[True])

    result = run_serial_live_ticks(
        broker="ctrader",
        stop_flag=stop_flag,
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        generation_id="generation-4",
        log=lambda _message: None,
        runtime=_runtime(
            run_tick=lambda **_kwargs: {
                "recovery_bootstrapped": False,
                "break_loop": False,
                "wait_seconds": 10.0,
            },
            diagnostics=diagnostics,
        ),
    )

    assert stop_flag.wait_calls == [10.0]
    assert diagnostics == [(1, "checking"), (1, None)]
    assert result["exit_reason"] == "stop_during_tick_wait"


def test_safety_wait_is_scheduled_from_tick_start_not_added_after_work():
    clock = iter([100.0, 107.5])
    stop_flag = _StopFlag(wait_results=[True])

    result = run_serial_live_ticks(
        broker="ctrader",
        stop_flag=stop_flag,
        bridge_cfg=SimpleNamespace(),
        timeframe="M5",
        generation_id="generation-5",
        log=lambda _message: None,
        runtime=SerialLiveTickRuntime(
            set_loop_diagnostic=lambda *_args: None,
            run_tick_body=lambda **_kwargs: {
                "recovery_bootstrapped": False,
                "break_loop": False,
                "wait_seconds": 5.0,
            },
            factor_pipeline=lambda: None,
            acknowledge_factor_projections=lambda **_kwargs: None,
            live_state_update=lambda **_kwargs: None,
            monotonic=lambda: next(clock),
        ),
    )

    assert stop_flag.wait_calls == [0.0]
    assert result["exit_reason"] == "stop_during_tick_wait"
