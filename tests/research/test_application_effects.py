from research.learning.application_effects import classify_effect, observation_window_expired


def test_closed_window_without_enough_samples_is_terminal():
    result = classify_effect(
        post_count=1,
        baseline_count=3,
        min_trades=3,
        baseline_min_trades=2,
        delta=0.5,
        effective_threshold=0.08,
        ineffective_threshold=-0.08,
        window_closed=True,
    )
    assert result.status == "inconclusive"
    assert result.retry_via_new_application is True


def test_closed_window_with_evidence_keeps_bounded_attribution():
    result = classify_effect(
        post_count=3,
        baseline_count=2,
        min_trades=3,
        baseline_min_trades=2,
        delta=0.2,
        effective_threshold=0.08,
        ineffective_threshold=-0.08,
        window_closed=True,
    )
    assert result.status == "effective"
    assert result.causal_status == "bounded_comparative_effective"


def test_only_open_observation_windows_expire_by_age():
    assert observation_window_expired(
        status="observing", cycle_ts=1_700_000_000.0, now=1_700_200_000.0, max_age_seconds=86400.0
    )
    assert not observation_window_expired(
        status="inconclusive", cycle_ts=1_700_000_000.0, now=1_700_200_000.0, max_age_seconds=86400.0
    )
