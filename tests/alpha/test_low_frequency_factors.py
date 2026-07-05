from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from alpha.registry import (
    factor_cb_china_3m_zscore,
    factor_cot_extreme_signal,
    factor_cot_mm_net_chg_4w,
    factor_dxy_corr_20,
    factor_gld_tonnes_chg_5d,
    factor_real_yield_chg,
    factor_slv_gld_ratio,
)
from data.external_loader import ExternalDataLoader


def _m5_frame(n: int = 20) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01 00:00:00", periods=n, freq="5min")
    return pd.DataFrame(
        {
            "close": np.linspace(100.0, 110.0, n),
            "GLD_tonnes": np.linspace(900.0, 920.0, n),
            "real_yield_10y": np.linspace(1.0, 1.2, n),
            "cot_mm_net_pct_oi": np.linspace(0.1, 0.2, n),
            "dxy": np.linspace(100.0, 101.0, n),
            "SLV": np.linspace(20.0, 21.0, n),
            "GLD": np.linspace(180.0, 181.0, n),
            "cb_china_chg_3m": np.linspace(10.0, 12.0, n),
        },
        index=idx,
    )


def test_m5_low_frequency_factors_do_not_diff_forward_filled_raw_columns():
    frame = _m5_frame()

    assert np.isnan(factor_gld_tonnes_chg_5d(frame)).all()
    assert np.isnan(factor_real_yield_chg(frame)).all()
    assert np.isnan(factor_cot_mm_net_chg_4w(frame)).all()
    assert np.isnan(factor_dxy_corr_20(frame)).all()
    assert np.isnan(factor_slv_gld_ratio(frame)).all()
    assert np.isnan(factor_cb_china_3m_zscore(frame)).all()


def test_low_frequency_factors_prefer_standard_precomputed_columns_on_m5():
    frame = _m5_frame()
    frame["GLD_tonnes_chg_5d"] = np.arange(len(frame), dtype=float)
    frame["real_yield_chg_5d"] = np.arange(len(frame), dtype=float) + 10.0
    frame["cot_mm_net_chg_4w"] = np.arange(len(frame), dtype=float) + 20.0
    frame["dxy_corr_20"] = np.arange(len(frame), dtype=float) + 30.0
    frame["slv_gld_ratio_5d"] = np.arange(len(frame), dtype=float) + 40.0
    frame["cb_china_3m_zscore"] = np.arange(len(frame), dtype=float) + 50.0

    assert factor_gld_tonnes_chg_5d(frame)[-1] == pytest.approx(19.0)
    assert factor_real_yield_chg(frame)[-1] == pytest.approx(29.0)
    assert factor_cot_mm_net_chg_4w(frame)[-1] == pytest.approx(39.0)
    assert factor_dxy_corr_20(frame)[-1] == pytest.approx(49.0)
    assert factor_slv_gld_ratio(frame)[-1] == pytest.approx(59.0)
    assert factor_cb_china_3m_zscore(frame)[-1] == pytest.approx(69.0)


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


def test_cot_extreme_signal_is_discrete_reversal_direction():
    idx = pd.date_range("2025-01-03", periods=60, freq="7D")
    top_frame = pd.DataFrame(
        {
            "cot_mm_net_pct_oi": np.r_[np.linspace(-0.2, 0.0, 40), np.linspace(0.1, 0.5, 20)],
            "cot_pm_net": np.r_[np.linspace(100.0, 80.0, 40), np.linspace(20.0, -120.0, 20)],
        },
        index=idx,
    )
    bottom_frame = pd.DataFrame(
        {
            "cot_mm_net_pct_oi": np.r_[np.linspace(0.2, 0.0, 40), np.linspace(-0.1, -0.5, 20)],
            "cot_pm_net": np.r_[np.linspace(-100.0, -80.0, 40), np.linspace(-20.0, 120.0, 20)],
        },
        index=idx,
    )

    assert factor_cot_extreme_signal(top_frame)[-1] == -1.0
    assert factor_cot_extreme_signal(bottom_frame)[-1] == 1.0


def test_external_loader_precomputes_low_frequency_standard_columns():
    idx = pd.date_range("2026-01-01", periods=70, freq="1D")
    etf = pd.DataFrame(
        {
            "GLD": np.linspace(180.0, 190.0, len(idx)),
            "SLV": np.linspace(20.0, 24.0, len(idx)),
        },
        index=idx,
    )
    cb = pd.DataFrame(
        {"cb_china_chg": np.linspace(-5.0, 20.0, len(idx))},
        index=idx,
    )
    loader = ExternalDataLoader()

    etf_out = loader._compute_etf_price_derived(etf)
    cb_out = loader._compute_cb_derived(cb)

    assert "slv_gld_ratio_5d" in etf_out.columns
    assert "slv_gld_ratio" in etf_out.columns
    assert np.isfinite(etf_out["slv_gld_ratio_5d"].iloc[-1])
    assert "cb_china_3m_zscore" in cb_out.columns
    assert np.isfinite(cb_out["cb_china_3m_zscore"].iloc[-1])
