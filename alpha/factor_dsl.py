"""alpha/factor_dsl.py — 因子表达式 DSL (T15.1, 2026-06-02)

L2 因子自动化核心. 让用户用表达式描述因子, 系统自动求值.

DSL 语法 (类 WorldQuant BRAIN):
    ts_corr(ts_mean(close, 5), ts_std(volume, 10), 20) - rank(close)

设计:
- 递归 AST 节点 (op / args / params)
- LALR-style parser (手写递归下降, 避免引入 PLY/lark 依赖)
- 算子白名单 + 安全沙箱 (不 import / 不文件 IO / 不网络)
- vectorized evaluator (用 pandas rolling + numpy)

算子库 (20+):
- 时序: ts_mean / ts_std / ts_corr / ts_rank / ts_delta / ts_decay_linear / ts_min/max/sum
- 横截面: rank / normalize / quantile
- 数学: sign / abs / log / sqrt / power / delta / delay
- 算术: + - * /
- leaf: close / volume / open / high / low (列名)

v1 简化:
- 只支持叶子节点用列名 (close/volume/open/high/low)
- 不支持嵌套函数 (v2 加)
- 不支持时序对 (v2 加 ts_corr(x, y) 形式)
"""
from __future__ import annotations

import ast as _ast
import logging
import re
import time as _time
from dataclasses import dataclass, field
from typing import Optional, Union

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── 算子白名单 (安全沙箱) ─────────────────────────────────────────
ALLOWED_OPS = {
    # 时序算子 (参数: x, n 或 x, y, n)
    "ts_mean": ("ts", 2, 2),      # ts_mean(x, n)
    "ts_std": ("ts", 2, 2),       # ts_std(x, n)
    "ts_sum": ("ts", 2, 2),
    "ts_min": ("ts", 2, 2),
    "ts_max": ("ts", 2, 2),
    "ts_corr": ("ts", 3, 3),      # ts_corr(x, y, n)
    "ts_rank": ("ts", 2, 2),      # ts_rank(x, n)  -- rolling rank

    # 横截面算子 (单参数)
    "rank": ("cs", 1, 1),
    "normalize": ("cs", 1, 1),    # z-score
    "quantile": ("cs", 2, 2),     # quantile(x, q)

    # 一元数学
    "sign": ("math", 1, 1),
    "abs": ("math", 1, 1),
    "log": ("math", 1, 1),
    "sqrt": ("math", 1, 1),
    "power": ("math", 2, 2),      # power(x, exp)

    # 时序差分 / 延迟
    "delta": ("ts", 2, 2),        # delta(x, n)
    "delay": ("ts", 2, 2),        # delay(x, n)

    # 时序衰减
    "ts_decay_linear": ("ts", 2, 2),  # ts_decay_linear(x, n)
}

ALLOWED_LEAVES = {"close", "volume", "open", "high", "low", "dxy", "real_yield_10y", "gvz", "vix"}


# ── AST 节点 ────────────────────────────────────────────────────────
@dataclass
class FactorNode:
    """因子表达式 AST 节点 — 递归"""
    op: str                                # "ts_mean" | "+" | "rank" | "close"
    args: list = field(default_factory=list)  # [FactorNode, ...] 或 [int, ...]
    params: dict = field(default_factory=dict)  # 命名参数 (e.g. {"n": 20})

    def to_string(self, depth: int = 0) -> str:
        """转回表达式字符串 (用于落盘/调试)"""
        if not self.args:
            return self.op
        args_str = ", ".join(
            a.to_string(depth + 1) if isinstance(a, FactorNode) else str(a)
            for a in self.args
        )
        return f"{self.op}({args_str})"


# ── 表达式 parser (手写递归下降) ──────────────────────────────────
class FactorParseError(ValueError):
    pass


class FactorParser:
    """
    因子 DSL parser. 输入表达式字符串, 输出 FactorNode (AST).
    拒绝: import, 文件 IO, 网络, 任意函数调用.
    """

    def __init__(self, expression: str):
        self.expression = expression.strip()
        self.tokens = self._tokenize(self.expression)
        self.pos = 0

    def parse(self) -> FactorNode:
        """解析入口"""
        if not self.tokens:
            raise FactorParseError("Empty expression")
        node = self._parse_expr()
        if self.pos < len(self.tokens):
            raise FactorParseError(
                f"Unexpected token at pos {self.pos}: {self.tokens[self.pos]}"
            )
        return node

    def _tokenize(self, s: str) -> list:
        """简单 tokenizer: 数字 / 标识符 / 操作符 / 括号 / 逗号"""
        tokens = []
        i = 0
        while i < len(s):
            c = s[i]
            if c.isspace():
                i += 1
                continue
            if c.isalpha() or c == "_":
                # 标识符
                j = i
                while j < len(s) and (s[j].isalnum() or s[j] == "_"):
                    j += 1
                tokens.append(("ID", s[i:j]))
                i = j
            elif c.isdigit() or c == ".":
                # 数字 (int or float)
                j = i
                has_dot = False
                while j < len(s) and (s[j].isdigit() or (s[j] == "." and not has_dot)):
                    if s[j] == ".":
                        has_dot = True
                    j += 1
                num_str = s[i:j]
                tokens.append(("NUM", float(num_str) if has_dot else int(num_str)))
                i = j
            elif c in "+-*/":
                tokens.append(("OP", c))
                i += 1
            elif c == "(":
                tokens.append(("LPAREN", "("))
                i += 1
            elif c == ")":
                tokens.append(("RPAREN", ")"))
                i += 1
            elif c == ",":
                tokens.append(("COMMA", ","))
                i += 1
            else:
                raise FactorParseError(f"Unexpected char: {c!r} at pos {i}")
        return tokens

    def _peek(self) -> Optional[tuple]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _consume(self, expected_type: Optional[str] = None) -> tuple:
        tok = self.tokens[self.pos]
        if expected_type and tok[0] != expected_type:
            raise FactorParseError(
                f"Expected {expected_type}, got {tok[0]}: {tok[1]} at pos {self.pos}"
            )
        self.pos += 1
        return tok

    def _parse_expr(self) -> FactorNode:
        """表达式 = 乘除 | 加减"""
        return self._parse_additive()

    def _parse_additive(self) -> FactorNode:
        left = self._parse_multiplicative()
        while self._peek() and self._peek()[0] == "OP" and self._peek()[1] in ("+", "-"):
            op = self._consume()[1]
            right = self._parse_multiplicative()
            left = FactorNode(op=op, args=[left, right])
        return left

    def _parse_multiplicative(self) -> FactorNode:
        left = self._parse_unary()
        while self._peek() and self._peek()[0] == "OP" and self._peek()[1] in ("*", "/"):
            op = self._consume()[1]
            right = self._parse_unary()
            left = FactorNode(op=op, args=[left, right])
        return left

    def _parse_unary(self) -> FactorNode:
        if self._peek() and self._peek()[0] == "OP" and self._peek()[1] in ("+", "-"):
            op = self._consume()[1]
            operand = self._parse_unary()
            return FactorNode(op=op, args=[operand])
        return self._parse_primary()

    def _parse_primary(self) -> FactorNode:
        """primary = 数字 | leaf | func_call"""
        tok = self._peek()
        if tok is None:
            raise FactorParseError("Unexpected end of expression")
        if tok[0] == "NUM":
            # 数字作为常量, 包成 ast.Constant node 风格
            self._consume()
            return FactorNode(op="const", args=[tok[1]])
        if tok[0] == "ID":
            self._consume()
            # 后面是 ( 就是函数调用, 否则是 leaf
            if self._peek() and self._peek()[0] == "LPAREN":
                return self._parse_func_call(tok[1])
            # leaf
            if tok[1] not in ALLOWED_LEAVES:
                raise FactorParseError(
                    f"Unknown leaf: {tok[1]!r}. Allowed: {sorted(ALLOWED_LEAVES)}"
                )
            return FactorNode(op=tok[1])
        if tok[0] == "LPAREN":
            self._consume()
            node = self._parse_expr()
            if not self._peek() or self._peek()[0] != "RPAREN":
                raise FactorParseError("Expected ')'")
            self._consume()
            return node
        raise FactorParseError(f"Unexpected token: {tok}")

    def _parse_func_call(self, name: str) -> FactorNode:
        """函数调用: name(arg1, arg2, ...)"""
        if name not in ALLOWED_OPS:
            raise FactorParseError(
                f"Unknown op: {name!r}. Allowed: {sorted(ALLOWED_OPS)}"
            )
        cat, min_args, max_args = ALLOWED_OPS[name]
        self._consume("LPAREN")
        args = []
        if self._peek() and self._peek()[0] != "RPAREN":
            args.append(self._parse_expr())
            while self._peek() and self._peek()[0] == "COMMA":
                self._consume()
                args.append(self._parse_expr())
        if not (min_args <= len(args) <= max_args):
            raise FactorParseError(
                f"{name} expects {min_args}-{max_args} args, got {len(args)}"
            )
        if not self._peek() or self._peek()[0] != "RPAREN":
            raise FactorParseError("Expected ')'")
        self._consume("RPAREN")
        return FactorNode(op=name, args=args)


# ── AST evaluator (递归求值) ─────────────────────────────────────
class DSLEvaluator:
    """
    把 AST 节点转成 numpy/pandas 计算, 返回 np.ndarray.

    用法:
        ast = FactorParser("ts_corr(close, volume, 20)").parse()
        evaluator = DSLEvaluator(df)
        values = evaluator.evaluate(ast)
    """

    def __init__(self, df: pd.DataFrame, timeout_sec: float = 30.0):
        self.df = df
        self.timeout_sec = timeout_sec
        self._t0 = _time.time()

    def evaluate(self, node: FactorNode) -> np.ndarray:
        """递归求值"""
        if _time.time() - self._t0 > self.timeout_sec:
            raise TimeoutError(f"DSL evaluation exceeded {self.timeout_sec}s timeout")
        op = node.op
        args = node.args

        # 1. 常量
        if op == "const":
            return np.full(len(self.df), float(args[0]))

        # 2. 叶子节点 (列名)
        if op in ALLOWED_LEAVES:
            if op not in self.df.columns:
                return np.full(len(self.df), np.nan)
            return self.df[op].values.astype(np.float64)

        # 3. 二元算术
        if op in ("+", "-", "*", "/"):
            left = self.evaluate(args[0]) if isinstance(args[0], FactorNode) else np.full(len(self.df), float(args[0]))
            right = self.evaluate(args[1]) if isinstance(args[1], FactorNode) else np.full(len(self.df), float(args[1]))
            if op == "+":
                return left + right
            if op == "-":
                return left - right
            if op == "*":
                return left * right
            if op == "/":
                with np.errstate(divide="ignore", invalid="ignore"):
                    return np.where(right == 0, 0.0, left / right)

        # 4. 一元算子
        if op in ("sign", "abs", "log", "sqrt"):
            x = self.evaluate(args[0]) if isinstance(args[0], FactorNode) else np.full(len(self.df), float(args[0]))
            if op == "sign":
                return np.sign(x)
            if op == "abs":
                return np.abs(x)
            if op == "log":
                with np.errstate(divide="ignore", invalid="ignore"):
                    return np.where(x <= 0, 0.0, np.log(x))
            if op == "sqrt":
                with np.errstate(invalid="ignore"):
                    return np.sqrt(np.abs(x))

        # 5. power
        if op == "power":
            x = self.evaluate(args[0]) if isinstance(args[0], FactorNode) else np.full(len(self.df), float(args[0]))
            p = args[1] if not isinstance(args[1], FactorNode) else self.evaluate(args[1])
            with np.errstate(invalid="ignore"):
                return np.power(x, p)

        # 6. 横截面算子
        if op in ("rank", "normalize"):
            x = self.evaluate(args[0]) if isinstance(args[0], FactorNode) else np.full(len(self.df), float(args[0]))
            if op == "rank":
                return pd.Series(x).rank(pct=True).values
            if op == "normalize":
                mu = np.nanmean(x)
                std = np.nanstd(x)
                if std < 1e-12:
                    return np.zeros_like(x)
                return (x - mu) / std

        if op == "quantile":
            x = self.evaluate(args[0]) if isinstance(args[0], FactorNode) else np.full(len(self.df), float(args[0]))
            q = args[1] if not isinstance(args[1], FactorNode) else self.evaluate(args[1])
            q = float(q[0]) if hasattr(q, "__len__") else float(q)
            return pd.Series(x).quantile(q)

        # 7. 时序算子 (参数: x, n)
        if op in ("ts_mean", "ts_std", "ts_sum", "ts_min", "ts_max",
                   "ts_rank", "delta", "delay", "ts_decay_linear"):
            x = self.evaluate(args[0]) if isinstance(args[0], FactorNode) else np.full(len(self.df), float(args[0]))
            n = int(args[1]) if not isinstance(args[1], FactorNode) else int(self.evaluate(args[1])[0])
            n = max(1, n)
            s = pd.Series(x)
            if op == "ts_mean":
                return s.rolling(n, min_periods=n).mean().values
            if op == "ts_std":
                return s.rolling(n, min_periods=n).std().values
            if op == "ts_sum":
                return s.rolling(n, min_periods=n).sum().values
            if op == "ts_min":
                return s.rolling(n, min_periods=n).min().values
            if op == "ts_max":
                return s.rolling(n, min_periods=n).max().values
            if op == "ts_rank":
                return s.rolling(n, min_periods=n).apply(
                    lambda w: pd.Series(w).rank(pct=True).iloc[-1], raw=False
                ).values
            if op == "delta":
                return s.diff(n).values
            if op == "delay":
                return s.shift(n).values
            if op == "ts_decay_linear":
                # 线性衰减加权: weight[i] = (n-i)/n, sum=1
                weights = np.arange(1, n + 1, dtype=np.float64) / n
                return s.rolling(n, min_periods=n).apply(
                    lambda w: np.dot(w, weights[-len(w):]) if len(w) == n else np.nan,
                    raw=True
                ).values

        # 8. ts_corr (x, y, n)
        if op == "ts_corr":
            x = self.evaluate(args[0]) if isinstance(args[0], FactorNode) else np.full(len(self.df), float(args[0]))
            y = self.evaluate(args[1]) if isinstance(args[1], FactorNode) else np.full(len(self.df), float(args[1]))
            n = int(args[2]) if not isinstance(args[2], FactorNode) else int(self.evaluate(args[2])[0])
            n = max(2, n)
            sx = pd.Series(x)
            sy = pd.Series(y)
            return sx.rolling(n, min_periods=n).corr(sy).values

        raise ValueError(f"Unknown op: {op}")


# ── 一站式入口 ────────────────────────────────────────────────────
def parse_dsl(expression: str) -> FactorNode:
    """parse DSL string → AST"""
    return FactorParser(expression).parse()


def evaluate_dsl(expression: str, df: pd.DataFrame, timeout_sec: float = 30.0) -> np.ndarray:
    """parse + evaluate DSL string → numpy array"""
    ast = parse_dsl(expression)
    return DSLEvaluator(df, timeout_sec=timeout_sec).evaluate(ast)
