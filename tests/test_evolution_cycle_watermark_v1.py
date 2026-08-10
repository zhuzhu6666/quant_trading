from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from backend.runtime import evolution_orchestrator as evolution


def _bars() -> pd.DataFrame:
    index = pd.date_range(
        "2026-08-01",
        periods=600,
        freq="5min",
        tz="UTC",
    )
    values = np.linspace(100.0, 110.0, len(index))
    return pd.DataFrame(
        {
            "open": values,
            "high": values + 0.5,
            "low": values - 0.5,
            "close": values + 0.1,
            "volume": np.ones(len(index)),
        },
        index=index,
    )


def _isolate_cycle(monkeypatch, counters: dict[str, int]) -> None:
    import alpha.factor_health as factor_health
    import alpha.ic_tracker as ic_tracker
    import data.quality_gate as quality_gate

    monkeypatch.setattr(
        quality_gate,
        "run_quality_gate",
        lambda **_kwargs: SimpleNamespace(detail="ok"),
    )
    monkeypatch.setattr(quality_gate, "evolution_guard", lambda _report: True)
    monkeypatch.setattr(evolution, "_load_bars", lambda *_args: _bars())
    monkeypatch.setattr(evolution, "_emit_evolution_story", lambda *_args: None)
    monkeypatch.setattr(
        evolution,
        "_register_shadow_factors",
        lambda expressions: counters.__setitem__(
            "register",
            counters.get("register", 0) + 1,
        )
        or len(expressions),
    )
    monkeypatch.setattr(
        evolution,
        "_update_shadow_performance",
        lambda *_args: counters.__setitem__(
            "shadow",
            counters.get("shadow", 0) + 1,
        )
        or 0,
    )
    monkeypatch.setattr(
        evolution,
        "_run_canary_evaluation",
        lambda *_args: (
            counters.__setitem__(
                "canary",
                counters.get("canary", 0) + 1,
            )
            or [],
            [],
            [],
        ),
    )
    monkeypatch.setattr(
        evolution,
        "_check_retirement",
        lambda: counters.__setitem__(
            "retirement",
            counters.get("retirement", 0) + 1,
        )
        or {"candidates": [], "reason": ""},
    )
    monkeypatch.setattr(
        evolution,
        "_update_weights",
        lambda **_kwargs: counters.__setitem__(
            "weights",
            counters.get("weights", 0) + 1,
        )
        or False,
    )
    monkeypatch.setattr(
        ic_tracker,
        "refresh_all_factors",
        lambda **_kwargs: {"factors_checked": 0, "errors": []},
    )
    monkeypatch.setattr(
        factor_health,
        "evaluate_factors",
        lambda *_args, **_kwargs: {
            "healthy": 0,
            "watch": 0,
            "decaying": 0,
        },
    )
    monkeypatch.setattr(
        factor_health,
        "write_report",
        lambda *_args, **_kwargs: {
            "persisted": True,
            "updated_at": 1_786_000_000.0,
        },
    )


def test_same_evolution_input_runs_gp_once_but_maintenance_every_cycle(
    monkeypatch,
):
    monkeypatch.setenv("QUANT_PROCESS_ROLE", "learning_worker")
    counters: dict[str, int] = {}
    watermark: dict = {}
    _isolate_cycle(monkeypatch, counters)
    monkeypatch.setattr(
        evolution,
        "_canary_registration_backpressure",
        lambda: {
            "ok": True,
            "status": "available",
            "can_register": True,
            "nonterminal_candidate_count": 0,
            "evaluation_limit": 200,
            "reason_code": "",
        },
    )
    monkeypatch.setattr(
        evolution,
        "_run_gp",
        lambda *_args, **_kwargs: counters.__setitem__(
            "gp",
            counters.get("gp", 0) + 1,
        )
        or [],
    )
    monkeypatch.setattr(
        evolution,
        "_load_evolution_cycle_watermark",
        lambda: (
            {**watermark, "read_status": "known"}
            if watermark
            else {"read_status": "missing"}
        ),
    )

    def persist(payload):
        watermark.clear()
        watermark.update(payload)
        counters["watermark_write"] = counters.get("watermark_write", 0) + 1
        return {**payload, "write_status": "completed"}

    monkeypatch.setattr(
        evolution,
        "_persist_evolution_cycle_watermark",
        persist,
    )

    first = evolution.scheduled_evolution_cycle()
    second = evolution.scheduled_evolution_cycle()

    assert first.gp_status == "completed"
    assert second.gp_status == "skipped_same_input"
    assert counters["gp"] == 1
    assert counters["watermark_write"] == 1
    assert counters["shadow"] == 2
    assert counters["canary"] == 2
    assert counters["retirement"] == 2
    assert counters["weights"] == 2


def test_canary_budget_backpressure_stops_registration_not_maintenance(
    monkeypatch,
):
    monkeypatch.setenv("QUANT_PROCESS_ROLE", "learning_worker")
    counters: dict[str, int] = {}
    _isolate_cycle(monkeypatch, counters)
    monkeypatch.setattr(
        evolution,
        "_load_evolution_cycle_watermark",
        lambda: {"read_status": "missing"},
    )
    monkeypatch.setattr(
        evolution,
        "_canary_registration_backpressure",
        lambda: {
            "ok": True,
            "status": "backpressured",
            "can_register": False,
            "nonterminal_candidate_count": 200,
            "evaluation_limit": 200,
            "reason_code": "canary_evaluation_backlog_at_budget",
        },
    )
    monkeypatch.setattr(
        evolution,
        "_run_gp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("GP must not run while backlog is at budget")
        ),
    )
    monkeypatch.setattr(
        evolution,
        "_persist_evolution_cycle_watermark",
        lambda _payload: (_ for _ in ()).throw(
            AssertionError("backpressured input must not advance watermark")
        ),
    )

    result = evolution.scheduled_evolution_cycle()

    assert result.gp_status == "blocked_by_backpressure"
    assert result.gp_registered_shadow == 0
    assert counters["shadow"] == 1
    assert counters["canary"] == 1
    assert counters["retirement"] == 1
    assert counters["weights"] == 1


def test_failed_gp_does_not_advance_evolution_watermark(monkeypatch):
    monkeypatch.setenv("QUANT_PROCESS_ROLE", "learning_worker")
    counters: dict[str, int] = {}
    _isolate_cycle(monkeypatch, counters)
    monkeypatch.setattr(
        evolution,
        "_load_evolution_cycle_watermark",
        lambda: {"read_status": "missing"},
    )
    monkeypatch.setattr(
        evolution,
        "_canary_registration_backpressure",
        lambda: {
            "ok": True,
            "status": "available",
            "can_register": True,
            "nonterminal_candidate_count": 0,
            "evaluation_limit": 200,
            "reason_code": "",
        },
    )
    monkeypatch.setattr(
        evolution,
        "_run_gp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("gp failed")
        ),
    )
    writes = []
    monkeypatch.setattr(
        evolution,
        "_persist_evolution_cycle_watermark",
        lambda payload: writes.append(payload),
    )

    result = evolution.scheduled_evolution_cycle()

    assert result.error == "unexpected: gp failed"
    assert writes == []
    assert counters.get("register", 0) == 0
    assert counters.get("shadow", 0) == 0
