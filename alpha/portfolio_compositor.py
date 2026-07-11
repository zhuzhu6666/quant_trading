"""PortfolioCompositor — role-aware factor compositor."""

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
    composer_version: str = "factor_roles.v2"
    alpha_score: float = 0.0
    context_signals: dict[str, float] = field(default_factory=dict)
    factor_roles: dict[str, str] = field(default_factory=dict)
    n_active_alpha_factors: int = 0
    context_state: dict[str, Any] = field(default_factory=dict)
    redundancy_groups: dict[str, list[str]] = field(default_factory=dict)
    effective_alpha_factor_count: int = 0


VALID_FACTOR_ROLES = {"alpha", "context", "gate", "sizing"}
DEFAULT_FACTOR_ROLES = {
    "adx": "context",
    "atr_ratio": "context",
    "bb_width": "context",
    "keltner_width": "context",
    "hours_to_fomc": "gate",
    "hours_to_nfp": "gate",
    "hour_utc": "context",
    "day_of_week": "context",
}


def resolve_factor_role(name: str, cfg: dict[str, Any] | None = None) -> str:
    """Return the role used by the V2 composer; unknown factors remain alpha."""
    raw = ""
    if isinstance(cfg, dict):
        raw = str(cfg.get("role") or "").strip().lower()
    if raw == "observe":
        raw = "context"
    role = raw or DEFAULT_FACTOR_ROLES.get(name, "alpha")
    return role if role in VALID_FACTOR_ROLES else "alpha"


# ═══════════════════════════════════════════════════════════
# PortfolioCompositor
# ═══════════════════════════════════════════════════════════

class PortfolioCompositor:
    """Role-aware compositor.

    Only role=alpha factors decide direction. Context/gate/sizing factors are
    preserved for observability but carry zero directional contribution.
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
        # 1. 按 role + tags 分组。只有 alpha 因子进入方向评分。
        tactical: dict[str, tuple[float, float]] = {}   # name → (signal, weight)
        macro: dict[str, tuple[float, float]] = {}
        all_weights: dict[str, float] = {}
        factor_roles: dict[str, str] = {}
        context_signals: dict[str, float] = {}

        for name, sig in signals.items():
            cfg = self._factor_configs.get(name, self._default_gp_config(name))
            role = resolve_factor_role(name, cfg)
            factor_roles[name] = role
            all_weights[name] = 0.0
            if sig is None:
                continue
            if not cfg.get("enabled", True):
                continue
            if role != "alpha":
                context_signals[name] = float(sig)
                continue
            w = float(cfg.get("weight", 1.0) or 0.0)
            if w <= 0:
                continue
            all_weights[name] = w
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

        # 4. 动态层权重: 缺失的一层不再固定拖低另一层。
        if tactical and macro:
            tactical_weight = self._tactical_alpha
            macro_weight = 1 - self._tactical_alpha
        elif tactical:
            tactical_weight = 1.0
            macro_weight = 0.0
        elif macro:
            tactical_weight = 0.0
            macro_weight = 1.0
        else:
            tactical_weight = 0.0
            macro_weight = 0.0
        combined = tactical_weight * tactical_score + macro_weight * macro_score

        # 5. 方向判定
        threshold = self._signal_threshold
        if combined >= threshold:
            direction = 1
        elif combined <= -threshold:
            direction = -1
        else:
            direction = 0

        # 6. 标签分解只统计 alpha 贡献。
        tags_breakdown = self._compute_tags_breakdown(signals)
        context_state = self._build_context_state(signals, factor_values, factor_roles)
        redundancy_groups = self._build_redundancy_groups(factor_roles)
        n_alpha = len(tactical) + len(macro)

        # 7. 构建 CompositeSignal
        return CompositeSignal(
            direction=direction,
            score=round(combined, 6),
            tactical_score=round(tactical_score, 6),
            macro_score=round(macro_score, 6),
            tactical_weight=tactical_weight,
            macro_weight=macro_weight,
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
            alpha_score=round(combined, 6),
            context_signals=context_signals,
            factor_roles=factor_roles,
            n_active_alpha_factors=n_alpha,
            context_state=context_state,
            redundancy_groups=redundancy_groups,
            effective_alpha_factor_count=n_alpha,
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
            if not cfg.get("enabled", True) or resolve_factor_role(name, cfg) != "alpha":
                continue
            w = float(cfg.get("weight", 1.0) or 0.0)
            if w <= 0:
                continue
            for t in cfg.get("tags", []):
                tag_scores[t] += sig * w
                tag_weights[t] += abs(w)
        return {
            t: round(tag_scores[t] / tag_weights[t], 3)
            if abs(tag_weights[t]) > 1e-10
            else 0.0
            for t in tag_scores
        }

    def _build_context_state(
        self,
        signals: dict[str, float | None],
        factor_values: dict[str, float | None],
        factor_roles: dict[str, str],
    ) -> dict[str, Any]:
        def finite(name: str) -> float | None:
            value = signals.get(name)
            if value is None:
                return None
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
            return value if -1.0 <= value <= 1.0 else max(-1.0, min(1.0, value))

        vol_values = [v for v in (finite("bb_width"), finite("atr_ratio"), finite("keltner_width")) if v is not None]
        if vol_values:
            vol_score = max(vol_values, key=abs)
            volatility_state = "high" if vol_score >= 0.6 else "low" if vol_score <= -0.6 else "normal"
        else:
            vol_score = 0.0
            volatility_state = "unknown"

        adx_sig = finite("adx")
        if adx_sig is None:
            trend_strength_state = "unknown"
        elif adx_sig >= 0.5:
            trend_strength_state = "strong"
        elif adx_sig <= -0.5:
            trend_strength_state = "weak"
        else:
            trend_strength_state = "normal"

        event_values = [
            v for name, v in signals.items()
            if factor_roles.get(name) == "gate" and v is not None
        ]
        event_score = max((float(v) for v in event_values), default=0.0)
        event_window_state = "active" if event_score >= 0.9 else "near" if event_score > 0.0 else "none"

        macro_context_names = {
            "cot_extreme_signal", "real_yield_pct_rank", "dxy_corr_20",
            "slv_gld_ratio", "slv_tonnes_chg_20d", "silver_gold_holdings_ratio",
            "cb_total_chg_3m", "cb_china_chg_3m", "cb_russia_chg_3m",
            "cb_china_3m_zscore",
        }
        macro_context_values = []
        for name in macro_context_names:
            value = finite(name)
            if value is not None:
                macro_context_values.append(value)
        macro_context_score = (
            sum(float(value) for value in macro_context_values) / len(macro_context_values)
            if macro_context_values else 0.0
        )

        hour = factor_values.get("hour_utc")
        try:
            hour_i = int(float(hour)) if hour is not None else -1
        except (TypeError, ValueError):
            hour_i = -1
        if 0 <= hour_i < 7:
            session_state = "asia"
        elif 7 <= hour_i < 13:
            session_state = "europe"
        elif 13 <= hour_i < 21:
            session_state = "us"
        elif 21 <= hour_i < 24:
            session_state = "rollover"
        else:
            session_state = "unknown"

        return {
            "volatility_state": volatility_state,
            "volatility_score": round(float(vol_score), 6),
            "trend_strength_state": trend_strength_state,
            "trend_strength_score": round(float(adx_sig or 0.0), 6),
            "event_window_state": event_window_state,
            "event_window_score": round(float(event_score), 6),
            "session_state": session_state,
            "macro_context_score": round(float(macro_context_score), 6),
            "macro_evidence_count": len(macro_context_values),
            "macro_context_source": "external_factor_frame",
        }

    def _build_redundancy_groups(self, factor_roles: dict[str, str]) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for name, role in factor_roles.items():
            if role != "alpha":
                continue
            cfg = self._factor_configs.get(name, self._default_gp_config(name))
            group = str(cfg.get("redundancy_group") or "").strip()
            if group:
                groups[group].append(name)
        return {group: sorted(names) for group, names in groups.items()}

    # ── 默认配置 ─────────────────────────────────────────

    def _default_gp_config(self, name: str) -> dict:
        """GP 发现因子的默认配置, 尝试从 GPClassifier 获取标签。"""
        tags = self._try_classify_gp(name)
        return {
            "enabled": True,
            "weight": 0.3,
            "tags": tags or ["GP发现"],
            "role": DEFAULT_FACTOR_ROLES.get(name, "alpha"),
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

    def reload_configs(self, config: dict[str, Any]) -> None:
        self._factor_configs = dict(config or {})
        self._tactical_alpha = float(self._factor_configs.get("_tactical_alpha", 0.7))
        self._signal_threshold = float(self._factor_configs.get("_signal_threshold", 0.4))
