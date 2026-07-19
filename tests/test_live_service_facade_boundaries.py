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
        "_recover_emergency_execution_intents",
        "_recover_execution_outcomes_before_alpha",
        "_attempt_generation_startup_barrier",
        "_bootstrap_position_recovery",
        "_build_open_trade_risk_context",
        "_build_close_position_risk_context",
        "_evaluate_position_supervisor_for_position",
        "_evaluate_risk_reduction_policy",
        "_execute_live_safety_candidate",
        "_handle_closed_positions_after_tick",
        "_closed_position_processing_runtime",
        "_collect_closed_position_attribution",
        "_write_close_decision_log_after_tick",
        "_log_closed_position_ledger_after_tick",
        "_run_closed_position_learning_after_tick",
        "_cleanup_closed_position_after_tick",
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
        "_recovery_position_store",
        "_load_recovery_position_row",
        "_merge_recovery_position_meta",
        "_upsert_recovery_position_state",
        "_list_active_recovery_positions",
        "_recovery_last_seen_by_position",
        "_recovery_remaining_volume_by_position",
        "_mark_recovery_position_closed",
        "_lookup_recovery_context_integrity",
        "_replay_recovered_close",
        "_retire_broker_missing_position",
        "_restore_session_state_for_day",
        "_run_position_protection_cycle",
        "_release_entry_protection_pending_latch",
        "_remember_supervisor_reentry_block",
        "_sync_partial_close_session_fact",
        "_submit_open_trade_candidate",
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


def test_recovery_position_state_writes_live_outside_facade():
    source = LIVE_SERVICE.read_text(encoding="utf-8")
    store_source = Path(
        "backend/services/live_recovery_position_store.py"
    ).read_text(encoding="utf-8")

    assert "INSERT INTO recovery_position_state" not in source
    assert "UPDATE recovery_position_state" not in source
    assert "INSERT INTO recovery_position_state" in store_source
    assert "UPDATE recovery_position_state" in store_source
    assert "backend.services.live_service" not in store_source


def test_emergency_execution_recovery_decision_lives_outside_facade():
    source = LIVE_SERVICE.read_text(encoding="utf-8")
    recovery_source = Path(
        "backend/services/live_execution_recovery.py"
    ).read_text(encoding="utf-8")

    assert "bridge_execution_recovery_contract_missing" not in source
    assert "local_execution_recovery_unavailable" not in source
    assert "bridge_execution_recovery_contract_missing" in recovery_source
    assert "backend.services.live_service" not in recovery_source


def test_recovery_close_replay_and_retirement_live_outside_facade():
    source = LIVE_SERVICE.read_text(encoding="utf-8")
    recovery_source = Path(
        "backend/services/live_recovery_close.py"
    ).read_text(encoding="utf-8")

    assert "restart_replay_close_deal_unavailable" not in source
    assert "broker_position_missing_close_deal_unavailable" not in source
    assert "restart_replay_close_deal_unavailable" in recovery_source
    assert "broker_position_missing_close_deal_unavailable" in recovery_source
    assert "backend.services.live_service" not in recovery_source


def test_closed_position_cycle_lives_outside_facade_and_has_no_order_surface():
    source = LIVE_SERVICE.read_text(encoding="utf-8")
    cycle_source = Path(
        "backend/services/live_closed_position_cycle.py"
    ).read_text(encoding="utf-8")

    assert "post_close_session_projection_unavailable" not in source
    assert "post_close_session_projection_unavailable" in cycle_source
    assert "backend.services.live_service" not in cycle_source
    assert ".market_buy(" not in cycle_source
    assert ".market_sell(" not in cycle_source
    assert ".close_position(" not in cycle_source


def test_closed_position_processing_lives_outside_facade():
    source = LIVE_SERVICE.read_text(encoding="utf-8")
    processing_source = Path(
        "backend/services/live_closed_position_processing.py"
    ).read_text(encoding="utf-8")

    assert "skipped unverified trade review" not in source
    assert "recovery close persist failed" not in source
    assert "skipped unverified trade review" in processing_source
    assert "recovery close persist failed" in processing_source
    assert "backend.services.live_service" not in processing_source
    assert ".market_buy(" not in processing_source
    assert ".market_sell(" not in processing_source
    assert ".close_position(" not in processing_source


def test_open_submission_state_machine_lives_outside_facade():
    source = LIVE_SERVICE.read_text(encoding="utf-8")
    submission_source = Path(
        "backend/services/live_open_submission.py"
    ).read_text(encoding="utf-8")

    assert "confirmed_open_post_fill_processing_failed" not in source
    assert "confirmed_open_post_fill_processing_failed" in submission_source
    assert "backend.services.live_service" not in submission_source
    assert "bridge.market_buy(" not in submission_source
    assert "bridge.market_sell(" not in submission_source


def test_open_protection_state_machine_lives_outside_facade():
    source = LIVE_SERVICE.read_text(encoding="utf-8")
    node = _definitions(ast.parse(source))["_attach_open_trade_protection"]
    protection_source = Path(
        "backend/services/live_open_protection.py"
    ).read_text(encoding="utf-8")

    assert not any(
        isinstance(child, (ast.For, ast.While, ast.If, ast.Try, ast.With))
        for child in ast.walk(node)
    )
    assert "entry_protection_projection_unverified:" not in source
    assert "entry_protection_projection_unverified:" in protection_source
    assert "backend.services.live_service" not in protection_source
    assert ".market_buy(" not in protection_source
    assert ".market_sell(" not in protection_source
    assert ".close_position(" not in protection_source


def test_open_post_fill_processing_lives_outside_facade():
    source = LIVE_SERVICE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    processing_source = Path(
        "backend/services/live_open_processing.py"
    ).read_text(encoding="utf-8")

    for name in (
        "_record_filled_position_open_context",
        "_record_amended_open_success_context",
        "_record_amend_failure_after_fill",
    ):
        node = _definitions(tree)[name]
        assert not any(
            isinstance(child, (ast.For, ast.While, ast.If, ast.Try, ast.With))
            for child in ast.walk(node)
        )
    assert "entry_protection_fail_closed_unavailable" not in source
    assert "entry_protection_fail_closed_unavailable" in processing_source
    assert "def record_filled_position_open_context(" in processing_source
    assert "def record_amended_open_success_context(" in processing_source
    assert "backend.services.live_service" not in processing_source
    assert ".market_buy(" not in processing_source
    assert ".market_sell(" not in processing_source
    assert ".close_position(" not in processing_source


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


def test_position_protection_cycle_domain_does_not_import_live_facade():
    source = Path(
        "backend/services/live_position_protection_cycle.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        str(node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert "backend.services.live_service" not in imports
    assert "market_order" not in source
    assert "buy" not in source.lower()
    assert "sell" not in source.lower()


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
