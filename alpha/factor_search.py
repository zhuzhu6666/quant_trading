"""alpha/factor_search.py — 因子表达式搜索 (T15.3, 2026-06-02)

L2 因子自动化核心. 两种策略:
1. Random search (baseline): 随机生成 AST, 评估 IC, 选 top-K
2. Genetic Programming (GP): 种群 + 交叉/变异/选择, 多代进化

v1 先做 Random search, 跑 1000 候选 / 1 小时, 选 top-50.
v2 加 GP.
"""
from __future__ import annotations

import logging
import random
import time as _time
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd

from alpha.factor_dsl import FactorNode, parse_dsl, ALLOWED_OPS, ALLOWED_LEAVES
from alpha.factor_score_evaluator import FactorScoreEvaluator, ExpressionScore

logger = logging.getLogger(__name__)


# ── AST 生成器 ────────────────────────────────────────────────────
def random_leaf() -> FactorNode:
    """随机叶子: 列名或常量"""
    if random.random() < 0.7:
        # 列名
        return FactorNode(op=random.choice(list(ALLOWED_LEAVES)))
    else:
        # 常量
        if random.random() < 0.5:
            return FactorNode(op="const", args=[random.randint(2, 50)])
        else:
            return FactorNode(op="const", args=[round(random.uniform(0.1, 5.0), 2)])


def random_unary_op() -> FactorNode:
    """随机一元算子 (sign / abs / log / sqrt) 包一个随机子节点"""
    op = random.choice(["sign", "abs", "log", "sqrt"])
    return FactorNode(op=op, args=[random_node(depth=0, max_depth=3)])


def random_ts_op(depth: int, max_depth: int) -> FactorNode:
    """随机时序算子: ts_mean / ts_std / ts_corr / delta / delay"""
    choice = random.choice(["ts_unary", "ts_binary"])
    if choice == "ts_unary":
        op = random.choice(["ts_mean", "ts_std", "ts_sum", "ts_min", "ts_max",
                            "ts_rank", "delta", "delay", "ts_decay_linear"])
        n = random.choice([3, 5, 10, 20, 30, 50])
        return FactorNode(op=op, args=[random_node(depth + 1, max_depth), n])
    else:
        op = "ts_corr"
        n = random.choice([5, 10, 20, 30, 50])
        return FactorNode(
            op=op,
            args=[random_node(depth + 1, max_depth), random_node(depth + 1, max_depth), n],
        )


def random_node(depth: int = 0, max_depth: int = 4) -> FactorNode:
    """
    递归生成随机 AST 节点.
    depth=0 是最浅, max_depth=4 限制最深.
    """
    if depth >= max_depth:
        return random_leaf()
    # 概率分布
    r = random.random()
    if r < 0.35:
        return random_leaf()
    elif r < 0.55:
        return random_unary_op()
    elif r < 0.85:
        return random_ts_op(depth + 1, max_depth)
    else:
        # 二元算术 (递归加深)
        op = random.choice(["+", "-", "*", "/"])
        return FactorNode(
            op=op,
            args=[random_node(depth + 1, max_depth), random_node(depth + 1, max_depth)],
        )


def generate_random_expressions(n: int, max_depth: int = 4, seed: int = 42) -> list[str]:
    """生成 n 个随机表达式字符串"""
    random.seed(seed)
    expressions = []
    for _ in range(n):
        try:
            ast = random_node(max_depth=max_depth)
            expressions.append(ast.to_string())
        except Exception:
            continue
    return expressions


# ── 搜索 orchestrator ───────────────────────────────────────────
@dataclass
class SearchResult:
    """搜索结果"""
    candidates: list[ExpressionScore] = field(default_factory=list)
    top_k: list[ExpressionScore] = field(default_factory=list)
    n_total: int = 0
    n_valid: int = 0
    n_healthy: int = 0
    n_watch: int = 0
    n_decaying: int = 0
    elapsed_sec: float = 0.0
    avg_time_per_expr: float = 0.0

    def to_dict(self) -> dict:
        return {
            "n_total": self.n_total,
            "n_valid": self.n_valid,
            "n_healthy": self.n_healthy,
            "n_watch": self.n_watch,
            "n_decaying": self.n_decaying,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "avg_time_per_expr": round(self.avg_time_per_expr, 4),
            "top_k": [s.to_dict() for s in self.top_k],
        }


class FactorSearch:
    """
    因子搜索器 — 随机搜索 + (未来) GP

    用法:
        evaluator = FactorScoreEvaluator(df, forward_period=1)
        search = FactorSearch(evaluator)
        result = search.random_search(n_candidates=1000, top_k=50, verbose=True)
    """

    def __init__(self, evaluator: FactorScoreEvaluator):
        self.evaluator = evaluator

    def random_search(
        self,
        n_candidates: int = 1000,
        top_k: int = 50,
        max_depth: int = 4,
        seed: int = 42,
        verbose: bool = True,
    ) -> SearchResult:
        """
        随机搜索 n_candidates 个候选, 选 top_k 按 score.

        Args:
            n_candidates: 生成的候选数
            top_k: 选 top_k
            max_depth: AST 最大深度
            seed: 随机种子
            verbose: 每 100 打印进度
        """
        t0 = _time.time()
        expressions = generate_random_expressions(n_candidates, max_depth=max_depth, seed=seed)
        if verbose:
            logger.info(f"[RandomSearch] 生成 {len(expressions)} 候选 (max_depth={max_depth})")

        # 评估
        scores = self.evaluator.score_batch(expressions, verbose=False)
        if verbose:
            for i, s in enumerate(scores):
                if (i + 1) % 100 == 0:
                    logger.info(
                        f"  [{i+1}/{len(scores)}] last={s.expression[:40]:40s} "
                        f"score={s.score:.1f} status={s.status}"
                    )

        # 排序 + top-k
        valid = [s for s in scores if s.status != "UNKNOWN"]
        valid.sort(key=lambda s: s.score, reverse=True)
        top_k_list = valid[:top_k]

        elapsed = _time.time() - t0
        result = SearchResult(
            candidates=scores,
            top_k=top_k_list,
            n_total=len(scores),
            n_valid=len(valid),
            n_healthy=sum(1 for s in valid if s.status == "HEALTHY"),
            n_watch=sum(1 for s in valid if s.status == "WATCH"),
            n_decaying=sum(1 for s in valid if s.status == "DECAYING"),
            elapsed_sec=elapsed,
            avg_time_per_expr=elapsed / max(1, len(scores)),
        )
        if verbose:
            logger.info(
                f"[RandomSearch] 完成: n_valid={result.n_valid}/{result.n_total} | "
                f"healthy={result.n_healthy} watch={result.n_watch} decaying={result.n_decaying} | "
                f"top1_score={top_k_list[0].score if top_k_list else 0:.1f} | "
                f"{elapsed:.1f}s ({result.avg_time_per_expr*1000:.1f}ms/expr)"
            )
        return result
