from __future__ import annotations

import ast
from pathlib import Path


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def test_startup_has_no_legacy_governance_projection_writes():
    tree = ast.parse(Path("backend/app.py").read_text(encoding="utf-8"))
    guarded_calls = {"sync_runtime_config", "rc_patch", "restore_from_canonical"}
    seen: set[str] = set()
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        name = _call_name(call)
        if name not in guarded_calls:
            continue
        seen.add(name)
    assert seen == set()


def test_evolution_generated_factor_name_never_uses_python_hash():
    source = Path("backend/runtime/evolution_orchestrator.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    register = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_register_shadow_factors"
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "hash"
        for node in ast.walk(register)
    )
