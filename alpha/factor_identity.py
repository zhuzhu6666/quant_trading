"""Stable identities for generated factor definitions.

This module lives in the alpha domain because runtime factor selection imports
it while the ``alpha`` package is still initializing.  Backend services keep a
compatibility re-export, but the canonical implementation must not depend on
the backend package or import order.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .factor_dsl import FactorNode, parse_dsl


FACTOR_IDENTITY_VERSION = "factor_dsl_ast.v1"
_COMMUTATIVE_OPS = frozenset({"+", "*"})


def _number(value: Any) -> int | float | str:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("factor DSL constants must be finite")
        if value.is_integer():
            return int(value)
        return float(format(value, ".15g"))
    return str(value)


def _canonical_node(node: FactorNode) -> dict[str, Any]:
    op = str(node.op).strip().lower()
    args: list[Any] = [
        _canonical_node(arg) if isinstance(arg, FactorNode) else _number(arg)
        for arg in node.args
    ]

    if op in {"+", "-"} and len(args) == 1:
        child = args[0]
        if op == "+":
            return child if isinstance(child, dict) else {"op": "const", "args": [child]}
        if isinstance(child, dict) and child.get("op") == "const" and len(child.get("args") or []) == 1:
            value = child["args"][0]
            if isinstance(value, (int, float)):
                return {"op": "const", "args": [_number(-value)]}

    if op in _COMMUTATIVE_OPS:
        flattened: list[Any] = []
        for arg in args:
            if isinstance(arg, dict) and arg.get("op") == op:
                flattened.extend(list(arg.get("args") or []))
            else:
                flattened.append(arg)
        args = sorted(
            flattened,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    result: dict[str, Any] = {"op": op, "args": args}
    if node.params:
        result["params"] = {
            str(key): _number(value)
            for key, value in sorted(node.params.items(), key=lambda item: str(item[0]))
        }
    return result


def canonical_factor_ast(expression: str) -> dict[str, Any]:
    node = parse_dsl(str(expression or "").strip())
    return {
        "schema_version": FACTOR_IDENTITY_VERSION,
        "ast": _canonical_node(node),
    }


def canonical_factor_ast_json(expression: str) -> str:
    return json.dumps(
        canonical_factor_ast(expression),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def factor_definition_fingerprint(expression: str) -> str:
    return hashlib.sha256(canonical_factor_ast_json(expression).encode("utf-8")).hexdigest()


def canonical_factor_id(expression: str, *, namespace: str = "dsl") -> str:
    prefix = str(namespace or "dsl").strip().lower()
    return f"{prefix}:{factor_definition_fingerprint(expression)}"
