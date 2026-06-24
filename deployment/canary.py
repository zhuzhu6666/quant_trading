"""deployment/canary.py — 金丝雀晋升/回滚导演 (Phase 2.3, 2026-06-12)

管理因子从 SHADOW → ACTIVE 的多阶段金丝雀部署.
每个阶段要求最低 OOS bars 数 和 最低累计 PnL 阈值.
- 满足条件 → 自动晋升下一阶段
- 未满足 (PnL 低于阈值) → rollback 到 SHADOW
- 连续回滚 N 次 → QUARANTINED 隔离
- 手动退役 → RETIRED

阶段定义:
    SHADOW       (已发现, 尚未 OOS 测试)
    CANARY_5     (5 根 OOS bar 验证)
    CANARY_20    (20 根 OOS bar 验证)
    CANARY_50    (50 根 OOS bar 验证, 仅低权重试运行)
    PROBATION    (晋升观察期, 低风险继续验证)
    ACTIVE       (全部署)
    QUARANTINED  (异常隔离, 需手动解除)
    RETIRED      (永久退役)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── 阶段常量 ──────────────────────────────────────────────────────────
SHADOW = "SHADOW"
CANARY_5 = "CANARY_5"
CANARY_20 = "CANARY_20"
CANARY_50 = "CANARY_50"
PROBATION = "PROBATION"
ACTIVE = "ACTIVE"
QUARANTINED = "QUARANTINED"
RETIRED = "RETIRED"

CANARY_STAGES = [SHADOW, CANARY_5, CANARY_20, CANARY_50, PROBATION, ACTIVE]
TERMINAL_STAGES = {QUARANTINED, RETIRED}  # 不再自动晋升/回滚

# 各阶段最低要求: (min_oos_bars, min_oos_pnl)
# 这里用绝对 PnL (净收益率) 作为阈值, 正收益才晋升
STAGE_REQUIREMENTS: dict[str, tuple[int, float]] = {
    SHADOW: (0, 0.0),                # shadow 无要求, 只是起点
    CANARY_5: (5, 0.001),            # 5 bars, +0.1% 净收益
    CANARY_20: (20, 0.003),          # 20 bars, +0.3%
    CANARY_50: (50, 0.005),          # 50 bars, +0.5%
    PROBATION: (80, 0.007),          # 80 bars, +0.7% 且进入观察期
    ACTIVE: (0, 0.0),                # 最终阶段, 无晋升要求
}

# 多维晋升过滤
MIN_HIT_RATE_EARLY = 0.48
MIN_HIT_RATE_LATE = 0.52
MIN_HEALTH_SCORE = 35.0
MIN_INDEPENDENCE_SCORE = 25.0
MAX_DRAWDOWN_TO_PNL_RATIO = 2.5
MIN_ACTIVE_BARS_FOR_PROBATION = 20
MAX_DISCOVERED_FACTORS = 24
MAX_NEW_DISCOVERED_PER_CYCLE = 3

# PnL 低于此比例 × 阈值时触发 rollback (默认 -50% 阈值即回滚)
ROLLBACK_PNL_RATIO = -0.5
# 连续回滚 N 次后自动隔离
MAX_ROLLBACKS_BEFORE_QUARANTINE = 3


@dataclass
class CanaryState:
    """单因子的金丝雀状态"""
    factor: str
    stage: str = SHADOW
    oos_bars: int = 0                  # OOS 样本 bar 数
    cumulative_pnl: float = 0.0        # 累计 OOS PnL (净收益率)
    promote_time: float | None = None  # 上次晋升时间戳
    rollback_count: int = 0            # 累计回滚次数
    history: list[dict] = field(default_factory=list)


@dataclass
class CanaryEvalContext:
    """check_promotion 的评估上下文 — 由调用方提供 OOS 结果"""
    oos_bars: int = 0
    oos_pnl: float = 0.0
    # 可选扩展字段
    additional_metrics: dict = field(default_factory=dict)  # hit_rate/max_drawdown/health_score/independence 等

    @classmethod
    def from_pnl_series(cls, pnl_series: list[float]) -> CanaryEvalContext:
        """从 PnL 序列构造上下文 (算累计值)"""
        arr = np.asarray(pnl_series, dtype=np.float64)
        return cls(
            oos_bars=len(arr),
            oos_pnl=float(np.sum(arr)),
        )


class CanaryDirector:
    """
    金丝雀晋升/回滚导演.

    用法:
        director = CanaryDirector()
        ctx = CanaryEvalContext(oos_bars=22, oos_pnl=0.008)
        action = director.check_promotion("factor_dsl_001", ctx)
        if action == "promote":
            director.promote("factor_dsl_001")
        elif action == "rollback":
            director.rollback("factor_dsl_001")
        print(director.summary())
    """

    def __init__(self,
                 stage_requirements: dict[str, tuple[int, float]] | None = None,
                 rollback_pnl_ratio: float = ROLLBACK_PNL_RATIO,
                 ):
        self._states: dict[str, CanaryState] = {}
        self._stage_requirements = stage_requirements or STAGE_REQUIREMENTS
        self._rollback_pnl_ratio = rollback_pnl_ratio

    # ── 状态查询 ──────────────────────────────────────────────────

    def get_state(self, factor_name: str) -> CanaryState:
        """获取因子状态 (不存在则创建默认)"""
        if factor_name not in self._states:
            self._states[factor_name] = CanaryState(factor=factor_name)
        return self._states[factor_name]

    def get_stage(self, factor_name: str) -> str:
        return self.get_state(factor_name).stage

    def can_promote(self, factor_name: str) -> bool:
        """返回当前阶段是否可以继续晋升 (非最终/终端阶段)"""
        stage = self.get_stage(factor_name)
        return stage != ACTIVE and stage not in TERMINAL_STAGES

    # ── 晋升检查 ──────────────────────────────────────────────────

    def check_promotion(self,
                        factor_name: str,
                        eval_ctx: CanaryEvalContext,
                        progress_cb: Optional[Callable[[str, float, str], None]] = None,
                        ) -> str:
        """
        检查因子是否满足晋升条件.

        Args:
            factor_name: 因子名
            eval_ctx:    OOS 评估上下文 (bars, pnl)
            progress_cb: 进度回调 (phase, percent, message) — 兼容 evaluate_factors 签名

        Returns:
            "promote"  | "rollback" | "stay"
        """
        cb = progress_cb or (lambda *_: None)
        state = self.get_state(factor_name)
        current_stage = state.stage

        cb("check_promotion", 10, f"{factor_name}: stage={current_stage}, "
                                   f"oos_bars={eval_ctx.oos_bars}, "
                                   f"oos_pnl={eval_ctx.oos_pnl:.4f}")

        # P1.2: QUARANTINED / RETIRED 不再自动评估
        if current_stage in TERMINAL_STAGES:
            cb("check_promotion", 100, f"{factor_name}: {current_stage}, skip")
            return "stay"

        if current_stage == ACTIVE:
            cb("check_promotion", 100, f"{factor_name}: already ACTIVE, skip")
            return "stay"

        # 更新统计
        state.oos_bars = eval_ctx.oos_bars
        state.cumulative_pnl = eval_ctx.oos_pnl
        metrics = dict(eval_ctx.additional_metrics or {})
        self._record_event(state, "eval", {
            "oos_bars": eval_ctx.oos_bars,
            "oos_pnl": round(eval_ctx.oos_pnl, 6),
            "metrics": {
                "source": metrics.get("source", "unknown"),
                "hit_rate": round(float(metrics.get("hit_rate", 0.0) or 0.0), 6),
                "max_drawdown": round(float(metrics.get("max_drawdown", 0.0) or 0.0), 6),
                "health_score": round(float(metrics.get("health_score", 0.0) or 0.0), 4),
                "independence_score": round(float(metrics.get("independence_score", 0.0) or 0.0), 4),
                "n_active": int(metrics.get("n_active", eval_ctx.oos_bars) or eval_ctx.oos_bars),
            },
        })

        # ── 步骤 1: 检查当前阶段是否需要回滚 ──
        # Rollback 检查: 当前阶段 (非 SHADOW/ACTIVE) 的 PnL 低于阈值
        if current_stage not in (SHADOW, ACTIVE):
            cur_bars, cur_pnl = self._stage_requirements.get(current_stage, (0, 0.0))
            if (eval_ctx.oos_bars >= cur_bars and cur_pnl > 0
                    and eval_ctx.oos_pnl < cur_pnl * self._rollback_pnl_ratio):
                cb("check_promotion", 80, f"{factor_name}: PnL below rollback "
                                           f"threshold ({eval_ctx.oos_pnl:.4f} < "
                                           f"{cur_pnl * self._rollback_pnl_ratio:.6f}), "
                                           f"action=rollback")
                self._record_event(state, "rollback_check", {
                    "reason": f"pnl={eval_ctx.oos_pnl:.6f} < "
                              f"threshold={cur_pnl * self._rollback_pnl_ratio:.6f}",
                })
                return "rollback"

        # ── 步骤 2: 检查是否满足下一阶段晋升条件 ──
        next_stage = self._next_stage(current_stage)
        if next_stage is None:
            return "stay"

        min_bars, min_pnl = self._stage_requirements.get(next_stage, (0, 0.0))

        cb("check_promotion", 40, f"{factor_name}: next={next_stage}, "
                                   f"need {min_bars} bars / {min_pnl} PnL")

        # 条件检查
        if eval_ctx.oos_bars < min_bars:
            reason = f"insufficient bars ({eval_ctx.oos_bars} < {min_bars})"
            self._record_event(state, "stay", {"reason": reason, "next_stage": next_stage})
            cb("check_promotion", 100, f"{factor_name}: {reason}")
            return "stay"

        if eval_ctx.oos_pnl < min_pnl:
            reason = f"insufficient PnL ({eval_ctx.oos_pnl:.4f} < {min_pnl})"
            self._record_event(state, "stay", {"reason": reason, "next_stage": next_stage})
            cb("check_promotion", 100, f"{factor_name}: {reason}")
            return "stay"

        metrics = eval_ctx.additional_metrics or {}
        hit_rate = float(metrics.get("hit_rate", 0.0) or 0.0)
        max_drawdown = float(metrics.get("max_drawdown", 0.0) or 0.0)
        health_score = float(metrics.get("health_score", 50.0) or 50.0)
        independence_score = float(metrics.get("independence_score", 50.0) or 50.0)
        n_active = int(metrics.get("n_active", eval_ctx.oos_bars) or eval_ctx.oos_bars)

        min_hit_rate = MIN_HIT_RATE_LATE if next_stage in (PROBATION, ACTIVE) else MIN_HIT_RATE_EARLY
        if hit_rate > 0 and hit_rate < min_hit_rate:
            reason = f"hit_rate too low ({hit_rate:.3f} < {min_hit_rate:.3f})"
            self._record_event(state, "stay", {"reason": reason, "next_stage": next_stage})
            cb("check_promotion", 100, f"{factor_name}: {reason}")
            return "stay"

        if next_stage in (PROBATION, ACTIVE) and n_active < MIN_ACTIVE_BARS_FOR_PROBATION:
            reason = f"insufficient active bars ({n_active} < {MIN_ACTIVE_BARS_FOR_PROBATION})"
            self._record_event(state, "stay", {"reason": reason, "next_stage": next_stage})
            cb("check_promotion", 100, f"{factor_name}: {reason}")
            return "stay"

        if next_stage in (PROBATION, ACTIVE) and health_score < MIN_HEALTH_SCORE:
            reason = f"health score too low ({health_score:.1f} < {MIN_HEALTH_SCORE:.1f})"
            self._record_event(state, "stay", {"reason": reason, "next_stage": next_stage})
            cb("check_promotion", 100, f"{factor_name}: {reason}")
            return "stay"

        if next_stage in (PROBATION, ACTIVE) and independence_score < MIN_INDEPENDENCE_SCORE:
            reason = f"independence too low ({independence_score:.1f} < {MIN_INDEPENDENCE_SCORE:.1f})"
            self._record_event(state, "stay", {"reason": reason, "next_stage": next_stage})
            cb("check_promotion", 100, f"{factor_name}: {reason}")
            return "stay"

        if next_stage in (PROBATION, ACTIVE) and eval_ctx.oos_pnl > 0:
            dd_ratio = max_drawdown / max(abs(eval_ctx.oos_pnl), 1e-6)
            if max_drawdown > 0 and dd_ratio > MAX_DRAWDOWN_TO_PNL_RATIO:
                reason = f"drawdown too large (ratio={dd_ratio:.2f})"
                self._record_event(state, "stay", {"reason": reason, "next_stage": next_stage})
                cb("check_promotion", 100, f"{factor_name}: {reason}")
                return "stay"

        self._record_event(state, "qualify", {
            "next_stage": next_stage,
            "oos_bars": eval_ctx.oos_bars,
            "oos_pnl": round(eval_ctx.oos_pnl, 6),
            "metrics": {
                "hit_rate": round(hit_rate, 6),
                "max_drawdown": round(max_drawdown, 6),
                "health_score": round(health_score, 4),
                "independence_score": round(independence_score, 4),
                "n_active": n_active,
            },
        })
        cb("check_promotion", 100, f"{factor_name}: qualifies for {next_stage}")
        return "promote"

    # ── 晋升 / 回滚 ───────────────────────────────────────────────

    def promote(self,
                factor_name: str,
                progress_cb: Optional[Callable[[str, float, str], None]] = None,
                ) -> bool:
        """
        将因子晋升到下一阶段.
        返回 True 晋升成功, False 已达最终阶段或失败.
        """
        cb = progress_cb or (lambda *_: None)
        state = self.get_state(factor_name)
        current = state.stage

        if current == ACTIVE:
            cb("promote", 100, f"{factor_name}: already ACTIVE")
            return False

        next_stage = self._next_stage(current)
        if next_stage is None:
            return False

        state.stage = next_stage
        state.promote_time = _now()
        self._record_event(state, "promote", {
            "from": current,
            "to": next_stage,
        })
        logger.info(f"[CanaryDirector] promote {factor_name}: {current} -> {next_stage}")
        cb("promote", 100, f"{factor_name}: {current} -> {next_stage}")
        return True

    def rollback(self,
                 factor_name: str,
                 progress_cb: Optional[Callable[[str, float, str], None]] = None,
                 ) -> bool:
        """
        将因子回滚到 SHADOW 阶段. 保留历史记录.
        如果连续回滚次数 >= MAX_ROLLBACKS_BEFORE_QUARANTINE, 自动隔离.
        返回 True 回滚成功, False 已在 SHADOW.
        """
        cb = progress_cb or (lambda *_: None)
        state = self.get_state(factor_name)
        current = state.stage

        if current == SHADOW:
            cb("rollback", 100, f"{factor_name}: already SHADOW")
            return False

        # P1.2: 增加回滚计数
        state.rollback_count += 1

        # P1.2: 连续回滚超限 → 自动隔离
        if state.rollback_count >= MAX_ROLLBACKS_BEFORE_QUARANTINE:
            state.stage = QUARANTINED
            self._record_event(state, "quarantine", {
                "from": current,
                "rollback_count": state.rollback_count,
                "reason": f"{state.rollback_count} consecutive rollbacks",
            })
            logger.warning(f"[CanaryDirector] quarantine {factor_name}: "
                          f"{current} -> QUARANTINED ({state.rollback_count} rollbacks)")
            cb("rollback", 100, f"{factor_name}: {current} -> QUARANTINED "
                                f"(rollback #{state.rollback_count})")
            return True

        state.stage = SHADOW
        self._record_event(state, "rollback", {
            "from": current,
            "rollback_count": state.rollback_count,
        })
        logger.info(f"[CanaryDirector] rollback {factor_name}: {current} -> SHADOW "
                    f"(rollback #{state.rollback_count})")
        cb("rollback", 100, f"{factor_name}: {current} -> SHADOW")
        return True

    # ── 隔离 / 退役 (P1.2) ──────────────────────────────────────────

    def quarantine(self, factor_name: str, reason: str = "") -> bool:
        """手动隔离因子到 QUARANTINED. 返回 True 成功."""
        state = self.get_state(factor_name)
        if state.stage == QUARANTINED:
            return False
        prev = state.stage
        state.stage = QUARANTINED
        self._record_event(state, "quarantine", {"from": prev, "reason": reason})
        logger.warning(f"[CanaryDirector] quarantine {factor_name}: {prev} -> QUARANTINED ({reason})")
        return True

    def unquarantine(self, factor_name: str, reason: str = "") -> bool:
        """解除隔离, 回到 SHADOW. 返回 True 成功."""
        state = self.get_state(factor_name)
        if state.stage != QUARANTINED:
            return False
        state.stage = SHADOW
        state.rollback_count = 0  # 重置回滚计数
        self._record_event(state, "unquarantine", {"to": SHADOW, "reason": reason})
        logger.info(f"[CanaryDirector] unquarantine {factor_name}: -> SHADOW ({reason})")
        return True

    def retire(self, factor_name: str, reason: str = "") -> bool:
        """标记因子为 RETIRED (永久退役). 返回 True 成功."""
        state = self.get_state(factor_name)
        if state.stage == RETIRED:
            return False
        prev = state.stage
        state.stage = RETIRED
        self._record_event(state, "retire", {"from": prev, "reason": reason})
        logger.info(f"[CanaryDirector] retire {factor_name}: {prev} -> RETIRED ({reason})")
        return True

    def unretire(self, factor_name: str, reason: str = "") -> bool:
        """恢复退役因子到 SHADOW. 返回 True 成功."""
        state = self.get_state(factor_name)
        if state.stage != RETIRED:
            return False
        state.stage = SHADOW
        state.rollback_count = 0
        self._record_event(state, "unretire", {"to": SHADOW, "reason": reason})
        logger.info(f"[CanaryDirector] unretire {factor_name}: -> SHADOW ({reason})")
        return True

    # ── 批量操作 ──────────────────────────────────────────────────

    def evaluate_all(self,
                     eval_results: dict[str, CanaryEvalContext],
                     progress_cb: Optional[Callable[[str, float, str], None]] = None,
                     ) -> list[dict]:
        """
        批量评估所有因子, 执行晋升/回滚.

        Args:
            eval_results: {factor_name: CanaryEvalContext}
            progress_cb:  进度回调

        Returns:
            [{factor, from_stage, to_stage, action}, ...]
        """
        cb = progress_cb or (lambda *_: None)
        results: list[dict] = []
        names = list(eval_results.keys())
        n = len(names)

        for i, name in enumerate(names):
            ctx = eval_results[name]
            pct = 10 + 80 * (i + 1) / max(n, 1)
            cb("evaluate_all", pct, f"{i+1}/{n}: {name}")

            before = self.get_stage(name)
            action = self.check_promotion(name, ctx, progress_cb=progress_cb)

            if action == "promote":
                self.promote(name)
            elif action == "rollback":
                self.rollback(name)

            after = self.get_stage(name)
            results.append({
                "factor": name,
                "from_stage": before,
                "to_stage": after,
                "action": action,
            })

        cb("evaluate_all", 100, f"evaluated {n} factors")
        return results

    # ── 报告 ──────────────────────────────────────────────────────

    def summary(self) -> dict:
        """返回各阶段因子分布 (包括 QUARANTINED / RETIRED)"""
        all_stages = CANARY_STAGES + [QUARANTINED, RETIRED]
        stages: dict[str, list[str]] = {s: [] for s in all_stages}
        for name, st in self._states.items():
            stages.setdefault(st.stage, []).append(name)
        return {
            stage: {
                "count": len(items),
                "factors": items,
            }
            for stage, items in stages.items()
        }

    def summary_text(self) -> str:
        """可读报告"""
        data = self.summary()
        lines = ["=" * 56, "  CANARY DEPLOYMENT SUMMARY", "=" * 56]
        for stage in CANARY_STAGES + [QUARANTINED, RETIRED]:
            info = data.get(stage, {})
            names = info.get("factors", [])
            lines.append(f"  {stage:12s}: {info.get('count', 0):3d} factors")
            for n in names[:5]:
                lines.append(f"    - {n}")
            if len(names) > 5:
                lines.append(f"    ... (+{len(names) - 5} more)")
        lines.append("=" * 56)
        return "\n".join(lines)

    def factor_report(self, factor_name: str) -> dict:
        """单因子详情报告"""
        state = self.get_state(factor_name)
        return {
            "factor": state.factor,
            "stage": state.stage,
            "oos_bars": state.oos_bars,
            "cumulative_pnl": round(state.cumulative_pnl, 6),
            "promote_time": state.promote_time,
            "rollback_count": state.rollback_count,
            "quarantined": state.stage == QUARANTINED,
            "retired": state.stage == RETIRED,
            "history": state.history[-10:],  # 最近 10 条
        }

    # ── 内部 ──────────────────────────────────────────────────────

    def _next_stage(self, current: str) -> str | None:
        idx = CANARY_STAGES.index(current) if current in CANARY_STAGES else -1
        if 0 <= idx < len(CANARY_STAGES) - 1:
            return CANARY_STAGES[idx + 1]
        return None

    def _record_event(self, state: CanaryState, event: str, detail: dict) -> None:
        import time
        state.history.append({
            "timestamp": time.time(),
            "event": event,
            **detail,
        })
        if len(state.history) > 200:
            state.history = state.history[-200:]


def _now() -> float:
    import time
    return time.time()
