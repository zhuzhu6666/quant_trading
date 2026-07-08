from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from alpha.factor_health import evaluate_factors
from alpha.registry import factor_registry


def test_evaluate_factors_skips_invalid_dsl_expression_before_execution(caplog):
    original = dict(factor_registry._factors)

    def bad_factor(_df):
        raise AssertionError("invalid DSL factor should not execute")

    bad_factor._factor_desc = "rank(close"

    try:
        factor_registry._factors.clear()
        factor_registry._factors["dsl_auto_bad"] = bad_factor
        caplog.set_level(logging.WARNING)

        df = pd.DataFrame(
            {
                "open": np.linspace(100.0, 120.0, 140),
                "high": np.linspace(101.0, 121.0, 140),
                "low": np.linspace(99.0, 119.0, 140),
                "close": np.linspace(100.0, 120.0, 140),
                "volume": np.ones(140) * 100.0,
            }
        )

        result = evaluate_factors(df, exclude_dead=False)

        assert result["invalid_dsl"] == 1
        assert result["dead"] == 1
        assert result["factors"][0]["factor"] == "dsl_auto_bad"
        assert result["factors"][0]["status"] == "DEAD"
        assert result["factors"][0]["components"]["invalid_dsl_expression"] is True
        assert "evaluate_factors: dsl_auto_bad failed" not in caplog.text
    finally:
        factor_registry._factors.clear()
        factor_registry._factors.update(original)
