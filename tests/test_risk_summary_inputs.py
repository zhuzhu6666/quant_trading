import pytest

from backend.api import risk


def test_risk_summary_uses_realized_pnl_inputs(monkeypatch):
    monkeypatch.setattr(
        risk,
        "get_realized_pnl_series",
        lambda scope="30d": {
            "ok": True,
            "points": [
                {"pnl": 1.2, "cumulative": 1.2, "balance": 0.0},
                {"pnl": -0.8, "cumulative": 0.4, "balance": 0.0},
                {"pnl": 2.0, "cumulative": 2.4, "balance": 0.0},
                {"pnl": -1.4, "cumulative": 1.0, "balance": 0.0},
            ],
        },
    )
    monkeypatch.setattr(risk, "_current_account_equity", lambda: 10_001.0)
    monkeypatch.setattr(risk, "_system_health_summary", lambda: {"overall": "healthy"})
    monkeypatch.setattr(
        risk,
        "_recent_policy_verdicts",
        lambda limit=25: {
            "limit": limit,
            "total": 4,
            "counts": {"allowed": 1, "blocked": 3},
            "by_reason": {"daily_trade_limit": 3, "ok": 1},
            "by_action": {},
            "items": [],
        },
    )

    summary = risk.get_risk_summary("zhu")

    assert summary["var"]["status"] == "ok"
    assert summary["var"]["source"] == "realized_pnl_30d"
    assert summary["var"]["lookback"] == 3
    assert summary["var"]["limit"] == pytest.approx(200.02)
    assert summary["kelly"]["status"] == "ok"
    assert summary["kelly"]["source"] == "realized_pnl_30d"
    assert summary["kelly"]["trades"] == 4
    assert summary["kelly"]["win_rate"] == pytest.approx(0.5)
    assert summary["stress"]["status"] == "ok"
    assert summary["stress"]["source"] == "realized_pnl_30d"
    assert summary["stress"]["stress_var"] >= 0
    assert summary["concentration"]["status"] in {"ok", "alert"}
    assert summary["concentration"]["source"] == "policy_reason_distribution"
