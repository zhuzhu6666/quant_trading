"""StreamingFactorEngine — 流式因子计算引擎。

取代 FactorEngine 的 batch 模式，改为每根 bar 增量计算。
所有因子计算失败时独立处理，不互相影响。

设计文档: docs/architecture.md
"""

import logging
import math
from collections import deque

import numpy as np
import pandas as pd

from alpha.registry import factor_registry

logger = logging.getLogger(__name__)


class StreamingFactorEngine:
    """流式因子计算引擎。

    维护滚动 bar 缓存，每 append 一根 bar 就重算所有因子。
    支持增量计算：EMA/mean 类因子只递推，全量因子按需重算。

    用法:
        engine = StreamingFactorEngine(max_buffer=200)
        for bar in bars:
            factor_values = engine.append_bar(bar)
            if engine.is_warm:
                print(factor_values)
    """

    # 所有因子所需最小 bar 数（取 max 安全值）
    MIN_BARS = 50

    def __init__(self, max_buffer: int = 200, factor_runtime_config: dict | None = None):
        self._buffer: deque[dict] = deque(maxlen=max_buffer)
        self._factor_cache: dict[str, float | None] = {}
        self._available_factors: list[str] = list(factor_registry.list())
        self._incremental_state: dict[str, float] = {}
        self._warm: bool = False
        self._factor_runtime_config: dict[str, dict] = dict(factor_runtime_config or {})
        # Ensure restored shadow factors do not enter the live voting/calculation path.
        self.refresh_factor_list()

    # ── 核心接口 ────────────────────────────────────────

    def append_bar(self, bar: dict) -> dict[str, float | None]:
        """追加一根 bar，重算所有因子，返回 {name: value}。

        单个因子失败 → 该因子返回 None，不影响其他因子。
        buffer 不足 (小于 MIN_BARS) → 返回空 dict。
        """
        self._buffer.append(bar)
        if len(self._buffer) < self.MIN_BARS:
            return {}

        self._warm = True
        df = self._to_dataframe()

        for name in self._available_factors:
            try:
                series = self._compute_factor_series(name, df)
                if series is None:
                    self._factor_cache[name] = None
                    continue
                val = float(series.iloc[-1] if hasattr(series, 'iloc') else series[-1])
                if math.isnan(val) or math.isinf(val):
                    self._factor_cache[name] = None
                else:
                    self._factor_cache[name] = val
            except Exception as e:
                logger.warning("Factor '%s' calculation failed: %s", name, e)
                self._factor_cache[name] = None

        return self.get_snapshot()

    def get_snapshot(self) -> dict[str, float | None]:
        """返回最近一次计算的因子值快照。"""
        return dict(self._factor_cache)

    # ── 状态查询 ────────────────────────────────────────

    @property
    def is_warm(self) -> bool:
        return self._warm

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    # ── 动态因子支持 ─────────────────────────────────────

    def refresh_factor_list(self):
        """重新扫描 factor_registry，跳过 shadow 因子（不参与投票）。"""
        all_factors = factor_registry.list()
        # 过滤掉 shadow 因子——只让 BUILTIN 和 DISCOVERED 参与交易
        try:
            from alpha.registry_adapter import RegistryAdapter
            adapter = RegistryAdapter.shared()
            voting = []
            for name in all_factors:
                meta = adapter.get_meta(name)
                source = meta.get("source", "builtin") if meta else "builtin"
                if source != "shadow":
                    voting.append(name)
            skipped = set(all_factors) - set(voting)
            if skipped:
                logger.debug("StreamingFactorEngine: skipping shadow factors: %s", skipped)
            self._available_factors = voting
        except Exception:
            self._available_factors = list(all_factors)

    def set_factor_runtime_config(self, config: dict[str, dict] | None) -> None:
        self._factor_runtime_config = dict(config or {})

    # ── 重置 ─────────────────────────────────────────────

    def reset(self):
        """清空缓冲区（策略切换/重启时）。"""
        self._buffer.clear()
        self._factor_cache.clear()
        self._incremental_state.clear()
        self._warm = False

    # ── 内部工具 ─────────────────────────────────────────

    def _to_dataframe(self) -> pd.DataFrame:
        """将 buffer 转为 DataFrame。"""
        return pd.DataFrame(list(self._buffer))

    def export_factor_history(self) -> tuple[dict[str, "np.ndarray"], "np.ndarray"]:
        """导出 buffer 内所有 bar 的因子值序列 + forward returns。

        用于 CausalCheck 和回测分析。
        返回: (factor_values_dict, forward_returns_array)
          factor_values_dict: {name: np.ndarray of shape (n_bars,)}
          forward_returns_array: np.ndarray of shape (n_bars-1,), 去掉了最后一个 bar 的未来未知
        """
        import numpy as _np
        if len(self._buffer) < self.MIN_BARS + 1:
            return {}, _np.array([])
        
        df = self._to_dataframe()
        n = len(df)
        # 计算所有因子值
        factor_arrays: dict[str, "np.ndarray"] = {}
        for name in self._available_factors:
            try:
                vals = self._compute_factor_series(name, df)
                if vals is None:
                    continue
                arr = _np.asarray(vals, dtype=float)
                # NaN/Inf → NaN
                arr[_np.isinf(arr)] = _np.nan
                factor_arrays[name] = arr
            except Exception:
                continue
        
        # Forward returns: close[t+1] / close[t] - 1, then drop last (no future)
        closes = df['close'].values.astype(float)
        fwd_rets = (closes[1:] - closes[:-1]) / closes[:-1]
        # Pad to same length as factor arrays (NaN for last bar)
        fwd_rets = _np.append(fwd_rets, _np.nan)
        # Forward shift: ret[t] = (close[t+1]-close[t])/close[t]
        # So at index t, fwd_ret is the return from t to t+1
        
        return factor_arrays, fwd_rets

    def _compute_factor_series(self, name: str, df: pd.DataFrame):
        cfg = self._factor_runtime_config.get(name) or {}
        overrides = cfg.get("parameter_overrides") or {}
        if overrides:
            custom = self._compute_with_overrides(name, df, overrides)
            if custom is not None:
                return custom
        fn = factor_registry.get(name)
        if fn is None:
            return None
        return fn(df)

    def _compute_with_overrides(self, name: str, df: pd.DataFrame, overrides: dict):
        if name == "rsi_14":
            return self._factor_rsi(df, length=int(overrides.get("length", 14) or 14))
        if name == "macd_hist":
            return self._factor_macd_hist(
                df,
                fast_length=int(overrides.get("fast_length", 12) or 12),
                slow_length=int(overrides.get("slow_length", 26) or 26),
                signal_length=int(overrides.get("signal_length", 9) or 9),
            )
        if name == "adx":
            return self._factor_adx(df, length=int(overrides.get("length", 14) or 14))
        if name == "stoch_k":
            return self._factor_stoch_k(
                df,
                k_length=int(overrides.get("k_length", overrides.get("length", 14)) or 14),
            )
        if name == "ema_slope":
            return self._factor_ema_slope(
                df,
                period=int(overrides.get("period", overrides.get("ema_length", 20)) or 20),
                lookback=int(overrides.get("lookback", 5) or 5),
            )
        if name == "bb_width":
            return self._factor_bb_width(
                df,
                length=int(overrides.get("length", 20) or 20),
                stddev=float(overrides.get("stddev", 2.0) or 2.0),
            )
        if name == "obv_slope":
            return self._factor_obv_slope(
                df,
                lookback=int(overrides.get("lookback", 20) or 20),
            )
        if name == "vol_ma_ratio":
            return self._factor_vol_ma_ratio(
                df,
                period=int(overrides.get("period", 20) or 20),
            )
        if name == "supertrend_str":
            return self._factor_supertrend_str(
                df,
                atr_length=int(
                    overrides.get("atr_length", overrides.get("period", 10)) or 10
                ),
                multiplier=float(overrides.get("multiplier", 3.0) or 3.0),
            )
        if name == "keltner_width":
            return self._factor_keltner_width(
                df,
                ema_length=int(
                    overrides.get("ema_length", overrides.get("period", 20)) or 20
                ),
                atr_multiplier=float(
                    overrides.get("atr_multiplier", overrides.get("multiplier", 1.5)) or 1.5
                ),
            )
        return None

    @staticmethod
    def _factor_rsi(df: pd.DataFrame, *, length: int = 14):
        close = df["close"].values
        delta = np.diff(close, prepend=close[0])
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        avg_gain = pd.Series(gain).ewm(span=length, min_periods=length).mean().values
        avg_loss = pd.Series(loss).ewm(span=length, min_periods=length).mean().values
        rs = np.divide(avg_gain, avg_loss, out=np.full_like(avg_gain, 100.0), where=avg_loss != 0)
        return 100 - 100 / (1 + rs)

    @staticmethod
    def _factor_macd_hist(
        df: pd.DataFrame,
        *,
        fast_length: int = 12,
        slow_length: int = 26,
        signal_length: int = 9,
    ):
        close = df["close"].values
        ema_fast = pd.Series(close).ewm(span=fast_length).mean().values
        ema_slow = pd.Series(close).ewm(span=slow_length).mean().values
        macd = ema_fast - ema_slow
        signal = pd.Series(macd).ewm(span=signal_length).mean().values
        return macd - signal

    @staticmethod
    def _factor_adx(df: pd.DataFrame, *, length: int = 14):
        high, low, close = df["high"].values, df["low"].values, df["close"].values
        n = len(close)
        tr = np.zeros(n)
        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)
        for i in range(1, n):
            tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
            up = high[i] - high[i - 1]
            down = low[i - 1] - low[i]
            plus_dm[i] = up if up > down and up > 0 else 0
            minus_dm[i] = down if down > up and down > 0 else 0
        atr = pd.Series(tr).ewm(span=length, min_periods=length).mean().values
        smooth_plus = pd.Series(plus_dm).ewm(span=length).mean().values
        smooth_minus = pd.Series(minus_dm).ewm(span=length).mean().values
        di_plus = np.divide(smooth_plus * 100, atr, out=np.zeros_like(atr), where=atr != 0)
        di_minus = np.divide(smooth_minus * 100, atr, out=np.zeros_like(atr), where=atr != 0)
        dx = np.divide(
            np.abs(di_plus - di_minus) * 100,
            di_plus + di_minus,
            out=np.zeros_like(atr),
            where=(di_plus + di_minus) != 0,
        )
        return pd.Series(dx).ewm(span=length).mean().values

    @staticmethod
    def _factor_stoch_k(df: pd.DataFrame, *, k_length: int = 14):
        high, low, close = df["high"].values, df["low"].values, df["close"].values
        n = len(close)
        k = np.full(n, np.nan)
        start = max(0, int(k_length) - 1)
        for i in range(start, n):
            hi = high[i - start:i + 1].max()
            lo = low[i - start:i + 1].min()
            k[i] = (close[i] - lo) / (hi - lo) * 100 if hi != lo else 50
        return k

    @staticmethod
    def _factor_ema_slope(df: pd.DataFrame, *, period: int = 20, lookback: int = 5):
        close = df["close"].values
        ema = pd.Series(close).ewm(span=period, min_periods=period).mean().values
        n = len(ema)
        out = np.full(n, np.nan)
        start = max(0, int(period) + int(lookback) - 1)
        for i in range(start, n):
            prev = ema[i - int(lookback)]
            if np.isnan(prev) or close[i] == 0:
                continue
            out[i] = (ema[i] - prev) / close[i]
        return out

    @staticmethod
    def _factor_bb_width(df: pd.DataFrame, *, length: int = 20, stddev: float = 2.0):
        close = df["close"].values
        sma = pd.Series(close).rolling(length).mean().values
        std = pd.Series(close).rolling(length).std().values
        bb_top = sma + stddev * std
        bb_bot = sma - stddev * std
        return np.divide(bb_top - bb_bot, sma, out=np.zeros_like(sma), where=sma != 0)

    @staticmethod
    def _factor_obv_slope(df: pd.DataFrame, *, lookback: int = 20):
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

    @staticmethod
    def _factor_vol_ma_ratio(df: pd.DataFrame, *, period: int = 20):
        vol = df["volume"].values if "volume" in df.columns else np.zeros(len(df))
        n = len(vol)
        if n < period:
            return np.full(n, np.nan)
        vol_ma = pd.Series(vol).rolling(period, min_periods=period).mean().values
        return np.divide(vol, vol_ma, out=np.zeros_like(vol), where=vol_ma != 0) - 1.0

    @staticmethod
    def _factor_supertrend_str(
        df: pd.DataFrame,
        *,
        atr_length: int = 10,
        multiplier: float = 3.0,
    ):
        high, low, close = df["high"].values, df["low"].values, df["close"].values
        n = len(close)
        out = np.full(n, np.nan)
        if n < atr_length + 2:
            return out

        tr = np.zeros(n)
        for i in range(1, n):
            tr[i] = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )
        atr = pd.Series(tr).ewm(span=atr_length, min_periods=atr_length).mean().values
        hl2 = (high + low) / 2.0
        upper = hl2 + multiplier * atr
        lower = hl2 - multiplier * atr

        final_upper = np.nan
        final_lower = np.nan
        direction = np.nan
        for i in range(atr_length, n):
            if i == atr_length:
                final_upper = upper[i]
                final_lower = lower[i]
                direction = 1.0 if close[i] > hl2[i] else -1.0
                out[i] = 0.0
                continue

            fu = upper[i]
            fl = lower[i]
            if not np.isnan(final_upper) and fu < final_upper:
                fu = final_upper
            if not np.isnan(final_lower) and fl > final_lower:
                fl = final_lower
            final_upper = fu
            final_lower = fl

            prev_dir = direction
            if close[i] > fu:
                direction = 1.0
            elif close[i] < fl:
                direction = -1.0
            else:
                direction = prev_dir

            if direction > 0:
                out[i] = (close[i] - final_lower) / atr[i] if atr[i] > 0 else 0.0
            else:
                out[i] = (final_upper - close[i]) / atr[i] if atr[i] > 0 else 0.0
            out[i] = out[i] * direction

        return out

    @staticmethod
    def _factor_keltner_width(
        df: pd.DataFrame,
        *,
        ema_length: int = 20,
        atr_multiplier: float = 1.5,
    ):
        high, low, close = df["high"].values, df["low"].values, df["close"].values
        n = len(close)
        if n < ema_length + 1:
            return np.full(n, np.nan)

        tr = np.zeros(n)
        for i in range(1, n):
            tr[i] = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )
        atr = pd.Series(tr).ewm(span=ema_length, min_periods=ema_length).mean().values
        ema = pd.Series(close).ewm(span=ema_length, min_periods=ema_length).mean().values
        upper = ema + atr_multiplier * atr
        lower = ema - atr_multiplier * atr
        return np.divide(upper - lower, close, out=np.zeros_like(close), where=close != 0)

