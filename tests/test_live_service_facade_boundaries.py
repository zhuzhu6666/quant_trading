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
        "_active_entry_cluster_learning_policy",
        "_active_entry_quality_learning_policy",
        "_active_event_window_learning_policy",
        "_activate_entry_protection_pending_latch",
        "_run_live_safety_cycle",
        "_run_live_loop_tick_body",
        "_run_live_loop_tick_body_legacy",
        "_recover_execution_outcomes_before_alpha",
        "_attempt_generation_startup_barrier",
        "_bootstrap_position_recovery",
        "_build_open_trade_risk_context",
        "_build_close_position_risk_context",
        "_evaluate_position_supervisor_for_position",
        "_evaluate_risk_reduction_policy",
        "_execute_live_safety_candidate",
        "_load_recovery_row_for_risk_reduction",
        "_load_authoritative_session_deal_facts",
        "_lookup_entry_context_for_risk_reduction",
        "_lookup_entry_decision_for_risk_reduction",
        "_open_learning_context_payload",
        "_active_supervisor_reentry_block",
        "_pending_supervisor_reentry_block_from_positions",
        "_position_path_metrics_for_position",
        "_recent_review_reentry_block",
        "_record_risk_reduction_aux_failure",
        "_restore_session_state_for_day",
        "_release_entry_protection_pending_latch",
        "_remember_supervisor_reentry_block",
        "_sync_partial_close_session_fact",
        "emergency_close",
        "get_live_readiness",
        "stop_loop",
        "start_loop",
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


def test_session_restore_decision_has_no_runtime_or_facade_dependency():
    source = Path("backend/services/session_restore.py").read_text(encoding="utf-8")

    assert "runtime_kv" not in source.replace("``runtime_kv``", "")
    assert "backend.services.live_service" not in source
    assert "def resolve_session_restore(" in source


def test_position_supervision_entrypoint_is_dependency_wiring_only():
    tree = ast.parse(LIVE_SERVICE.read_text(encoding="utf-8"))
    node = _definitions(tree)["_run_position_supervision"]

    assert int(node.end_lineno or 0) - int(node.lineno) < 100
    assert not any(
        isinstance(child, (ast.For, ast.While, ast.Try, ast.With))
        for child in ast.walk(node)
    )


def test_live_loop_entrypoint_owns_and_closes_generation_log_resource():
    tree = ast.parse(LIVE_SERVICE.read_text(encoding="utf-8"))
    node = _definitions(tree)["_run_loop_body"]

    assert int(node.end_lineno or 0) - int(node.lineno) < 55
    try_nodes = [
        child for child in ast.walk(node) if isinstance(child, ast.Try)
    ]
    assert len(try_nodes) == 1
    assert try_nodes[0].finalbody
    assert any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "close"
        for child in ast.walk(try_nodes[0].finalbody[0])
    )


def test_live_loop_generation_body_has_no_embedded_tick_loop():
    tree = ast.parse(LIVE_SERVICE.read_text(encoding="utf-8"))
    node = _definitions(tree)["_run_loop_body_active"]

    assert int(node.end_lineno or 0) - int(node.lineno) < 100
    assert not any(
        isinstance(child, (ast.For, ast.While))
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
