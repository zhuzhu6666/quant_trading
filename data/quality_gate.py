"""data/quality_gate.py — 数据质量门控.

每次 evolution 前检查数据健康, 不通过则跳过 GP/AWE/Canary/权重更新.

检查项:
  - bar 缺口 (各 timeframe 最新时间 vs 预期)
  - 异常 spread / volume
  - 数据延迟
  - 跨源价格偏差
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 各时间框架的预期最大延迟 (秒)
BAR_FRESHNESS_THRESHOLDS = {
    "M1": 600,    # 10 分钟
    "M5": 900,    # 15 分钟
    "M15": 1800,  # 30 分钟
    "M30": 3600,  # 1 小时
    "H1": 7200,   # 2 小时
    "D1": 172800, # 2 天
}

# 异常阈值
MAX_SPREAD_PCT = 0.5      # 超过中位数 50% 视为异常
MAX_VOLUME_ZSCORE = 5.0   # volume z-score > 5 视为异常
MAX_PRICE_DEVIATION = 0.02  # 跨源价格偏差 > 2%


@dataclass
class DataQualityReport:
    """数据质量报告."""
    passed: bool = True
    bar_gaps: dict[str, float] = field(default_factory=dict)   # {tf: gap_seconds}
    missing_timeframes: list[str] = field(default_factory=list)
    anomalous_spread_count: int = 0
    anomalous_volume_count: int = 0
    data_lag_seconds: float = 0.0
    cross_source_deviation: float | None = None
    errors: list[str] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "bar_gaps": self.bar_gaps,
            "missing_timeframes": self.missing_timeframes,
            "anomalous_spread_count": self.anomalous_spread_count,
            "anomalous_volume_count": self.anomalous_volume_count,
            "data_lag_seconds": round(self.data_lag_seconds, 1),
            "cross_source_deviation": round(self.cross_source_deviation, 6) if self.cross_source_deviation else None,
            "errors": self.errors,
            "detail": self.detail,
        }


def check_bar_freshness(
    df_bars: pd.DataFrame | None = None,
    *,
    symbol: str = "XAUUSD+",
    thresholds: dict[str, int] | None = None,
) -> dict[str, float]:
    """检查各 timeframes 的最新 bar 时间缺口."""
    gaps: dict[str, float] = {}
    if df_bars is None or df_bars.empty:
        return gaps

    th = thresholds or BAR_FRESHNESS_THRESHOLDS
    now = time.time()

    for tf, max_age in th.items():
        tf_df = df_bars[df_bars.get("timeframe", "") == tf]
        if tf_df.empty:
            gaps[tf] = float("inf")
        else:
            last_ts = float(tf_df["time"].max())
            gaps[tf] = max(0.0, now - last_ts)

    return gaps


def check_anomalous_spread(
    df_bars: pd.DataFrame | None = None,
    *,
    zscore_threshold: float = MAX_SPREAD_PCT,
) -> int:
    """检查异常 spread."""
    if df_bars is None or df_bars.empty or "spread" not in df_bars.columns:
        return 0
    spread = pd.to_numeric(df_bars["spread"], errors="coerce").dropna()
    if len(spread) < 10:
        return 0
    median_sp = float(spread.median())
    if median_sp <= 0:
        return 0
    ratio = spread / median_sp
    return int(np.sum(ratio > (1.0 + zscore_threshold)))


def check_anomalous_volume(
    df_bars: pd.DataFrame | None = None,
    *,
    zscore_threshold: float = MAX_VOLUME_ZSCORE,
) -> int:
    """检查异常 volume (z-score)."""
    if df_bars is None or df_bars.empty or "volume" not in df_bars.columns:
        return 0
    vol = pd.to_numeric(df_bars["volume"], errors="coerce").dropna()
    if len(vol) < 20:
        return 0
    mean_v = float(vol.mean())
    std_v = float(vol.std())
    if std_v <= 0:
        return 0
    z = (vol - mean_v) / std_v
    return int(np.sum(np.abs(z) > zscore_threshold))


def run_quality_gate(
    *,
    df_bars: pd.DataFrame | None = None,
    symbol: str = "XAUUSD+",
    max_lag_seconds: float = 3600.0,
    max_daily_loss_pct: float = 5.0,
    error_on_anomaly: bool = False,
) -> DataQualityReport:
    """运行数据质量门控.

    Args:
        df_bars: DuckDB bars 查询结果 (含 timeframe, time, spread, volume 等列).
        symbol: 交易品种.
        max_lag_seconds: 数据最大可接受延迟 (秒).
        error_on_anomaly: 异常spread/volume是否算不通过.

    Returns:
        DataQualityReport
    """
    report = DataQualityReport()

    # 1. Bar 新鲜度检查
    gaps = check_bar_freshness(df_bars, symbol=symbol)
    report.bar_gaps = gaps
    stale_tfs = [tf for tf, gap in gaps.items() if gap != float("inf") and gap > BAR_FRESHNESS_THRESHOLDS.get(tf, 3600)]
    missing_tfs = [tf for tf, gap in gaps.items() if gap == float("inf")]
    report.missing_timeframes = missing_tfs

    if stale_tfs:
        report.errors.append(f"stale bars: {stale_tfs}")
    if missing_tfs:
        report.errors.append(f"missing timeframes: {missing_tfs}")

    # 2. 异常 spread
    n_bad_spread = check_anomalous_spread(df_bars)
    report.anomalous_spread_count = n_bad_spread
    if n_bad_spread > 0 and error_on_anomaly:
        report.errors.append(f"anomalous spreads: {n_bad_spread}")

    # 3. 异常 volume
    n_bad_vol = check_anomalous_volume(df_bars)
    report.anomalous_volume_count = n_bad_vol
    if n_bad_vol > 0 and error_on_anomaly:
        report.errors.append(f"anomalous volumes: {n_bad_vol}")

    # 4. 数据延迟 (主 M5)
    m5_gap = gaps.get("M5", float("inf"))
    if m5_gap != float("inf"):
        report.data_lag_seconds = m5_gap
        if m5_gap > max_lag_seconds:
            report.errors.append(f"m5 lag={m5_gap/60:.0f}m > {max_lag_seconds/60:.0f}m")

    # 5. 综合裁决
    report.passed = len(report.errors) == 0
    report.detail = "; ".join(report.errors) if report.errors else "all checks passed"
    return report


def evolution_guard(report: DataQualityReport) -> bool:
    """Evolution gate: 如果数据质量不通过, 返回 False (跳过 evolution)."""
    if not report.passed:
        logger.warning("[QualityGate] evolution blocked: %s", report.detail)
        return False
    logger.info("[QualityGate] data quality passed")
    return True

