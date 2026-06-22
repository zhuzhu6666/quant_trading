"""StreamingFactorEngine — 流式因子计算引擎。

取代 FactorEngine 的 batch 模式，改为每根 bar 增量计算。
所有因子计算失败时独立处理，不互相影响。

设计文档: docs/FACTOR_TAKEOVER_V4.md §4
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

    def __init__(self, max_buffer: int = 200):
        self._buffer: deque[dict] = deque(maxlen=max_buffer)
        self._factor_cache: dict[str, float | None] = {}
        self._available_factors: list[str] = list(factor_registry.list())
        self._incremental_state: dict[str, float] = {}
        self._warm: bool = False

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
                fn = factor_registry.get(name)
                if fn is None:
                    self._factor_cache[name] = None
                    continue
                series = fn(df)
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
            fn = factor_registry.get(name)
            if fn is None:
                continue
            try:
                vals = fn(df)
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

