"""alpha/factor_score_evaluator.py — 因子评分器 (T15.2, 2026-06-02)

L2 因子自动化. 接收 DSL 表达式 (或 AST), 在历史 bar 上:
1. evaluate_dsl 算出因子值
2. 算跟 forward return 的 rolling IC
3. 多维评分 (复用 FactorHealth 评分)
4. 输出 ExpressionScore dataclass

跟 alpha/factor_health.py 关系:
- factor_health.py 评估已注册因子 (post-hoc)
- factor_score_evaluator.py 评估候选 DSL 表达式 (pre-registration)
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import numpy as np
import pandas as pd

from alpha.factor_dsl import parse_dsl, evaluate_dsl, FactorNode
from alpha.factor_health import FactorHealth, FactorHealthStatus
from alpha.ic_tracker import safe_corrcoef

logger = logging.getLogger(__name__)


@dataclass
class ExpressionScore:
    """一个 DSL 表达式的评分结果"""
    expression: str
    signed_ic_mean: float = 0.0
    abs_ic_mean: float = 0.0
    direction: int = 0
    polarity: str = "unknown"
    ic_stability: float = 0.0
    ic_decay_rate: float = 0.0
    n_obs: int = 0
    score: float = 0.0          # 综合分 (0-100)
    status: str = "UNKNOWN"     # HEALTHY / WATCH / DECAYING / DEAD / UNKNOWN
    computation_time_sec: float = 0.0
    forward_period: int = 1
    candidate_validation: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class FactorScoreEvaluator:
    """
    因子评分器 — 评估 DSL 表达式

    用法:
        evaluator = FactorScoreEvaluator(df, forward_period=1)
        score = evaluator.score_expression("ts_corr(close, volume, 20)")
        # 或:
        scores = evaluator.score_batch(["expr1", "expr2", ...])
    """

    def __init__(self, df: pd.DataFrame, forward_period: int = 1,
                 min_n_obs: int = 30, timeout_sec: float = 30.0):
        self.df = df
        self.forward_period = forward_period
        self.min_n_obs = min_n_obs
        self.timeout_sec = timeout_sec
        # forward returns (默认 1 bar forward)
        self.forward_returns = self._compute_forward_returns(df, forward_period)

    def _compute_forward_returns(self, df: pd.DataFrame, period: int) -> np.ndarray:
        """算 forward return: (close[i+period] - close[i]) / close[i]"""
        close = df["close"].values
        n = len(close)
        out = np.full(n, np.nan)
        for i in range(n - period):
            if close[i] > 0 and np.isfinite(close[i + period]):
                out[i] = (close[i + period] - close[i]) / close[i]
        return out

    def score_expression(self, expression: str) -> ExpressionScore:
        """评估单个 DSL 表达式的 IC 评分"""
        t0 = _time.time()
        score = ExpressionScore(
            expression=expression,
            forward_period=self.forward_period,
        )
        try:
            # 1. 算因子值
            values = evaluate_dsl(expression, self.df, timeout_sec=self.timeout_sec)
            if values is None or len(values) != len(self.df):
                score.error = f"evaluate_dsl returned invalid result"
                return score
            # 2. 算 IC 序列
            ic_series = self._compute_ic_series(values, self.forward_returns)
            score.n_obs = len(ic_series)
            if score.n_obs < self.min_n_obs:
                score.error = f"n_obs={score.n_obs} < min_n_obs={self.min_n_obs}"
                return score
            # 3. 多维评分
            score.signed_ic_mean = float(np.mean(ic_series))
            score.abs_ic_mean = float(np.mean(np.abs(ic_series)))
            score.direction = 1 if score.signed_ic_mean > 0.0 else -1 if score.signed_ic_mean < 0.0 else 0
            score.polarity = (
                "positive"
                if score.direction > 0
                else "negative"
                if score.direction < 0
                else "unknown"
            )
            score.candidate_validation = self._candidate_validation(
                values,
                ic_series,
                direction=score.direction,
                signed_ic_mean=score.signed_ic_mean,
            )
            score.ic_stability = self._stability(ic_series)
            score.ic_decay_rate = self._decay_rate(ic_series)
            # 4. 综合分 (跟 FactorHealth 公式一致, 简化版: 只用 mean_abs + stability + decay)
            mean_abs_score = min(100.0, score.abs_ic_mean / 0.04 * 100.0)
            decay_score = max(0.0, min(100.0, score.ic_decay_rate * 100.0))
            score.score = (mean_abs_score * 0.5
                          + score.ic_stability * 0.3
                          + decay_score * 0.2)
            # 5. 状态
            if score.score >= 70:
                score.status = "HEALTHY"
            elif score.score >= 40:
                score.status = "WATCH"
            else:
                score.status = "DECAYING"
        except Exception as e:
            score.error = f"{type(e).__name__}: {str(e)[:100]}"
            score.status = "DEAD"
        finally:
            score.computation_time_sec = _time.time() - t0
        return score

    def _candidate_validation(
        self,
        values: np.ndarray,
        primary_ic_series: np.ndarray,
        *,
        direction: int,
        signed_ic_mean: float,
    ) -> dict[str, Any]:
        """Attach reproducible research evidence without granting admission."""
        index = self.df.index
        pit_passed = bool(
            self.forward_period > 0
            and index.is_monotonic_increasing
            and index.is_unique
        )
        multi_forward: dict[str, dict[str, Any]] = {}
        for period in (1, 3, 5):
            if len(self.df) <= period + 30:
                continue
            series = self._compute_ic_series(
                values,
                self._compute_forward_returns(self.df, period),
            )
            mean_ic = float(np.mean(series)) if len(series) else 0.0
            multi_forward[str(period)] = {
                "signed_ic_mean": mean_ic,
                "magnitude_ic_mean": abs(mean_ic),
                "n_obs": int(len(series)),
                "direction_consistent": bool(
                    direction in {-1, 1}
                    and mean_ic * direction > 0.0
                    and len(series) >= self.min_n_obs
                ),
            }
        consistent_forward = sum(
            bool(item["direction_consistent"])
            for item in multi_forward.values()
        )
        multi_forward_passed = bool(
            len(multi_forward) >= 2
            and consistent_forward == len(multi_forward)
        )

        folds = [chunk for chunk in np.array_split(primary_ic_series, 3) if len(chunk)]
        fold_results = [
            {
                "signed_ic_mean": float(np.mean(chunk)),
                "n_obs": int(len(chunk)),
                "direction_consistent": bool(
                    direction in {-1, 1}
                    and float(np.mean(chunk)) * direction > 0.0
                ),
            }
            for chunk in folds
        ]
        walk_forward_passed = bool(
            len(fold_results) == 3
            and sum(bool(item["direction_consistent"]) for item in fold_results)
            >= 2
        )
        regime_column = (
            "regime_id"
            if "regime_id" in self.df.columns
            else "regime"
            if "regime" in self.df.columns
            else ""
        )
        regime_ids = (
            sorted(
                {
                    str(item)
                    for item in self.df[regime_column].dropna().tolist()
                    if str(item)
                }
            )
            if regime_column
            else []
        )
        return {
            "schema_version": "factor_candidate_validation.v1",
            "direction": direction if direction in {-1, 1} else None,
            "polarity": (
                "positive" if direction == 1 else "negative" if direction == -1 else None
            ),
            "signed_ic_mean": float(signed_ic_mean),
            "magnitude_ic_mean": abs(float(signed_ic_mean)),
            "pit_passed": pit_passed,
            "walk_forward_passed": walk_forward_passed,
            "multi_forward_passed": multi_forward_passed,
            "cost_test_passed": False,
            "execution_evidence_complete": False,
            "contamination_status": "unknown",
            "regime_ids": regime_ids,
            "walk_forward": {"folds": fold_results},
            "multi_forward": multi_forward,
            "cost_test": {
                "status": "not_evaluated",
                "reason": "requires_cost_aware_oos_or_parity_evidence",
            },
        }

    def score_batch(self, expressions: list[str], verbose: bool = False) -> list[ExpressionScore]:
        """批量评估"""
        scores = []
        for i, expr in enumerate(expressions):
            s = self.score_expression(expr)
            scores.append(s)
            if verbose and (i + 1) % 50 == 0:
                logger.info(
                    f"  scored {i+1}/{len(expressions)} | "
                    f"last score={s.score:.1f} status={s.status}"
                )
        return scores

    def _compute_ic_series(self, values: np.ndarray, fwd_returns: np.ndarray,
                            window: int = 1000, step: int = 20) -> np.ndarray:
        """算 IC 时间序列 (滑动 window 算 corrcoef)"""
        n = min(len(values), len(fwd_returns))
        vals = values[:n]
        rets = fwd_returns[:n]
        mask = ~(np.isnan(vals) | np.isnan(rets))
        vals = vals[mask]
        rets = rets[mask]
        if len(vals) < 30:
            return np.array([])

        ics = []
        for i in range(30, len(vals), step):
            sub_v = vals[max(0, i - window):i]
            sub_r = rets[max(0, i - window):i]
            if len(sub_v) < 10:
                continue
            try:
                ic = safe_corrcoef(sub_v, sub_r, min_samples=10)
                if np.isfinite(ic):
                    ics.append(ic)
            except Exception:
                pass
        return np.array(ics)

    def _stability(self, ic_series: np.ndarray) -> float:
        """IC 稳定性: 1 - 变异系数 (CV), 转 0-100"""
        if len(ic_series) < 5:
            return 0.0
        mean_abs = float(np.mean(np.abs(ic_series)))
        if mean_abs < 1e-6:
            return 0.0
        std = float(np.std(ic_series))
        cv = std / mean_abs
        return max(0.0, min(100.0, (1.0 - cv) * 100.0))

    def _decay_rate(self, ic_series: np.ndarray) -> float:
        """decay ratio: 最近 25% / 之前 25% 的 |IC|"""
        n = len(ic_series)
        if n < 20:
            return 0.5
        q1 = ic_series[:n // 4]
        q4 = ic_series[3 * n // 4:]
        mean_q1 = float(np.mean(np.abs(q1)))
        mean_q4 = float(np.mean(np.abs(q4)))
        if mean_q1 < 1e-6:
            return 0.0
        return mean_q4 / mean_q1
