from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from backend.services.governance_mutation_coordinator import (
    GovernanceMutationCoordinator,
    GovernanceMutationPlan,
)
from backend.services.runtime_config_overlay import (
    RuntimeConfigOverlayAuthorityError,
    RuntimeConfigOverlayService,
)
from config import runtime_config
from config.runtime_config import RuntimeConfig


@pytest.fixture(autouse=True)
def _reset_runtime_config():
    runtime_config.reset_for_tests()
    yield
    runtime_config.reset_for_tests()


def _set_mode(monkeypatch, mode: str) -> None:
    from backend.core import static_feature_flags

    monkeypatch.setattr(
        static_feature_flags,
        "shared_static_feature_flags",
        lambda: SimpleNamespace(
            governance_mutation_coordinator_v2_mode=mode,
        ),
    )


@pytest.mark.parametrize("mode", ["off", "dual_record", "enforce"])
def test_blank_legacy_overlay_is_quarantined_without_relaxing_existing_protection(
    tmp_path, monkeypatch, mode
):
    _set_mode(monkeypatch, mode)
    db_path = tmp_path / "state.db"
    service = RuntimeConfigOverlayService(db_path)
    service.apply_patch(
        {
            "position_supervisor_template_id": "position_supervisor:profit_protection.v1",
            "runtime_incident_mode": "normal",
            "live_autonomy_unlocked": True,
        },
        source="legacy_runtime_projection",
        run_id="legacy_1",
    )
    base = RuntimeConfig(
        runtime_incident_mode="only_close",
        live_autonomy_unlocked=False,
    )

    with pytest.raises(RuntimeConfigOverlayAuthorityError) as caught:
        service.restore_on_startup(base)

    projected = caught.value.quarantined_config
    assert projected is not None
    # Preserve the already-running supervisor behavior for open positions.
    assert (
        projected.position_supervisor_template_id
        == "position_supervisor:profit_protection.v1"
    )
    # The quarantined row cannot thaw release/operator controls.
    assert projected.runtime_incident_mode == "only_close"
    assert projected.live_autonomy_unlocked is False
    assert caught.value.report["new_risk_authorized"] is False
    assert caught.value.report["quarantine_projection"] == "legacy_behavior_preserved"


@pytest.mark.parametrize("mode", ["off", "dual_record", "enforce"])
def test_committed_current_hash_bound_overlay_restores_in_every_mode(
    tmp_path, monkeypatch, mode
):
    _set_mode(monkeypatch, mode)
    db_path = tmp_path / "state.db"
    base = RuntimeConfig()
    runtime_config.register_overlay_base(base, db_path)
    result = GovernanceMutationCoordinator(db_path).execute(
        GovernanceMutationPlan(
            patch={"governance_expansion_paused": True},
            source="operator_pause",
            actor="operator:test",
            action="pause_governance_expansion",
            control_surface="operator_governance_pause",
            scope_type="operator_governance_pause",
            scope_key="global",
            run_id=f"committed_{mode}",
        )
    )
    assert result["ok"] is True

    restored = RuntimeConfigOverlayService(db_path).restore_on_startup(base)

    assert restored["restored"] is True
    assert restored["config"].governance_expansion_paused is True
    assert restored["authority"]["authority"] == "committed_mutation"
    assert restored["authority"]["checks"]["projection_current"] is True


@pytest.mark.parametrize("mode", ["off", "dual_record", "enforce"])
def test_dangling_mutation_overlay_retains_only_derived_tightening_controls(
    tmp_path, monkeypatch, mode
):
    _set_mode(monkeypatch, mode)
    db_path = tmp_path / "state.db"
    base = RuntimeConfig()
    runtime_config.register_overlay_base(base, db_path)
    result = GovernanceMutationCoordinator(db_path).execute(
        GovernanceMutationPlan(
            patch={
                "governance_expansion_paused": True,
                "position_supervisor_template_id": "position_supervisor:profit_protection.v1",
            },
            source="mixed_control",
            actor="operator:test",
            action="mixed_control",
            control_surface="mixed_control",
            scope_type="mixed_control",
            scope_key="global",
            run_id=f"dangling_{mode}",
        )
    )
    mutation_id = result["mutation_id"]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE governance_mutation_intent SET status='aborted' WHERE mutation_id=?",
            (mutation_id,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeConfigOverlayAuthorityError) as caught:
        RuntimeConfigOverlayService(db_path).restore_on_startup(base)

    projected = caught.value.quarantined_config
    assert projected is not None
    assert projected.governance_expansion_paused is True
    assert (
        projected.position_supervisor_template_id
        == base.position_supervisor_template_id
    )
    assert "governance_expansion_paused" in caught.value.report["retained_keys"]
    assert "position_supervisor_template_id" in caught.value.report["excluded_keys"]
    assert caught.value.report["new_risk_authorized"] is False


def test_operator_can_backfill_only_derived_tightening_legacy_controls(tmp_path):
    db_path = tmp_path / "state.db"
    base = RuntimeConfig(governance_expansion_paused=False)
    service = RuntimeConfigOverlayService(db_path)
    service.apply_patch(
        {"governance_expansion_paused": True},
        source="legacy_operator_pause",
        run_id="legacy_pause_1",
    )
    overlay_hash = service.latest()["overlay_hash"]

    review = service.review_legacy_quarantine(
        base,
        expected_overlay_hash=overlay_hash,
        reviewed_keys=["governance_expansion_paused"],
        reviewer="operator:pytest",
        review_id="review-legacy-pause-1",
    )
    restored = service.restore_on_startup(base)

    assert review["status"] == "legacy_quarantine_complete"
    assert restored["config"].governance_expansion_paused is True
    assert restored["authority"]["authority"] == "legacy_quarantined"


def test_runtime_refresh_latches_and_keeps_legacy_overlay_read_only(
    tmp_path, monkeypatch
):
    from backend.services import live_safety_state

    _set_mode(monkeypatch, "off")
    db_path = tmp_path / "state.db"
    service = RuntimeConfigOverlayService(db_path)
    service.apply_patch(
        {
            "position_supervisor_template_id": "position_supervisor:profit_protection.v1",
        },
        source="legacy_supervisor",
        run_id="legacy_supervisor_1",
    )
    runtime_config.register_overlay_base(RuntimeConfig(), db_path)
    latched = []
    monkeypatch.setattr(
        live_safety_state,
        "activate_no_new_risk_latch",
        lambda **kwargs: latched.append(kwargs) or {"active": True},
    )

    refreshed = runtime_config.refresh_from_overlay(db_path, force=True)

    assert refreshed is True
    assert latched[0]["cause"] == "governance_authority"
    assert latched[0]["cause_id"] == "runtime_config_overlay_refresh"
    assert (
        runtime_config.shared_holder().get().position_supervisor_template_id
        == "position_supervisor:profit_protection.v1"
    )


def test_production_overlay_base_uses_yaml_before_runtime_bootstrap(
    tmp_path, monkeypatch
):
    from backend.core import db
    from backend.services import runtime_config_startup

    db_path = tmp_path / "production-state.db"
    yaml_base = RuntimeConfig(
        ctrader_send_orders=True,
        runtime_incident_mode="normal",
    )
    runtime_config.replace(
        RuntimeConfig(
            ctrader_send_orders=False,
            runtime_incident_mode="frozen",
        )
    )
    monkeypatch.setattr(db, "is_state_db_path", lambda path: path == db_path)
    monkeypatch.setattr(
        runtime_config_startup,
        "load_yaml_runtime_config",
        lambda: (yaml_base, {}),
    )

    loaded = runtime_config.overlay_base_config(db_path)

    assert loaded == yaml_base.to_dict()
    assert loaded != runtime_config.shared_holder().get().to_dict()


def test_runtime_refresh_retries_transient_projection_and_releases_exact_cause(
    tmp_path, monkeypatch
):
    from backend.services import live_safety_state

    _set_mode(monkeypatch, "dual_record")
    db_path = tmp_path / "state.db"
    base = RuntimeConfig()
    runtime_config.register_overlay_base(base, db_path)
    committed = GovernanceMutationCoordinator(db_path).execute(
        GovernanceMutationPlan(
            patch={"governance_expansion_paused": True},
            source="operator_pause",
            actor="operator:test",
            action="pause_governance_expansion",
            control_surface="operator_governance_pause",
            scope_type="operator_governance_pause",
            scope_key="global",
            run_id="transient_projection_test",
        )
    )
    assert committed["ok"] is True, committed
    mutation_id = committed["mutation_id"]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE governance_mutation_intent SET projection_status='pending' "
            "WHERE mutation_id=?",
            (mutation_id,),
        )
        conn.commit()
    finally:
        conn.close()

    activated = []
    released = []
    latch = {
        "active": True,
        "causes": [
            {
                "cause": "governance_authority",
                "cause_id": "runtime_config_overlay_refresh",
            },
            {"cause": "incident", "cause_id": "operator_freeze"},
        ],
    }
    monkeypatch.setattr(
        live_safety_state,
        "activate_no_new_risk_latch",
        lambda **kwargs: activated.append(kwargs) or latch,
    )
    monkeypatch.setattr(
        live_safety_state,
        "no_new_risk_latch_status",
        lambda **_kwargs: latch,
    )

    def _release(**kwargs):
        released.append(kwargs)
        return {
            "active": True,
            "causes": [{"cause": "incident", "cause_id": "operator_freeze"}],
        }

    monkeypatch.setattr(
        live_safety_state,
        "release_no_new_risk_latch_cause",
        _release,
    )

    assert runtime_config.refresh_from_overlay(db_path, force=True) is True
    assert activated
    assert released == []

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE governance_mutation_intent SET projection_status='current' "
            "WHERE mutation_id=?",
            (mutation_id,),
        )
        conn.commit()
    finally:
        conn.close()

    assert runtime_config.refresh_from_overlay(db_path, force=True) is True
    assert released[0]["cause"] == "governance_authority"
    assert released[0]["cause_id"] == "runtime_config_overlay_refresh"
    assert released[0]["reason"] == "runtime_overlay_authority_recovered"
    assert runtime_config.shared_holder().get().governance_expansion_paused is True
