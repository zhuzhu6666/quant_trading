from backend.services.fact_envelope import attach_fact, fact_envelope


def test_known_fact_requires_observation_inside_freshness_window():
    fact = fact_envelope(
        contract="live.account.v2",
        source="ctrader",
        observed_at=95.0,
        stale_after_sec=15.0,
        now=100.0,
    )

    assert fact.state == "known"
    assert fact.reason_code is None


def test_missing_observation_is_unknown_not_zero_or_healthy():
    fact = fact_envelope(
        contract="live.account.v2",
        source="none",
        observed_at=0.0,
        stale_after_sec=15.0,
        now=100.0,
    )

    assert fact.state == "unknown"
    assert fact.reason_code == "missing_observed_at"


def test_stale_and_error_are_distinct_states():
    stale = fact_envelope(
        contract="live.positions.v2",
        source="ctrader",
        observed_at=80.0,
        stale_after_sec=15.0,
        now=100.0,
    )
    error = fact_envelope(
        contract="live.positions.v2",
        source="ctrader",
        observed_at=99.0,
        stale_after_sec=15.0,
        error="reconcile failed",
        now=100.0,
    )

    assert stale.state == "stale"
    assert stale.reason_code == "freshness_expired"
    assert error.state == "error"
    assert error.reason_code == "source_error"


def test_attach_fact_is_additive_and_preserves_legacy_top_level_fields():
    payload = {"ok": True, "balance": 123.0}

    result = attach_fact(
        payload,
        contract="live.account.v2",
        source="ctrader",
        observed_at=95.0,
        stale_after_sec=15.0,
        components={"balance": {"state": "known"}},
        now=100.0,
    )

    assert result["ok"] is True
    assert result["balance"] == 123.0
    assert result["_fact"]["envelope"] == "fact.v1"
    assert result["_fact"]["components"]["balance"]["state"] == "known"
