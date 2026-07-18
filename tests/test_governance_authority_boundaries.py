from __future__ import annotations

import ast
from pathlib import Path


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _is_off_mode_guard(node: ast.If) -> bool:
    try:
        return ast.unparse(node.test) == "coordinator_mode == 'off'"
    except Exception:
        return False


def test_legacy_startup_governance_projections_are_off_mode_only():
    tree = ast.parse(Path("backend/app.py").read_text(encoding="utf-8"))
    parents: dict[ast.AST, ast.AST] = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    guarded_calls = {"sync_runtime_config", "rc_patch", "restore_from_log"}
    seen: set[str] = set()
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        name = _call_name(call)
        if name not in guarded_calls:
            continue
        seen.add(name)
        child: ast.AST = call
        parent = parents.get(child)
        guarded = False
        while parent is not None:
            if (
                isinstance(parent, ast.If)
                and _is_off_mode_guard(parent)
                and child in parent.body
            ):
                guarded = True
                break
            child = parent
            parent = parents.get(child)
        assert guarded, f"legacy startup call {name} escaped coordinator off guard"

    assert seen == guarded_calls


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
