from types import SimpleNamespace

import pandas as pd

from backend.services.live_factor_bootstrap import (
    FactorInitializationRuntime,
    FactorWarmupRuntime,
    initialize_factor_pipelines,
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
        self.warmup_calls = []

    def warmup(self, snapshots):
        self.warmed = list(snapshots)
        self.warmup_calls.append(self.warmed)

    def normalize(self, values):
        return {"momentum": values["momentum"]}


class _Gate:
    def __init__(self):
        self.ticks = 0

    def filter(self, _composite, _values, _bar):
        return SimpleNamespace(reason="allowed")

    def tick(self):
        self.ticks += 1


def _runtime(*, snapshots=None, acknowledgements=None, low_frequency=None):
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
        build_low_frequency_snapshots=low_frequency,
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


def test_factor_warmup_seeds_daily_history_before_intraday_history():
    engine = _Engine()
    normalizer = _Normalizer()
    pipeline = {
        "engine": engine,
        "normalizer": normalizer,
        "compositor": SimpleNamespace(
            compose=lambda _signals, _values: SimpleNamespace(
                direction=0,
                score=0.0,
                n_active_factors=1,
                factor_roles={"momentum": "alpha"},
                active_weights={"momentum": 1.0},
            )
        ),
        "gate": _Gate(),
    }

    result = warmup_factor_pipeline(
        pipeline,
        _frame(),
        cfg=SimpleNamespace(
            live_factor_warmup_bars=5,
            factor_signal_config={"macro": {"window": 100}},
        ),
        timeframe="M5",
        generation_id="generation-low-frequency",
        log=lambda _message: None,
        runtime=_runtime(
            low_frequency=lambda **_kwargs: {
                "snapshots": [{"macro": float(i)} for i in range(40)],
                "factor_counts": {"macro": 40},
                "daily_bar_count": 40,
            }
        ),
    )

    assert normalizer.warmup_calls[0][0] == {"macro": 0.0}
    assert normalizer.warmup_calls[1][0] == {"momentum": 0.0}
    assert result["low_frequency_warmup"] == {
        "daily_bar_count": 40,
        "snapshot_count": 40,
        "factor_counts": {"macro": 40},
        "factor_errors": {},
    }


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


def _factor_config():
    return SimpleNamespace(
        factor_signal_config={"momentum": {"enabled": True}},
        factor_portfolio_weights={"momentum": 1.0},
        factor_tactical_alpha=0.1,
        factor_signal_threshold=0.2,
        ctrader_send_orders=False,
        cross_asset_covariance_window=50,
    )


def _initialization_runtime(
    *,
    subscriptions=None,
    applied=None,
    acknowledgements=None,
    projections=None,
    generation_active=None,
    engine_cls=None,
    debug=None,
):
    subscriptions = subscriptions if subscriptions is not None else []
    applied = applied if applied is not None else []
    acknowledgements = (
        acknowledgements if acknowledgements is not None else []
    )
    projections = projections if projections is not None else []
    debug = debug if debug is not None else []

    class Engine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Normalizer:
        def __init__(self, config):
            self.config = config

    class Compositor:
        def __init__(self, config):
            self.config = config

    class Gate:
        def __init__(self, config):
            self.config = config

    class AdaptiveWeights:
        def __init__(self, config, *, ictracker):
            self.config = config
            self.ictracker = ictracker
            self.initialized = False

        def initialize(self, _weights, *, ictracker):
            assert ictracker is self.ictracker
            self.initialized = True

    class ProjectionService:
        def publish(self, selection):
            projections.append(selection)

    class Covariance:
        def __init__(self, symbols, *, window):
            self.symbols = list(symbols)
            self.window = window

    return FactorInitializationRuntime(
        config_factory=_factor_config,
        engine_cls=engine_cls or Engine,
        normalizer_cls=Normalizer,
        compositor_cls=Compositor,
        gate_cls=Gate,
        attribution_cls=object,
        adaptive_weight_cls=AdaptiveWeights,
        ic_tracker_cls=lambda **kwargs: SimpleNamespace(**kwargs),
        selection_factory=lambda config: {"selected": sorted(config)},
        projection_service_factory=ProjectionService,
        event_sizing_factory=lambda: {"enabled": True},
        subscribe_config=lambda callback: subscriptions.append(callback),
        generation_active=(generation_active or (lambda _generation: True)),
        merge_portfolio_configs=lambda *args: {"merged": args},
        execution_gate_config=lambda _cfg: {"gate": True},
        adaptive_weight_config=lambda _cfg: {"adaptive": True},
        unique_factor_pipelines=lambda primary, extras: [
            primary,
            *extras.values(),
        ],
        apply_config_update=lambda **kwargs: applied.append(kwargs),
        acknowledge_projections=lambda **kwargs: acknowledgements.append(
            kwargs
        )
        or {"acknowledged": True},
        enabled_symbols=lambda _cfg: ["XAUUSD+", "EURUSD"],
        build_extra_symbol_pipelines=lambda **kwargs: {
            "XAUUSD+": kwargs["primary_pipeline"],
            "EURUSD": {"engine": "secondary"},
        },
        cross_asset_symbols=lambda _cfg: ["XAUUSD+", "EURUSD"],
        covariance_cls=Covariance,
        logger_warning=lambda *args: debug.append(args),
        logger_debug=lambda *args: debug.append(args),
    )


def test_factor_initialization_builds_multi_symbol_state_and_hot_reload():
    subscriptions = []
    applied = []
    acknowledgements = []
    projections = []
    logs = []
    runtime = _initialization_runtime(
        subscriptions=subscriptions,
        applied=applied,
        acknowledgements=acknowledgements,
        projections=projections,
    )

    result = initialize_factor_pipelines(
        generation_id="generation-1",
        log=logs.append,
        runtime=runtime,
    )

    assert result.error == ""
    assert result.pipeline["awe"].initialized is True
    assert set(result.pipelines) == {"XAUUSD+", "EURUSD"}
    assert result.cross_asset_covariance.window == 50
    assert projections == [{"selected": ["momentum"]}]
    assert len(subscriptions) == 1

    subscriptions[0](_factor_config(), 2)
    assert applied
    assert acknowledgements[0]["generation_id"] == "generation-1"
    assert len(projections) == 2


def test_stale_generation_ignores_factor_hot_reload():
    subscriptions = []
    applied = []
    runtime = _initialization_runtime(
        subscriptions=subscriptions,
        applied=applied,
        generation_active=lambda _generation: False,
    )
    initialize_factor_pipelines(
        generation_id="old-generation",
        log=lambda _message: None,
        runtime=runtime,
    )

    subscriptions[0](_factor_config(), 3)

    assert applied == []


def test_primary_factor_initialization_failure_returns_no_alpha_pipeline():
    class UnavailableEngine:
        def __init__(self, **_kwargs):
            raise RuntimeError("factor engine unavailable")

    logs = []
    result = initialize_factor_pipelines(
        generation_id="generation-4",
        log=logs.append,
        runtime=_initialization_runtime(engine_cls=UnavailableEngine),
    )

    assert result.pipeline is None
    assert result.pipelines == {}
    assert "factor engine unavailable" in result.error
    assert any("init failed" in message for message in logs)
