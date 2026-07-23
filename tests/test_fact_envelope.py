from backend.services.fact_envelope import DEFAULT_STALE_AFTER_SEC, attach_fact, fact_envelope


def test_default_freshness_matches_fact_v1_contract_categories():
    assert DEFAULT_STALE_AFTER_SEC == {
        "ws": 5.0,
        "state": 5.0,
        "spot": 5.0,
        "account": 15.0,
        "positions": 15.0,
        "loop": 15.0,
        "risk": 30.0,
        "session": 30.0,
        "system_health": 75.0,
        "readiness": 180.0,
        "learning": 180.0,
        "ops": 180.0,
        "recovery": 75.0,
    }


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


def test_source_none_is_unknown_even_with_a_fresh_timestamp():
    fact = fact_envelope(
        contract="live.state.v2",
        source="none",
        observed_at=99.0,
        stale_after_sec=5.0,
        now=100.0,
    )

    assert fact.state == "unknown"
    assert fact.reason_code == "source_unavailable"


def test_iso_observation_is_normalized_for_freshness():
    fact = fact_envelope(
        contract="ops.backend-readiness.v2",
        source="persistent_snapshot",
        observed_at="1970-01-01T00:01:35Z",
        stale_after_sec=15.0,
        now=100.0,
    )

    assert fact.state == "known"


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
