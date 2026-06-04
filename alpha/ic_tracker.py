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

logger = logging.getLogger(__name__)


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
            if not (np.isnan(factor_values[i]) or np.isnan(forward_returns[i])):
                self._history[name].append((factor_values[i], forward_returns[i]))

    def rolling_ic(self, name: str) -> float:
        """当前滚动IC"""
        h = self._history.get(name)
        if not h or len(h) < 30:
            return 0.0
        vals = np.array([v[0] for v in h])
        rets = np.array([v[1] for v in h])
        mask = ~(np.isnan(vals) | np.isnan(rets))
        if mask.sum() < 10:
            return 0.0
        return float(np.corrcoef(vals[mask], rets[mask])[0, 1])

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

    def all_status(self) -> list[dict]:
        return [self.status(name) for name in self._history]
