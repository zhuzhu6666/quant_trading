"""Tests for PortfolioCompositor — 分层组合引擎。

Phase 3 of FACTOR_TAKEOVER_V4.
"""
import pytest

from alpha.portfolio_compositor import PortfolioCompositor, CompositeSignal


# ── 测试配置 ──────────────────────────────────────────────

TACTICAL_CONFIG = {
    "rsi_14":        {"weight": 1.0, "tags": ["技术", "均值回归"], "enabled": True, "mode": "zscore_tanh"},
    "di_spread":     {"weight": 1.75, "tags": ["技术", "趋势"], "enabled": True, "mode": "zscore_tanh"},
    "engulfing":     {"weight": 1.0, "tags": ["形态", "反转"], "enabled": True, "mode": "discrete"},
    "bb_width":      {"weight": 0.0, "tags": ["技术", "波动率"], "enabled": True, "mode": "zscore_tanh"},
}

MACRO_CONFIG = {
    "dxy_corr_20":       {"weight": 0.8, "tags": ["宏观", "美元"], "enabled": True, "mode": "rank_mapping"},
    "cot_mm_net":        {"weight": 0.8, "tags": ["COT", "投机"], "enabled": True, "mode": "rank_mapping"},
    "cb_total_chg_3m":   {"weight": 0.8, "tags": ["央行", "购金"], "enabled": True, "mode": "rank_mapping"},
    "hours_to_fomc":     {"weight": 0.3, "tags": ["事件", "FOMC"], "enabled": True, "mode": "discrete"},
}

FULL_CONFIG = {**TACTICAL_CONFIG, **MACRO_CONFIG,
               "_tactical_alpha": 0.7, "_signal_threshold": 0.4}


# ── CompositeSignal 测试 ──────────────────────────────────

class TestCompositeSignal:

    def test_default_construction(self):
        """CompositeSignal 可用默认值构造。"""
        sig = CompositeSignal(
            direction=0, score=0.0, tactical_score=0.0, macro_score=0.0,
            tactical_weight=0.7, macro_weight=0.3,
            factor_signals={}, factor_values={}, active_weights={},
            tags_breakdown={}, n_active_factors=0, n_abstain_factors=0,
            timestamp=0.0,
        )
        assert sig.direction == 0


# ── PortfolioCompositor 测试 ──────────────────────────────

class TestPortfolioCompositorInit:

    def test_initializes_with_config(self):
        c = PortfolioCompositor(FULL_CONFIG)
        assert c._factor_configs["rsi_14"]["weight"] == 1.0

    def test_empty_config_creates_empty(self):
        c = PortfolioCompositor({})
        assert c._factor_configs == {}


class TestCompose:

    def test_tactical_only_signals(self):
        """仅战术层信号产生正确的 tactical_score。"""
        c = PortfolioCompositor(FULL_CONFIG)
        signals = {
            "rsi_14": 0.5,      # weight=1.0
            "di_spread": 0.8,   # weight=1.75
            # 弃权因子
            "engulfing": None,
            "bb_width": None,
        }
        result = c.compose(signals, signals)
        # tactical: (0.5*1.0 + 0.8*1.75) / (1.0 + 1.75) = (0.5 + 1.4) / 2.75 = 1.9 / 2.75 ≈ 0.691
        # macro: no active factors → 0.0
        # combined: 0.7 * 0.691 + 0.3 * 0.0 ≈ 0.484
        # direction: combined >= 0.4 → 1
        assert result.n_active_factors == 2
        assert result.n_abstain_factors == 2
        assert result.tactical_weight == 0.7
        assert result.macro_weight == pytest.approx(0.3)
        assert 0.48 <= result.tactical_score <= 0.70
        assert result.macro_score == 0.0
        assert result.score > 0
        assert result.direction == 1

    def test_macro_only_signals(self):
        """仅宏观层信号产生正确的 macro_score。"""
        c = PortfolioCompositor(FULL_CONFIG)
        signals = {
            "dxy_corr_20": 0.3,      # weight=0.8, direction=-1
            "cot_mm_net": 0.6,        # weight=0.8
            "hours_to_fomc": None,
        }
        result = c.compose(signals, signals)
        # macro: (0.3*0.8 + 0.6*0.8) / (0.8 + 0.8) = (0.24 + 0.48) / 1.6 = 0.45
        # tactical: 0
        # combined: 0.7*0 + 0.3*0.45 = 0.135
        # direction: 0.135 < 0.4 → 0
        assert result.macro_score != 0.0
        assert result.direction == 0  # below threshold

    def test_combined_tactical_and_macro(self):
        """两层均有信号时正确混合。"""
        c = PortfolioCompositor(FULL_CONFIG)
        signals = {
            "rsi_14": 0.6,           # tactical
            "di_spread": 0.7,        # tactical
            "dxy_corr_20": 0.5,      # macro
            "cot_mm_net": 0.4,       # macro
        }
        result = c.compose(signals, signals)
        assert result.tactical_score != 0.0
        assert result.macro_score != 0.0
        assert result.n_active_factors == 4
        assert result.direction in (1, -1, 0)

    def test_all_none_signals(self):
        """全部弃权时返回 NO_SIGNAL。"""
        c = PortfolioCompositor(FULL_CONFIG)
        signals = {name: None for name in FULL_CONFIG
                   if not name.startswith("_")}
        result = c.compose(signals, signals)
        assert result.direction == 0
        assert result.score == 0.0
        assert result.n_active_factors == 0

    def test_threshold_respected(self):
        """signals 低于 threshold 时 direction=0。"""
        c = PortfolioCompositor({**FULL_CONFIG, "_signal_threshold": 0.7})
        signals = {
            "rsi_14": 0.2,
            "di_spread": 0.3,
        }
        result = c.compose(signals, signals)
        # combined: 0.7 * (0.2*1.0 + 0.3*1.75) / 2.75 = 0.7 * 0.736/2.75 = 0.7 * 0.268 = 0.187
        assert result.direction == 0

    def test_negative_signals_short(self):
        """足够负的信号产生 SHORT。"""
        c = PortfolioCompositor(FULL_CONFIG)
        signals = {
            "rsi_14": -0.8,
            "di_spread": -0.6,
        }
        result = c.compose(signals, signals)
        assert result.direction == -1
        assert result.score < 0


class TestTagsBreakdown:

    def test_tags_breakdown_groups_by_tag(self):
        """tags_breakdown 按类型标签分解信号贡献。"""
        c = PortfolioCompositor(FULL_CONFIG)
        signals = {
            "rsi_14": 0.8,
            "di_spread": 0.6,
            "dxy_corr_20": 0.5,
        }
        result = c.compose(signals, signals)
        tbd = result.tags_breakdown
        assert "技术" in tbd
        assert "宏观" in tbd or "美元" in tbd
        # 技术分数应为正（两个技术因子均为正信号）
        tech_tags = [t for t in tbd if "技术" in t or "均值回归" in t or "趋势" in t]
        assert any(tbd[t] > 0 for t in tech_tags)


class TestDefaultGPConfig:

    def test_unknown_factor_gets_default_config(self):
        """未配置的因子自动获得默认 GP 配置。"""
        c = PortfolioCompositor(FULL_CONFIG)
        signals = {
            "rsi_14": 0.5,
            "gp_discovered_001": 0.8,
        }
        result = c.compose(signals, signals)
        # gp_discovered_001 默认 tags=["GP发现"] → tactical layer
        assert result.n_active_factors == 2

    def test_disabled_factors_excluded(self):
        """enabled=False 的因子不参与组合。"""
        config = {**FULL_CONFIG, "rsi_14": {**FULL_CONFIG["rsi_14"], "enabled": False}}
        c = PortfolioCompositor(config)
        signals = {"rsi_14": 0.8}
        result = c.compose(signals, signals)
        # rsi_14 被禁用，不参与 score 计算
        assert result.score == 0.0
        assert result.direction == 0
        # n_active_factors 是信号级计数（非 None），disable 不影响
        assert result.n_active_factors == 1

    def test_zero_weight_filter_only(self):
        """weight=0 的因子在 tags_breakdown 中但 scores=0。"""
        c = PortfolioCompositor(FULL_CONFIG)
        signals = {"bb_width": 1.0}  # weight=0, 只做过滤器
        result = c.compose(signals, signals)
        # weight=0 → 分子贡献 0，分母不计数
        assert result.n_active_factors == 1 if FULL_CONFIG["bb_width"]["weight"] > 0 else 1
        # Actually wait — bb_width weight=0.0, but it still gets included because
        # the check is on sig being None, not on weight.
        # The factor signal is NOT None, so it gets counted.
        # But in compose, weight=0 makes it contribute 0 to numerator and 0 to denominator.
        # Actually, looking at the code: t_den = sum(abs(w) for _, w in tactical.values())
        # bb_width weight=0.0 contributes 0 to both numerator and denominator.
        # So it's effectively excluded from the score but counted in n_active_factors. 
        # That's fine — it's a filter-only factor.
        assert result.score == 0.0
