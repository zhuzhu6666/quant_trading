from types import SimpleNamespace

import pandas as pd

from backend.services.live_factor_bootstrap import (
    FactorWarmupRuntime,
    warmup_factor_pipeline,
)


def _frame(count=5):
    index = pd.date_range("2026-01-01", periods=count, freq="5min")
    return pd.DataFrame(
        {
            "open": [2_400.0] * count,
            "high": [2_405.0] * count,
            "low": [2_395.0] * count,
            "close": [2_402.0] * count,
            "volume": [100.0] * count,
        },
        index=index,
    )


class _Engine:
    MIN_BARS = 3
    buffer_size = 5
    is_warm = True

    def __init__(self):
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1

    def warmup_bars(self, bars):
        return [{"momentum": float(index)} for index, _bar in enumerate(bars)]


class _Normalizer:
    def __init__(self):
        self.warmed = []

    def warmup(self, snapshots):
        self.warmed = list(snapshots)

    def normalize(self, values):
        return {"momentum": values["momentum"]}


class _Gate:
    def __init__(self):
        self.ticks = 0

    def filter(self, _composite, _values, _bar):
        return SimpleNamespace(reason="allowed")

    def tick(self):
        self.ticks += 1


def _runtime(*, snapshots=None, acknowledgements=None):
    snapshots = snapshots if snapshots is not None else []
    acknowledgements = (
        acknowledgements if acknowledgements is not None else []
    )
    return FactorWarmupRuntime(
        build_warmup_feed=lambda frame, **_kwargs: {
            "warmup_df": frame,
            "warmup_bars": frame.to_dict("records"),
        },
        build_factor_votes=lambda *args: {"args": args},
        build_snapshot_summary=lambda *args, **kwargs: {
            "args": args,
            "now": kwargs["now"],
        },
        set_factor_snapshot=lambda votes, summary: snapshots.append(
            (votes, summary)
        ),
        acknowledge_projections=lambda **kwargs: acknowledgements.append(
            kwargs
        )
        or {"acknowledged": True},
        now=lambda: 1_000.0,
    )


def test_factor_warmup_publishes_initial_signal_and_projection_ack():
    engine = _Engine()
    normalizer = _Normalizer()
    gate = _Gate()
    snapshots = []
    acknowledgements = []
    pipeline = {
        "engine": engine,
        "normalizer": normalizer,
        "compositor": SimpleNamespace(
            compose=lambda _signals, _values: SimpleNamespace(
                direction=1,
                score=0.75,
                n_active_factors=1,
                factor_roles={"momentum": "alpha"},
                active_weights={"momentum": 1.0},
            )
        ),
        "gate": gate,
    }

    result = warmup_factor_pipeline(
        pipeline,
        _frame(),
        cfg=SimpleNamespace(live_factor_warmup_bars=5),
        timeframe="M5",
        generation_id="generation-1",
        log=lambda _message: None,
        runtime=_runtime(
            snapshots=snapshots,
            acknowledgements=acknowledgements,
        ),
    )

    assert result["ok"] is True
    assert result["warm"] is True
    assert result["snapshot_count"] == 5
    assert result["initial_signal"]["direction"] == 1
    assert engine.reset_calls == 1
    assert len(normalizer.warmed) == 5
    assert gate.ticks == 1
    assert snapshots
    assert acknowledgements[0]["generation_id"] == "generation-1"


def test_initial_signal_failure_is_nonfatal_after_engine_warmup():
    engine = _Engine()
    logs = []
    pipeline = {
        "engine": engine,
        "normalizer": _Normalizer(),
        "compositor": SimpleNamespace(
            compose=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("compose unavailable")
            )
        ),
        "gate": _Gate(),
    }

    result = warmup_factor_pipeline(
        pipeline,
        _frame(),
        cfg=SimpleNamespace(live_factor_warmup_bars=5),
        timeframe="M5",
        generation_id="generation-2",
        log=logs.append,
        runtime=_runtime(),
    )

    assert result["ok"] is True
    assert result["warm"] is True
    assert result["initial_signal"]["published"] is False
    assert any("non-fatal" in message for message in logs)


def test_missing_pipeline_is_explicitly_skipped():
    result = warmup_factor_pipeline(
        None,
        _frame(),
        cfg=SimpleNamespace(live_factor_warmup_bars=5),
        timeframe="M5",
        generation_id="generation-3",
        log=lambda _message: None,
        runtime=_runtime(),
    )

    assert result == {
        "ok": False,
        "skipped": True,
        "reason": "factor_pipeline_unavailable",
        "warm": False,
        "snapshot_count": 0,
    }
