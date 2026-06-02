"""
Factor Engine — 流式因子计算

特性：
- 注册制：因子函数注册到 registry
- 流式更新：每个新bar增量计算（不重算历史）
- 向量化批量：回测模式全量计算
- 输出标准化：统一 DataFrame 格式
"""

import logging
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from alpha.registry import factor_registry

logger = logging.getLogger(__name__)


@dataclass
class FactorResult:
    """单个因子计算结果"""
    name: str
    values: np.ndarray          # 因子值序列
    ic_train: float = 0.0       # 训练集IC
    ic_test: float = 0.0        # 测试集IC
    decay_half_life: float = 0.0  # IC衰减半衰期(小时)
    active: bool = True


class FactorEngine:
    """
    流式因子计算引擎

    用法:
        engine = FactorEngine(df_h1)
        engine.compute_all()
        ic_report = engine.ic_analysis()
    """

    def __init__(self, df: pd.DataFrame | None = None):
        self.df = df
        self.results: dict[str, FactorResult] = {}
        self._factor_cache: dict[str, np.ndarray] = {}

    def set_data(self, df: pd.DataFrame):
        """设置/更新数据"""
        self.df = df.copy()
        self._factor_cache.clear()

    def compute(self, name: str) -> np.ndarray | None:
        """计算单个因子"""
        if name in self._factor_cache:
            return self._factor_cache[name]

        if self.df is None:
            return None

        func = factor_registry.get(name)
        if func is None:
            logger.warning(f"Factor '{name}' not registered")
            return None

        try:
            values = func(self.df)
            self._factor_cache[name] = values
            return values
        except Exception:
            logger.exception(f"Factor '{name}' computation failed")
            return None

    def compute_all(self) -> dict[str, np.ndarray]:
        """计算所有注册因子"""
        for name in factor_registry.list():
            self.compute(name)
        return self._factor_cache

    def ic_analysis(self, forward_periods: list[int] | None = None
                    ) -> pd.DataFrame:
        """
        IC分析：每个因子与未来收益的相关性

        返回: DataFrame with columns: factor, ic_mean, ic_std, ir, decay_half_life
        """
        if self.df is None or "close" not in self.df.columns:
            return pd.DataFrame()

        forward_periods = forward_periods or [1, 5, 10, 20]
        close = self.df["close"].values
        records = []

        for name, values in self._factor_cache.items():
            if values is None or len(values) < 50:
                continue

            # 未来收益
            fwd_ret = (close[1:] - close[:-1]) / close[:-1]

            # 确保对齐
            n = min(len(values) - 1, len(fwd_ret))
            vals = values[:n]
            rets = fwd_ret[:n]

            # 过滤NaN
            mask = ~(np.isnan(vals) | np.isnan(rets))
            if mask.sum() < 30:
                continue

            ic = np.corrcoef(vals[mask], rets[mask])[0, 1]

            records.append({
                "factor": name,
                "ic": round(ic, 4),
                "abs_ic": round(abs(ic), 4),
                "n_valid": int(mask.sum()),
            })

        return pd.DataFrame(records).sort_values("abs_ic", ascending=False)

    def get_active_factors(self, min_abs_ic: float = 0.02) -> list[str]:
        """获取IC显著的活跃因子"""
        ic_df = self.ic_analysis()
        if ic_df.empty:
            return []
        return ic_df[ic_df["abs_ic"] >= min_abs_ic]["factor"].tolist()
