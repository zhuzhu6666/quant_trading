from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from backend.services.live_position_lifecycle import (
    build_position_protection_cycle_result,
)
from backend.services.live_position_protection_cycle import (
    PositionProtectionCycleRuntime,
    run_position_protection_cycle,
)
from backend.services.live_safety_plane import SafetyCandidate
from backend.services.live_safety_planner import protection_candidate_to_safety


@dataclass
class _LegacyCandidate:
    source: str
    action: str
    priority: int
    position_id: int
    risk_action: str
    controls: dict
    reason: str


def test_protection_cycle_preserves_priority_and_single_position_authority():
    entry_candidates = [
        _LegacyCandidate("entry", "tighten", 20, 1, "tighten_position", {}, "repair"),
        _LegacyCandidate("entry", "tighten", 20, 2, "tighten_position", {}, "repair"),
    ]
    trailing_candidates = [
        _LegacyCandidate("trail", "tighten", 50, 2, "tighten_position", {}, "trail"),
        _LegacyCandidate("trail", "tighten", 50, 4, "tighten_position", {}, "trail"),
    ]
    executed: list[tuple[str, int]] = []
    superseded: list[tuple[int, str]] = []

    def timeout(*_args, candidate_recorder, **_kwargs):
        candidate_recorder(
            SafetyCandidate(
                action="close",
                position_id=1,
                reason="timeout",
                fingerprint="timeout-1",
            )
        )
        return {1}

    def supervisor(*_args, candidate_recorder, skip_position_ids, **_kwargs):
        assert skip_position_ids == {1, 2}
        candidate_recorder(
            SafetyCandidate(
                action="reduce",
                position_id=3,
                reason="supervisor",
                fingerprint="supervisor-3",
            )
        )
        return {3}

    runtime = PositionProtectionCycleRuntime(
        update_trailing_stops=lambda *_args, **_kwargs: trailing_candidates,
        enforce_holding_timeout=timeout,
        entry_protection_repair_candidates=lambda *_args, **_kwargs: entry_candidates,
        log_candidate_superseded=lambda item, **kwargs: superseded.append(
            (item.position_id, kwargs["reason"])
        ),
        execute_candidate=lambda item, **_kwargs: executed.append(
            (item.source, item.position_id)
        )
        or True,
        run_position_supervision=supervisor,
        protection_candidate_to_safety=protection_candidate_to_safety,
        candidate_supersede_reason=lambda *, position_id, protected_position_ids, **_kwargs: (
            "higher_priority_protection"
            if position_id in protected_position_ids
            else ""
        ),
        build_cycle_result=build_position_protection_cycle_result,
        record_aux_failure=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        now=lambda: 100.0,
    )

    result = run_position_protection_cycle(
        SimpleNamespace(is_connected=True),
        [{"position_id": value} for value in (1, 2, 3, 4)],
        cfg=SimpleNamespace(timeframe="M5"),
        account={},
        pipeline={},
        current_price=2400.0,
        atr_price=5.0,
        tick=1,
        log=lambda _message: None,
        runtime=runtime,
    )

    assert executed == [("entry", 2), ("trail", 4)]
    assert superseded == [(1, "holding_timeout"), (2, "higher_priority_protection")]
    assert result["timeout"] == [1]
    assert result["entry_repair"] == [2]
    assert result["supervisor"] == [3]
    assert result["trailing_applied"] == [4]
    assert result["trailing_superseded"] == [2]
    assert [item["position_id"] for item in result["safety_candidates"]] == [1, 2, 3, 4]
    assert [item["priority"] for item in result["safety_arbitration"]] == [10, 20, 20, 30, 50, 50]


def test_demo_protection_cycle_observes_legacy_trailing_without_execution():
    candidate = _LegacyCandidate(
        "legacy_awe_trailing",
        "tighten",
        50,
        8,
        "tighten_position",
        {"target_stop_loss": 2405.0},
        "legacy_awe_trailing",
    )
    executed: list[int] = []
    superseded: list[tuple[int, str]] = []

    runtime = PositionProtectionCycleRuntime(
        update_trailing_stops=lambda *_args, **_kwargs: [candidate],
        enforce_holding_timeout=lambda *_args, **_kwargs: set(),
        entry_protection_repair_candidates=lambda *_args, **_kwargs: [],
        log_candidate_superseded=lambda item, **kwargs: superseded.append(
            (item.position_id, kwargs["reason"])
        ),
        execute_candidate=lambda item, **_kwargs: executed.append(item.position_id) or True,
        run_position_supervision=lambda *_args, **_kwargs: set(),
        protection_candidate_to_safety=protection_candidate_to_safety,
        candidate_supersede_reason=lambda **_kwargs: "",
        build_cycle_result=build_position_protection_cycle_result,
        record_aux_failure=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        now=lambda: 100.0,
    )

    result = run_position_protection_cycle(
        SimpleNamespace(is_connected=True),
        [{"position_id": 8}],
        cfg=SimpleNamespace(autonomy_mode="demo_autonomous", timeframe="M5"),
        account={},
        pipeline={},
        current_price=2400.0,
        atr_price=5.0,
        tick=2,
        log=lambda _message: None,
        runtime=runtime,
    )

    assert executed == []
    assert superseded == [(8, "demo_adaptive_observation")]
    assert result["trailing_applied"] == []
    assert result["trailing_superseded"] == [8]
