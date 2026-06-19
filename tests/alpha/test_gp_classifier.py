"""
tests/alpha/test_gp_classifier.py — GPClassifier 单元测试

覆盖所有分类模式、边缘情况、字符串/AST 输入路径。
"""

from __future__ import annotations

import pytest

from alpha.factor_dsl import FactorNode, FactorParser
from alpha.gp_classifier import GPClassifier, classify_expr


# ── 测试夹具 ──────────────────────────────────────────


@pytest.fixture
def classifier() -> GPClassifier:
    return GPClassifier()


@pytest.fixture
def parser() -> FactorParser:
    """辅佐: 将字符串解析为 FactorNode。"""
    return FactorParser


# ── 工具函数 ──────────────────────────────────────────


def _parse(expr: str) -> FactorNode:
    """快捷解析。"""
    return FactorParser(expr).parse()


# ══════════════════════════════════════════════════════
# 核心测试：分类模式
# ══════════════════════════════════════════════════════


class TestGPClassifierBasicPatterns:
    """基本模式匹配测试。"""

    def test_ts_corr_close_volume(self, classifier: GPClassifier):
        """ts_corr(close, volume, n) → ["量价"]"""
        expr = "ts_corr(close, volume, 20)"
        assert classifier.classify(expr) == ["量价"]

    def test_ts_corr_close_dxy(self, classifier: GPClassifier):
        """ts_corr(close, dxy, n) → ["宏观", "美元"]"""
        expr = "ts_corr(close, dxy, 20)"
        assert classifier.classify(expr) == ["宏观", "美元"]

    def test_delta_close(self, classifier: GPClassifier):
        """delta(close, n) → ["动量"]"""
        expr = "delta(close, 5)"
        assert classifier.classify(expr) == ["动量"]

    def test_zscore_close(self, classifier: GPClassifier):
        """zscore(close, n) → ["均值回归"]"""
        expr = "zscore(close, 20)"
        assert classifier.classify(expr) == ["均值回归"]

    def test_ts_std_close(self, classifier: GPClassifier):
        """ts_std(close, n) → ["波动率"]"""
        expr = "ts_std(close, 20)"
        assert classifier.classify(expr) == ["波动率"]

    def test_power_root(self, classifier: GPClassifier):
        """power 作为根节点 → ["非线性"]"""
        expr = "power(close, 2)"
        assert classifier.classify(expr) == ["非线性"]

    def test_log_root(self, classifier: GPClassifier):
        """log 作为根节点 → ["非线性"]"""
        expr = "log(close)"
        assert classifier.classify(expr) == ["非线性"]

    def test_sqrt_root(self, classifier: GPClassifier):
        """sqrt 作为根节点 → ["非线性"]"""
        expr = "sqrt(close)"
        assert classifier.classify(expr) == ["非线性"]

    def test_sqrt_near_root(self, classifier: GPClassifier):
        """sqrt 在 near-root（depth=1）→ ["非线性"]"""
        expr = "rank(sqrt(close))"
        assert classifier.classify(expr) == ["非线性"]

    def test_log_near_root_via_ts_mean(self, classifier: GPClassifier):
        """log 在 depth=1 仍视为 near-root → ["非线性"]"""
        expr = "ts_mean(log(close), 5)"
        assert classifier.classify(expr) == ["非线性"]

    def test_power_near_root_deep(self, classifier: GPClassifier):
        """power 在 depth=2 以内 → ["非线性"]"""
        expr = "rank(ts_mean(power(close, 2), 5))"
        assert classifier.classify(expr) == ["非线性"]

    def test_power_buried_deep(self, classifier: GPClassifier):
        """power 在 depth>=3 → 不视为非线性（太深）"""
        expr = "ts_corr(ts_mean(rank(power(close, 2)), 10), ts_mean(volume, 20), 30)"
        # root=ts_corr (depth 0) → ts_mean (depth 1) → rank (depth 2) → power (depth 3)
        # power at depth 3 is beyond near-root threshold (max_depth=2)
        tags = classifier.classify(expr)
        assert "非线性" not in tags
        assert "量价" in tags  # close in arg0 subtree, volume in arg1 subtree


class TestGPClassifierComposite:
    """复合 / GP发现 / 多标签测试。"""

    def test_single_leaf(self, classifier: GPClassifier):
        """单一叶子节点 → ["GP发现"]"""
        assert classifier.classify("close") == ["GP发现"]

    def test_simple_arithmetic_no_match(self, classifier: GPClassifier):
        """简单算术无匹配 → ["GP发现"]"""
        assert classifier.classify("close + volume") == ["GP发现"]

    def test_three_leaf_not_composite(self, classifier: GPClassifier):
        """正好 3 叶子（非 4+）→ 不标复合"""
        # ts_corr(close, volume, 20) — 3 leaves: close, volume, 20
        assert classifier.classify("ts_corr(close, volume, 20)") == ["量价"]

    def test_four_plus_leaves(self, classifier: GPClassifier):
        """4+ 叶子节点且无根模式匹配 → ["复合"]"""
        # close + volume + high + low — 4 leaves, root=+
        expr = "close + volume + high + low"
        assert classifier.classify(expr) == ["复合"]

    def test_five_leaf_nested(self, classifier: GPClassifier):
        """嵌套 5 叶无根模式匹配 → ["复合"]"""
        # ts_mean(close, 5) + ts_mean(volume, 10) — leaves: close, 5, volume, 10
        expr = "ts_mean(close, 5) + ts_mean(volume, 10)"
        assert classifier.classify(expr) == ["复合"]

    def test_complex_expression_no_match(self, classifier: GPClassifier):
        """复杂表达式无根模式 → ["复合"]"""
        expr = "ts_mean(close, 5) + delta(volume, 3) - ts_std(high, 10)"
        # leaves: close, 5, volume, 3, high, 10 = 6
        # root=+ (actually + and - create a tree)
        assert classifier.classify(expr) == ["复合"]


class TestGPClassifierEdgeCases:
    """边缘情况测试。"""

    def test_empty_string(self, classifier: GPClassifier):
        """空字符串 → ["GP发现"]"""
        assert classifier.classify("") == ["GP发现"]

    def test_whitespace_only(self, classifier: GPClassifier):
        """纯空白 → ["GP发现"]"""
        assert classifier.classify("   ") == ["GP发现"]

    def test_invalid_expression(self, classifier: GPClassifier):
        """非法表达式 → ["GP发现"]"""
        assert classifier.classify("close +++ volume") == ["GP发现"]

    def test_unknown_leaf(self, classifier: GPClassifier):
        """未知叶子（解析会抛异常）→ ["GP发现"]"""
        assert classifier.classify("unknown_column") == ["GP发现"]

    def test_malformed_parens(self, classifier: GPClassifier):
        """括号不匹配 → ["GP发现"]"""
        assert classifier.classify("ts_corr(close, volume, 20") == ["GP发现"]

    def test_none_input(self, classifier: GPClassifier):
        """None 输入 → ["GP发现"]"""
        assert classifier.classify(None) == ["GP发现"]  # type: ignore


class TestGPClassifierWithAST:
    """直接传入 FactorNode AST 测试。"""

    def test_ast_ts_corr_volume(self, classifier: GPClassifier):
        """AST 节点 → 量价"""
        node = _parse("ts_corr(close, volume, 20)")
        assert classifier.classify(node) == ["量价"]

    def test_ast_delta(self, classifier: GPClassifier):
        """AST 节点 → 动量"""
        node = _parse("delta(close, 5)")
        assert classifier.classify(node) == ["动量"]

    def test_ast_zscore(self, classifier: GPClassifier):
        """AST 节点 → 均值回归"""
        node = _parse("zscore(close, 20)")
        assert classifier.classify(node) == ["均值回归"]

    def test_ast_ts_std(self, classifier: GPClassifier):
        """AST 节点 → 波动率"""
        node = _parse("ts_std(close, 20)")
        assert classifier.classify(node) == ["波动率"]

    def test_ast_composite(self, classifier: GPClassifier):
        """AST 节点 → 复合"""
        node = _parse("close + volume + high + low")
        assert classifier.classify(node) == ["复合"]

    def test_ast_nonlinear_sqrt(self, classifier: GPClassifier):
        """AST sqrt 节点 → 非线性"""
        node = _parse("sqrt(close)")
        assert classifier.classify(node) == ["非线性"]


class TestGPClassifierMultiTag:
    """多标签场景测试。"""

    def test_power_with_delta(self, classifier: GPClassifier):
        """power 根节点 + delta 子节点 → ["非线性"] + 不复用 delta"""
        expr = "power(delta(close, 5), 2)"
        # root=power → 非线性; delta 不是根所以不标动量; 叶子: close, 5, 2 = 3 < 4
        assert set(classifier.classify(expr)) == {"非线性"}

    def test_nonlinear_and_composite(self, classifier: GPClassifier):
        """非线性 + 4+ 叶子 → ["非线性", "复合"]"""
        expr = "power(close + volume + high, 2)"
        # root=power → 非线性; leaves: close, volume, high, 2 = 4 → 复合
        tags = classifier.classify(expr)
        assert "非线性" in tags
        assert "复合" in tags

    def test_ts_corr_with_extra_leaves(self, classifier: GPClassifier):
        """ts_corr(close, volume, n) with 4+ leaves → ["量价", "复合"]"""
        expr = "ts_corr(ts_mean(close, 5), ts_mean(volume, 10), 20)"
        # ts_corr with close/volume → 量价; leaves: close, 5, volume, 10, 20 = 5
        tags = classifier.classify(expr)
        assert "量价" in tags
        assert "复合" in tags

    def test_ts_corr_dxy_with_extra_leaves(self, classifier: GPClassifier):
        """ts_corr(close, dxy) + extra leaves → ["宏观", "美元", "复合"]"""
        expr = "ts_corr(close, ts_decay_linear(dxy, 10), 20)"
        # ts_corr with close (in arg0 subtree) and dxy (in arg1 subtree) → 宏观 + 美元
        # leaves: close, dxy, 10, 20 = 4 → 复合
        tags = classifier.classify(expr)
        assert "宏观" in tags
        assert "美元" in tags
        assert "复合" in tags


class TestGPClassifierModuleHelper:
    """模块级 classify_expr 快捷函数测试。"""

    def test_helper_ts_corr(self):
        """快捷函数返回正确结果。"""
        assert classify_expr("ts_corr(close, volume, 20)") == ["量价"]

    def test_helper_delta(self):
        assert classify_expr("delta(close, 5)") == ["动量"]

    def test_helper_zscore(self):
        assert classify_expr("zscore(close, 20)") == ["均值回归"]

    def test_helper_empty(self):
        assert classify_expr("") == ["GP发现"]
