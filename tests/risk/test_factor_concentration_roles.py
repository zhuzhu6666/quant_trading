from risk.concentration import FactorExposureMonitor


def test_consensus_risk_ignores_context_factor_signals():
    monitor = FactorExposureMonitor(max_type_pct=1.0, alert_type_pct=1.0, max_single_weight=10.0)

    report = monitor.check(
        factor_signals={
            "rsi_14": 0.4,
            "di_spread": -0.5,
            "bb_width": 1.0,
            "adx": 1.0,
        },
        factor_tags={
            "rsi_14": ["均值回归"],
            "di_spread": ["趋势"],
            "bb_width": ["波动率"],
            "adx": ["趋势"],
        },
        factor_weights={
            "rsi_14": 0.5,
            "di_spread": 0.5,
            "bb_width": 0.4,
            "adx": 0.5,
        },
        factor_roles={
            "rsi_14": "alpha",
            "di_spread": "alpha",
            "bb_width": "context",
            "adx": "context",
        },
    )

    assert report.total_factors == 2
    assert report.consensus_risk is False
    assert report.type_exposures == {"均值回归": 0.5, "趋势": 0.5}
