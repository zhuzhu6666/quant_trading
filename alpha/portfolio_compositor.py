"""PortfolioCompositor — 分层组合引擎。

两层架构:
  - Tactical Layer (70%): 技术/量价/形态/波动率/GP因子
  - Macro Layer (30%):    美元/利率/持仓/COT/央行/事件因子

设计文档: docs/architecture.md
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# CompositeSignal 数据结构
# ═══════════════════════════════════════════════════════════

@dataclass
class CompositeSignal:
    """综合信号 — 包含两层分解、权重和标签明细。"""

    direction: int                 # 1=LONG, -1=SHORT, 0=NO_SIGNAL
    score: float                   # 综合信号强度 ∈ [-1, +1]
    tactical_score: float          # 技术层信号强度 ∈ [-1, +1]
    macro_score: float             # 宏观层信号强度 ∈ [-1, +1]
    tactical_weight: float         # 当前战术层权重 (初始 0.7)
    macro_weight: float            # 当前宏观层权重 (初始 0.3)
    factor_signals: dict[str, float | None]   # 所有归一化信号
    factor_values: dict[str, float | None]    # 所有原始值
    active_weights: dict[str, float]           # 本 tick 实际参与组合的权重
    tags_breakdown: dict[str, float]           # {类型标签: 该类型贡献的 score}
    n_active_factors: int                      # 非 None 信号的因子数
    n_abstain_factors: int                     # 弃权因子数
    timestamp: float                           # bar 时间戳


# ═══════════════════════════════════════════════════════════
# PortfolioCompositor
# ═══════════════════════════════════════════════════════════

class PortfolioCompositor:
    """分层组合引擎。

    战术层和宏观层分别做加权归一化，再按比例混合。

    Args:
        config: 来自 RuntimeConfig 的因子配置 dict。
                格式: {factor_name: {weight, tags, enabled, ...}}
                顶层键 _tactical_alpha 和 _signal_threshold 控制混合参数。
    """

    # 宏观层标签关键词 — 匹配 tags 中任意一个即归入 Macro Layer
    MACRO_TAGS = {"宏观", "COT", "央行", "持仓", "美元", "利率", "事件", "日历"}

    def __init__(self, config: dict[str, Any]):
        self._factor_configs: dict[str, dict] = dict(config or {})
        # 提取顶层控制参数
        self._tactical_alpha = float(self._factor_configs.get("_tactical_alpha", 0.7))
        self._signal_threshold = float(self._factor_configs.get("_signal_threshold", 0.4))

    # ── 核心接口 ────────────────────────────────────────

    def compose(
        self,
        signals: dict[str, float | None],
        factor_values: dict[str, float | None],
        timestamp: float | None = None,
    ) -> CompositeSignal:
        """组合所有因子信号生成 CompositeSignal。

        Args:
            signals: 归一化信号 {name: signal ∈ [-1, +1] or None}
            factor_values: 原始因子值 {name: raw_value}

        Returns:
            CompositeSignal 包含方向、强度、两层分解。
        """
        # 1. 按 tags 分组
        tactical: dict[str, tuple[float, float]] = {}   # name → (signal, weight)
        macro: dict[str, tuple[float, float]] = {}

        for name, sig in signals.items():
            if sig is None:
                continue
            cfg = self._factor_configs.get(name, self._default_gp_config(name))
            if not cfg.get("enabled", True):
                continue
            w = cfg.get("weight", 1.0)
            tags = cfg.get("tags", [])
            if any(t in self.MACRO_TAGS for t in tags):
                macro[name] = (sig, w)
            else:
                tactical[name] = (sig, w)

        # 2. 战术层加权平均
        t_num = sum(sig * w for sig, w in tactical.values())
        t_den = sum(abs(w) for _, w in tactical.values())
        tactical_score = t_num / t_den if abs(t_den) > 1e-10 else 0.0

        # 3. 宏观层加权平均
        m_num = sum(sig * w for sig, w in macro.values())
        m_den = sum(abs(w) for _, w in macro.values())
        macro_score = m_num / m_den if abs(m_den) > 1e-10 else 0.0

        # 4. 混合
        combined = self._tactical_alpha * tactical_score + (
            1 - self._tactical_alpha
        ) * macro_score

        # 5. 方向判定
        threshold = self._signal_threshold
        if combined >= threshold:
            direction = 1
        elif combined <= -threshold:
            direction = -1
        else:
            direction = 0

        # 6. 标签分解
        tags_breakdown = self._compute_tags_breakdown(signals)

        # 7. 构建 CompositeSignal
        all_weights: dict[str, float] = {}
        for name, (_, w) in {**tactical, **macro}.items():
            all_weights[name] = w

        return CompositeSignal(
            direction=direction,
            score=round(combined, 6),
            tactical_score=round(tactical_score, 6),
            macro_score=round(macro_score, 6),
            tactical_weight=self._tactical_alpha,
            macro_weight=1 - self._tactical_alpha,
            factor_signals=dict(signals),
            factor_values=dict(factor_values),
            active_weights=all_weights,
            tags_breakdown=tags_breakdown,
            n_active_factors=sum(
                1 for s in signals.values() if s is not None
            ),
            n_abstain_factors=sum(
                1 for s in signals.values() if s is None
            ),
            timestamp=float(timestamp if timestamp is not None else time.time()),
        )

    # ── 标签分解 ─────────────────────────────────────────

    def _compute_tags_breakdown(
        self, signals: dict[str, float | None]
    ) -> dict[str, float]:
        """按类型标签分解信号贡献。"""
        tag_scores: dict[str, float] = defaultdict(float)
        tag_weights: dict[str, float] = defaultdict(float)
        for name, sig in signals.items():
            if sig is None:
                continue
            cfg = self._factor_configs.get(name, self._default_gp_config(name))
            w = cfg.get("weight", 1.0)
            for t in cfg.get("tags", []):
                tag_scores[t] += sig * w
                tag_weights[t] += abs(w)
        return {
            t: round(tag_scores[t] / tag_weights[t], 3)
            if abs(tag_weights[t]) > 1e-10
            else 0.0
            for t in tag_scores
        }

    # ── 默认配置 ─────────────────────────────────────────

    def _default_gp_config(self, name: str) -> dict:
        """GP 发现因子的默认配置, 尝试从 GPClassifier 获取标签。"""
        tags = self._try_classify_gp(name)
        return {
            "enabled": True,
            "weight": 0.3,
            "tags": tags or ["GP发现"],
            "source": "gp",
        }

    def _try_classify_gp(self, name: str) -> list[str] | None:
        """尝试用 GPClassifier 分类因子名称/表达式."""
        try:
            from alpha.gp_classifier import classify_expr
            tags = classify_expr(name)
            if tags and tags != ["GP发现"]:
                return tags
        except Exception as e:
            logger.debug("GP classifier unavailable for '%s': %s", name, e)
        return None

    def refresh_configs(self, factor_names: list[str] | None = None):
        """为新因子自动添加默认配置。

        Args:
            factor_names: 新因子列表。None 时从 factor_registry 扫描。
        """
        if factor_names is None:
            try:
                from alpha.registry import factor_registry
                factor_names = factor_registry.list()
            except Exception:
                return
        for name in factor_names:
            if name not in self._factor_configs:
                self._factor_configs[name] = self._default_gp_config(name)

    def update_weights(self, weights: dict[str, float]) -> None:
        """热更新因子权重 (供 RuntimeConfig 订阅回调)。

        Args:
            weights: {factor_name: new_weight}
        """
        for name, w in weights.items():
            if name not in self._factor_configs:
                self._factor_configs[name] = self._default_gp_config(name)
            self._factor_configs[name]["weight"] = float(w)
        logger.debug("PortfolioCompositor: updated weights for %d factors", len(weights))
