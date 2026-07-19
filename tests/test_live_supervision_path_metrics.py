from backend.services.live_supervision_runtime import (
    PositionPathMetricsRuntime,
    position_path_metrics_for_position,
)


def _runtime(*, position_id=7, persist=None, failures=None):
    failures = failures if failures is not None else []
    persist = persist or (lambda *_args, **_kwargs: None)
    return PositionPathMetricsRuntime(
        position_id=lambda _position: position_id,
        holding_summary=lambda *_args, **_kwargs: {
            "holding_seconds": 120.0,
            "max_holding_seconds": 300.0,
        },
        load_recovery_row=lambda *_args, **_kwargs: {"recovery_meta": {}},
        lookup_entry_context=lambda *_args, **_kwargs: {"source": "ledger"},
        build_inputs=lambda **kwargs: {
            "recovery_meta": {},
            "entry_context": kwargs["entry_context"],
            "current_pnl": kwargs["current_pnl"],
            "now_ts": kwargs["now_ts"],
            "holding_seconds": 120.0,
            "max_holding_seconds": 300.0,
            "current_regime": kwargs["current_regime"],
            "upsert_defaults": {
                "broker": kwargs["broker"],
                "strategy_name": kwargs["strategy_name"],
                "status": "open",
                "context_integrity": kwargs["default_context_integrity"],
            },
        },
        current_regime_hint=lambda: "trend",
        position_unrealized_pnl=lambda _position: 42.0,
        now=lambda: 1_000.0,
        loop_strategy_name="factor_v4",
        default_context_integrity="full",
        build_update=lambda **kwargs: {
            "next_meta": {"mfe": kwargs["current_pnl"]},
            "result": {
                "mfe": kwargs["current_pnl"],
                "holding_seconds": kwargs["holding_seconds"],
            },
        },
        normalize_path_state=lambda value: value,
        update_path_metrics=lambda **kwargs: kwargs,
        upsert_recovery_position=persist,
        record_aux_failure=lambda *args, **kwargs: failures.append(
            (args, kwargs)
        ),
    )


def test_position_without_broker_identity_has_no_path_metrics():
    assert position_path_metrics_for_position(
        {"symbol": "XAUUSD"},
        runtime=_runtime(position_id=0),
    ) == {}


def test_path_metrics_persistence_failure_is_enrichment_only():
    failures = []

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("postgres unavailable")

    result = position_path_metrics_for_position(
        {"position_id": 7, "profit": 42.0},
        runtime=_runtime(persist=unavailable, failures=failures),
        now_ts=1_200.0,
        persist=True,
        broker="ctrader",
        strategy_name="factor_v4",
    )

    assert result == {"mfe": 42.0, "holding_seconds": 120.0}
    assert failures[0][0] == ("risk_reduction_state_persist_failed",)
    assert failures[0][1]["position_id"] == 7
    assert failures[0][1]["action"] == "position_path_metrics"
