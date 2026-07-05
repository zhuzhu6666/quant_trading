"""
Factor Registry — 因子注册表

所有因子函数在此注册。每个因子函数签名:
    func(df: pd.DataFrame) -> np.ndarray
"""

from collections.abc import Callable
import logging

import numpy as np
import pandas as pd

from alpha.technical_indicators import adx_wilder, atr_wilder

logger = logging.getLogger(__name__)


class FactorRegistry:
    """因子注册表"""

    def __init__(self):
        self._factors: dict[str, Callable] = {}

    def register(self, name: str, description: str = ""):
        """装饰器：注册因子函数"""
        def decorator(func):
            self._factors[name] = func
            func._factor_name = name
            func._factor_desc = description
            logger.debug(f"Registered factor: {name}")
            return func
        return decorator

    def get(self, name: str) -> Callable | None:
        return self._factors.get(name)

    def list(self) -> list[str]:
        return list(self._factors.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._factors


# 全局注册表
factor_registry = FactorRegistry()


# =============================================================================
# 内置因子
# =============================================================================

@factor_registry.register("rsi_14", "RSI(14)")
def factor_rsi_14(df):
    """RSI 14周期"""
    close = df["close"].values
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gain).ewm(span=14, min_periods=14).mean().values
    avg_loss = pd.Series(loss).ewm(span=14, min_periods=14).mean().values
    rs = np.divide(avg_gain, avg_loss, out=np.full_like(avg_gain, 100.0), where=avg_loss != 0)
    return 100 - 100 / (1 + rs)


@factor_registry.register("macd_hist", "MACD柱 (12,26,9)")
def factor_macd_hist(df):
    """MACD Histogram"""
    close = df["close"].values
    ema12 = pd.Series(close).ewm(span=12).mean().values
    ema26 = pd.Series(close).ewm(span=26).mean().values
    macd = ema12 - ema26
    signal = pd.Series(macd).ewm(span=9).mean().values
    return macd - signal


@factor_registry.register("adx", "ADX(14) 趋势强度")
def factor_adx(df):
    """Average Directional Index using the shared Wilder implementation."""
    adx, _, _ = adx_wilder(df["high"].values, df["low"].values, df["close"].values, period=14)
    return adx


@factor_registry.register("bb_width", "布林带宽度(20,2)")
def factor_bb_width(df):
    """Bollinger Band Width"""
    close = df["close"].values
    sma = pd.Series(close).rolling(20).mean().values
    std = pd.Series(close).rolling(20).std().values
    bb_top = sma + 2 * std
    bb_bot = sma - 2 * std
    return np.divide(bb_top - bb_bot, sma, out=np.zeros_like(sma), where=sma != 0)


@factor_registry.register("di_spread", "ADX方向差 (DI+ - DI-)")
def factor_di_spread(df):
    """DI+ minus DI- using the shared Wilder implementation."""
    _, plus_di, minus_di = adx_wilder(df["high"].values, df["low"].values, df["close"].values, period=14)
    return plus_di - minus_di


@factor_registry.register("stoch_k", "Stochastic %K (14,3,3)")
def factor_stoch_k(df):
    """Stochastic %K"""
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    n = len(close)
    k = np.full(n, np.nan)
    for i in range(13, n):
        h14 = high[i-13:i+1].max()
        l14 = low[i-13:i+1].min()
        k[i] = (close[i] - l14) / (h14 - l14) * 100 if h14 != l14 else 50
    return k


@factor_registry.register("atr_ratio", "ATR(14)/Close")
def factor_atr_ratio(df):
    """ATR占价格比例"""
    close = df["close"].values
    atr = atr_wilder(df["high"].values, df["low"].values, close, period=14)
    return np.divide(atr, close, out=np.zeros_like(close), where=close != 0)


# =============================================================================
# P0-1 补因子 (2026-06-02) — 8 个新因子, 覆盖 TODO 列表
# 趋势: EMA slope / Supertrend
# 波动: Keltner width
# 量: OBV slope / Vol MA ratio
# 形态: Engulfing / Pin bar / Inside bar
# =============================================================================

@factor_registry.register("ema_slope", "EMA(20) 5-bar slope (标准化)")
def factor_ema_slope(df, period: int = 20, lookback: int = 5):
    """EMA 斜率: (EMA_now - EMA_{t-lookback}) / close, 趋势强度代理。

    范围: 通常 [-0.05, +0.05] (5 bar 内 5% 级别的变化)。
    正值 = 上升趋势加速, 负值 = 下降趋势加速。
    """
    close = df["close"].values
    ema = pd.Series(close).ewm(span=period, min_periods=period).mean().values
    n = len(ema)
    out = np.full(n, np.nan)
    for i in range(period + lookback - 1, n):
        prev = ema[i - lookback]
        if np.isnan(prev) or close[i] == 0:
            continue
        out[i] = (ema[i] - prev) / close[i]
    return out


@factor_registry.register("supertrend_str", "SuperTrend(10,3) 强度 (-1 ~ +1 continuous)")
def factor_supertrend_str(df, period: int = 10, multiplier: float = 3.0):
    """SuperTrend 强度因子: continuous 范围, 避免二值 std=0 问题。

    输出 = direction * (close - final_band) / ATR
    正值 = 价格在上轨上方, 趋势强; 负值 = 价格在下轨下方, 跌势强。
    0 = 价格在通道内, 趋势不明。
    """
    return _supertrend_strength_array(
        df["high"].values,
        df["low"].values,
        df["close"].values,
        period=period,
        multiplier=multiplier,
    )


def _supertrend_strength_array(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    *,
    period: int = 10,
    multiplier: float = 3.0,
) -> np.ndarray:
    """Return signed SuperTrend distance in ATR units."""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)
    out = np.full(n, np.nan)
    if n < period + 2:
        return out

    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr = pd.Series(tr).ewm(span=period, min_periods=period).mean().values

    hl2 = (high + low) / 2.0
    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr

    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    direction = 0.0

    for i in range(period, n):
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        if i == period or not np.isfinite(final_upper[i - 1]) or not np.isfinite(final_lower[i - 1]):
            final_upper[i] = basic_upper[i]
            final_lower[i] = basic_lower[i]
            direction = 1.0 if close[i] >= close[i - 1] else -1.0
            out[i] = 0.0
            continue

        prev_upper = final_upper[i - 1]
        prev_lower = final_lower[i - 1]
        final_upper[i] = (
            basic_upper[i]
            if basic_upper[i] < prev_upper or close[i - 1] > prev_upper
            else prev_upper
        )
        final_lower[i] = (
            basic_lower[i]
            if basic_lower[i] > prev_lower or close[i - 1] < prev_lower
            else prev_lower
        )

        if direction <= 0 and close[i] > final_upper[i]:
            direction = 1.0
        elif direction >= 0 and close[i] < final_lower[i]:
            direction = -1.0

        if direction > 0:
            out[i] = max(0.0, (close[i] - final_lower[i]) / atr[i])
        else:
            out[i] = -max(0.0, (final_upper[i] - close[i]) / atr[i])

    return out


@factor_registry.register("keltner_width", "Keltner 通道宽度 (EMA20 ± 1.5*ATR)")
def factor_keltner_width(df, period: int = 20, multiplier: float = 1.5):
    """Keltner 通道宽度: (上轨 - 下轨) / close。

    与 BB width 互补: BB width 用 std (对称), Keltner 用 ATR (非对称,
    对跳空敏感)。两个一起看 = 区分"波动率扩张"和"价格扩张"。
    """
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    n = len(close)
    if n < period + 1:
        return np.full(n, np.nan)

    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    atr = pd.Series(tr).ewm(span=period, min_periods=period).mean().values
    ema = pd.Series(close).ewm(span=period, min_periods=period).mean().values

    upper = ema + multiplier * atr
    lower = ema - multiplier * atr
    return np.divide(upper - lower, close, out=np.zeros_like(close), where=close != 0)


@factor_registry.register("obv_slope", "OBV 20-bar 斜率 (sign*vol 累计)")
def factor_obv_slope(df, lookback: int = 20):
    """OBV 20-bar 斜率: 归一化的 OBV 变化率, 量价配合代理。

    OBV 上升 + 价平/升 = 健康趋势; OBV 下降 + 价升 = 量价背离 (顶部信号)。
    """
    close = df["close"].values
    vol = df["volume"].values if "volume" in df.columns else np.zeros(len(close))
    n = len(close)
    if n < 2:
        return np.full(n, np.nan)

    sign = np.sign(np.diff(close, prepend=close[0]))
    obv = np.cumsum(sign * vol)

    out = np.full(n, np.nan)
    for i in range(lookback, n):
        prev = obv[i - lookback]
        if prev == 0 or np.isnan(prev):
            out[i] = 0.0
        else:
            out[i] = (obv[i] - prev) / abs(prev)
    return out


@factor_registry.register("vol_ma_ratio", "Volume / Volume_MA(20) - 1")
def factor_vol_ma_ratio(df, period: int = 20):
    """量比: 当前成交量 / 20-bar 均量 - 1。

    正值 = 放量, 负值 = 缩量。配合价格 = 突破/反转的强度证据。
    """
    vol = df["volume"].values if "volume" in df.columns else np.zeros(len(df))
    n = len(vol)
    if n < period:
        return np.full(n, np.nan)
    vol_ma = pd.Series(vol).rolling(period, min_periods=period).mean().values
    return np.divide(vol, vol_ma, out=np.zeros_like(vol), where=vol_ma != 0) - 1.0


@factor_registry.register("engulfing", "Engulfing 形态: +1 bullish / -1 bearish / 0 none")
def factor_engulfing(df):
    """看涨/看跌吞没形态分类信号。

    Bullish engulfing: 前阴后阳, 阳线实体完全覆盖阴线实体。
    Bearish engulfing: 前阳后阴, 阴线实体完全覆盖阳线实体。
    """
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    n = len(c)
    out = np.zeros(n)
    for i in range(1, n):
        prev_body_hi = max(o[i - 1], c[i - 1])
        prev_body_lo = min(o[i - 1], c[i - 1])
        cur_body_hi = max(o[i], c[i])
        cur_body_lo = min(o[i], c[i])
        prev_bearish = c[i - 1] < o[i - 1]
        cur_bullish = c[i] > o[i]
        prev_bullish = c[i - 1] > o[i - 1]
        cur_bearish = c[i] < o[i]
        # Bullish engulfing
        if prev_bearish and cur_bullish and cur_body_hi >= prev_body_hi and cur_body_lo <= prev_body_lo:
            out[i] = 1.0
        # Bearish engulfing
        elif prev_bullish and cur_bearish and cur_body_hi >= prev_body_hi and cur_body_lo <= prev_body_lo:
            out[i] = -1.0
    return out


@factor_registry.register("pin_bar", "Pin bar 形态: +1 bullish / -1 bearish / 0 none")
def factor_pin_bar(df, wick_ratio: float = 2.0):
    """Pin bar (锤子/流星): 一边影线 ≥ wick_ratio 倍实体, 另一边影线 ≤ 实体。

    Bullish pin (锤子): 长下影, 出现在支撑位 = 强反转信号。
    Bearish pin (流星): 长上影, 出现在阻力位 = 顶部信号。
    """
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    n = len(c)
    out = np.zeros(n)
    for i in range(n):
        body = abs(c[i] - o[i])
        upper_wick = h[i] - max(o[i], c[i])
        lower_wick = min(o[i], c[i]) - l[i]
        if body < 1e-10:
            continue
        if lower_wick >= wick_ratio * body and upper_wick <= body:
            out[i] = 1.0  # bullish pin (hammer)
        elif upper_wick >= wick_ratio * body and lower_wick <= body:
            out[i] = -1.0  # bearish pin (shooting star)
    return out


@factor_registry.register("inside_bar", "Inside bar: 1=完全内包, 0=无")
def factor_inside_bar(df):
    """Inside bar: 当前 bar 的 high/low 完全在前一根 bar 范围内。

    通常代表"震荡/蓄势", 后市突破方向 = 关键信号。
    本因子只标记形态存在, 不预测方向 (方向由后续 bar 决定)。
    """
    h = df["high"].values
    l = df["low"].values
    n = len(h)
    out = np.zeros(n)
    for i in range(1, n):
        if h[i] <= h[i - 1] and l[i] >= l[i - 1]:
            out[i] = 1.0
    return out


# =============================================================================
# P0-3 跨资产 / 时段 / 事件距离因子 (2026-06-02)
# 数据源: data/external_loader.py (dxy / real_yield / gvz / vix / GLD/SLV/TLT / events)
# 接入: 因子函数检测 df 是否含 "dxy" 等列, 若无则降级返回 NaN
# =============================================================================


def _has_external(df: pd.DataFrame, cols: list[str]) -> bool:
    """检查 df 是否已通过 external_loader 对齐过"""
    if not isinstance(df, pd.DataFrame):
        return False
    return all(c in df.columns for c in cols)


def _standard_col(df: pd.DataFrame, col: str) -> np.ndarray | None:
    if isinstance(df, pd.DataFrame) and col in df.columns:
        return df[col].values
    return None


def _is_low_frequency_frame(df: pd.DataFrame, min_hours: float = 20.0) -> bool:
    """True when rolling/diff periods can be interpreted as daily/weekly rows."""
    if not isinstance(df, pd.DataFrame) or not isinstance(df.index, pd.DatetimeIndex):
        return False
    if len(df.index) < 3:
        return False
    diffs = pd.Series(df.index.sort_values()).diff().dropna()
    if diffs.empty:
        return False
    median_hours = diffs.median().total_seconds() / 3600.0
    return bool(median_hours >= min_hours)


def _rolling_corr_by_day(
    df: pd.DataFrame,
    x_col: str,
    y_col: str,
    period: int,
) -> np.ndarray:
    """Compute a daily rolling correlation without intraday lookahead."""
    n = len(df)
    out = np.full(n, np.nan)
    if not isinstance(df.index, pd.DatetimeIndex):
        return out

    daily_x: list[float] = []
    daily_y: list[float] = []
    current_day = None
    x = df[x_col].values
    y = df[y_col].values
    for i, ts in enumerate(df.index):
        day = pd.Timestamp(ts).normalize()
        if current_day is None or day != current_day:
            current_day = day
            daily_x.append(float(x[i]))
            daily_y.append(float(y[i]))
        else:
            daily_x[-1] = float(x[i])
            daily_y[-1] = float(y[i])

        if len(daily_x) < period:
            continue
        x_win = np.asarray(daily_x[-period:], dtype=float)
        y_win = np.asarray(daily_y[-period:], dtype=float)
        if np.isnan(x_win).any() or np.isnan(y_win).any():
            continue
        if x_win.std() < 1e-12 or y_win.std() < 1e-12:
            continue
        out[i] = float(np.corrcoef(x_win, y_win)[0, 1])
    return out


def _rolling_ratio_change_by_day(
    df: pd.DataFrame,
    numerator_col: str,
    denominator_col: str,
    lookback: int,
) -> np.ndarray:
    """Compute a daily ratio change without treating M5 bars as days."""
    n = len(df)
    out = np.full(n, np.nan)
    if not isinstance(df.index, pd.DatetimeIndex):
        return out

    daily_ratio: list[float] = []
    current_day = None
    numerator = df[numerator_col].values
    denominator = df[denominator_col].values
    for i, ts in enumerate(df.index):
        day = pd.Timestamp(ts).normalize()
        denom = float(denominator[i])
        ratio = float(numerator[i]) / denom if denom != 0 else np.nan
        if current_day is None or day != current_day:
            current_day = day
            daily_ratio.append(ratio)
        else:
            daily_ratio[-1] = ratio

        if len(daily_ratio) <= lookback:
            continue
        prev = daily_ratio[-lookback - 1]
        cur = daily_ratio[-1]
        if np.isnan(cur) or np.isnan(prev) or prev == 0:
            continue
        out[i] = (cur - prev) / prev
    return out


@factor_registry.register("dxy_corr_20", "20-bar rolling DXY (DTWEXBGS) 相关性")
def factor_dxy_corr_20(df, period: int = 20):
    """20-bar close 跟 DXY 的滚动相关系数。

    黄金理论上 DXY 强负相关 (DXY 升 → 黄金贬), corr ≈ -0.5 ~ -0.8.
    corr 绝对值突降 = 脱钩 (regime shift 信号)。
    需要 df 包含 'dxy' 列 (由 external_loader 注入)。
    """
    standard = _standard_col(df, "dxy_corr_20")
    if standard is not None:
        return standard
    if not _has_external(df, ["close", "dxy"]):
        return np.full(len(df), np.nan)
    if not _is_low_frequency_frame(df):
        return _rolling_corr_by_day(df, "close", "dxy", period)
    close = df["close"].values
    dxy = df["dxy"].values
    n = len(close)
    out = np.full(n, np.nan)
    for i in range(period, n):
        c_win = close[i - period + 1 : i + 1]
        d_win = dxy[i - period + 1 : i + 1]
        # 任一全 NaN 跳过
        if np.isnan(c_win).any() or np.isnan(d_win).any():
            continue
        if c_win.std() < 1e-12 or d_win.std() < 1e-12:
            continue
        out[i] = float(np.corrcoef(c_win, d_win)[0, 1])
    return out


@factor_registry.register("slv_gld_ratio", "SLV/GLD ratio 5-bar 变化率")
def factor_slv_gld_ratio(df, lookback: int = 5):
    """白银/黄金 ETF 价格比的 5-bar 变化率。

    比率上升 = 白银相对强 (工业/风险偏好回归), 对黄金是中性偏弱信号。
    比率下降 = 白银相对弱 (避险/降息预期), 对黄金是中性偏强信号。
    """
    standard = _standard_col(df, "slv_gld_ratio_5d")
    if standard is not None:
        return standard
    standard = _standard_col(df, "slv_gld_ratio")
    if standard is not None:
        return standard
    if not _has_external(df, ["SLV", "GLD"]):
        return np.full(len(df), np.nan)
    if not _is_low_frequency_frame(df):
        return _rolling_ratio_change_by_day(df, "SLV", "GLD", lookback)
    slv = df["SLV"].values
    gld = df["GLD"].values
    n = len(df)
    ratio = np.divide(slv, gld, out=np.full(n, np.nan), where=gld != 0)
    out = np.full(n, np.nan)
    for i in range(lookback, n):
        if np.isnan(ratio[i]) or np.isnan(ratio[i - lookback]) or ratio[i - lookback] == 0:
            continue
        out[i] = (ratio[i] - ratio[i - lookback]) / ratio[i - lookback]
    return out


@factor_registry.register("real_yield_chg", "10Y real yield 5-bar 变化 (bp)")
def factor_real_yield_chg(df, lookback: int = 5):
    """10Y real yield (DFII10) 5-bar 变化, 单位百分点。

    Real yield 是黄金最直接的"对手盘": 上升 = 黄金承压, 下降 = 黄金利好。
    5-bar 变化 ≥ +10bp = 重大利空, ≤ -10bp = 重大利好。
    """
    standard = _standard_col(df, "real_yield_chg_5d")
    if standard is not None:
        return standard
    standard = _standard_col(df, "real_yield_chg")
    if standard is not None:
        return standard
    if not _has_external(df, ["real_yield_10y"]) or not _is_low_frequency_frame(df):
        return np.full(len(df), np.nan)
    ry = df["real_yield_10y"].values
    n = len(ry)
    out = np.full(n, np.nan)
    for i in range(lookback, n):
        if np.isnan(ry[i]) or np.isnan(ry[i - lookback]):
            continue
        out[i] = (ry[i] - ry[i - lookback]) * 100  # % → bp
    return out


@factor_registry.register("hours_to_fomc", "FOMC 事件时间桶 (0=发布窗口)")
def factor_hours_to_fomc(df):
    """FOMC 事件接近度上下文。

    值来自点时事件日历的签名小时桶: 负数=事件前, 0=发布窗口,
    正数=事件后, NaN=已知事件窗口外。它描述事件状态, 不表达多空方向。
    """
    standard = _standard_col(df, "hours_to_fomc")
    if standard is not None:
        return standard
    return _compute_event_distance(df, "evt_fomc")


@factor_registry.register("hours_to_nfp", "NFP 事件时间桶 (0=发布窗口)")
def factor_hours_to_nfp(df):
    """NFP 事件接近度上下文, 只供 gate/sizing/学习使用。"""
    standard = _standard_col(df, "hours_to_nfp")
    if standard is not None:
        return standard
    return _compute_event_distance(df, "evt_nfp")


def _infer_bars_per_day(df) -> int:
    """BUG-22 (audit 2026-06-04): 从 df.index 推 bars_per_day, 替代 hardcode 96

    若 df.index 是 DatetimeIndex 且有 freq, 用 24*60/freq_minutes。
    否则 fallback 96 (M15)。
    """
    try:
        idx = df.index
        if hasattr(idx, "inferred_freq") and idx.inferred_freq:
            minutes = pd.Timedelta(idx.inferred_freq).total_seconds() / 60
            if minutes > 0:
                return int(24 * 60 / minutes)
    except Exception:
        pass
    return 96  # M15 fallback


def _compute_event_distance(df: pd.DataFrame, evt_col: str) -> np.ndarray:
    """通用事件距离计算: 找下一次事件日, 返回天数 (浮点, 0=当天)。

    算法:
      1. evt_col == 1 的 bar 标为事件日
      2. 每个 bar 找"未来"最近一个事件日
      3. (event_ts - current_ts) / 1day 取整
    """
    n = len(df)
    out = np.full(n, np.nan)
    if not _has_external(df, [evt_col]):
        return out
    if not isinstance(df.index, pd.DatetimeIndex):
        return out

    flags = df[evt_col].values.astype(int)
    # 找事件日索引
    event_indices = np.where(flags == 1)[0]
    if len(event_indices) == 0:
        return out

    # 累计天数 (BUG-22 audit 2026-06-04: bars_per_day hardcode 96, 应从 df 推)
    bars_per_day = _infer_bars_per_day(df)
    for i in range(n):
        # 找最近未来事件 (index 差)
        future = event_indices[event_indices >= i]
        if len(future) == 0:
            out[i] = np.nan  # 已知事件之外
        else:
            dist_bars = int(future[0] - i)
            out[i] = dist_bars / bars_per_day
    return out


@factor_registry.register("hour_utc", "UTC 小时 (0-23), 时段特征")
def factor_hour_utc(df):
    """UTC 小时 (0-23), 时段特征。

    黄金: 亚盘 (0-7) / 欧盘 (7-15) / 美盘 (15-22) / 夜盘 (22-24)
    不同小时波动率和趋势持续性差异显著 (M15 已有发现)。
    范围: 0 ~ 23, 可直接当连续特征 (周期性用 sin/cos 二次变换更佳)。
    """
    if not isinstance(df, pd.DataFrame) or not isinstance(df.index, pd.DatetimeIndex):
        return np.full(len(df), np.nan)
    return df.index.hour.astype(np.float64).values


@factor_registry.register("day_of_week", "星期 (0=Mon, 4=Fri), 周内效应")
def factor_day_of_week(df):
    """星期 (0=Mon, 4=Fri, 5-6=周末无数据)。

    周一周五效应 / 跨周末跳空风险, 简单连续编码。
    """
    if not isinstance(df, pd.DataFrame) or not isinstance(df.index, pd.DatetimeIndex):
        return np.full(len(df), np.nan)
    return df.index.dayofweek.astype(np.float64).values


# =============================================================================
# P0-ETF 因子 (2026-06-03) — GLD / SLV 持仓量与资金流代理
# 数据源: data/external_loader.py → etf_holdings 表 (前向填充)
# 逻辑: 持仓量上升 = 机构净流入 (看多黄金); 持仓量下降 = 净赎回 (看空)
# 实际接入: 等 etf_holdings 表被 SPDR/SLV 真实持仓数据填充后激活
# 当前 (空表): 因子会全部返回 NaN, IC 报告里会标 N/A, 不影响其他因子
# =============================================================================


@factor_registry.register("gld_tonnes_chg_5d", "GLD 5 日持仓量变化 (吨)")
def factor_gld_tonnes_chg_5d(df):
    """GLD (SPDR Gold Shares) 5 日持仓量变化, 单位吨。

    逻辑: GLD 每天公布总持仓 (金衡盎司), 转吨 (÷32150.7) 后 5d diff。
    正值 = 机构 5 日净增持 = 黄金看多信号 (Jim Rogers / WGC 一致结论)。
    典型: GLD 5d 净增 5 吨 ≈ 1.6 亿美元流入。
    需要 df 包含 'gld_tonnes' 列 (由 external_loader 注入)。
    """
    standard = _standard_col(df, "GLD_tonnes_chg_5d")
    if standard is not None:
        return standard
    if not _has_external(df, ["GLD_tonnes"]) or not _is_low_frequency_frame(df):
        return np.full(len(df), np.nan)
    return pd.Series(df["GLD_tonnes"].values).diff(5).values


@factor_registry.register("gld_tonnes_chg_20d", "GLD 20 日持仓量变化 (吨)")
def factor_gld_tonnes_chg_20d(df):
    """GLD 20 日持仓量变化, 单位吨。

    月度级别的资金流向, 慢信号。适合跟短期价格回归一起做反转策略。
    极值: GLD 20d 净减 > 30 吨 = 长期资本撤离, 黄金触底信号。
    """
    standard = _standard_col(df, "GLD_tonnes_chg_20d")
    if standard is not None:
        return standard
    if not _has_external(df, ["GLD_tonnes"]) or not _is_low_frequency_frame(df):
        return np.full(len(df), np.nan)
    return pd.Series(df["GLD_tonnes"].values).diff(20).values


@factor_registry.register("gld_tonnes_pct_20d", "GLD 20 日持仓变化百分比")
def factor_gld_tonnes_pct_20d(df):
    """GLD 20 日持仓量变化百分比。

    相对量纲, 跨时间可比。背景: GLD 历史上总持仓在 600-1100 吨区间波动,
    20d 1% 变化 ≈ 6-11 吨净流动, 是显著事件。
    """
    standard = _standard_col(df, "GLD_tonnes_pct_20d")
    if standard is not None:
        return standard
    if not _has_external(df, ["GLD_tonnes"]) or not _is_low_frequency_frame(df):
        return np.full(len(df), np.nan)
    return pd.Series(df["GLD_tonnes"].values).pct_change(20).values * 100


@factor_registry.register("gld_tonnes_zscore_60d", "GLD 持仓 60d z-score (极值反转信号)")
def factor_gld_tonnes_zscore_60d(df):
    """GLD 持仓量 60 日滚动 z-score。

    |z| > 2 = 极值, 历史上对应 1-3 个月的反转 (均值回归)。
    极值正: 持仓异常高 → 拥挤交易, 短期回调风险
    极值负: 持仓异常低 → 投降, 长期底部信号
    """
    standard = _standard_col(df, "GLD_tonnes_zscore_60d")
    if standard is not None:
        return standard
    if not _has_external(df, ["GLD_tonnes"]) or not _is_low_frequency_frame(df):
        return np.full(len(df), np.nan)
    s = pd.Series(df["GLD_tonnes"].values)
    roll = s.rolling(60, min_periods=20)
    mean = roll.mean()
    std = roll.std()
    z = np.where(std > 1e-9, (s - mean) / std, 0.0)
    return z


@factor_registry.register("slv_tonnes_chg_20d", "SLV 20 日持仓量变化 (吨)")
def factor_slv_tonnes_chg_20d(df):
    """SLV (iShares Silver Trust) 20 日持仓量变化, 单位吨。

    银的 ETF 持仓是黄金的强相关但更波动的代理 (散户参与度高)。
    银/金持仓比 (silver_gold_holdings_ratio) 看相对配置, 见 'silver_gold_holdings_ratio' 因子。
    """
    standard = _standard_col(df, "SLV_tonnes_chg_20d")
    if standard is not None:
        return standard
    if not _has_external(df, ["SLV_tonnes"]) or not _is_low_frequency_frame(df):
        return np.full(len(df), np.nan)
    return pd.Series(df["SLV_tonnes"].values).diff(20).values


@factor_registry.register("silver_gold_holdings_ratio", "SLV/GLD 持仓量比 5d 变化")
def factor_silver_gold_holdings_ratio(df, lookback: int = 5):
    """SLV 持仓 / GLD 持仓 的 5 日变化率。

    比率上升 = 银相对金强势, 工业/风险偏好回归 (对黄金中性偏弱)
    比率下降 = 银相对金弱势, 避险/降息预期 (对黄金中性偏强)

    历史上金银 ETF 持仓比突破 +5% 5d, 跟黄金短期回调 -0.5%~-1% 同步。
    """
    if not _has_external(df, ["SLV_tonnes", "GLD_tonnes"]) or not _is_low_frequency_frame(df):
        return np.full(len(df), np.nan)
    s_slv = df["SLV_tonnes"].values
    s_gld = df["GLD_tonnes"].values
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(s_gld != 0, s_slv / s_gld, np.nan)
    return pd.Series(ratio).pct_change(lookback).values * 100


# =============================================================================
# P0-CB 因子 (2026-06-03) — 央行黄金月度净买入
# 数据源: data/external_loader.py → cb_gold 表 (前向填充到日度)
# 逻辑: 央行净买入是黄金最长期的需求支撑 (中国/俄罗斯/印度/土耳其)
# 关键观点: 央行买金跟价格脱钩, 跟地缘政治+去美元化绑定
# =============================================================================


@factor_registry.register("cb_total_chg_3m", "全球央行 3 月累计净买入 (吨)")
def factor_cb_total_chg_3m(df):
    """全球央行 3 月累计净买入黄金, 单位吨。

    央行买金是黄金长期需求的核心驱动 (>年 1000 吨需求侧)。
    3m 累计 > 200 吨 = 强势支撑 (尤其央行净买不是短线博弈, 是结构性)。
    3m 累计 < 50 吨 = 需求侧疲软, 黄金易回调。
    """
    if not _has_external(df, ["cb_total_chg_3m"]):
        return np.full(len(df), np.nan)
    return df["cb_total_chg_3m"].values


@factor_registry.register("cb_china_chg_3m", "中国央行 3 月累计净买入 (吨)")
def factor_cb_china_chg_3m(df):
    """中国央行 (PBOC) 3 月累计净买入黄金, 单位吨。

    PBOC 2022-2024 持续月净买 10-30 吨, 是金价 1900→2400 关键驱动力。
    月报通常滞后 1 个月 (下月 7 号左右公布)。
    """
    if not _has_external(df, ["cb_china_chg_3m"]):
        return np.full(len(df), np.nan)
    return df["cb_china_chg_3m"].values


@factor_registry.register("cb_russia_chg_3m", "俄罗斯央行 3 月累计净买入 (吨)")
def factor_cb_russia_chg_3m(df):
    """俄罗斯央行 (CBR) 3 月累计净买入黄金, 单位吨。

    2022 制裁后俄央行暂停公布, 2023 恢复。
    跟中国同步 = 东方阵营去美元化指标。
    """
    if not _has_external(df, ["cb_russia_chg_3m"]):
        return np.full(len(df), np.nan)
    return df["cb_russia_chg_3m"].values


@factor_registry.register("cb_china_3m_zscore", "中国央行 3m 净买 z-score (极值信号)")
def factor_cb_china_3m_zscore(df, lookback: int = 60):
    """中国央行 3m 净买 60 日 z-score。

    历史中国央行 3m 净买 z-score > 1.5 = 强支撑 (持续累积)
    < -1.5 = 停止买入 = 利空黄金

    数据 freq = 月度, 但对齐到日度后会有大量重复值 (forward fill)。
    z-score 跨月度窗口的滚动能避免月内重复影响。
    """
    standard = _standard_col(df, "cb_china_3m_zscore")
    if standard is not None:
        return standard
    if not _has_external(df, ["cb_china_chg_3m"]):
        return np.full(len(df), np.nan)
    if not _is_low_frequency_frame(df):
        return np.full(len(df), np.nan)
    s = pd.Series(df["cb_china_chg_3m"].values)
    roll = s.rolling(lookback, min_periods=10)
    mean = roll.mean()
    std = roll.std()
    z = np.where(std > 1e-9, (s - mean) / std, 0.0)
    return z


# =============================================================================
# P0 衍生因子 (2026-06-03) — 实际利率 percentile rank (BUG-3 修复后多周期 IC 优化)
# 旧版只算 5-bar 变化, IC=0.022; 新版加 percentile rank 跟历史比, 预期 IC 0.04-0.06
# =============================================================================


@factor_registry.register("real_yield_pct_rank", "实际利率 5y percentile rank")
def factor_real_yield_pct_rank(df, lookback: int = 1260):
    """10Y 实际利率 (DFII10) 5y 滚动 percentile rank (5y × 252d ≈ 1260 天)。

    原理: 实际利率是黄金的反向驱动 (上升 = 黄金承压)。
    把绝对水平归一化到 0-1 百分位, 看"当前实际利率在历史中贵不贵"。
    极值 (>0.8): 实际利率历史高位 = 黄金强压制
    极低 (<0.2): 实际利率历史低位 = 黄金强支撑

    IC 报告: 5-bar 实际利率变化 IC=0.022 → 5y percentile rank IC 预期 0.05+
    """
    standard = _standard_col(df, "real_yield_pct_rank_5y")
    if standard is not None:
        return standard
    standard = _standard_col(df, "real_yield_pct_rank")
    if standard is not None:
        return standard
    if not _has_external(df, ["real_yield_10y"]) or not _is_low_frequency_frame(df):
        return np.full(len(df), np.nan)
    s = pd.Series(df["real_yield_10y"].values)
    out = np.full(len(df), np.nan)
    for i in range(lookback, len(df)):
        window = s.iloc[i - lookback + 1: i + 1].dropna()
        if len(window) < 60:
            continue
        cur = s.iloc[i]
        if pd.isna(cur):
            continue
        out[i] = (window < cur).sum() / len(window) * 100
    return out


# =============================================================================
# P0-COT 因子 (2026-06-03) — CFTC 黄金 COT 持仓 (周度, 真实数据)
# 数据源: scripts/load_cot_gold.py → cot_gold 表 (周度, forward fill)
# 关键逻辑:
#   - Managed Money (非商业/投机): 跟金价同向 (追涨杀跌)
#   - Producer/Merchant (商业/对冲): 跟金价反向 (顶部做空套保, 底部平仓)
#   - COT 极值反转: |mm_net| z-score 极端时是反转信号
# =============================================================================


@factor_registry.register("cot_mm_net", "COT Managed Money 净持仓 (合约)")
def factor_cot_mm_net(df):
    """CFTC 黄金 COT — Managed Money (投机) 净持仓 = mm_long - mm_short。

    解读: 投机者净多 = 看多情绪强。历史上跟金价同向。
    数值越大越看多, 越负越看空。
    """
    if not _has_external(df, ["cot_mm_net"]):
        return np.full(len(df), np.nan)
    return df["cot_mm_net"].values


@factor_registry.register("cot_mm_net_pct_oi", "COT MM 净持仓 / 总持仓 %")
def factor_cot_mm_net_pct_oi(df):
    """COT MM 净持仓 / Open Interest (标准化到 %).

    解读: 跟总持仓比, 消除合约数变化影响。0.3+ = 投机者强烈看多, -0.3- = 强烈看空。
    """
    if not _has_external(df, ["cot_mm_net_pct_oi"]):
        return np.full(len(df), np.nan)
    return df["cot_mm_net_pct_oi"].values


@factor_registry.register("cot_mm_net_chg_4w", "COT MM 净持仓 4 周变化")
def factor_cot_mm_net_chg_4w(df):
    """COT MM 净持仓 4 周变化 (投机者加仓/减仓速度).

    解读: 正值 = 投机者 4 周累计加仓 (看多强化)
    负值 = 投机者 4 周累计减仓 (看多削弱 / 反向)
    极值 (>0.2 OI): 拥挤, 反转风险
    """
    standard = _standard_col(df, "cot_mm_net_chg_4w")
    if standard is not None:
        return standard
    if not _has_external(df, ["cot_mm_net_pct_oi"]) or not _is_low_frequency_frame(df, min_hours=24.0 * 5.0):
        return np.full(len(df), np.nan)
    return pd.Series(df["cot_mm_net_pct_oi"].values).diff(4).values


@factor_registry.register("cot_mm_net_zscore_52w", "COT MM 净持仓 52w z-score (极值反转)")
def factor_cot_mm_net_zscore_52w(df):
    """COT MM 净持仓 52 周滚动 z-score。

    |z| > 1.5: 历史上对应 1-3 个月的反转
    极值正: 投机者极端看多 → 拥挤, 短期回调风险
    极值负: 投机者极端看空 → 投降, 长期底部信号
    """
    standard = _standard_col(df, "cot_mm_net_zscore_52w")
    if standard is not None:
        return standard
    if not _has_external(df, ["cot_mm_net_pct_oi"]) or not _is_low_frequency_frame(df, min_hours=24.0 * 5.0):
        return np.full(len(df), np.nan)
    s = pd.Series(df["cot_mm_net_pct_oi"].values)
    roll = s.rolling(52, min_periods=12)
    z = (s - roll.mean()) / roll.std()
    return z.values


@factor_registry.register("cot_pm_net", "COT Producer/Merchant 净持仓 (对冲)")
def factor_cot_pm_net(df):
    """CFTC 黄金 COT — Producer/Merchant (商业/对冲) 净持仓。

    解读: 商业对冲者跟金价反向。极值负 (套保空头) 通常是底部信号,
    极值正 (套保多头) 通常是顶部信号。
    """
    if not _has_external(df, ["cot_pm_net"]):
        return np.full(len(df), np.nan)
    return df["cot_pm_net"].values


@factor_registry.register("cot_extreme_signal", "COT 极端反转综合信号 (mm+pm 极值)")
def factor_cot_extreme_signal(df):
    """COT 极值反转综合信号: 投机者极端看多 + 商业极端套保 = 顶部

    信号构造:
      mm_z = cot_mm_net_pct_oi z-score
      pm_z = cot_pm_net z-score (反向化, 套保空头越多越负)
      signal = mm_z - pm_z (双重极值)
    signal > 1.5: 顶部 (投机者极多 + 商业极空)
    signal < -1.5: 底部 (投机者极空 + 商业极多)

    黄金历史: 2020-08 投机者 +260k → 黄金顶部 2075
              2018-08 投机者 -100k → 黄金底部 1160
    """
    if not _has_external(df, ["cot_mm_net_pct_oi", "cot_pm_net"]) or not _is_low_frequency_frame(df, min_hours=24.0 * 5.0):
        return np.full(len(df), np.nan)
    mm_s = pd.Series(df["cot_mm_net_pct_oi"].values)
    pm_s = pd.Series(df["cot_pm_net"].values)

    mm_roll = mm_s.rolling(52, min_periods=12)
    pm_roll = pm_s.rolling(52, min_periods=12)

    mm_z = (mm_s - mm_roll.mean()) / mm_roll.std()
    pm_z = (pm_s - pm_roll.mean()) / pm_roll.std()

    raw = (mm_z - pm_z).values
    out = np.zeros(len(df), dtype=float)
    out[raw > 1.5] = -1.0
    out[raw < -1.5] = 1.0
    out[np.isnan(raw)] = np.nan
    return out
