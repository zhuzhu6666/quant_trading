import ast
from pathlib import Path


LIVE_SERVICE = Path("backend/services/live_service.py")


def _definitions(tree: ast.AST) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def test_reconcile_emergency_and_barrier_domains_live_outside_facade():
    tree = ast.parse(LIVE_SERVICE.read_text(encoding="utf-8"))
    definitions = _definitions(tree)

    assert {
        "_reconcile_value",
        "_fresh_observation_timestamp",
        "_explicit_position_reconcile",
        "_explicit_account_reconcile",
        "_fresh_emergency_position_reconcile",
        "_emergency_response",
        "_EmergencyRiskReducingVerdict",
        "_complete_generation_barrier_step",
    }.isdisjoint(definitions)


def test_live_service_domain_entrypoints_remain_thin_wiring():
    tree = ast.parse(LIVE_SERVICE.read_text(encoding="utf-8"))
    definitions = _definitions(tree)

    for name in (
        "_run_live_safety_cycle",
        "_recover_execution_outcomes_before_alpha",
        "_attempt_generation_startup_barrier",
        "_bootstrap_position_recovery",
        "_load_authoritative_session_deal_facts",
        "_sync_partial_close_session_fact",
        "emergency_close",
        "get_live_readiness",
    ):
        node = definitions[name]
        assert int(node.end_lineno or 0) - int(node.lineno) < 55
        assert not any(
            isinstance(child, (ast.For, ast.While, ast.Try, ast.With))
            for child in ast.walk(node)
        )


def test_session_deal_sql_lives_outside_live_facade():
    source = LIVE_SERVICE.read_text(encoding="utf-8")

    assert "WITH final_close AS" not in source
    assert "FROM ctrader_deals d" not in source


def test_position_supervision_entrypoint_is_dependency_wiring_only():
    tree = ast.parse(LIVE_SERVICE.read_text(encoding="utf-8"))
    node = _definitions(tree)["_run_position_supervision"]

    assert int(node.end_lineno or 0) - int(node.lineno) < 100
    assert not any(
        isinstance(child, (ast.For, ast.While, ast.Try, ast.With))
        for child in ast.walk(node)
    )


def test_safety_reconciliation_modules_do_not_depend_on_postgres_or_legacy_refresh():
    for relative_path in (
        "backend/services/live_reconciliation.py",
        "backend/services/live_emergency.py",
    ):
        source = Path(relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            str(node.module or "")
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        called_attributes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

        assert not any(name.startswith("backend.core.db") for name in imports)
        assert "refresh_positions" not in called_attributes
