from types import SimpleNamespace

from risk.runtime_policy import RiskLimitSnapshot


def test_risk_limit_snapshot_fail_closed_defaults_match_production_budget():
    snapshot = RiskLimitSnapshot.from_runtime_config(
        SimpleNamespace(autonomy_mode="demo_autonomous")
    )

    assert snapshot.max_daily_loss_pct == 2.0
    assert snapshot.max_drawdown_pct == 8.0
    assert snapshot.max_daily_trades == 10


def test_demo_autonomous_uses_demo_learning_daily_trade_limit():
    cfg = SimpleNamespace(
        autonomy_mode="demo_autonomous",
        risk_max_daily_trades=20,
        demo_learning_max_daily_trades=60,
    )

    snapshot = RiskLimitSnapshot.from_runtime_config(cfg)

    assert snapshot.source == "runtime_config:demo_learning"
    assert snapshot.max_daily_trades == 60


def test_demo_nursery_uses_explicit_daily_trade_sampling_limit():
    cfg = SimpleNamespace(
        autonomy_mode="demo_nursery",
        risk_max_daily_trades=20,
        demo_learning_max_daily_trades=60,
    )

    snapshot = RiskLimitSnapshot.from_runtime_config(cfg)

    assert snapshot.source == "runtime_config:demo_nursery_learning_limit"
    assert snapshot.max_daily_trades == 60


def test_non_demo_mode_keeps_regular_daily_trade_limit():
    cfg = SimpleNamespace(
        autonomy_mode="manual",
        risk_max_daily_trades=20,
        demo_learning_max_daily_trades=60,
    )

    snapshot = RiskLimitSnapshot.from_runtime_config(cfg)

    assert snapshot.source == "runtime_config"
    assert snapshot.max_daily_trades == 20


def test_demo_learning_limit_never_reduces_regular_daily_trade_limit():
    cfg = SimpleNamespace(
        autonomy_mode="demo_autonomous",
        risk_max_daily_trades=80,
        demo_learning_max_daily_trades=60,
    )

    snapshot = RiskLimitSnapshot.from_runtime_config(cfg)

    assert snapshot.source == "runtime_config"
    assert snapshot.max_daily_trades == 80
