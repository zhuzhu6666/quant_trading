from backend.api import risk


def test_risk_summary_uses_canonical_snapshot(monkeypatch):
    snapshot = {
        "schema_version": "risk_metrics_snapshot.v2",
        "status": "known",
        "as_of": 100.0,
        "components": {
            "var": {"status": "known", "var_pct": 1.2, "cvar_pct": 1.8},
            "kelly": {"status": "known", "kelly_fraction": 0.2},
            "stress": {"status": "known", "stress_loss_pct": 2.5},
            "concentration": {
                "status": "known",
                "concentration_pct": 50.0,
            },
        },
    }
    monkeypatch.setattr(risk, "_risk_metrics_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        risk,
        "_system_health_summary",
        lambda: {"overall": "healthy"},
    )
    monkeypatch.setattr(
        risk,
        "_recent_policy_verdicts",
        lambda limit=25: {"limit": limit, "items": []},
    )

    summary = risk.get_risk_summary("zhu")

    assert summary["snapshot"] == snapshot
    assert summary["var"]["cvar_pct"] == 1.8
    assert summary["stress"]["stress_loss_pct"] == 2.5


def test_component_status_carries_snapshot_freshness(monkeypatch):
    monkeypatch.setattr(
        risk,
        "_risk_metrics_snapshot",
        lambda: {
            "status": "stale",
            "as_of": 90.0,
            "components": {"var": {"status": "known", "var_pct": 1.0}},
        },
    )

    result = risk.get_var_status("zhu")

    assert result["status"] == "stale"
    assert result["metric_status"] == "known"
    assert result["snapshot_status"] == "stale"
    assert result["as_of"] == 90.0
