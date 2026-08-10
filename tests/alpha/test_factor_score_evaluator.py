from __future__ import annotations

import numpy as np
import pandas as pd

from alpha.factor_score_evaluator import FactorScoreEvaluator


def test_candidate_score_preserves_signed_direction_and_fail_closed_cost(
    monkeypatch,
):
    index = pd.date_range("2026-08-01", periods=180, freq="5min", tz="UTC")
    frame = pd.DataFrame(
        {
            "close": np.linspace(2000.0, 2050.0, len(index)),
            "volume": np.linspace(100.0, 200.0, len(index)),
            "regime_id": ["trend"] * 90 + ["range"] * 90,
        },
        index=index,
    )
    evaluator = FactorScoreEvaluator(frame, min_n_obs=30)
    monkeypatch.setattr(
        "alpha.factor_score_evaluator.evaluate_dsl",
        lambda *_args, **_kwargs: np.arange(len(frame), dtype=float),
    )
    monkeypatch.setattr(
        evaluator,
        "_compute_ic_series",
        lambda *_args, **_kwargs: np.full(40, -0.03, dtype=float),
    )

    result = evaluator.score_expression("delta(close, 1)")

    assert result.error == ""
    assert result.signed_ic_mean == -0.03
    assert result.abs_ic_mean == 0.03
    assert result.direction == -1
    assert result.polarity == "negative"
    validation = result.candidate_validation
    assert validation["direction"] == -1
    assert validation["polarity"] == "negative"
    assert validation["pit_passed"] is True
    assert validation["walk_forward_passed"] is True
    assert validation["multi_forward_passed"] is True
    assert validation["regime_ids"] == ["range", "trend"]
    assert validation["cost_test_passed"] is False
    assert validation["cost_test"]["status"] == "not_evaluated"

