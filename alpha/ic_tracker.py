"""
IC Tracker — 因子IC滚动追踪

实时监控每个因子的预测能力衰减：
- 滚动IC：最近N根bar的IC
- 衰减曲线：不同持有期的IC变化
- 自动标记失效因子
"""

import logging
from collections import deque

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def safe_corrcoef(a: np.ndarray, b: np.ndarray, *, min_samples: int = 2) -> float:
    """Pearson correlation with explicit guards for constant/non-finite series."""
    left = np.asarray(a, dtype=np.float64).ravel()
    right = np.asarray(b, dtype=np.float64).ravel()
    n = min(len(left), len(right))
    if n < min_samples:
        return 0.0
    left = left[:n]
    right = right[:n]
    mask = np.isfinite(left) & np.isfinite(right)
    if int(mask.sum()) < min_samples:
        return 0.0
    left = left[mask]
    right = right[mask]
    if float(np.ptp(left)) < 1e-12 or float(np.ptp(right)) < 1e-12:
        return 0.0
    corr = float(np.corrcoef(left, right)[0, 1])
    return corr if np.isfinite(corr) else 0.0


class ICTracker:
    """
    滚动IC追踪器

    用法:
        tracker = ICTracker(window=500)
        tracker.update("rsi_14", factor_values, forward_returns)
        status = tracker.status("rsi_14")
    """

    def __init__(self, window: int = 500):
        self.window = window
        # {factor_name: deque of (factor_val, fwd_return)}
        self._history: dict[str, deque] = {}
        # Internal buffer for bar data (used by refresh_on_new_data)
        self._bars_buffer: pd.DataFrame = pd.DataFrame()

    def update(self, name: str, factor_values: np.ndarray,
               forward_returns: np.ndarray):
        """更新因子观察值

        BUG-16 (audit 2026-06-04): factor_values 和 forward_returns 长度
        不等时, 旧版用 min() 静默截断, caller 无感。修复后 raise ValueError。
        """
        if name not in self._history:
            self._history[name] = deque(maxlen=self.window)

        if len(factor_values) != len(forward_returns):
            raise ValueError(
                f"factor_values len={len(factor_values)} != "
                f"forward_returns len={len(forward_returns)} for factor {name!r}"
            )

        n = len(factor_values)
        for i in range(n):
            if not (np.isnan(factor_values[i]) or np.isinf(factor_values[i])
                    or np.isnan(forward_returns[i]) or np.isinf(forward_returns[i])):
                self._history[name].append((factor_values[i], forward_returns[i]))
        # Emit factor_ic metric
        try:
            from backend.runtime.runtime_state import RuntimeState
            ic_val = self.rolling_ic(name)
            RuntimeState.shared().emit_metric("factor_ic", {
                "factor": name,
                "ic": round(ic_val, 4),
            })
        except Exception:
            pass

    def rolling_ic(self, name: str) -> float:
        """当前滚动IC"""
        h = self._history.get(name)
        if not h or len(h) < 30:
            return 0.0
        vals = np.array([v[0] for v in h], dtype=np.float64)
        rets = np.array([v[1] for v in h], dtype=np.float64)
        mask = ~(np.isnan(vals) | np.isnan(rets) | np.isinf(vals) | np.isinf(rets))
        if mask.sum() < 10:
            return 0.0
        return safe_corrcoef(vals[mask], rets[mask], min_samples=10)

    def status(self, name: str) -> dict:
        """因子状态报告"""
        ic = self.rolling_ic(name)
        return {
            "factor": name,
            "rolling_ic": round(ic, 4),
            "n_obs": len(self._history.get(name, [])),
            "active": abs(ic) >= 0.02,
            "decay": "stable" if abs(ic) >= 0.1 else "fading" if abs(ic) >= 0.02 else "dead",
        }

    def export_vals(self, name: str) -> np.ndarray:
        """导出因子值的序列 (不含 NaN, 用于 FactorHealth 计算因子间相关性)."""
        h = self._history.get(name)
        if not h:
            return np.array([])
        vals = np.array([v[0] for v in h])
        mask = ~np.isnan(vals)
        return vals[mask]

    def all_status(self) -> list[dict]:
        return [self.status(name) for name in self._history]

    def refresh_on_new_data(self, df_new: pd.DataFrame,
                            factor_values: dict[str, np.ndarray]) -> int:
        """将新 bar 数据追加到内部缓冲区, 并刷新所有因子的 IC 追踪.

        Args:
            df_new: 新 bar 数据 (必须含 'close' 列).
            factor_values: {因子名: 因子值数组}.

        Returns:
            更新后 |rolling_ic| 变化超过 0.01 的因子数量.
        """
        # 1. 追加新 bar 到内部缓冲区
        if self._bars_buffer.empty:
            self._bars_buffer = df_new.copy()
        else:
            self._bars_buffer = pd.concat([self._bars_buffer, df_new])
            max_bars = self.window * 2
            if len(self._bars_buffer) > max_bars:
                self._bars_buffer = self._bars_buffer.iloc[-max_bars:]

        # 2. 从 close 计算 1 步 forward returns
        close = df_new["close"].values
        n = len(close)
        forward_returns = np.full(n, np.nan)
        if n > 1:
            forward_returns[:-1] = (close[1:] - close[:-1]) / close[:-1]

        # 记录更新前的 |rolling_ic|
        old_abs_ics: dict[str, float] = {}
        for name in factor_values:
            old_abs_ics[name] = abs(self.rolling_ic(name))

        # 3. 对每个因子调用 self.update
        for name, values in factor_values.items():
            values_arr = np.asarray(values, dtype=np.float64)
            length = min(len(values_arr), len(forward_returns))
            if length > 0:
                self.update(name, values_arr[:length], forward_returns[:length])

        # 4. 统计 |rolling_ic| 变化超过 0.01 的因子数
        changed = 0
        for name in factor_values:
            new_abs_ic = abs(self.rolling_ic(name))
            old_abs_ic = old_abs_ics.get(name, 0.0)
            if abs(new_abs_ic - old_abs_ic) > 0.01:
                changed += 1

        return changed


# ── 模块级便捷函数 ──────────────────────────────────────────────


def refresh_all_factors(symbol: str = "XAUUSD+",
                        timeframe: str = "M15",
                        n_bars: int = 5000) -> dict:
    """从 DataStore 加载 bars 并刷新所有已注册因子的 IC 追踪.

    Args:
        symbol:   品种代码 (默认 "XAUUSD+").
        timeframe: 周期 (默认 "M15").
        n_bars:    加载的 bar 数量 (默认 5000).

    Returns:
        {"factors_checked": int, "ic_changed_count": int, "errors": list[str]}.
    """
    errors: list[str] = []

    # 1. 加载 bars
    try:
        from data.store import DataStore
        ds = DataStore()
        df = ds.load_bars(symbol, timeframe, limit=n_bars)
    except Exception as e:
        errors.append(f"load_bars failed: {e}")
        return {"factors_checked": 0, "ic_changed_count": 0, "errors": errors}

    if df is None or len(df) < 30:
        errors.append(f"insufficient bars: {len(df) if df is not None else 0}")
        return {"factors_checked": 0, "ic_changed_count": 0, "errors": errors}

    # 2. 计算 forward returns
    close = df["close"].values
    n = len(close)
    forward_returns = np.full(n, np.nan)
    if n > 1:
        forward_returns[:-1] = (close[1:] - close[:-1]) / close[:-1]

    # 3. 创建 tracker 并收集因子值
    from alpha.registry import factor_registry

    tracker = ICTracker(window=min(2000, n))
    factor_values_dict: dict[str, np.ndarray] = {}
    factors_checked = 0

    for name in factor_registry.list():
        try:
            fn = factor_registry.get(name)
            if fn is None:
                continue
            vals = fn(df)
            vals_arr = np.asarray(vals, dtype=np.float64)
            factor_values_dict[name] = vals_arr
            factors_checked += 1
        except Exception as e:
            errors.append(f"{name}: {e}")

    # 4. 调用 refresh_on_new_data 一次刷新所有因子
    ic_changed_count = 0
    if factor_values_dict:
        ic_changed_count = tracker.refresh_on_new_data(df, factor_values_dict)

    # 5. 可选: 评估 FactorHealth
    try:
        from alpha.factor_health import FactorHealth
        health = FactorHealth(tracker)
        health.evaluate_all()
    except Exception as e:
        errors.append(f"FactorHealth.evaluate: {e}")

    return {
        "factors_checked": factors_checked,
        "ic_changed_count": ic_changed_count,
        "errors": errors,
    }
