from execution.analytics import ExecutionQuality, TradeExecution


def _trade(signal_time: float, fill_time: float) -> TradeExecution:
    return TradeExecution(
        signal_time=signal_time,
        submit_time=signal_time,
        fill_time=fill_time,
        signal_price=2000.0,
        fill_price=2000.1,
        symbol="XAUUSD+",
        direction=1,
        volume=1000.0,
        order_id=1,
    )


def test_execution_quality_uses_comparable_order_timestamps_only():
    quality = ExecutionQuality()
    quality.record(_trade(100.0, 100.25))
    quality.record(_trade(100.0, 500.0))

    report = quality.report()

    assert report["n_filled"] == 2
    assert report["n_latency_samples"] == 1
    assert report["avg_latency_ms"] == 250.0
