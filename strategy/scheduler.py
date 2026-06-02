"""
SelfLearningScheduler — 自学习调度器

每 N 笔交易后自动评估所有策略表现, 动态调权。

设计:
- 包装 MABRouter, 在 router.update() 基础上增加近期胜率监控
- 每 check_interval 笔交易触发一次 _reevaluate()
- WR < underperformer_threshold: 权重 *= 0.5 (最低到 0.0)
- WR > recovery_threshold: 权重 *= 1.5 (最高到 1.0)
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from strategy.mab_router import MABRouter

logger = logging.getLogger(__name__)


class SelfLearningScheduler:
    """
    自学习调度器 — 根据近期策略胜率动态调整权重。

    权重含义: 1.0 = 正常使用, 0.0 = 禁用
    调整在 _reevaluate() 中批量执行, 不涉及实时干预。

    Parameters
    ----------
    router : MABRouter
        被包装的 MAB 路由器实例。
    check_interval : int
        每多少笔已平仓交易触发一次评估, 默认 50。
    underperformer_threshold : float
        WR 低于此值触发降权 (weight *= 0.5), 默认 0.45。
    recovery_threshold : float
        WR 高于此值触发恢复 (weight *= 1.5), 默认 0.55。
    """

    def __init__(
        self,
        router: MABRouter,
        check_interval: int = 50,
        underperformer_threshold: float = 0.45,
        recovery_threshold: float = 0.55,
    ):
        self.router = router
        self.check_interval = check_interval
        self.underperformer_threshold = underperformer_threshold
        self.recovery_threshold = recovery_threshold

        self._trade_count = 0
        # 自上次评估以来的交易记录
        self._recent_records: list[dict] = []
        # 每个策略的当前权重, 初始均为 1.0
        self._weights: dict[str, float] = {s: 1.0 for s in router.strategies}
        # 所有调权事件
        self._events: list[dict] = []
        # 评估次数 (即 _reevaluate() 被调用的次数)
        self._eval_count = 0

    # ── 公共接口 ──────────────────────────────────────

    def on_trade_close(self, strategy: str, regime: str, win: bool, pnl: float):
        """
        记录一笔已平仓交易, 并触发可能的重评估。

        Parameters
        ----------
        strategy : str
            产生这笔交易的策略名。
        regime : str
            交易时的市场 regime (透传给 router.update)。
        win : bool
            是否盈利。
        pnl : float
            盈亏金额。
        """
        # 1. 更新 MAB 后验
        self.router.update(strategy, regime, win)

        # 2. 记录近期交易
        self._recent_records.append({
            "strategy": strategy,
            "win": win,
            "pnl": pnl,
        })
        self._trade_count += 1

        # 3. 达到检查间隔 → 重新评估
        if self._trade_count % self.check_interval == 0:
            self._reevaluate()

    def get_events(self) -> list[dict]:
        """返回所有调权事件列表 (每策略每次调整一条事件)。"""
        return list(self._events)

    def stats(self) -> pd.DataFrame:
        """
        调权事件汇总。

        Returns
        -------
        pd.DataFrame
            Columns: [strategy, current_weight, max_weight, min_weight, adjustments]
        """
        rows = []
        for strategy in self.router.strategies:
            strategy_events = [
                e for e in self._events if e["strategy"] == strategy
            ]
            # 从初始权重 1.0 开始追踪极值
            weights_in_history = [1.0]
            for e in strategy_events:
                weights_in_history.append(e["old_weight"])
                weights_in_history.append(e["new_weight"])

            current_weight = self._weights.get(strategy, 1.0)
            rows.append({
                "strategy": strategy,
                "current_weight": current_weight,
                "max_weight": round(max(weights_in_history), 4),
                "min_weight": round(min(weights_in_history), 4),
                "adjustments": len(strategy_events),
            })
        return pd.DataFrame(rows)

    @property
    def weights(self) -> dict[str, float]:
        """获取当前各策略权重快照。"""
        return dict(self._weights)

    @property
    def eval_count(self) -> int:
        """评估执行次数。"""
        return self._eval_count

    # ── 内部方法 ──────────────────────────────────────

    def _reevaluate(self):
        """
        对每个有近期交易记录的策略, 计算最近 check_interval 笔的 WR,
        根据阈值动态调整权重并记录事件。
        """
        self._eval_count += 1

        for strategy in self.router.strategies:
            # 筛选该策略在自上次评估以来的记录
            strat_records = [
                r for r in self._recent_records if r["strategy"] == strategy
            ]
            if not strat_records:
                logger.debug("_reevaluate: %s 无近期记录, 跳过", strategy)
                continue

            wins = sum(1 for r in strat_records if r["win"])
            total = len(strat_records)
            wr = wins / total if total > 0 else 0.0

            old_weight = self._weights.get(strategy, 1.0)
            new_weight = old_weight
            changed = False
            reason = ""

            if wr < self.underperformer_threshold:
                new_weight = old_weight * 0.5
                new_weight = max(new_weight, 0.0)
                changed = True
                reason = (
                    f"WR={wr:.2f} < {self.underperformer_threshold}, "
                    f"weight*0.5"
                )
            elif wr > self.recovery_threshold:
                new_weight = old_weight * 1.5
                new_weight = min(new_weight, 1.0)
                changed = True
                reason = (
                    f"WR={wr:.2f} > {self.recovery_threshold}, "
                    f"weight*1.5"
                )
            else:
                reason = (
                    f"WR={wr:.2f} in "
                    f"[{self.underperformer_threshold}, "
                    f"{self.recovery_threshold}], no change"
                )

            if new_weight != old_weight:
                self._weights[strategy] = new_weight

            self._events.append({
                "ts": datetime.now().isoformat(),
                "strategy": strategy,
                "old_weight": round(old_weight, 4),
                "new_weight": round(new_weight, 4),
                "recent_wr": round(wr, 4),
                "reason": reason,
                "changed": changed,
            })

        # 清空近期记录, 开始下一周期
        self._recent_records = []
