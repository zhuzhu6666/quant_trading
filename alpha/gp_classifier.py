"""
alpha/gp_classifier.py — GP因子 AST 表达式类型标签分类器

根据 AST 结构识别因子表达式的类型标签，用于因子分类、组合分层等场景。

用法:
    classifier = GPClassifier()
    tags = classifier.classify("ts_corr(close, volume, 20)")  # ["量价"]
    tags = classifier.classify(ast_node)                      # ["动量"]

导出:
    GPClassifier      类
    classify_expr     模块级快捷函数
"""

from __future__ import annotations

import logging
from typing import List, Optional, Union

from alpha.factor_dsl import FactorNode, FactorParser, FactorParseError

logger = logging.getLogger(__name__)


# ── AST 辅助函数 ─────────────────────────────────────────


def _get_leaf_name(node: FactorNode) -> Optional[str]:
    """叶子节点 → 列名；非叶子返回 None。"""
    if isinstance(node, FactorNode) and not node.args:
        return node.op
    return None


def _has_leaf_in_subtree(node, leaf_name: str) -> bool:
    """检查 AST 子树中是否存在指定列名的叶子节点。"""
    if not isinstance(node, FactorNode):
        return False
    if not node.args:
        return node.op == leaf_name
    return any(_has_leaf_in_subtree(a, leaf_name) for a in node.args if isinstance(a, FactorNode))


def _count_leaves(node: FactorNode) -> int:
    """递归统计 AST 中叶子节点总数。

    叶子定义：不包含任何 FactorNode 子节点的节点。
    包括列引用 ('close' etc.)、常量 ('const') 等。
    """
    if not isinstance(node, FactorNode):
        return 0
    has_factor_children = any(isinstance(a, FactorNode) for a in node.args)
    if not has_factor_children:
        return 1
    total = 0
    for a in node.args:
        if isinstance(a, FactorNode):
            total += _count_leaves(a)
    return total


def _has_nonlinear_near_root(node: FactorNode, depth: int = 0, max_depth: int = 2) -> bool:
    """检查 power / log / sqrt 是否出现在 root 或 near-root（depth <= max_depth）。

    "near-root" 意味着该算子对表达式整体语义有显著影响。
    """
    if not isinstance(node, FactorNode):
        return False
    if node.op in ("power", "log", "sqrt"):
        return True
    if depth < max_depth:
        for a in node.args:
            if isinstance(a, FactorNode) and _has_nonlinear_near_root(a, depth + 1, max_depth):
                return True
    return False


def _has_ts_decay_in_tree(node: FactorNode) -> bool:
    """检查 AST 中是否存在 ts_decay_linear 节点。"""
    if not isinstance(node, FactorNode):
        return False
    if node.op == "ts_decay_linear":
        return True
    for a in node.args:
        if isinstance(a, FactorNode) and _has_ts_decay_in_tree(a):
            return True
    return False


# ── 分类器 ───────────────────────────────────────────────


class GPClassifier:
    """GP因子 AST 表达式类型标签分类器。

    根据表达式 AST 结构识别因子类型标签，分类规则：

    | AST 模式                           | 标签                      |
    |------------------------------------|---------------------------|
    | ts_corr(close, volume, n)          | ["量价"]                  |
    | ts_corr(close, dxy, n) + 衰减      | ["宏观", "美元"]          |
    | delta(close, n)                    | ["动量"]                  |
    | zscore(close, n)                   | ["均值回归"]              |
    | ts_std(close, n)                   | ["波动率"]                |
    | power/log/sqrt 在 root 或 near-root | ["非线性"]               |
    | 4+ 叶子节点                        | ["复合"]                  |
    | 无匹配                             | ["GP发现"]                |

    支持输入 FactorNode AST 或表达式字符串。
    """

    def classify(self, expr: Union[str, FactorNode]) -> List[str]:
        """对因子表达式执行分类，返回标签列表。

        Args:
            expr: FactorNode AST 或表达式字符串。

        Returns:
            标签列表，如 ["量价"], ["宏观", "美元"], ["GP发现"]。
        """
        node = self._to_node(expr)
        if node is None:
            return ["GP发现"]
        return self._classify_node(node)

    # ── 内部实现 ──────────────────────────────────────

    def _to_node(self, expr: Union[str, FactorNode]) -> Optional[FactorNode]:
        """将输入统一转为 FactorNode；字符串解析失败返回 None。"""
        if isinstance(expr, FactorNode):
            return expr
        if isinstance(expr, str):
            expr = expr.strip()
            if not expr:
                return None
            try:
                return FactorParser(expr).parse()
            except (FactorParseError, ValueError, Exception):
                logger.debug("GPClassifier: failed to parse %r", expr, exc_info=True)
                return None
        return None

    def _classify_node(self, node: FactorNode) -> List[str]:
        """核心分类逻辑。"""
        tags: List[str] = []
        root_op = node.op

        # ── 1. 根节点模式匹配 ──
        if root_op == "ts_corr":
            tags.extend(self._classify_ts_corr(node))
        elif root_op == "delta":
            tags.append("动量")
        elif root_op == "zscore":
            tags.append("均值回归")
        elif root_op == "ts_std":
            tags.append("波动率")

        # ── 2. 非线性检测 (power/log/sqrt 在 root 或 near-root) ──
        if _has_nonlinear_near_root(node):
            if "非线性" not in tags:
                tags.append("非线性")

        # ── 3. 复合检测 (4+ 叶子节点) ──
        leaf_count = _count_leaves(node)
        if leaf_count >= 4:
            tags.append("复合")

        # ── 4. 无匹配 → GP发现 ──
        if not tags:
            tags.append("GP发现")

        return tags

    def _classify_ts_corr(self, node: FactorNode) -> List[str]:
        """ts_corr 子分类 — 检查 args 子树中是否包含 close + volume 或 close + dxy。"""
        tags: List[str] = []
        if len(node.args) >= 2:
            arg0 = node.args[0]
            arg1 = node.args[1]

            if isinstance(arg0, FactorNode) and isinstance(arg1, FactorNode):
                has_close = _has_leaf_in_subtree(arg0, "close")
                has_volume = _has_leaf_in_subtree(arg1, "volume")
                has_dxy = _has_leaf_in_subtree(arg1, "dxy")

                if has_close and has_volume:
                    tags.append("量价")
                elif has_close and has_dxy:
                    tags.extend(["宏观", "美元"])

        return tags


# ── 模块级快捷函数 ─────────────────────────────────


def classify_expr(expr: Union[str, FactorNode]) -> List[str]:
    """模块级快捷函数：对因子表达式执行分类。

    用法:
        from alpha.gp_classifier import classify_expr
        tags = classify_expr("ts_corr(close, volume, 20)")
    """
    return GPClassifier().classify(expr)
