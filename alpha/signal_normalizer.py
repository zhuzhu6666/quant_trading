"""SignalNormalizer — 三域归一化引擎。

所有因子无论原始值域，统一映射到 [-1, +1] 连续域。

三种归一化模式:
  - zscore_tanh:   连续有界因子 (RSI, DI, Stoch, ADX...)
  - rank_mapping:  无量纲/宏观因子 (COT, 央行, 持仓...)
  - discrete:      分类因子 (形态, 事件, 时段...)

设计文档: docs/architecture.md
"""

import logging
import math
from collections import deque
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 黄金时段权重 (hour_utc)
# ═══════════════════════════════════════════════════════════
HOUR_WEIGHTS: dict[range, float] = {
    range(14, 21): 0.0,   # 亚洲盘 → 中性
    range(0, 4):   0.3,   # 伦敦开盘 → 轻微看多（流动性注入）
    range(8, 13):  0.5,   # 纽约盘上午 → 信号放大
    range(13, 15): 0.0,   # 午间低谷 → 中性
    range(15, 18): 0.3,   # 纽约收盘 → 轻微
    range(19, 24): 0.0,   # 低流动性 → 中性
}


# ═══════════════════════════════════════════════════════════
# 周内效应权重 (day_of_week: 0=Mon ... 4=Fri)
# ═══════════════════════════════════════════════════════════
DAY_WEIGHTS: dict[int, float] = {
    0: 0.0,    # Mon: 中性
    1: 0.0,    # Tue: 中性
    2: 0.1,    # Wed: 轻微正向（FOMC 常在周三）
    3: -0.1,   # Thu: 反转日
    4: -0.2,   # Fri: 周末平仓效应
}


# ═══════════════════════════════════════════════════════════
# 归一化函数
# ═══════════════════════════════════════════════════════════

def _normalize_zscore_tanh(
    value: float,
    history: deque[float],
    window: int,
    min_samples: int = 30,
) -> float | None:
    """模式 A: zscore_tanh — 连续有界因子。

    signal = tanh((value - rolling_mean) / rolling_std)

    自适应阈值: RSI=50 在低波动市可能强信号，在高波动市是中性。
    """
    if len(history) < min_samples:
        return None
    arr = np.array(list(history)[-window:])
    mean, std = arr.mean(), arr.std()
    if std < 1e-10:
        return 0.0  # 无波动 → 中性
    z = (value - mean) / std
    return float(np.tanh(z))


def _normalize_rank(
    value: float,
    history: deque[float],
    window: int,
    min_samples: int = 30,
    direction: int = 1,
) -> float | None:
    """模式 B: rank_mapping — 无量纲/宏观因子。

    signal = 2 × (rank_in_window / window_size - 0.5) × direction
    """
    if len(history) < min_samples:
        return None
    arr = np.array(list(history)[-window:])
    # 百分位排名
    rank = np.searchsorted(np.sort(arr), value) / len(arr)
    signal = 2.0 * rank - 1.0  # [0, 1] → [-1, +1]
    return float(np.clip(signal * direction, -1.0, 1.0))


def _normalize_discrete(
    value: Any,
    value_map: dict[str, float],
) -> float | None:
    """模式 C: discrete — 分类因子。

    直接把原始值映射到 value_map 定义的信号值。
    未知值 → 中性 (0.0)。
    """
    key = str(value)
    return value_map.get(key, 0.0)


# ═══════════════════════════════════════════════════════════
# 工具: 解析 value_map 字符串引用
# ═══════════════════════════════════════════════════════════

_VALUE_MAP_REFS: dict[str, dict] = {}


def _resolve_value_map_ref(ref: str) -> dict:
    """将设计文档中的字符串引用 (如 "hour_weights") 解析为实际 value_map dict。

    设计文档 §5.2 模式 C 中, hour_utc/day_of_week/hours_to_fomc/hours_to_nfp
    的 value_map 写的是 "hour_weights" / "day_weights" 等字符串引用。
    此处将引用解析为实际的 {str: signal} 映射。
    """
    if ref in _VALUE_MAP_REFS:
        return _VALUE_MAP_REFS[ref]

    result: dict[str, float] = {}
    if ref == "hour_weights":
        for r, w in HOUR_WEIGHTS.items():
            if isinstance(r, range):
                for h in r:
                    result[str(h)] = w
    elif ref == "day_weights":
        for d, w in DAY_WEIGHTS.items():
            result[str(d)] = w
    elif ref == "fomc_weights":
        # FOMC 前后: 24h 前轻微看多, 48h 后回归中性
        result = {"-48": 0.0, "-24": 0.2, "0": 0.3, "24": 0.1, "48": 0.0}
    elif ref == "nfp_weights":
        # NFP 前后: 24h 前不开仓, 24h 后中性
        result = {"-24": 0.0, "0": 0.0, "24": 0.1}
    _VALUE_MAP_REFS[ref] = result
    return result


# ═══════════════════════════════════════════════════════════
# SignalNormalizer 类
# ═══════════════════════════════════════════════════════════

class SignalNormalizer:
    """三域归一引擎。

    为每个因子维护独立的滚动历史窗口，用于 zscore_tanh 和 rank_mapping。
    discrete 因子不需要历史窗口。

    Args:
        config: 来自 RuntimeConfig.factor_signal_config 或等效 dict。
                格式: {factor_name: {mode, window, min_samples, direction, ...}}
    """

    def __init__(self, config: dict[str, dict]):
        self._configs: dict[str, dict] = config or {}
        self._histories: dict[str, deque[float]] = {}

    def normalize(
        self, factor_values: dict[str, float | None]
    ) -> dict[str, float | None]:
        """归一化所有因子值到 [-1, +1]。

        Args:
            factor_values: {name: raw_value}，来自 StreamingFactorEngine。
                           None 的因子跳过并返回 None。

        Returns:
            {name: signal}，signal ∈ [-1, +1] 或 None（冷启动弃权）。
        """
        signals: dict[str, float | None] = {}
        for name, raw_value in factor_values.items():
            if raw_value is None or (
                isinstance(raw_value, float) and math.isnan(raw_value)
            ):
                signals[name] = None
                continue

            cfg = self._configs.get(name)
            if cfg is None:
                cfg = self._default_gp_config(name)
                self._configs[name] = cfg

            # 更新历史窗口
            if name not in self._histories:
                maxlen = cfg.get("window", 100)
                self._histories[name] = deque(maxlen=maxlen)
            self._histories[name].append(raw_value)

            # 按模式归一化
            mode = cfg.get("mode", "rank_mapping")
            if mode == "zscore_tanh":
                signals[name] = _normalize_zscore_tanh(
                    raw_value,
                    self._histories[name],
                    window=cfg.get("window", 100),
                    min_samples=cfg.get("min_samples", 30),
                )
            elif mode == "rank_mapping":
                signals[name] = _normalize_rank(
                    raw_value,
                    self._histories[name],
                    window=cfg.get("window", 100),
                    min_samples=cfg.get("min_samples", 30),
                    direction=cfg.get("direction", 1),
                )
            elif mode == "discrete":
                value_map = cfg.get("value_map", {})
                # 解析字符串引用 (如 "hour_weights" → HOUR_WEIGHTS)
                if isinstance(value_map, str):
                    value_map = _resolve_value_map_ref(value_map)
                signals[name] = _normalize_discrete(
                    raw_value, value_map,
                )
            else:
                logger.warning("Unknown normalization mode '%s' for factor '%s'", mode, name)
                signals[name] = None

        return signals

    def warmup(self, factor_snapshots: list[dict[str, float | None]]):
        """从历史因子快照预热滚动窗口。

        Phase 3 启动时，用 warmup bars 的因子值填充。
        """
        for snapshot in factor_snapshots:
            for name, value in snapshot.items():
                if value is None or (
                    isinstance(value, float) and math.isnan(value)
                ):
                    continue
                if name not in self._histories:
                    cfg = self._configs.get(name) or self._default_gp_config(name)
                    maxlen = cfg.get("window", 100)
                    self._histories[name] = deque(maxlen=maxlen)
                self._histories[name].append(value)

    def _default_gp_config(self, name: str) -> dict:
        """GP 发现因子的默认配置, 尝试从 GPClassifier 获取标签."""
        tags = None
        try:
            from alpha.gp_classifier import classify_expr
            t = classify_expr(name)
            if t and t != ["GP发现"]:
                tags = t
        except Exception as e:
            logger.debug("GP classifier unavailable for '%s': %s", name, e)
        return {
            "enabled": True,
            "weight": 0.3,
            "mode": "rank_mapping",
            "window": 100,
            "min_samples": 30,
            "direction": 1,
            "tags": tags or ["GP发现"],
            "source": "gp",
        }
