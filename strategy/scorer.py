"""
Weighted Scorer — 多策略信号加权打分融合

对同一根 bar 上同时活跃的多个策略信号:
  1. 按方向分组 (long / short)
  2. 每路信号计算 weighted_score = weight × confidence × |strength|
  3. 方向分组内求和, 取总分更高的方向
  4. 返回该方向 weighted_score 最高的个体信号, 并在 meta 中记录 fused_from
"""

import logging
from strategy.base import Signal

logger = logging.getLogger(__name__)

# 默认权重 (multi_factor_m15 基础权重最高, ma_cross + macd_bb 辅助)
# 亏损策略置 0: trend_following / mean_reversion / breakout / gold_momentum
DEFAULT_WEIGHTS: dict[str, float] = {
    "multi_factor_m15": 0.4,
    "ma_cross_h4": 0.2,
    "macd_bb": 0.2,
    "trend_following": 0.0,
    "mean_reversion": 0.0,
    "breakout": 0.0,
    "gold_momentum": 0.0,
}


class WeightedScorer:
    """加权打分融合器"""

    def __init__(self, weights: dict[str, float] | None = None):
        self.weights: dict[str, float] = (
            {**DEFAULT_WEIGHTS, **weights} if weights else dict(DEFAULT_WEIGHTS)
        )

    # ── 公开接口 ────────────────────────────────────────

    def score(self, signals: list[Signal]) -> Signal | None:
        """输入一批信号, 返回融合后的最强信号 (或 None)"""
        if not signals:
            return None

        longs, shorts = self._group_by_direction(signals)

        long_total = self._group_score(longs)
        short_total = self._group_score(shorts)

        if long_total == 0.0 and short_total == 0.0:
            return None

        # 方向选择：总分高者胜, 平局倾向 long
        if long_total >= short_total and longs:
            winner_group, winner_total = longs, long_total
        elif short_total > long_total and shorts:
            winner_group, winner_total = shorts, short_total
        else:
            return None

        # 组内取加权分最高的个体
        best = max(winner_group, key=self._individual_score)

        # 记录融合来源
        fused_from = sorted({s.strategy for s in winner_group})
        if best.meta is None:
            best.meta = {}
        best.meta["fused_from"] = fused_from
        best.meta["direction_total_score"] = round(winner_total, 4)
        best.meta["weighted_score"] = round(self._individual_score(best), 4)

        return best

    def update_weight(self, strategy: str, weight: float) -> None:
        """动态调整某个策略的权重"""
        self.weights[strategy] = weight
        logger.info("Updated weight: %s → %.2f", strategy, weight)

    def get_weights(self) -> dict[str, float]:
        """返回当前权重快照"""
        return dict(self.weights)

    # ── 内部方法 ────────────────────────────────────────

    @staticmethod
    def _group_by_direction(signals: list[Signal]) -> tuple[list[Signal], list[Signal]]:
        longs, shorts = [], []
        for s in signals:
            if s.direction == 1:
                longs.append(s)
            elif s.direction == -1:
                shorts.append(s)
        return longs, shorts

    def _individual_score(self, sig: Signal) -> float:
        """单个信号的加权分"""
        w = self.weights.get(sig.strategy, 0.0)
        c = sig.confidence if sig.confidence is not None else sig.strength
        return w * c * abs(sig.strength)

    def _group_score(self, group: list[Signal]) -> float:
        """一组信号的加权总分"""
        return sum(self._individual_score(s) for s in group)
