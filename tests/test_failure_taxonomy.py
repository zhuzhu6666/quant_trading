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
