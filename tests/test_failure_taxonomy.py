from backend.services.failure_taxonomy import build_failure_taxonomy


def test_build_failure_taxonomy_marks_exit_capture_and_entry_good_exit_bad():
    result = build_failure_taxonomy(
        {
            "entry_quality": 0.72,
            "exit_quality": 0.32,
            "regime_fit": 0.61,
            "holding_efficiency": 0.41,
            "giveback_ratio": 0.76,
            "profit_capture_ratio": 0.24,
            "holding_seconds": 7200,
            "time_decay_score": 0.44,
            "real_pnl": {"net": 8.0},
            "mfe": 28.0,
            "context_integrity": "full",
        }
    )

    assert result["primary_responsibility"] == "exit"
    assert "entry_good_exit_bad" in result["responsibility_labels"]
    assert "alpha_correct_but_capture_failed" in result["responsibility_labels"]


def test_build_failure_taxonomy_marks_timing_and_regime_cases():
    result = build_failure_taxonomy(
        {
            "entry_quality": 0.52,
            "exit_quality": 0.51,
            "regime_fit": 0.33,
            "holding_efficiency": 0.2,
            "giveback_ratio": 0.12,
            "profit_capture_ratio": 0.0,
            "holding_seconds": 90000,
            "time_decay_score": 0.2,
            "close_reason": "holding_timeout",
            "regime_shift_at_exit": "confirmed",
            "context_integrity": "partial",
        }
    )

    assert result["primary_responsibility"] == "timing"
    assert "holding_too_long" in result["responsibility_labels"]
    assert "regime_changed_during_hold" in result["responsibility_labels"]
    assert "factor_logic_ok_but_param_suspect" in result["responsibility_labels"]


def test_build_failure_taxonomy_marks_granular_entry_failures():
    result = build_failure_taxonomy(
        {
            "entry_quality": 0.31,
            "exit_quality": 0.55,
            "action_score": 0.22,
            "direction": "buy",
            "pnl": -11.0,
            "mae": -14.0,
            "mfe": 2.0,
            "bar_context": {"bar_close_location": 0.91},
            "event_context": {"event_near": True, "event_multiplier": 0.75},
            "decision_quality_context": {
                "factor_conflict_ratio": 0.52,
                "positive_contribution_abs": 0.2,
                "negative_contribution_abs": 0.6,
            },
            "context_integrity": "full",
        }
    )

    labels = result["responsibility_labels"]
    assert "entry_chase" in labels
    assert "weak_signal_overtraded" in labels
    assert "conflicting_factor_entry" in labels
    assert "macro_event_overridden" in labels
    assert "low_reward_to_risk_entry" in labels
    assert result["primary_responsibility"] == "timing"


def test_build_failure_taxonomy_prioritizes_system_data_contamination():
    result = build_failure_taxonomy(
        {
            "entry_quality": 0.31,
            "exit_quality": 0.55,
            "action_score": -0.91,
            "pnl": -0.27,
            "mae": -0.48,
            "mfe": 0.05,
            "timeframe": "M5",
            "entry_timing_context": {
                "schema_version": "entry_timing_context.v1",
                "timeframe_seconds": 300,
                "signal_to_decision_delay_seconds": 619.0,
                "signal_to_fill_delay_seconds": 622.0,
            },
            "decision_freshness_context": {
                "schema_version": "review_decision_freshness_context.v1",
                "fresh": True,
                "data_lag_seconds": 619.0,
            },
            "data_quality_context": {"quote_fresh": True},
            "context_integrity": "full",
        }
    )

    labels = result["responsibility_labels"]
    assert result["primary_responsibility"] == "data_quality"
    assert "market_data_stale" in labels
    assert "signal_execution_delay" in labels
    assert "data_quality_issue" in labels
    assert result["system_issue_context"]["contaminates_learning"] is True


def test_advisory_tick_data_health_does_not_contaminate_trade_review():
    result = build_failure_taxonomy(
        {
            "entry_quality": 0.31,
            "exit_quality": 0.55,
            "action_score": -0.91,
            "pnl": -0.27,
            "mae": -0.48,
            "mfe": 0.05,
            "timeframe": "M5",
            "entry_timing_context": {
                "schema_version": "entry_timing_context.v1",
                "timeframe_seconds": 300,
            },
            "decision_freshness_context": {
                "schema_version": "review_decision_freshness_context.v1",
                "fresh": True,
                "data_lag_seconds": 0.0,
            },
            "data_quality_context": {
                "quote_fresh": True,
                "runtime_health": {
                    "system_health": {
                        "component_status": {"tick_data": "critical"},
                    },
                },
            },
            "context_integrity": "full",
        }
    )

    labels = result["responsibility_labels"]
    assert "market_data_stale" not in labels
    assert "data_quality_issue" not in labels
    assert result["system_issue_context"]["primary_responsibility"] == ""
    assert result["system_issue_context"]["contaminates_learning"] is False


def test_component_health_without_freshness_failure_does_not_contaminate_m5_review():
    result = build_failure_taxonomy(
        {
            "entry_quality": 0.31,
            "exit_quality": 0.55,
            "action_score": -0.91,
            "pnl": -0.27,
            "mae": -0.48,
            "mfe": 0.05,
            "timeframe": "M5",
            "entry_timing_context": {
                "schema_version": "entry_timing_context.v1",
                "timeframe_seconds": 300,
            },
            "decision_freshness_context": {
                "schema_version": "review_decision_freshness_context.v1",
                "fresh": True,
                "data_lag_seconds": 0.0,
            },
            "data_quality_context": {
                "quote_fresh": True,
                "runtime_health": {
                    "system_health": {
                        "component_status": {
                            "bar_m1": "degraded",
                            "tick_data": "critical",
                        },
                    },
                },
            },
            "context_integrity": "full",
        }
    )

    labels = result["responsibility_labels"]
    assert "market_data_stale" not in labels
    assert "bar_data_degraded" not in labels
    assert result["system_issue_context"]["contaminates_learning"] is False
