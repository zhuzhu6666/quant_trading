from backend.services.api_fact_views import (
    account_fact_payload,
    alerts_fact_payload,
    db_health_fact_payload,
    ctrader_token_status_fact_payload,
    external_data_status_fact_payload,
    health_fact_payload,
    live_autonomy_evaluation_fact_payload,
    live_autonomy_status_fact_payload,
    live_status_fact_payload,
    loop_fact_payload,
    positions_fact_payload,
    policy_verdicts_fact_payload,
    readiness_fact_payload,
    recovery_fact_payload,
    realized_fact_payload,
    risk_summary_fact_payload,
    session_fact_payload,
    state_snapshot_fact_payload,
    strategy_fact_payload,
    sync_status_fact_payload,
    trade_traces_fact_payload,
)
from backend.services.live_runtime_state import safe_container_snapshot


def _known_position_components(observed_at: float = 99.0) -> dict:
    return {
        name: {
            "state": "known",
            "source": "ctrader_reconcile",
            "observed_at": observed_at,
            "reason_code": None,
            "known_position_ids": [42],
            "unknown_position_ids": [],
        }
        for name in ("identity", "protection", "price", "pnl")
    }


def test_account_fact_preserves_legacy_fields_and_uses_reconcile_timestamp():
    payload = account_fact_payload(
        {
            "ok": True,
            "broker": "ctrader",
            "balance": 1234.5,
            "readiness": {"account_updated_at": 95.0},
        },
        now=100.0,
    )

    assert payload["balance"] == 1234.5
    assert payload["_fact"]["state"] == "known"
    assert payload["_fact"]["contract"] == "live.account.v2"


def test_successful_empty_positions_is_known_but_missing_reconcile_is_unknown():
    known = positions_fact_payload(
        {
            "ok": True,
            "broker": "ctrader",
            "positions": [],
            "readiness": {"positions_updated_at": 99.0},
        },
        now=100.0,
    )
    unknown = positions_fact_payload(
        {
            "ok": True,
            "broker": "ctrader",
            "positions": [],
            "readiness": {"positions_updated_at": None},
        },
        now=100.0,
    )

    assert known["_fact"]["state"] == "known"
    assert unknown["_fact"]["state"] == "unknown"


def test_fresh_nonempty_positions_require_complete_component_facts():
    complete = positions_fact_payload(
        {
            "ok": True,
            "positions": [{"position_id": 42}],
            "readiness": {
                "positions_updated_at": 99.0,
                "positions_component_facts": _known_position_components(),
            },
        },
        now=100.0,
    )
    missing = positions_fact_payload(
        {
            "ok": True,
            "positions": [{"position_id": 42}],
            "readiness": {"positions_updated_at": 99.0},
        },
        now=100.0,
    )

    assert complete["_fact"]["state"] == "known"
    assert missing["_fact"]["state"] == "unknown"
    assert missing["_fact"]["reason_code"] == "position_components_not_reported"
    for component in complete["_fact"]["components"]["broker_reconcile"].values():
        assert component["envelope"] == "fact.v1"
        assert component["contract"].startswith("live.positions.")
        assert component["state"] == "known"
        assert component["source"] == "ctrader_reconcile"
        assert component["observed_at"] == 99.0
        assert component["generated_at"] == 100.0
        assert component["stale_after_sec"] == 15.0
        assert "reason_code" in component
        assert "components" in component


def test_fresh_position_component_error_is_top_level_error():
    components = _known_position_components()
    components["pnl"] = {
        "state": "error",
        "source": "ctrader_pnl_rpc",
        "observed_at": 99.0,
        "reason_code": "pnl_rpc_failed",
        "known_position_ids": [],
        "unknown_position_ids": [42],
    }

    payload = positions_fact_payload(
        {
            "ok": True,
            "positions": [{"position_id": 42}],
            "readiness": {
                "positions_updated_at": 99.0,
                "positions_component_facts": components,
            },
        },
        now=100.0,
    )

    assert payload["_fact"]["state"] == "error"
    assert payload["_fact"]["reason_code"] == "source_error"
    pnl_fact = payload["_fact"]["components"]["broker_reconcile"]["pnl"]
    assert pnl_fact["envelope"] == "fact.v1"
    assert pnl_fact["state"] == "error"
    assert pnl_fact["reason_code"] == "pnl_rpc_failed"


def test_stale_position_snapshot_retains_timestamp_when_components_are_missing():
    payload = positions_fact_payload(
        {
            "ok": True,
            "positions": [{"position_id": 42}],
            "readiness": {
                "positions_updated_at": 80.0,
                "positions_reconcile_failed_at": 99.0,
                "positions_reconcile_error": "timeout",
            },
        },
        now=100.0,
    )

    assert payload["_fact"]["state"] == "stale"
    assert payload["_fact"]["observed_at"] == 80.0
    assert payload["_fact"]["reason_code"] == "freshness_expired"


def test_running_loop_requires_a_heartbeat_but_stopped_is_directly_observed():
    missing = loop_fact_payload({"running": True}, now=100.0)
    stale = loop_fact_payload({"running": True}, diagnostic_ts=80.0, now=100.0)
    stopped = loop_fact_payload({"running": False}, now=100.0)

    assert missing["_fact"]["state"] == "unknown"
    assert missing["_fact"]["reason_code"] == "loop_heartbeat_missing"
    assert stale["_fact"]["state"] == "stale"
    assert stopped["_fact"]["state"] == "known"


def test_composite_live_status_cannot_be_known_with_missing_loop_heartbeat():
    payload = live_status_fact_payload(
        {
            "ctrader": {"status": "connected", "error": None},
            "loop": {"running": True},
            "readiness": {
                "account_updated_at": 99.0,
                "positions_updated_at": 99.0,
            },
        },
        now=100.0,
    )

    assert payload["_fact"]["state"] == "unknown"
    assert payload["_fact"]["components"]["loop"]["state"] == "unknown"


def test_strategy_session_and_realized_contracts_use_domain_observations():
    strategy = strategy_fact_payload(
        {"v4_status": {"pipeline_active": False}, "running": False},
        diagnostic_ts=99.0,
        now=100.0,
    )
    session = session_fact_payload(
        {"pnl_today": 1.0, "trades": 1},
        source="ctrader_deals.final_close_rebuild.v1",
        observed_at=99.0,
        now=100.0,
    )
    realized = realized_fact_payload(
        {
            "ok": True,
            "source": "ctrader_deals",
            "fallback_source": "recovery_position_state",
            "to_ts": 99.0,
            "points": [],
        },
        now=100.0,
    )

    assert strategy["_fact"]["state"] == "unknown"
    assert session["_fact"]["state"] == "known"
    assert realized["_fact"]["state"] == "known"


def test_fact_projection_cuts_recursive_edges_without_changing_fact_contract():
    position = {"position_id": 42, "direction": 1}
    position["diagnostic"] = position
    payload = positions_fact_payload(
        {
            "ok": True,
            "broker": "ctrader",
            "positions": [position],
            "readiness": {
                "positions_updated_at": 99.0,
                "positions_component_facts": _known_position_components(),
            },
        },
        now=100.0,
    )

    assert payload["positions"][0]["position_id"] == 42
    assert payload["positions"][0]["diagnostic"] is None
    assert payload["_fact"]["contract"] == "live.positions.v2"


def test_safe_container_snapshot_copies_shared_values_but_cuts_only_cycles():
    shared = {"value": 1}
    source = {"left": shared, "right": shared}
    source["self"] = source

    copied = safe_container_snapshot(source)

    assert copied["left"] == {"value": 1}
    assert copied["right"] == {"value": 1}
    assert copied["left"] is not copied["right"]
    assert copied["self"] is None


def test_safe_container_snapshot_cuts_cycles_in_projection_objects():
    class Projection:
        pass

    root = Projection()
    root.position_id = 42
    root.self = root

    copied = safe_container_snapshot({"position": root})

    assert copied["position"]["position_id"] == 42
    assert copied["position"]["self"] is None


def test_ops_status_facts_preserve_payload_and_require_real_observations():
    sync = sync_status_fact_payload(
        {
            "daemon_running": False,
            "last_run_at": 99.0,
            "per_tf": {"M5": {"updated_at": 99.0}},
        },
        now=100.0,
    )
    missing_sync = sync_status_fact_payload(
        {"per_tf": {}, "daemon_running": False, "error": "no_status_file"},
        now=100.0,
    )
    token = ctrader_token_status_fact_payload(
        {"has_token": True, "expires_at": 200.0, "expired": False},
        now=100.0,
    )
    token_without_expiry = ctrader_token_status_fact_payload(
        {"has_token": True, "expires_at": None},
        now=100.0,
    )

    assert sync["daemon_running"] is False
    assert sync["_fact"]["contract"] == "ops.sync-status.v2"
    assert sync["_fact"]["state"] == "known"
    assert missing_sync["_fact"]["state"] == "error"
    assert token["_fact"]["contract"] == "ops.ctrader-token-status.v2"
    assert token["_fact"]["state"] == "known"
    assert token_without_expiry["has_token"] is True
    assert token_without_expiry["_fact"]["state"] == "unknown"


def test_external_and_trade_trace_facts_distinguish_empty_and_failed_sources():
    external = external_data_status_fact_payload(
        {"sources": [{"source": "cot", "stale": False, "last_refresh": 99.0}]},
        now=100.0,
    )
    empty_external = external_data_status_fact_payload({"sources": []}, now=100.0)
    failed_external = external_data_status_fact_payload(
        {"sources": [{"source": "fred", "stale": True, "error": "probe failed"}]},
        now=100.0,
    )
    traces = trade_traces_fact_payload(
        {"items": [{"review_id": "r1", "created_at": 99.0}], "count": 1, "limit": 20},
        now=100.0,
    )
    empty_traces = trade_traces_fact_payload(
        {"items": [], "count": 0, "limit": 20},
        now=100.0,
    )

    assert external["sources"][0]["source"] == "cot"
    assert external["_fact"]["contract"] == "ops.external-data-status.v2"
    assert external["_fact"]["state"] == "known"
    assert empty_external["_fact"]["state"] == "unknown"
    assert failed_external["_fact"]["state"] == "error"
    assert traces["_fact"]["contract"] == "risk.trade-trace-recent.v2"
    assert traces["_fact"]["state"] == "known"
    assert empty_traces["items"] == []
    assert empty_traces["_fact"]["state"] == "known"


def test_session_runtime_cache_and_risk_input_failure_cannot_look_known():
    cached_session = session_fact_payload(
        {"pnl_today": 0.0, "trades": 0},
        source="runtime_incremental",
        observed_at=99.0,
        now=100.0,
    )
    risk = risk_summary_fact_payload(
        {"system_health": {"ts": 99.0, "errors": []}},
        risk_observed_at=None,
        risk_error="postgres unavailable",
        now=100.0,
    )

    assert cached_session["_fact"]["state"] == "unknown"
    assert cached_session["_fact"]["source"] == "degraded_cache"
    assert risk["_fact"]["state"] == "error"
    assert risk["_fact"]["components"]["risk_inputs"]["state"] == "error"


def test_state_source_none_never_turns_zero_placeholders_into_known_facts():
    payload = state_snapshot_fact_payload(
        {"source": "none", "balance": 0.0, "equity": 0.0},
        account={},
        account_updated_at=99.0,
        positions_updated_at=99.0,
        diagnostic_ts=99.0,
        spot_quote=None,
        now=100.0,
    )

    assert payload["balance"] == 0.0
    assert payload["_fact"]["state"] == "unknown"
    assert payload["_fact"]["source"] == "none"


def test_state_reports_stale_when_underlying_broker_snapshots_are_old():
    payload = state_snapshot_fact_payload(
        {"source": "live", "balance": 1000.0, "equity": 1001.0},
        account={"ok": True},
        account_updated_at=90.0,
        positions_updated_at=90.0,
        diagnostic_ts=99.0,
        spot_quote={"ts": 99.0, "source": "ctrader_spot"},
        now=100.0,
    )

    assert payload["_fact"]["state"] == "stale"
    assert payload["_fact"]["components"]["spot"]["state"] == "known"


def test_readiness_warming_and_unregistered_recovery_are_explicitly_unknown():
    readiness = readiness_fact_payload(
        {
            "ok": True,
            "status": "warming_snapshot",
            "generated_at": 99.0,
            "cache": {"source": "warming"},
        },
        now=100.0,
    )
    recovery = recovery_fact_payload(
        {"running": False, "last_check": 0.0},
        registered=False,
        now=100.0,
    )

    assert readiness["_fact"]["state"] == "unknown"
    assert recovery["status"] == "not_registered"
    assert recovery["registered"] is False
    assert recovery["_fact"]["state"] == "unknown"


def test_ops_config_and_runtime_delivery_are_separate_facts():
    alerts = alerts_fact_payload(
        {"status": "Healthy", "rules_active": 6},
        now=100.0,
    )
    db_health = db_health_fact_payload(
        {"ok": True, "overall": "fresh", "checked_at": 99.0},
        now=100.0,
    )

    assert alerts["status"] == "Healthy"
    assert alerts["_fact"]["state"] == "known"
    assert alerts["_fact"]["components"]["runtime_delivery"]["state"] == "unknown"
    assert alerts["delivery"]["status"] == "not_registered"
    assert db_health["_fact"]["contract"] == "system.db-health.v2"
    assert db_health["_fact"]["state"] == "known"


def test_live_autonomy_endpoint_facts_use_readiness_observation():
    status = live_autonomy_status_fact_payload(
        {
            "ok": True,
            "live_autonomy": {"live_autonomy_unlocked": False},
            "readiness_generated_at": 99.0,
        },
        now=100.0,
    )
    blocked = live_autonomy_evaluation_fact_payload(
        {
            "ok": False,
            "evaluation": {"ok": False, "status": "blocked"},
            "readiness_generated_at": 99.0,
        },
        now=100.0,
    )
    stale = live_autonomy_status_fact_payload(
        {"ok": True, "readiness_generated_at": 1.0},
        now=200.0,
    )
    unknown = live_autonomy_evaluation_fact_payload(
        {"ok": False, "evaluation": {"status": "blocked"}},
        now=100.0,
    )

    assert status["_fact"]["contract"] == "ops.live-autonomy-status.v2"
    assert status["_fact"]["state"] == "known"
    assert blocked["_fact"]["contract"] == "ops.live-autonomy-unlock-evaluation.v2"
    assert blocked["_fact"]["state"] == "known"
    assert stale["_fact"]["state"] == "stale"
    assert unknown["_fact"]["state"] == "unknown"


def test_policy_verdicts_empty_query_is_a_known_empty_fact():
    payload = policy_verdicts_fact_payload(
        {"limit": 50, "total": 0, "items": []},
        now=100.0,
    )

    assert payload["items"] == []
    assert payload["_fact"]["contract"] == "risk.policy-verdicts.v2"
    assert payload["_fact"]["state"] == "known"


def test_policy_verdicts_old_events_do_not_make_current_query_stale():
    payload = policy_verdicts_fact_payload(
        {
            "limit": 50,
            "total": 1,
            "items": [{"decision_id": "dec_old", "decision_ts": 1.0}],
        },
        now=100.0,
    )

    assert payload["items"][0]["decision_ts"] == 1.0
    assert payload["_fact"]["observed_at"] == 100.0
    assert payload["_fact"]["generated_at"] == 100.0
    assert payload["_fact"]["state"] == "known"


def test_policy_verdicts_failed_query_remains_error():
    payload = policy_verdicts_fact_payload(
        {"ok": False, "error": "state_pg_unavailable", "items": []},
        now=100.0,
    )

    assert payload["_fact"]["state"] == "error"
    assert payload["_fact"]["reason_code"] == "source_error"


def test_risk_summary_without_health_observation_is_unknown_not_green():
    payload = risk_summary_fact_payload(
        {"var": {"status": "ok"}, "system_health": {"overall": "unknown"}},
        now=100.0,
    )

    assert payload["var"]["status"] == "ok"
    assert payload["_fact"]["state"] == "unknown"


def test_risk_summary_uses_component_specific_freshness_windows():
    healthy_between_minute_checks = risk_summary_fact_payload(
        {"system_health": {"ts": 50.0, "errors": []}},
        risk_observed_at=99.0,
        now=100.0,
    )
    stale_risk_inputs = risk_summary_fact_payload(
        {"system_health": {"ts": 99.0, "errors": []}},
        risk_observed_at=68.0,
        now=100.0,
    )
    stale_system_health = risk_summary_fact_payload(
        {"system_health": {"ts": 24.0, "errors": []}},
        risk_observed_at=99.0,
        now=100.0,
    )

    assert healthy_between_minute_checks["_fact"]["state"] == "known"
    assert healthy_between_minute_checks["_fact"]["stale_after_sec"] == 75.0
    assert healthy_between_minute_checks["_fact"]["components"]["system_health"]["state"] == "known"
    assert healthy_between_minute_checks["_fact"]["components"]["risk_inputs"]["state"] == "known"

    assert stale_risk_inputs["_fact"]["state"] == "stale"
    assert stale_risk_inputs["_fact"]["reason_code"] == "component_stale"
    assert stale_risk_inputs["_fact"]["components"]["risk_inputs"]["stale_after_sec"] == 30.0

    assert stale_system_health["_fact"]["state"] == "stale"
    assert stale_system_health["_fact"]["reason_code"] == "freshness_expired"
    assert stale_system_health["_fact"]["components"]["system_health"]["stale_after_sec"] == 75.0


def test_health_probe_adds_fact_without_removing_legacy_shape():
    payload = health_fact_payload(
        {"status": "ok", "db": "connected", "ctrader": "connected"},
        now=100.0,
    )

    assert payload["status"] == "ok"
    assert payload["_fact"]["state"] == "known"

    unknown = health_fact_payload(
        {"status": "ok", "db": "connected", "ctrader": "unknown"},
        now=100.0,
    )
    assert unknown["_fact"]["state"] == "unknown"


def test_live_api_boundaries_attach_fact_without_changing_legacy_payload(monkeypatch):
    from backend.api import live

    monkeypatch.setattr(
        live,
        "get_account",
        lambda broker: {
            "ok": True,
            "broker": broker,
            "balance": 10.0,
            "readiness": {"account_updated_at": 99.0},
        },
    )
    monkeypatch.setattr(
        live,
        "get_positions",
        lambda broker, symbol: {
            "ok": True,
            "broker": broker,
            "positions": [],
            "readiness": {"positions_updated_at": 99.0},
        },
    )
    monkeypatch.setattr("backend.services.api_fact_views.time.time", lambda: 100.0)

    account = live.get_account_endpoint(None, broker="ctrader")
    positions = live.get_positions_endpoint(None, broker="ctrader", symbol=None)

    assert account["balance"] == 10.0
    assert account["_fact"]["state"] == "known"
    assert positions["positions"] == []
    assert positions["_fact"]["state"] == "known"


def test_ops_recovery_route_marks_unregistered_monitor_unknown(monkeypatch):
    from backend.api import ops

    class _Recovery:
        _loop_check_fn = None
        _scheduler_check_fn = None

        @staticmethod
        def health_status():
            return {"running": False, "loop_healthy": True, "last_check": 0.0}

    monkeypatch.setattr(ops, "_auto_recovery", _Recovery())
    result = ops.get_recovery_status(None)

    assert result["status"] == "not_registered"
    assert result["_fact"]["state"] == "unknown"

    alerts = ops.get_alert_rules(None)
    assert alerts["status"] == "Healthy"
    assert alerts["delivery"]["registered"] is False
    assert alerts["_fact"]["contract"] == "ops.alerts.v2"


def test_ops_recovery_routes_do_not_construct_unregistered_monitor(monkeypatch):
    from backend.api import ops

    monkeypatch.setattr(ops, "_auto_recovery", None)
    monkeypatch.setattr(
        ops,
        "_get_auto_recovery",
        lambda: (_ for _ in ()).throw(AssertionError("must not construct monitor")),
    )

    status = ops.get_recovery_status(None)
    history = ops.get_recovery_history(None)

    assert status["status"] == "not_registered"
    assert status["loop_healthy"] is None
    assert status["_fact"]["state"] == "unknown"
    assert status["_fact"]["source"] == "not_registered"
    assert history["history"] == []
    assert history["_fact"]["contract"] == "ops.auto-recovery-history.v2"
    assert history["_fact"]["state"] == "unknown"


def test_new_fact_routes_keep_legacy_top_level_fields(monkeypatch):
    from backend.api import ctrader_auth, external_data, risk, sync

    monkeypatch.setattr(
        sync,
        "get_status",
        lambda: {
            "daemon_running": False,
            "last_run_at": 99.0,
            "per_tf": {"M5": {"updated_at": 99.0}},
        },
    )
    monkeypatch.setattr(
        ctrader_auth,
        "_read_env",
        lambda: {"CTRADER_ACCESS_TOKEN": "secret", "CTRADER_TOKEN_EXPIRES_AT": "200"},
    )
    monkeypatch.setattr(
        external_data,
        "_run_script",
        lambda *args: '{"sources":[{"source":"cot","stale":false}]}',
    )
    monkeypatch.setattr(
        risk,
        "_recent_trade_trace_index",
        lambda limit: {
            "items": [{"review_id": "r1", "created_at": 99.0}],
            "count": 1,
            "limit": limit,
        },
    )
    monkeypatch.setattr("backend.services.api_fact_views.time.time", lambda: 100.0)
    monkeypatch.setattr(ctrader_auth.time, "time", lambda: 100.0)

    sync_payload = sync.status(None)
    token_payload = ctrader_auth.token_status(None)
    external_payload = external_data.get_external_status(None)
    trace_payload = risk.get_recent_trade_traces(None, limit=7)

    assert sync_payload["daemon_running"] is False
    assert sync_payload["_fact"]["contract"] == "ops.sync-status.v2"
    assert token_payload["has_token"] is True
    assert token_payload["_fact"]["contract"] == "ops.ctrader-token-status.v2"
    assert external_payload["sources"][0]["source"] == "cot"
    assert external_payload["_fact"]["contract"] == "ops.external-data-status.v2"
    assert trace_payload["count"] == 1
    assert trace_payload["limit"] == 7
    assert trace_payload["_fact"]["contract"] == "risk.trade-trace-recent.v2"
