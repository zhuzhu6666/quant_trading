from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from alpha.factor_health import evaluate_factors
from alpha.factor_identity import factor_definition_fingerprint
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


def test_evaluate_factors_writes_dead_snapshot_for_retired_factor(
    monkeypatch,
    caplog,
):
    """Retired (DEAD) factors must keep a timestamped DEAD health snapshot.

    The recovery chain requires ``health_updated_at > disabled_at``; skipping
    retired factors lets the write_report orphan cleanup delete their health
    row, so the freshness timeline can never advance. A DEAD snapshot keeps
    the row alive (status=DEAD never triggers recovery by itself).
    """
    original = dict(factor_registry._factors)

    def retired_factor(df):
        return df["close"].pct_change().fillna(0.0)

    retired_factor._factor_desc = "rank(close)"

    class _DeadAdapter:
        def dead_names(self):
            return ["dsl_auto_retired"]

    try:
        factor_registry._factors.clear()
        factor_registry._factors["dsl_auto_retired"] = retired_factor
        monkeypatch.setattr(
            "alpha.registry_adapter.RegistryAdapter.shared",
            lambda: _DeadAdapter(),
        )
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

        result = evaluate_factors(df, exclude_dead=True)

        by_name = {f["factor"]: f for f in result["factors"]}
        assert "dsl_auto_retired" in by_name
        assert by_name["dsl_auto_retired"]["status"] == "DEAD"
        assert by_name["dsl_auto_retired"]["score"] == 0.0
        assert "retired_factor" in by_name["dsl_auto_retired"]["components"]
    finally:
        factor_registry._factors.clear()
        factor_registry._factors.update(original)


def test_evaluate_factors_includes_committed_prepared_dsl_after_registry_restart(
    monkeypatch,
):
    import backend.core.db as db

    expression = "rank(close)"
    fingerprint = factor_definition_fingerprint(expression)
    factor_id = f"dsl:{fingerprint}"

    class _Rows:
        def fetchall(self):
            return [
                {
                    "factor_id": factor_id,
                    "factor_name": f"dsl_auto_{fingerprint}",
                    "definition_fingerprint": fingerprint,
                    "metadata_json": {"expression": expression},
                }
            ]

    class _Conn:
        def execute(self, _sql, _params):
            return _Rows()

        def close(self):
            return None

    monkeypatch.setattr(db, "get_state_pg_conn", lambda read_only=True: _Conn())
    original = dict(factor_registry._factors)
    try:
        factor_registry._factors.clear()
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

        assert result["total"] == 1
        assert result["factors"][0]["factor"] == f"dsl_auto_{fingerprint}"
        assert result["factors"][0]["n_obs"] > 0
    finally:
        factor_registry._factors.clear()
        factor_registry._factors.update(original)


def test_low_frequency_repeated_values_are_sampled_on_change(monkeypatch):
    original = dict(factor_registry._factors)
    name = "cot_test_weekly"

    def weekly_factor(df):
        return np.repeat(np.arange(len(df) // 20 + 1, dtype=float), 20)[: len(df)]

    try:
        factor_registry._factors.clear()
        factor_registry._factors[name] = weekly_factor
        df = pd.DataFrame(
            {
                "open": np.linspace(100.0, 120.0, 240),
                "high": np.linspace(101.0, 121.0, 240),
                "low": np.linspace(99.0, 119.0, 240),
                "close": np.linspace(100.0, 120.0, 240),
                "volume": np.ones(240) * 100.0,
            }
        )

        result = evaluate_factors(df, exclude_dead=False)
        status = result["factors"][0]

        assert status["factor"] == name
        assert status["status"] == "UNKNOWN"
        assert status["components"]["cadence"] == "weekly"
        assert status["components"]["history_sample_policy"] == "on_value_change"
        assert status["components"]["sampled_observations"] < 20
        assert status["components"]["evaluation_mode"] == "cadence_aware"
    finally:
        factor_registry._factors.clear()
        factor_registry._factors.update(original)


def test_event_factor_is_unknown_not_ordinary_alpha_health():
    original = dict(factor_registry._factors)
    name = "evt_test_window"

    try:
        factor_registry._factors.clear()
        factor_registry._factors[name] = lambda df: np.ones(len(df))
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
        status = result["factors"][0]

        assert status["status"] == "UNKNOWN"
        assert status["components"]["cadence"] == "event"
        assert status["components"]["evaluation_reason"] == "non_directional_factor"
    finally:
        factor_registry._factors.clear()
        factor_registry._factors.update(original)
