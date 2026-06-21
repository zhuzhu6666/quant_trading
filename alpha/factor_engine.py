"""BATCH-ONLY Factor Engine - 离线批量因子计算 (已弃用于生产路径).

本模块仅用于批量离线分析脚本 (factor PCA, IC rolling, walkforward 等).
生产路径 (live/paper 实时流) 请使用 alpha/streaming_factor_engine.py.

特性:
- 注册制: 因子函数注册到 registry
- 向量化批量: 全量 DataFrame 一次性计算所有因子
- 输出标准化: 统一 DataFrame 格式

注意: 此模块不适用于实时 bar 流, 无增量计算能力.
新开发请使用 StreamingFactorEngine, 除非你需要 compute_all() 一次性批量计算."""

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

        # FOOTGUN-9 (audit 2026-06-04): compute() 验 df 有必要列
        # compute_all() 已有 "close" in columns 检查, compute() 没
        # 因子函数访问 self.df["close"] 不存在会 KeyError
        if self.df.empty or "close" not in self.df.columns:
            logger.warning(
                f"Factor '{name}' skip: df missing 'close' column or empty"
            )
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
        """计算所有已注册因子"""
        for name in factor_registry.list():
            self.compute(name)
        return self._factor_cache

    def ic_analysis(self, forward_periods: list[int] | None = None
                    ) -> pd.DataFrame:
        """
        IC分析: 每个因子与未来收益的相关性

        支持多周期 IC (default [1, 5, 10, 20]):
          - 旧版 (BUG-3): 只算 1-bar forward return, 跟 forward_periods 形参脱钩
          - 新版: 对每个 fp 算 close[i+fp]/close[i] - 1, 输出多列 ic_1/ic_5/ic_10/ic_20 + ic_mean
        """
        if self.df is None or self.df.empty:
            return pd.DataFrame()

        forward_periods = forward_periods or [1, 5, 10, 20]
        close = self.df["close"].to_numpy(dtype=np.float64)
        n_close = len(close)
        if n_close < 50:
            return pd.DataFrame()

        # 预计算每个 fp 的 forward return (NaN 末端补齐以便后续 slice)
        fwd_rets_by_fp: dict[int, np.ndarray] = {}
        for fp in forward_periods:
            fwd = np.full(n_close, np.nan, dtype=np.float64)
            fwd[:-fp] = close[fp:] / close[:-fp] - 1.0
            fwd_rets_by_fp[fp] = fwd

        records = []
        for name, values in self._factor_cache.items():
            if values is None or len(values) < 50:
                continue
            vals = np.asarray(values, dtype=np.float64)

            per_fp_ic: dict[int, float] = {}
            for fp, fwd in fwd_rets_by_fp.items():
                n = min(len(values), len(fwd))
                mask = ~(np.isnan(vals[:n]) | np.isnan(fwd[:n]) | np.isinf(vals[:n]) |
                         np.isinf(fwd[:n]))
                if mask.sum() < 30:
                    continue
                ic = float(np.corrcoef(vals[:n][mask], fwd[:n][mask])[0, 1])
                per_fp_ic[fp] = round(ic, 4)

            if not per_fp_ic:
                continue

            primary_ic = per_fp_ic.get(1, 0.0)
            ic_values = list(per_fp_ic.values())
            record = {
                "factor": name,
                "abs_ic": round(abs(primary_ic), 4),
                "ic_mean": round(float(np.mean(ic_values)), 4) if ic_values else 0.0,
                "n_valid": int(min(len(values), n_close)),
            }
            for fp, ic_val in per_fp_ic.items():
                record[f"ic_{fp}"] = ic_val
            records.append(record)

        return pd.DataFrame(records).sort_values("abs_ic", ascending=False)

    def get_active_factors(self, min_abs_ic: float = 0.02) -> list[str]:
        """返回 |IC| >= min_abs_ic 的因子名"""
        ic_df = self.ic_analysis()
        if ic_df.empty:
            return []
        return ic_df[ic_df["abs_ic"] >= min_abs_ic]["factor"].tolist()
