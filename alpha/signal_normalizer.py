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

from alpha.factor_cadence import infer_factor_cadence

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 时段上下文强度 (hour_utc). These values are not directional votes.
# ═══════════════════════════════════════════════════════════
HOUR_WEIGHTS: dict[range, float] = {
    range(0, 7):   0.1,   # Asia: lower activity context
    range(7, 13):  0.5,   # Europe: active liquidity context
    range(13, 21): 0.7,   # US: highest activity context
    range(21, 24): 0.0,   # Rollover: low activity context
}


# ═══════════════════════════════════════════════════════════
# 周内上下文强度 (day_of_week: 0=Mon ... 4=Fri). Not directional.
# ═══════════════════════════════════════════════════════════
DAY_WEIGHTS: dict[int, float] = {
    0: 0.0,    # Mon: 中性
    1: 0.0,    # Tue: 中性
    2: 0.1,    # Wed: event/calendar context
    3: 0.1,    # Thu: event/calendar context
    4: 0.2,    # Fri: weekend liquidity context
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
    arr = np.array(list(history)[-window:], dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) < min_samples:
        return None
    if not math.isfinite(float(value)):
        return None
    if arr.std() < 1e-10:
        return 0.0
    # 百分位排名。ties 用平均秩，避免常数/重复值被 searchsorted 映射成极端信号。
    less = float(np.sum(arr < value))
    equal = float(np.sum(arr == value))
    rank = (less + 0.5 * equal) / len(arr)
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
    if isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value).is_integer():
        key = str(int(value))
    else:
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
        # Event proximity intensity; direction is handled by alpha factors.
        result = {"-48": 0.0, "-24": 0.5, "0": 1.0, "24": 0.5, "48": 0.0}
    elif ref == "nfp_weights":
        # Event proximity intensity; gate/sizing decide how to use it.
        result = {"-24": 0.5, "0": 1.0, "24": 0.5}
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
        self._last_history_values: dict[str, float] = {}

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

            # 更新历史窗口。低频因子只在值变化时采样，避免 M5/M15 重复值污染 rank/zscore。
            self._ensure_history(name, cfg)
            if self._should_sample_history(name, raw_value, cfg):
                self._histories[name].append(float(raw_value))

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
                    self._ensure_history(name, cfg)
                else:
                    cfg = self._configs.get(name) or self._default_gp_config(name)
                if self._should_sample_history(name, value, cfg):
                    self._histories[name].append(float(value))

    def update_configs(self, config: dict[str, dict] | None) -> None:
        self._configs = dict(config or {})

    def _ensure_history(self, name: str, cfg: dict) -> None:
        if name not in self._histories:
            maxlen = cfg.get("window", 100)
            self._histories[name] = deque(maxlen=maxlen)

    def _should_sample_history(self, name: str, raw_value: float, cfg: dict) -> bool:
        _cadence, sample_policy = infer_factor_cadence(name, cfg)
        if sample_policy == "every_bar":
            return True
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value):
            return False
        previous = self._last_history_values.get(name)
        changed = previous is None or abs(previous - value) > 1e-12
        self._last_history_values[name] = value
        return changed

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
            # Unknown/generated factors may collect normalization history, but
            # they are observation-only until lifecycle governance publishes
            # an explicit enabled config and positive weight.
            "enabled": False,
            "weight": 0.0,
            "mode": "rank_mapping",
            "window": 100,
            "min_samples": 30,
            "direction": 1,
            "tags": tags or ["GP发现"],
            "source": "gp",
            "role": "alpha",
            "cadence": "bar",
            "history_sample_policy": "every_bar",
        }
