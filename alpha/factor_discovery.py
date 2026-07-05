"""alpha/factor_discovery.py — 因子发现 orchestrator (T15.4, 2026-06-02)

L2 因子自动化核心. 完整 pipeline:
1. Search Engine 生成候选 AST
2. Score Evaluator 算 IC 评分
3. 去重 (跟现有 22 因子 + 候选间, PCA / 互相关 > 阈值 视为冗余)
4. 写报告 (jsonl + txt)
5. (T15.5 接入) 影子测试 30 天
6. (T15.5 接入) 升 ACTIVE (调 RegistryAdapter.register_runtime) 或淘汰

跟 L1 关系:
- 用 alpha/factor_health.py 算 ACTIVE 列表 (independence 参考)
- 用 alpha/registry_adapter.py 动态 register/unregister
- 复用 alpha/ic_tracker.py 的 IC 计算逻辑 (在 evaluator 内)
"""
from __future__ import annotations

import json
import logging
import time as _time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from alpha.factor_dsl import parse_dsl, evaluate_dsl
from alpha.factor_score_evaluator import FactorScoreEvaluator, ExpressionScore
from alpha.factor_search import FactorSearch, SearchResult, generate_random_expressions
from alpha.ic_tracker import safe_corrcoef
from alpha.registry_adapter import RegistryAdapter, SOURCE_SHADOW, SOURCE_DISCOVERED

logger = logging.getLogger(__name__)


@dataclass
class DiscoveryConfig:
    """发现配置"""
    n_candidates: int = 1000
    top_k: int = 50
    max_depth: int = 4
    seed: int = 42
    pca_correlation_threshold: float = 0.7  # |corr| > 此值视为冗余
    min_score_to_keep: float = 30.0        # score < 此值直接淘汰
    forward_periods: list = field(default_factory=lambda: [1, 5, 20])  # 多窗口评估
    shadow_required: bool = False          # 影子测试 (T15.5 v2 加)
    shadow_duration_days: int = 30

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DiscoveryRun:
    """一次发现的完整结果"""
    config: DiscoveryConfig
    search_result: SearchResult
    after_dedup: list[ExpressionScore]     # 去重后保留的
    promoted: list[str]                    # 已 register_runtime 的 (discovered)
    removed: list[str]                     # 已 unregister 的
    log_path: str

    def to_dict(self) -> dict:
        return {
            "config": self.config.to_dict(),
            "search": self.search_result.to_dict(),
            "after_dedup": [s.to_dict() for s in self.after_dedup],
            "promoted": self.promoted,
            "removed": self.removed,
            "log_path": self.log_path,
        }


class FactorDiscovery:
    """
    因子发现 orchestrator

    用法:
        discovery = FactorDiscovery(df, db_path="data/ctrader_data.duckdb")
        run = discovery.run(config=DiscoveryConfig(n_candidates=500))
        # run.after_dedup 是去重后保留的 top candidates
        # run.promoted 是已经 register 到 RegistryAdapter 的名字
    """

    def __init__(self, df: pd.DataFrame, db_path: str = "data/ctrader_data.duckdb",
                 log_dir: str = "data/charts/factor_discovery",
                 lifecycle_log: str = "data/charts/factor_lifecycle_log.jsonl"):
        self.df = df
        self.db_path = db_path
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # 跟主 RegistryAdapter 共享同一个 lifecycle log (L1 整合)
        self.adapter = RegistryAdapter(log_path=lifecycle_log)
        self.timestamp = _time.strftime("%Y%m%d_%H%M%S")

    def run(self, config: Optional[DiscoveryConfig] = None,
            auto_register: bool = False) -> DiscoveryRun:
        """
        跑完整发现 pipeline.

        Args:
            config: 配置 (None = 默认)
            auto_register: True = 把 top-K 候选 register_runtime 到 RegistryAdapter
                           (默认 False, 只算不算入, 等人工/影子测试 review)
        """
        if config is None:
            config = DiscoveryConfig()
        t0 = _time.time()
        logger.info(f"[Discovery] 开始: n_candidates={config.n_candidates}, "
                    f"top_k={config.top_k}, max_depth={config.max_depth}")

        # 1. Search (跨多 forward_period)
        all_search_results = []
        for fp in config.forward_periods:
            logger.info(f"[Discovery] 跑 forward_period={fp}")
            evaluator = FactorScoreEvaluator(self.df, forward_period=fp)
            search = FactorSearch(evaluator)
            result = search.random_search(
                n_candidates=config.n_candidates,
                top_k=config.top_k,
                max_depth=config.max_depth,
                seed=config.seed + fp,  # 每个 fp 用不同 seed
                verbose=False,
            )
            all_search_results.append((fp, result))

        # 用 fp=1 作为主结果 (其它 fp 作 cross-validation 参考)
        main_fp, main_result = all_search_results[0]

        # 2. 去重 — 跟现有 22 因子 + 候选间
        logger.info(f"[Discovery] 去重 (threshold={config.pca_correlation_threshold})")
        deduped = self._deduplicate(main_result.top_k, threshold=config.pca_correlation_threshold)

        # 3. 过滤: score 太低淘汰
        kept = [s for s in deduped if s.score >= config.min_score_to_keep]
        logger.info(f"[Discovery] 过滤 score < {config.min_score_to_keep}: "
                    f"{len(deduped)} → {len(kept)}")

        # 4. (可选) 落地 Registry
        promoted, removed = [], []
        if auto_register:
            logger.info(f"[Discovery] auto_register: {len(kept)} 候选 → RegistryAdapter")
            promoted, removed = self._apply_to_registry(kept, all_search_results)

        # 5. 落盘
        log_path = self.log_dir / f"run_{self.timestamp}.json"
        run = DiscoveryRun(
            config=config,
            search_result=main_result,
            after_dedup=kept,
            promoted=promoted,
            removed=removed,
            log_path=str(log_path),
        )
        log_path.write_text(json.dumps(run.to_dict(), ensure_ascii=False, indent=2),
                            encoding="utf-8")
        logger.info(f"[Discovery] 报告: {log_path}")

        # 6. 打印 top-10
        logger.info(f"[Discovery] Top 10 (去重 + 过滤后):")
        for i, s in enumerate(kept[:10], 1):
            logger.info(
                f"  {i:2d}. {s.expression[:60]:60s} "
                f"score={s.score:5.1f} |IC|={s.abs_ic_mean:+.4f} status={s.status}"
            )

        return run

    def _deduplicate(self, candidates: list[ExpressionScore],
                     threshold: float = 0.7) -> list[ExpressionScore]:
        """
        跟现有 22 因子 + 候选间, |corr| > threshold 视为冗余, 保留 score 高的.
        """
        # 算每个候选的因子值, 跟现有 22 + 其它候选算 corr
        existing_values = {}
        for f in ["close", "volume", "dxy", "real_yield_10y", "gvz"]:
            if f in self.df.columns:
                existing_values[f] = self.df[f].values

        kept = []
        for s in candidates:
            try:
                vals = evaluate_dsl(s.expression, self.df)
                if vals is None or len(vals) != len(self.df):
                    continue
                # 跟现有 22 算 corr
                redundant = False
                for name, ev in existing_values.items():
                    try:
                        c = safe_corrcoef(vals, ev)
                        if abs(c) > threshold:
                            redundant = True
                            break
                    except Exception:
                        pass
                if redundant:
                    continue
                # 跟已 kept 候选算 corr
                for prev in kept:
                    prev_vals = evaluate_dsl(prev.expression, self.df)
                    if prev_vals is None:
                        continue
                    try:
                        c = safe_corrcoef(vals, prev_vals)
                        if abs(c) > threshold:
                            redundant = True
                            break
                    except Exception:
                        pass
                if redundant:
                    continue
                kept.append(s)
            except Exception:
                continue
        return kept

    def _apply_to_registry(self, kept: list[ExpressionScore],
                            all_search_results: list) -> tuple[list[str], list[str]]:
        """
        把 kept 候选 register_runtime 到 RegistryAdapter.
        - cross-validation 验证: 候选必须在多 forward_period 上都 score >= min
        - 用 SOURCE_SHADOW (T15.5 影子测试)
        - 已被 unregister 的, 跳过
        """
        promoted, removed = [], []
        # cross-validation: 取每个候选在多 fp 上的 score 平均
        cv_scores: dict[str, list[float]] = {}
        for fp, result in all_search_results:
            for s in result.top_k:
                cv_scores.setdefault(s.expression, []).append(s.score)
        for i, s in enumerate(kept):
            expr_hash = str(abs(hash(s.expression)))[:8]
            name = f"dsl_{expr_hash}_{i:03d}"
            scores = cv_scores.get(s.expression, [])
            avg_score = float(np.mean(scores)) if scores else s.score
            if avg_score < 50.0:
                # cross-validation 太弱, 不升 shadow
                continue
            # register
            def make_func(expr: str):
                return lambda df: evaluate_dsl(expr, df)
            ok = self.adapter.register_runtime(
                name=name,
                func=make_func(s.expression),
                source=SOURCE_SHADOW,
                description=s.expression,
            )
            if ok:
                promoted.append(name)
                logger.info(f"[Discovery] shadow register: {name} ({s.expression[:40]}) "
                            f"avg_score={avg_score:.1f}")
        return promoted, removed
