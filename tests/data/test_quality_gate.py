"""Tests for data/quality_gate.py — 数据质量门控."""

import time
import pandas as pd
import numpy as np
import pytest
from data.quality_gate import (
    check_bar_freshness,
    check_anomalous_spread,
    check_anomalous_volume,
    run_quality_gate,
    evolution_guard,
    DataQualityReport,
)


def _make_bars(n=100, spread=10, volume=100, tf="M5", ts_offset=0):
    """Create a synthetic bar DataFrame."""
    now = time.time()
    return pd.DataFrame({
        "timeframe": [tf] * n,
        "time": [now - (n - i) * 300 + ts_offset for i in range(n)],
        "open": np.linspace(100.0, 120.0, n),
        "high": np.linspace(101.0, 121.0, n),
        "low": np.linspace(99.0, 119.0, n),
        "close": np.linspace(100.0, 120.0, n),
        "volume": [volume] * n,
        "spread": [spread] * n,
    })


class TestBarFreshness:
    def test_fresh_bars_no_gaps(self):
        df = _make_bars(100, ts_offset=0)
        gaps = check_bar_freshness(df)
        assert "M5" in gaps
        assert gaps["M5"] < 600  # fresh

    def test_empty_df_returns_empty(self):
        gaps = check_bar_freshness(None)
        assert gaps == {}


class TestAnomalousSpread:
    def test_normal_spread_no_anomaly(self):
        df = _make_bars(100, spread=10)
        n = check_anomalous_spread(df)
        assert n == 0

    def test_anomalous_spread_detected(self):
        df = _make_bars(100, spread=10)
        # Inject some high spread values
        df.loc[10:15, "spread"] = 100
        n = check_anomalous_spread(df, zscore_threshold=0.5)
        assert n >= 6

    def test_no_spread_col_returns_zero(self):
        df = pd.DataFrame({"close": [1, 2, 3]})
        assert check_anomalous_spread(df) == 0


class TestAnomalousVolume:
    def test_normal_volume_no_anomaly(self):
        df = _make_bars(100, volume=100)
        n = check_anomalous_volume(df)
        assert n == 0

    def test_anomalous_volume_detected(self):
        df = _make_bars(100, volume=100)
        df.loc[5:7, "volume"] = 10000  # extreme spike
        n = check_anomalous_volume(df, zscore_threshold=3.0)
        assert n >= 3


class TestRunQualityGate:
    def test_pass(self):
        # 提供所有 timeframe 的 bar 避免 missing
        dfs = []
        for tf in ["M1", "M5", "M15", "M30", "H1", "D1"]:
            dfs.append(_make_bars(100, tf=tf))
        df = pd.concat(dfs, ignore_index=True)
        report = run_quality_gate(df_bars=df, max_lag_seconds=3600)
        assert report.passed
        assert report.detail == "all checks passed"

    def test_stale_bars_detected(self):
        df = _make_bars(100, ts_offset=-50000)  # very old bars
        report = run_quality_gate(df_bars=df, max_lag_seconds=3600)
        assert not report.passed
        assert any("stale" in e for e in report.errors)

    def test_evolution_guard(self):
        report = DataQualityReport(passed=True)
        assert evolution_guard(report) is True

        report = DataQualityReport(passed=False, errors=["stale bars: M5"])
        assert evolution_guard(report) is False
