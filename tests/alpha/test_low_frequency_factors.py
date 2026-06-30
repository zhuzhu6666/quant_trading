from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from alpha.registry import (
    factor_cot_mm_net_chg_4w,
    factor_gld_tonnes_chg_5d,
    factor_real_yield_chg,
)


def _m5_frame(n: int = 20) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01 00:00:00", periods=n, freq="5min")
    return pd.DataFrame(
        {
            "close": np.linspace(100.0, 110.0, n),
            "GLD_tonnes": np.linspace(900.0, 920.0, n),
            "real_yield_10y": np.linspace(1.0, 1.2, n),
            "cot_mm_net_pct_oi": np.linspace(0.1, 0.2, n),
        },
        index=idx,
    )


def test_m5_low_frequency_factors_do_not_diff_forward_filled_raw_columns():
    frame = _m5_frame()

    assert np.isnan(factor_gld_tonnes_chg_5d(frame)).all()
    assert np.isnan(factor_real_yield_chg(frame)).all()
    assert np.isnan(factor_cot_mm_net_chg_4w(frame)).all()


def test_low_frequency_factors_prefer_standard_precomputed_columns_on_m5():
    frame = _m5_frame()
    frame["GLD_tonnes_chg_5d"] = np.arange(len(frame), dtype=float)
    frame["real_yield_chg_5d"] = np.arange(len(frame), dtype=float) + 10.0
    frame["cot_mm_net_chg_4w"] = np.arange(len(frame), dtype=float) + 20.0

    assert factor_gld_tonnes_chg_5d(frame)[-1] == pytest.approx(19.0)
    assert factor_real_yield_chg(frame)[-1] == pytest.approx(29.0)
    assert factor_cot_mm_net_chg_4w(frame)[-1] == pytest.approx(39.0)


def test_low_frequency_daily_fallback_still_works_when_standard_column_missing():
    idx = pd.date_range("2026-01-01", periods=8, freq="1D")
    frame = pd.DataFrame(
        {
            "GLD_tonnes": np.arange(8, dtype=float) + 900.0,
            "real_yield_10y": np.arange(8, dtype=float) / 100.0,
        },
        index=idx,
    )

    assert factor_gld_tonnes_chg_5d(frame)[-1] == pytest.approx(5.0)
    assert factor_real_yield_chg(frame)[-1] == pytest.approx(5.0)
