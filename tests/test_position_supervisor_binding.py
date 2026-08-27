from __future__ import annotations

import copy
import sqlite3
import time
from types import SimpleNamespace

import pytest

from backend.core.db import STATE_DB_DDL
from backend.services import live_service
from backend.services.learning_application_store import LearningApplicationStore
from backend.services.live_position_lifecycle import (
    build_supervisor_trace_ledger_payload,
)
from backend.services.position_supervisor_governance import (
    build_position_supervisor_selection_projection,
    latest_position_supervisor_selection_projection,
    publish_position_supervisor_selection_projection,
    select_position_supervisor_binding,
)
from backend.services.position_supervisor_templates import (
    CONSERVATIVE_TEMPLATE_ID,
    DEFAULT_TEMPLATE_ID,
    build_legacy_position_supervisor_binding,
    build_position_supervisor_binding,
    get_position_supervisor_template,
    position_supervisor_template_hash,
    resolve_position_supervisor_binding_lineage,
    verify_position_supervisor_binding,
)
from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService
from backend.services.v16_command_gate import V16CommandGate
from config.runtime_config import RuntimeConfig
from risk.policy_service import RiskPolicyService


def _init_state(path):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()


def _candidate(template, *, score, selection_key, event_id):
    return {
        "template_id": template["template_id"],
        "template_version": template["template_version"],
        "template_hash": position_supervisor_template_hash(template),
        "template_snapshot": template,
        "selection_key": dict(selection_key),
        "effect_score": score,
        "mature_trade_count": 50,
        "observed_trade_count": 50,
        "posterior_fingerprint": f"posterior-{event_id}",
        "application_id": f"application-{event_id}",
        "effect_id": f"effect-{event_id}",
        "selection_event_id": event_id,
        "evidence_refs": {
            "counterfactual_ids": [f"cf-{event_id}"],
            "trace_ids": [f"trace-{event_id}"],
        },
    }


def test_binding_hash_is_stable_and_tampering_fails_closed():
    template = get_position_supervisor_template(DEFAULT_TEMPLATE_ID)
    reordered = {key: template[key] for key in reversed(list(template))}
    reordered["thresholds"] = {
        key: template["thresholds"][key]
        for key in reversed(list(template["thresholds"]))
    }

    assert position_supervisor_template_hash(template) == position_supervisor_template_hash(
        reordered
    )

    binding = build_position_supervisor_binding(
        template,
        binding_source="static_baseline",
        selection_key={"symbol": "XAUUSD+", "current_regime": "trend"},
        bound_at=100.0,
    )
    assert verify_position_supervisor_binding(binding)["valid"] is True

    tampered = copy.deepcopy(binding)
    tampered["template_snapshot"]["thresholds"]["min_thesis_break_seconds"] = 999.0
    checked = verify_position_supervisor_binding(tampered)
    assert checked["valid"] is False
    assert checked["reason"] == "binding_template_hash_mismatch"

    unknown_source = copy.deepcopy(binding)
    unknown_source["binding_source"] = "brain_memory"
    assert verify_position_supervisor_binding(unknown_source)["reason"] == (
        "binding_source_unknown"
    )


def test_legacy_binding_is_explicit_and_lineage_does_not_invent_history():
    legacy = build_legacy_position_supervisor_binding(
        DEFAULT_TEMPLATE_ID,
        selection_key={"symbol": "XAUUSD+"},
    )
    checked = verify_position_supervisor_binding(legacy)
    assert checked["valid"] is False
    assert checked["state"] == "legacy"
    assert checked["reason"] == "legacy_global_fallback"

    assert resolve_position_supervisor_binding_lineage(
        {"template_id": DEFAULT_TEMPLATE_ID, "template_hash": "compact-only"}
    )["state"] == "unknown"

    bound = build_position_supervisor_binding(
        DEFAULT_TEMPLATE_ID,
        selection_key={"symbol": "XAUUSD+"},
        bound_at=100.0,
    )
    assert resolve_position_supervisor_binding_lineage(
        {"entry_protection_plan": {"supervisor_binding": bound}},
        {"template_id": DEFAULT_TEMPLATE_ID, "template_hash": bound["template_hash"]},
    )["valid"] is True

    other = build_position_supervisor_binding(
        CONSERVATIVE_TEMPLATE_ID,
        selection_key={"symbol": "XAUUSD+"},
        bound_at=100.0,
    )
    conflict = resolve_position_supervisor_binding_lineage(bound, other)
    assert conflict["valid"] is False
    assert conflict["state"] == "conflict"


def test_selection_projection_has_safe_baseline_and_publishes_in_runtime_kv(tmp_path):
    db_path = tmp_path / "state.db"
    _init_state(db_path)
    cfg = RuntimeConfig()

    projection = build_position_supervisor_selection_projection(
        db_path=db_path,
        cfg=cfg,
        now_ts=1000.0,
    )
    assert projection["status"] == "insufficient_evidence"
    assert projection["candidate_count"] == 0
    assert verify_position_supervisor_binding(projection["default_binding"])["valid"] is True

    published_at = time.time()
    published = publish_position_supervisor_selection_projection(
        db_path=db_path,
        cfg=cfg,
        now_ts=published_at,
    )
    assert published["ok"] is True
    latest = latest_position_supervisor_selection_projection(
        db_path=db_path,
        max_age_seconds=900.0,
    )
    assert latest["ok"] is True
    assert latest["status"] == "fresh"
    assert latest["candidate_count"] == 0


def test_uncommitted_application_and_positive_effect_cannot_enter_selection(tmp_path):
    db_path = tmp_path / "state.db"
    _init_state(db_path)
    store = LearningApplicationStore(db_path)
    application_id = store.prepare_application(
        scope_type="position_supervisor_template",
        scope_key=CONSERVATIVE_TEMPLATE_ID,
        action="switch_position_supervisor_template",
        status="applied",
        cycle_ts=100.0,
        mutation_id="not-current",
        details={"commit_boundary": "governance_mutation_coordinator"},
    )
    store.write_effect(
        application_id=application_id,
        scope_key=CONSERVATIVE_TEMPLATE_ID,
        scope_type="position_supervisor_template",
        action="switch_position_supervisor_template",
        status="observing",
        observed_trade_count=50,
        delta_avg_reward=1.0,
        decision={"result": "positive"},
        mutation_id="not-current",
        updated_at=101.0,
    )

    projection = build_position_supervisor_selection_projection(
        db_path=db_path,
        cfg=RuntimeConfig(),
        now_ts=102.0,
    )
    assert projection["candidate_count"] == 0
    assert any(
        item["reason"] == "governance_mutation_unavailable"
        for item in projection["rejected_candidates"]
    )


def test_selector_requires_fresh_projection_and_preserves_current_on_tie():
    selection_key = {
        "symbol": "XAUUSD+",
        "timeframe": "M5",
        "entry_regime": "trend",
        "current_regime": "range",
    }
    default = build_position_supervisor_binding(
        DEFAULT_TEMPLATE_ID,
        selection_key=selection_key,
        bound_at=100.0,
    )
    conservative = get_position_supervisor_template(CONSERVATIVE_TEMPLATE_ID)
    profit = get_position_supervisor_template("position_supervisor:profit_protection.v1")
    projection = {
        "ok": True,
        "published_at": 100.0,
        "default_binding": default,
        "candidates": [
            _candidate(
                conservative,
                score=1.5,
                selection_key=selection_key,
                event_id="event-conservative",
            )
        ],
    }
    selected = select_position_supervisor_binding(
        projection,
        **selection_key,
        current_binding=default,
        now_ts=101.0,
    )
    assert selected["ok"] is True
    assert selected["changed"] is True
    assert selected["reason"] == "selected_highest_positive_effect"
    assert selected["binding"]["template_id"] == CONSERVATIVE_TEMPLATE_ID
    assert selected["binding"]["binding_source"] == "governed_selection_projection"

    tie_projection = {
        **projection,
        "candidates": [
            _candidate(
                conservative,
                score=1.5,
                selection_key=selection_key,
                event_id="event-conservative",
            ),
            _candidate(
                profit,
                score=1.5,
                selection_key=selection_key,
                event_id="event-profit",
            ),
        ],
    }
    tied = select_position_supervisor_binding(
        tie_projection,
        **selection_key,
        current_binding=default,
        now_ts=101.0,
    )
    assert tied["changed"] is False
    assert tied["reason"] == "effect_tie_or_conflict_no_change"
    assert tied["binding"] == default

    stale = select_position_supervisor_binding(
        {**projection, "published_at": 0.0},
        **selection_key,
        current_binding=default,
        now_ts=1001.0,
        max_age_seconds=900.0,
    )
    assert stale["ok"] is False
    assert stale["reason"] == "selection_projection_stale_or_unavailable"
    assert stale["binding"] == default


def test_binding_aware_trace_preserves_strategy_and_execution_lineage():
    binding = build_position_supervisor_binding(
        DEFAULT_TEMPLATE_ID,
        binding_source="governed_selection_projection",
        selection_key={"current_regime": "range"},
        evidence_refs={"selection_event_id": "selection-1"},
        bound_at=100.0,
    )
    template = get_position_supervisor_template(DEFAULT_TEMPLATE_ID)
    risk = {"allowed": True, "reason": "no_mutation"}
    payload = build_supervisor_trace_ledger_payload(
        position={"position_id": 9, "symbol": "XAUUSD+", "direction": 1},
        verdict={
            "action": "hold",
            "requested_action": "hold",
            "effective_action": "hold",
            "summary_reason": "no_change",
            "supervisor_template": template,
            "position_supervisor_policy": {
                "binding": binding,
                "binding_source": binding["binding_source"],
            },
            "evidence": {
                "position_supervisor_binding": binding,
                "current_regime": "range",
                "supervisor_posture": "observe",
            },
        },
        cfg=SimpleNamespace(timeframe="M5"),
        tick=3,
        stage="observed",
        outcome="hold",
        risk_verdict=risk,
        execution_status="no_op",
        execution_reason="cooldown",
        execution={"no_change_reason": "cooldown"},
    )

    assert payload["template_hash"] == binding["template_hash"]
    assert payload["binding_source"] == "governed_selection_projection"
    assert payload["selection_event_id"] == "selection-1"
    assert payload["current_regime"] == "range"
    assert payload["requested_action"] == "hold"
    assert payload["effective_action"] == "hold"
    assert payload["applied_action"] == ""
    assert payload["risk_policy_result"] == risk
    assert payload["no_change_reason"] == "cooldown"


def test_runtime_config_selection_defaults_off_and_rejects_unknown_mode():
    assert RuntimeConfig().position_supervisor_auto_selection_mode == "off"
    with pytest.raises(ValueError, match="invalid_position_supervisor_auto_selection_mode"):
        RuntimeConfig.from_dict({"position_supervisor_auto_selection_mode": "autonomous"})

    parsed = RuntimeConfig.from_dict(
        {
            "position_supervisor_auto_selection_mode": "demo_execute",
            "position_supervisor_switch_min_stable_bars": "2",
            "position_supervisor_selection_max_age_seconds": "901",
        }
    )
    assert parsed.position_supervisor_auto_selection_mode == "demo_execute"
    assert parsed.position_supervisor_switch_min_stable_bars == 2
    assert parsed.position_supervisor_selection_max_age_seconds == 901.0


def test_v16_auto_selection_mode_delegation_is_idempotent_and_claimable(tmp_path):
    db_path = tmp_path / "state.db"
    projection = {
        "schema_version": "position_supervisor_selection.v1",
        "status": "ready",
        "source_watermark": "123.0",
        "selection_fingerprint": "published-fingerprint",
        "evidence_policy": {
            "causal_scope": "supervisor",
            "requires_clean_mature_counterfactual": True,
            "requires_template_hash_binding": True,
            "requires_current_coordinator_mutation": True,
            "requires_positive_application_effect": True,
        },
        "candidates": [
            {
                "template_id": CONSERVATIVE_TEMPLATE_ID,
                "template_version": "v1",
                "template_hash": "template-hash",
                "selection_key": {"current_regime": "trend"},
                "effect_score": 1.5,
                "observed_trade_count": 50,
                "mature_trade_count": 50,
                "application_id": "application-1",
                "effect_id": "effect-1",
                "posterior_fingerprint": "posterior-1",
                "selection_event_id": "selection-1",
            }
        ],
    }
    service = V16BrainOrchestratorService(db_path)

    first = service.delegate_supervisor_selection_mode(
        projection,
        current_mode="off",
        persist=True,
    )
    second = service.delegate_supervisor_selection_mode(
        projection,
        current_mode="off",
        persist=True,
    )

    assert first["status"] == "delegated"
    assert second["status"] == "delegated"
    first_command = first["command"]
    second_command = second["command"]
    assert first_command["command_id"] == second_command["command_id"]
    assert first_command["action"] == "switch_position_supervisor_selection_mode"
    assert first_command["scope_type"] == "supervisor_selection"

    claim = V16CommandGate.claim(
        db_path,
        target_agent="position_supervisor_governance",
        scope_type="supervisor_selection",
        scope_key="position_supervisor_selection",
        action="switch_position_supervisor_selection_mode",
        candidate_id=first_command["candidate_id"],
        evidence_fingerprint=first_command["evidence_fingerprint"],
    )
    assert claim["allowed"] is True


def test_selection_mode_risk_policy_requires_ready_projection_and_v16_command():
    service = RiskPolicyService()
    blocked = service.evaluate(
        "switch_position_supervisor_selection_mode",
        {
            "current_mode": "off",
            "target_mode": "demo_execute",
            "bounded_demo_mode": True,
            "v16_command_id": "",
            "selection_projection": {
                "status": "insufficient_evidence",
                "candidate_count": 0,
            },
        },
    )
    assert blocked.allowed is False
    assert blocked.reason == "selection_projection_evidence_not_ready"

    allowed = service.evaluate(
        "switch_position_supervisor_selection_mode",
        {
            "current_mode": "off",
            "target_mode": "demo_execute",
            "bounded_demo_mode": True,
            "v16_command_id": "v16cmd-selection",
            "selection_projection": {
                "status": "ready",
                "candidate_count": 1,
                "evidence_policy": {
                    "causal_scope": "supervisor",
                    "requires_clean_mature_counterfactual": True,
                    "requires_template_hash_binding": True,
                    "requires_current_coordinator_mutation": True,
                    "requires_positive_application_effect": True,
                },
            },
        },
    )
    assert allowed.allowed is True
    assert allowed.reason == "selection_projection_ready_auto_enable"


def test_shadow_boundary_records_selection_trace_without_changing_binding(monkeypatch):
    selection_key = {
        "symbol": "XAUUSD+",
        "timeframe": "M5",
        "entry_regime": "trend",
        "current_regime": "range",
    }
    current_binding = build_position_supervisor_binding(
        DEFAULT_TEMPLATE_ID,
        selection_key={**selection_key, "current_regime": "trend"},
        bound_at=100.0,
    )
    candidate_template = get_position_supervisor_template(CONSERVATIVE_TEMPLATE_ID)
    projection = {
        "ok": True,
        "published_at": 100.0,
        "default_binding": current_binding,
        "candidates": [
            _candidate(
                candidate_template,
                score=1.0,
                selection_key=selection_key,
                event_id="shadow-event",
            )
        ],
    }
    merges = []
    traces = []
    monkeypatch.setattr(
        live_service,
        "_load_recovery_row_for_risk_reduction",
        lambda *_args, **_kwargs: {
            "recovery_meta": {
                "entry_protection_plan": {"supervisor_binding": current_binding}
            }
        },
    )
    monkeypatch.setattr(
        live_service,
        "_merge_recovery_position_meta",
        lambda position_id, payload: merges.append((position_id, payload)),
    )
    monkeypatch.setattr(
        live_service,
        "_runtime_kv_get",
        lambda *_args, **_kwargs: projection,
    )
    monkeypatch.setattr(
        live_service,
        "_live_state_get",
        lambda key, default=None, clone=False: {
            "execution_recovery": {"ready": True, "unresolved_count": 0},
            "safety_plane": {"reconciliation_state": "fresh", "blockers": []},
            "safety_cycle_active": False,
        }.get(key, default),
    )
    monkeypatch.setattr(live_service, "_LEDGER", object())
    monkeypatch.setattr(
        live_service,
        "_log_supervisor_trace",
        lambda **kwargs: traces.append(kwargs) or "trace-shadow-1",
    )

    result = live_service._maybe_switch_position_supervisor_binding(
        position={"position_id": 7, "symbol": "XAUUSD+"},
        cfg=SimpleNamespace(
            position_supervisor_auto_selection_mode="shadow",
            position_supervisor_switch_min_stable_bars=1,
            position_supervisor_switch_cooldown_bars=3,
            position_supervisor_max_switches_per_position=2,
            position_supervisor_selection_max_age_seconds=900.0,
            timeframe="M5",
        ),
        context={
            "position_supervisor_policy": {
                "binding_state": "bound",
                "binding": current_binding,
            },
            "position": {
                "current_price_state": "known",
                "pnl_state": "known",
                "position_path_metrics_state": "known",
            },
            "market": {"market_context_state": "known", "regime_id": "range"},
            "temporal_context": {"closed_bar_key": "bar-2"},
        },
        verdict={
            "action": "hold",
            "summary_reason": "position_healthy",
            "evidence": {
                "current_regime": "range",
                "closed_bar_key": "bar-2",
                "completed_bars_after_entry": 2,
                "market_dimensions_known": True,
                "trigger_tags": [],
            },
        },
        now_ts=101.0,
    )

    assert result["action"] == "hold"
    assert merges
    assert "entry_protection_plan" not in merges[-1][1]
    assert traces[0]["stage"] == "selection_shadow"
    assert traces[0]["outcome"] == "shadow"
    assert traces[0]["execution_status"] == "shadow_only"
    assert traces[0]["execution"]["broker_action_attempted"] is False
