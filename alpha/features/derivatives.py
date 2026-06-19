"""alpha/features/derivatives.py — 数学衍生层 (Phase 3)。

在每个已有因子值 + OHLCV 原始数据上施加数学变换，
自动膨胀候选特征池到 200+。

设计文档: docs/UPGRADE_BLUEPRINT.md §3.1
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

# Safe division: returns NaN where denominator is zero instead of raising warning
def _safe_div(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(np.abs(b) > 1e-12, a / b, np.nan)

logger = logging.getLogger(__name__)

# 滚动窗口配置
WINDOWS = [5, 10, 20, 50]


class FeatureDeriver:
    """数学衍生特征生成器。

    输入: OHLCV DataFrame + 已有因子值 dict
    输出: 200+ 衍生特征 DataFrame

    Args:
        include_wavelet: 是否包含 pywt 小波变换 (需要 pywt 包)
        include_fft: 是否包含 FFT 频率特征
    """

    def __init__(self, include_wavelet: bool = False, include_fft: bool = False):
        self.include_wavelet = include_wavelet
        self.include_fft = include_fft

    def derive(
        self,
        df: pd.DataFrame,
        factor_values: Optional[dict[str, np.ndarray]] = None,
    ) -> pd.DataFrame:
        """从 OHLCV + 已有因子值生成衍生特征。

        Args:
            df: OHLCV DataFrame (需含 open/high/low/close/volume)
            factor_values: {name: np.ndarray} 已有因子值 (可选, 也会被衍生)

        Returns:
            衍生特征 DataFrame, index 与 df 对齐
        """
        features = pd.DataFrame(index=df.index)
        n = len(df)

        close = df["close"].values.astype(float)
        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        open_ = df["open"].values.astype(float)
        volume = df["volume"].values.astype(float) if "volume" in df.columns else np.ones(n)

        # ═══════════════════════════════════════════════════
        # 1. 价格变换
        # ═══════════════════════════════════════════════════
        features["log_return_1"] = _safe_log_return(close, 1)
        features["log_return_5"] = _safe_log_return(close, 5)
        features["log_return_20"] = _safe_log_return(close, 20)
        features["pct_change_1"] = _safe_pct(close, 1)
        features["pct_change_5"] = _safe_pct(close, 5)

        # 价格位置: (close - low) / (high - low)
        hl_range = high - low
        hl_range[hl_range < 1e-8] = np.nan
        features["close_position"] = (close - low) / hl_range

        # gap: open - prev_close
        prev_close = np.roll(close, 1)
        prev_close[0] = np.nan
        features["gap"] = open_ - prev_close
        features["gap_pct"] = (open_ - prev_close) / np.abs(prev_close)

        # ═══════════════════════════════════════════════════
        # 2. 滚动统计 (价格)
        # ═══════════════════════════════════════════════════
        for w in WINDOWS:
            roll_close = pd.Series(close).rolling(w)
            features[f"close_zscore_{w}"] = (
                (close - roll_close.mean().values) / roll_close.std().values
            )
            features[f"close_skew_{w}"] = roll_close.skew().values
            features[f"close_kurt_{w}"] = roll_close.kurt().values
            features[f"close_rank_{w}"] = _rolling_rank(close, w)
            features[f"high_low_ratio_{w}"] = (
                pd.Series(high).rolling(w).max().values /
                pd.Series(low).rolling(w).min().values
            )

        # ═══════════════════════════════════════════════════
        # 3. 成交量变换
        # ═══════════════════════════════════════════════════
        features["log_volume"] = np.log(volume + 1)
        features["vol_pct_change_1"] = _safe_pct(volume, 1)
        for w in WINDOWS:
            roll_vol = pd.Series(volume).rolling(w, min_periods=max(2, w // 4))
            features[f"vol_zscore_{w}"] = _safe_div(
                (volume - roll_vol.mean().values), roll_vol.std().values
            )
            features[f"vol_ma_ratio_{w}"] = _safe_div(volume, roll_vol.mean().values)

        # ═══════════════════════════════════════════════════
        # 4. 波动率变换
        # ═══════════════════════════════════════════════════
        for w in WINDOWS:
            roll_close2 = pd.Series(close).rolling(w, min_periods=max(2, w // 4))
            ret_std = roll_close2.std().values
            features[f"volatility_{w}"] = _safe_div(ret_std, close)
            features[f"vol_change_{w}"] = _safe_pct(ret_std, 1)
            # Parkinson 波动率估计
            parkinson = np.sqrt(
                (1.0 / (4.0 * np.log(2))) *
                (np.log(pd.Series(high).rolling(w).max().values /
                        pd.Series(low).rolling(w).min().values)) ** 2
            )
            features[f"parkinson_vol_{w}"] = parkinson

        # ═══════════════════════════════════════════════════
        # 5. 形态特征
        # ═══════════════════════════════════════════════════
        body = close - open_
        candle_range = high - low
        candle_range[candle_range < 1e-8] = np.nan
        features["body_ratio"] = np.abs(body) / candle_range
        features["upper_shadow"] = (high - np.maximum(close, open_)) / candle_range
        features["lower_shadow"] = (np.minimum(close, open_) - low) / candle_range

        # Doji: body < 10% of range
        features["is_doji"] = (np.abs(body) / candle_range < 0.1).astype(float)

        # ═══════════════════════════════════════════════════
        # 6. 现有因子的衍生 (log/diff/pct/zscore)
        # ═══════════════════════════════════════════════════
        if factor_values:
            for name, vals in factor_values.items():
                arr = np.asarray(vals, dtype=float)
                if len(arr) != n:
                    continue
                arr[np.isinf(arr)] = np.nan
                # diff
                features[f"{name}_diff"] = _safe_diff(arr)
                # zscore (20 bars)
                s = pd.Series(arr)
                features[f"{name}_z20"] = (
                    (arr - s.rolling(20).mean().values) /
                    s.rolling(20).std().values
                )
                # pct_change
                features[f"{name}_pct"] = _safe_pct(arr, 1)

        # ═══════════════════════════════════════════════════
        # 7. (可选) 小波变换 + FFT
        # ═══════════════════════════════════════════════════
        if self.include_wavelet:
            _add_wavelet_features(features, close)
        if self.include_fft:
            _add_fft_features(features, close)

        return features


# ── 辅助函数 ──────────────────────────────────────────────

def _safe_log_return(arr: np.ndarray, lag: int) -> np.ndarray:
    result = np.full(len(arr), np.nan)
    result[lag:] = np.log(arr[lag:] / arr[:-lag])
    return result


def _safe_pct(arr: np.ndarray, lag: int) -> np.ndarray:
    result = np.full(len(arr), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        result[lag:] = arr[lag:] / arr[:-lag] - 1.0
    return result


def _safe_diff(arr: np.ndarray) -> np.ndarray:
    result = np.full(len(arr), np.nan)
    result[1:] = arr[1:] - arr[:-1]
    return result


def _rolling_rank(arr: np.ndarray, window: int) -> np.ndarray:
    """滚动排名: rank(arr[t-window:t]) / window → [0, 1]"""
    n = len(arr)
    result = np.full(n, np.nan)
    for i in range(window, n):
        window_slice = arr[i - window : i]
        valid = window_slice[~np.isnan(window_slice)]
        if len(valid) == 0:
            continue
        rank_val = np.sum(valid < arr[i]) / len(valid)
        result[i] = rank_val
    return result


def _add_wavelet_features(features: pd.DataFrame, close: np.ndarray):
    """添加小波变换特征 (需 pywt 包)。"""
    try:
        import pywt  # noqa: F401
    except ImportError:
        return

    # DWT 一级分解 → 近似系数 + 细节系数 (各约 n/2 长度)
    coeffs = pywt.dwt(close[~np.isnan(close)], "db4")
    n = len(close)
    approx = np.full(n, np.nan)
    detail = np.full(n, np.nan)
    half = len(coeffs[0])
    # 上采样到原始分辨率
    approx_up = np.repeat(coeffs[0], 2)[:n]
    detail_up = np.repeat(coeffs[1], 2)[:n]
    features["wavelet_approx"] = approx_up
    features["wavelet_detail"] = detail_up


def _add_fft_features(features: pd.DataFrame, close: np.ndarray):
    """添加 FFT 频率特征。"""
    clean = close[~np.isnan(close)]
    if len(clean) < 10:
        return
    fft = np.abs(np.fft.rfft(clean))
    # 取前 3 个主要频率成分 (跳过 DC)
    top_indices = np.argsort(fft[1:])[-3:] + 1
    n = len(close)
    for rank, idx in enumerate(top_indices):
        features[f"fft_freq_{rank}"] = np.full(n, float(fft[idx]))
        features[f"fft_phase_{rank}"] = np.full(n, float(np.angle(np.fft.rfft(clean)[idx])))
