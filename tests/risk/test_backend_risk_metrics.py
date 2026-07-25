from backend.risk.concentration import ConcentrationChecker
from backend.risk.kelly import KellyCriterion
from backend.risk.metrics_snapshot import (
    attach_internal_forward_var_input,
    build_risk_metrics_snapshot,
    freeze_closed_bar_returns,
    project_candidate_risk_snapshot,
)
from backend.risk.stress_test import StressTest
from backend.risk.var import VaRCalculator


def test_var_warmup_is_not_reported_as_zero_risk():
    result = VaRCalculator(min_returns=3).calculate_forward(
        [-0.001],
        net_notional_usd=1_000.0,
        current_equity=10_000.0,
    )

    assert result["status"] == "warming_up"
    assert result["var_pct"] is None
    assert result["cvar_pct"] is None


def _forward_input(prices=None):
    closes = prices or [
        100.0,
        99.0,
        101.0,
        98.0,
        102.0,
        97.0,
        103.0,
        96.0,
        104.0,
        95.0,
        105.0,
        94.0,
    ]
    return freeze_closed_bar_returns(
        closes,
        timestamps=list(range(len(closes))),
        symbol="XAUUSD+",
        timeframe="M5",
        as_of=100.0,
    )


def test_stress_uses_position_direction_and_notional():
    result = StressTest().run(
        [
            {"direction": "buy", "notional_usd": 10_000.0},
            {"direction": "sell", "notional_usd": 4_000.0},
        ],
        {"equity": 20_000.0},
        shocks=(-0.05, 0.05),
    )

    assert result["status"] == "known"
    assert result["stress_loss_usd"] == 300.0
    assert result["stress_loss_pct"] == 1.5


def test_concentration_unknown_is_not_safe_and_ignores_context_factors():
    checker = ConcentrationChecker(max_single_weight=0.60)

    assert checker.check(None)["is_safe"] is False
    result = checker.check(
        {"trend": 0.4, "mean_reversion": 0.4, "volatility": 5.0},
        factor_roles={
            "trend": "alpha",
            "mean_reversion": "alpha",
            "volatility": "context",
        },
    )

    assert result["status"] == "known"
    assert result["concentration_pct"] == 50.0
    assert result["is_safe"] is True


def test_kelly_rejects_non_finite_inputs():
    result = KellyCriterion().calculate(float("nan"), 10.0, 5.0)

    assert result["status"] == "unknown"
    assert result["kelly_fraction"] is None


def test_stress_requires_two_sided_scenarios():
    result = StressTest().run(
        [{"direction": "buy", "notional_usd": 10_000.0}],
        {"equity": 20_000.0},
        shocks=[],
    )

    assert result["status"] == "unknown"


def test_snapshot_preserves_warmup_instead_of_zero_risk():
    snapshot = build_risk_metrics_snapshot(
        forward_var_input=_forward_input([100.0]),
        clean_trade_pnls=[],
        positions=[],
        account={"equity": 10_000.0},
        account_reconcile_id="account-1",
        positions_reconcile_id="positions-1",
        as_of=100.0,
    ).to_dict()

    assert snapshot["status"] == "warming_up"
    assert snapshot["var_pct"] is None
    assert snapshot["components"]["stress"]["status"] == "known"
    assert snapshot["components"]["concentration"]["status"] == "known"


def test_snapshot_kelly_uses_configured_sample_and_bound():
    snapshot = build_risk_metrics_snapshot(
        forward_var_input=_forward_input(),
        clean_trade_pnls=[10.0, -5.0] * 10,
        positions=[],
        account={"equity": 10_000.0},
        account_reconcile_id="account-1",
        positions_reconcile_id="positions-1",
        as_of=100.0,
        kelly_min_closed_trades=20,
        kelly_multiplier=0.5,
        kelly_max_fraction=0.2,
    ).to_dict()

    assert snapshot["components"]["kelly"]["closed_trades"] == 20
    assert snapshot["components"]["kelly"]["status"] == "known"
    assert snapshot["kelly_fraction_bounded"] <= 0.2


def test_snapshot_does_not_turn_unknown_position_price_into_zero_exposure():
    snapshot = build_risk_metrics_snapshot(
        forward_var_input=_forward_input(),
        clean_trade_pnls=[],
        positions=[{"position_id": 7, "direction": "buy"}],
        account={"equity": 10_000.0},
        account_reconcile_id="account-1",
        positions_reconcile_id="positions-1",
        as_of=100.0,
    ).to_dict()

    assert snapshot["components"]["stress"]["status"] == "unknown"
    assert snapshot["components"]["concentration"]["status"] == "unknown"


def test_snapshot_aggregates_position_concentration_by_symbol():
    snapshot = build_risk_metrics_snapshot(
        forward_var_input=_forward_input(),
        clean_trade_pnls=[],
        positions=[
            {
                "position_id": position_id,
                "symbol": "XAUUSD+",
                "direction": "buy",
                "notional_usd": 1_000.0,
            }
            for position_id in (1, 2, 3)
        ],
        account={"equity": 10_000.0},
        account_reconcile_id="account-1",
        positions_reconcile_id="positions-1",
        as_of=100.0,
    ).to_dict()

    concentration = snapshot["components"]["concentration"]
    assert concentration["concentration_pct"] == 100.0
    assert concentration["applicable"] is False


def test_snapshot_fingerprint_includes_account_equity():
    inputs = {
        "forward_var_input": _forward_input(),
        "clean_trade_pnls": [],
        "positions": [],
        "account_reconcile_id": "account-1",
        "positions_reconcile_id": "positions-1",
        "as_of": 100.0,
    }

    low = build_risk_metrics_snapshot(
        **inputs,
        account={"equity": 10_000.0},
    )
    high = build_risk_metrics_snapshot(
        **inputs,
        account={"equity": 11_000.0},
    )

    assert low.input_fingerprint != high.input_fingerprint


def test_forward_var_uses_closed_bar_returns_and_candidate_notional():
    frozen = _forward_input()
    base = build_risk_metrics_snapshot(
        forward_var_input=frozen,
        clean_trade_pnls=[],
        positions=[],
        account={"equity": 10_000.0},
        account_reconcile_id="account-1",
        positions_reconcile_id="positions-1",
        as_of=100.0,
    ).to_dict()
    risk_state = attach_internal_forward_var_input(
        {**base["components"], "snapshot": base},
        frozen,
    )

    projected = project_candidate_risk_snapshot(
        risk_state,
        positions=[],
        account={"equity": 10_000.0},
        candidate={
            "symbol": "XAUUSD",
            "direction": 1,
            "current_price": 2_000.0,
            "requested_api_volume": 100.0,
        },
        contract_sizes={"XAUUSD+": 100.0},
    )

    assert base["components"]["var"]["var_usd"] == 0.0
    assert projected["var"]["status"] == "known"
    assert projected["var"]["candidate_notional_usd"] == 2_000.0
    assert projected["var"]["forward_net_notional_usd"] == 2_000.0
    assert projected["var"]["var_usd"] > 0.0
    assert "_forward_var_input" not in projected


def test_forward_var_projects_current_and_final_candidate_notional():
    frozen = _forward_input()
    base = build_risk_metrics_snapshot(
        forward_var_input=frozen,
        clean_trade_pnls=[],
        positions=[
            {
                "symbol": "XAUUSD+",
                "direction": "buy",
                "notional_usd": 3_000.0,
            }
        ],
        account={"equity": 10_000.0},
        account_reconcile_id="account-1",
        positions_reconcile_id="positions-1",
        as_of=100.0,
    ).to_dict()
    projected = project_candidate_risk_snapshot(
        attach_internal_forward_var_input(
            {**base["components"], "snapshot": base},
            frozen,
        ),
        positions=[
            {
                "symbol": "XAUUSD+",
                "direction": "buy",
                "notional_usd": 3_000.0,
            }
        ],
        account={"equity": 10_000.0},
        candidate={
            "symbol": "XAUUSD+",
            "direction": -1,
            "current_price": 2_000.0,
            "requested_api_volume": 100.0,
        },
        contract_sizes={"XAUUSD+": 100.0},
    )

    assert projected["var"]["current_net_notional_usd"] == 3_000.0
    assert projected["var"]["candidate_signed_notional_usd"] == -2_000.0
    assert projected["var"]["forward_net_notional_usd"] == 1_000.0
    assert projected["var_shadow_99"]["alpha"] == 0.99
