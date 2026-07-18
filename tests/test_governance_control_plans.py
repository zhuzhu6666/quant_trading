from __future__ import annotations

import sqlite3
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.db import STATE_DB_DDL
from backend.services.parameter_templates import ParameterTemplateService
from backend.services.model_influence_governance import (
    ModelInfluenceGovernanceService,
)
from backend.services.governance_mutation_coordinator import classify_governance_risk
from backend.services.governance_control_plans import (
    ModelPolicyActivationPlan,
    ParameterTemplateActivationPlan,
    PositionSupervisorTemplatePlan,
)
from backend.services.position_supervisor_governance import (
    PositionSupervisorGovernanceMutationService,
)
from config import runtime_config
from risk.policy_service import RiskPolicyService


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


def _init_state(db_path: Path, *, migrated_columns: bool = True) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        if migrated_columns:
            additions = {
                "learning_application_log": {
                    "mutation_id": "TEXT NOT NULL DEFAULT ''",
                    "governance_eligibility_version": "TEXT NOT NULL DEFAULT ''",
                },
                "learning_application_effect": {
                    "mutation_id": "TEXT NOT NULL DEFAULT ''",
                    "governance_eligibility_version": "TEXT NOT NULL DEFAULT ''",
                },
                "learning_experiment_reservation": {
                    "mutation_id": "TEXT NOT NULL DEFAULT ''",
                },
                "policy_suggestion": {
                    "applied_mutation_id": "TEXT NOT NULL DEFAULT ''",
                },
            }
            for table, columns in additions.items():
                existing = {
                    str(row[1])
                    for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                }
                for column, ddl in columns.items():
                    if column not in existing:
                        conn.execute(
                            f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}'
                        )
        conn.commit()
    finally:
        conn.close()


def test_parameter_template_startup_restore_is_overlay_owned_in_coordinator_mode(
    tmp_path, monkeypatch
):
    _set_mode(monkeypatch, "enforce")
    service = ParameterTemplateService(tmp_path / "unused.sqlite")
    monkeypatch.setattr(
        service,
        "build_runtime_signal_config",
        lambda: (_ for _ in ()).throw(AssertionError("legacy registry rebuild called")),
    )

    assert service.sync_runtime_config(restore_only=True) == runtime_config.version()


def test_parameter_template_projection_cannot_implicitly_activate_missing_factor(
    tmp_path, monkeypatch
):
    service = ParameterTemplateService(tmp_path / "state.db")
    monkeypatch.setattr(
        service,
        "list_active_templates",
        lambda: [
            {
                "factor_id": "new_dsl_factor",
                "regime_key": "default",
                "template_id": "new_dsl_factor:default:v1",
                "status": "active",
            }
        ],
    )
    monkeypatch.setattr(
        service,
        "get_template",
        lambda **_kwargs: {
            "factor_id": "new_dsl_factor",
            "regime_key": "default",
            "template_version": "v1",
            "template_role": "candidate",
            "source": "test",
            "parameters": {"window": 20},
        },
    )
    monkeypatch.setattr(service.cards, "list_cards", lambda **_kwargs: [])

    projected = service.build_runtime_signal_config(base_config={})

    assert projected["new_dsl_factor"]["enabled"] is False
    assert projected["new_dsl_factor"]["weight"] == 0.0
    assert projected["new_dsl_factor"]["lifecycle_status"] == "SHADOW"


def test_parameter_template_strict_startup_refuses_uncommitted_active_row(
    tmp_path, monkeypatch
):
    service = ParameterTemplateService(tmp_path / "state.db")
    monkeypatch.setattr(
        service,
        "list_active_templates",
        lambda: [
            {
                "factor_id": "rsi_14",
                "regime_key": "",
                "template_id": "rsi_14:default:v1",
                "context": {},
            }
        ],
    )

    with pytest.raises(RuntimeError, match="legacy_parameter_template_restore_unverified"):
        service._validated_startup_active_templates()


def test_parameter_template_strict_startup_allows_reviewed_tightening_quarantine(
    tmp_path, monkeypatch
):
    service = ParameterTemplateService(tmp_path / "state.db")
    active = {
        "factor_id": "rsi_14",
        "regime_key": "",
        "template_id": "rsi_14:conservative:v1",
        "context": {
            "governance_authority": "legacy_quarantined",
            "risk_class": "risk_tightening",
        },
    }
    monkeypatch.setattr(service, "list_active_templates", lambda: [active])

    restored = service._validated_startup_active_templates()

    assert restored[0]["governance_authority"] == "legacy_quarantined"
    assert restored[0]["committed_mutation_id"] == ""


def test_parameter_template_strict_startup_accepts_hash_bound_committed_row(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "state.db"
    service = ParameterTemplateService(db_path)
    mutation_id = "gmut_parameter_committed"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE governance_mutation_intent (
                mutation_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                projection_status TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                committed_config_hash TEXT NOT NULL,
                domain_hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO governance_mutation_intent
            (mutation_id, status, projection_status, scope_type,
             committed_config_hash, domain_hash)
            VALUES (?, 'committed', 'current', 'parameter_template',
                    'config-sha256', 'domain-sha256')
            """,
            (mutation_id,),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(
        service,
        "list_active_templates",
        lambda: [
            {
                "factor_id": "rsi_14",
                "regime_key": "",
                "template_id": "rsi_14:default:v1",
                "context": {
                    "mutation_id": mutation_id,
                    "commit_boundary": "governance_mutation_coordinator",
                },
            }
        ],
    )

    restored = service._validated_startup_active_templates()

    assert restored[0]["governance_authority"] == "committed_mutation"
    assert restored[0]["committed_mutation_id"] == mutation_id


def _seed_supervisor_switch(db_path: Path, suffix: str) -> tuple[str, str]:
    suggestion_id = f"psg_{suffix}"
    reservation_id = f"lexp_{suffix}"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO policy_suggestion
               (suggestion_id, scope_type, scope_key, action, confidence,
                reason, evidence_json, status, governance_eligible,
                governance_eligibility_version,
                governance_eligibility_fingerprint, created_at)
               VALUES (?, 'position_supervisor_template', ?,
                       'switch_position_supervisor_template', 0.9,
                       'test', '{}', 'approved', 1,
                       'governance_eligibility.v1', ?, 1.0)""",
            (
                suggestion_id,
                "position_supervisor:profit_protection.v1",
                f"eligible-{suffix}",
            ),
        )
        conn.execute(
            """INSERT INTO learning_experiment_reservation
               (reservation_id, scope_type, scope_key, action, status,
                application_id, expires_at, created_at, updated_at)
               VALUES (?, 'position_supervisor_template', ?,
                       'switch_position_supervisor_template', 'reserved',
                       '', 99999999999.0, 1.0, 1.0)""",
            (reservation_id, "position_supervisor:profit_protection.v1"),
        )
        conn.commit()
    finally:
        conn.close()
    return suggestion_id, reservation_id


def _row(db_path: Path, sql: str, params=()) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["dual_record", "enforce"])
def test_supervisor_switch_commits_domain_and_runtime_with_one_mutation(
    tmp_path, monkeypatch, mode
):
    _set_mode(monkeypatch, mode)
    db_path = tmp_path / f"{mode}.db"
    _init_state(db_path)
    suggestion_id, reservation_id = _seed_supervisor_switch(db_path, mode)

    result = PositionSupervisorGovernanceMutationService(db_path).switch_template(
        suggestion_id=suggestion_id,
        previous_template_id="position_supervisor:default.v1",
        target_template_id="position_supervisor:profit_protection.v1",
        actor="system:pytest",
        source="pytest_supervisor_switch",
        run_id=f"run-{mode}",
        reason="typed switch test",
        evidence={"sample_count": 100},
        risk_verdict={"allowed": True},
        reservation_id=reservation_id,
    )

    assert result["committed"] is True
    mutation_id = result["mutation_id"]
    assert mutation_id
    assert _row(
        db_path,
        "SELECT status FROM governance_mutation_intent WHERE mutation_id=?",
        (mutation_id,),
    )["status"] == "committed"
    application = _row(
        db_path,
        "SELECT status, mutation_id FROM learning_application_log WHERE application_id=?",
        (result["application_id"],),
    )
    effect = _row(
        db_path,
        "SELECT status, mutation_id FROM learning_application_effect WHERE application_id=?",
        (result["application_id"],),
    )
    suggestion = _row(
        db_path,
        "SELECT status, applied_mutation_id FROM policy_suggestion WHERE suggestion_id=?",
        (suggestion_id,),
    )
    reservation = _row(
        db_path,
        "SELECT status, mutation_id FROM learning_experiment_reservation WHERE reservation_id=?",
        (reservation_id,),
    )
    assert application == {"status": "applied", "mutation_id": mutation_id}
    assert effect == {"status": "observing", "mutation_id": mutation_id}
    assert suggestion == {"status": "applied", "applied_mutation_id": mutation_id}
    assert reservation == {"status": "consumed", "mutation_id": mutation_id}


def test_supervisor_domain_writer_failure_rolls_back_all_facts(
    tmp_path, monkeypatch
):
    _set_mode(monkeypatch, "enforce")
    db_path = tmp_path / "failure.db"
    _init_state(db_path)
    suggestion_id, reservation_id = _seed_supervisor_switch(db_path, "failure")
    from backend.services import position_supervisor_governance as module

    original = module._write_supervisor_switch_domain

    def fail_after_domain_write(conn, **kwargs):
        original(conn, **kwargs)
        raise RuntimeError("fault_after_domain_write")

    monkeypatch.setattr(module, "_write_supervisor_switch_domain", fail_after_domain_write)
    result = PositionSupervisorGovernanceMutationService(db_path).switch_template(
        suggestion_id=suggestion_id,
        previous_template_id="position_supervisor:default.v1",
        target_template_id="position_supervisor:profit_protection.v1",
        actor="system:pytest",
        source="pytest_supervisor_switch_failure",
        run_id="run-failure",
        reason="fault injection",
        evidence={"sample_count": 100},
        risk_verdict={"allowed": True},
        reservation_id=reservation_id,
    )

    assert result["committed"] is False
    assert _row(db_path, "SELECT COUNT(*) AS n FROM learning_application_log")["n"] == 0
    assert _row(
        db_path,
        "SELECT status FROM policy_suggestion WHERE suggestion_id=?",
        (suggestion_id,),
    )["status"] == "approved"
    assert _row(
        db_path,
        "SELECT status FROM learning_experiment_reservation WHERE reservation_id=?",
        (reservation_id,),
    )["status"] == "released"
    assert _row(
        db_path,
        "SELECT status FROM governance_mutation_intent ORDER BY created_at DESC LIMIT 1",
    )["status"] == "aborted"


def test_supervisor_switch_revalidates_suggestion_eligibility_inside_transaction(
    tmp_path, monkeypatch
):
    _set_mode(monkeypatch, "enforce")
    db_path = tmp_path / "ineligible-supervisor.db"
    _init_state(db_path)
    suggestion_id, reservation_id = _seed_supervisor_switch(db_path, "ineligible")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """UPDATE policy_suggestion
               SET governance_eligible=0,
                   governance_eligibility_version='',
                   governance_eligibility_fingerprint=''
               WHERE suggestion_id=?""",
            (suggestion_id,),
        )
        conn.commit()
    finally:
        conn.close()

    result = PositionSupervisorGovernanceMutationService(db_path).switch_template(
        suggestion_id=suggestion_id,
        previous_template_id="position_supervisor:default.v1",
        target_template_id="position_supervisor:profit_protection.v1",
        actor="system:pytest",
        source="pytest_ineligible_supervisor_switch",
        run_id="run-ineligible-supervisor",
        reason="must fail closed",
        evidence={"sample_count": 100},
        risk_verdict={"allowed": True},
        reservation_id=reservation_id,
    )

    assert result["committed"] is False
    assert _row(
        db_path,
        "SELECT status FROM policy_suggestion WHERE suggestion_id=?",
        (suggestion_id,),
    )["status"] == "approved"
    assert _row(
        db_path,
        "SELECT status FROM governance_mutation_intent ORDER BY created_at DESC LIMIT 1",
    )["status"] == "aborted"


def test_parameter_template_suggestion_gate_requires_eligibility_in_coordinator_mode(
    tmp_path, monkeypatch
):
    _set_mode(monkeypatch, "dual_record")
    db_path = tmp_path / "ineligible-parameter.db"
    _init_state(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO policy_suggestion
               (suggestion_id, scope_type, scope_key, action, confidence,
                reason, evidence_json, status, created_at)
               VALUES ('ineligible-parameter', 'parameter_template',
                       'rsi_14:range', 'switch_parameter_template', 0.9,
                       'test', '{}', 'approved', 1.0)"""
        )
        conn.commit()
    finally:
        conn.close()

    service = ParameterTemplateService(str(db_path))
    assert service._suggestion_is_approved("ineligible-parameter") is False

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """UPDATE policy_suggestion
               SET governance_eligible=1,
                   governance_eligibility_version='governance_eligibility.v1',
                   governance_eligibility_fingerprint='eligible-parameter'
               WHERE suggestion_id='ineligible-parameter'"""
        )
        conn.commit()
    finally:
        conn.close()

    assert service._suggestion_is_approved("ineligible-parameter") is True


def test_parameter_template_activation_uses_atomic_coordinator_writer(
    tmp_path, monkeypatch
):
    _set_mode(monkeypatch, "dual_record")
    db_path = tmp_path / "parameter.db"
    _init_state(db_path)

    class Allowed:
        def to_dict(self):
            return {"allowed": True, "reason": "pytest"}

    monkeypatch.setattr(RiskPolicyService, "evaluate", lambda *_args, **_kwargs: Allowed())
    service = ParameterTemplateService(str(db_path))
    monkeypatch.setattr(
        service,
        "assess_template_change",
        lambda **_kwargs: {"recommended_scope": "online_light", "changed": ["length"]},
    )
    previous = service.upsert_template(
        {
            "factor_id": "rsi_14",
            "regime_key": "range",
            "factor_family": "momentum_oscillator",
            "template_version": "typed.previous.v1",
            "template_role": "default",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 14, "upper_band": 70, "lower_band": 30},
            "applicable_regimes": ["range"],
            "avoid_regimes": ["strong_trend"],
            "holding_profile_hint": {"style": "short_swing"},
            "evidence": {"sample_count": 100},
        },
        source="pytest",
    )
    target = service.upsert_template(
        {
            "factor_id": "rsi_14",
            "regime_key": "range",
            "factor_family": "momentum_oscillator",
            "template_version": "typed.v1",
            "template_role": "conservative",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 21, "upper_band": 74, "lower_band": 26},
            "applicable_regimes": ["range"],
            "avoid_regimes": ["strong_trend"],
            "holding_profile_hint": {"style": "confirmation"},
            "evidence": {"sample_count": 100},
        },
        source="pytest",
    )

    result = service.activate_template(
        factor_id="rsi_14",
        regime_key="range",
        template_id=target["template_id"],
        note="typed parameter activation",
    )

    assert result["ok"] is True
    mutation_id = result["mutation_id"]
    active = service.get_active_template(factor_id="rsi_14", regime_key="range")
    application = _row(
        db_path,
        "SELECT status, mutation_id FROM learning_application_log WHERE application_id=?",
        (result["application_id"],),
    )
    assert active["template_id"] == target["template_id"]
    assert application == {"status": "applied", "mutation_id": mutation_id}
    assert result["mutation"]["risk_classification"]["classification_source"] == (
        "coordinator_before_after"
    )

    rollback = service.rollback_template_application(
        application_id=result["application_id"],
        factor_id="rsi_14",
        regime_key="range",
        current_template_id=target["template_id"],
        previous_template_id=previous["template_id"],
        reason="typed effect rollback",
        evidence={"delta_avg_reward": -0.2},
    )
    assert rollback["ok"] is True
    rolled_back = _row(
        db_path,
        "SELECT status, mutation_id FROM learning_application_log WHERE application_id=?",
        (result["application_id"],),
    )
    assert rolled_back == {
        "status": "rolled_back",
        "mutation_id": rollback["mutation_id"],
    }
    assert service.get_active_template(
        factor_id="rsi_14", regime_key="range"
    )["template_id"] == previous["template_id"]


def test_model_demotion_is_typed_tightening_and_does_not_require_v16(
    tmp_path, monkeypatch
):
    _set_mode(monkeypatch, "enforce")
    db_path = tmp_path / "model.db"
    _init_state(db_path)

    result = ModelInfluenceGovernanceService(db_path).demote(
        "open_quality_lightgbm", reason="pytest quarantine"
    )

    assert result["ok"] is True
    mutation = result["mutation"]
    assert mutation["risk_classification"]["risk_class"] == "risk_tightening"
    assert mutation["v16_authority"]["status"] == "risk_tightening_exempt"
    assert (
        runtime_config.shared().model_influence_config["models"]
        ["open_quality_lightgbm"]["stage"]
        == "quarantined"
    )
    absent = {"model_influence_config": {"models": {}}}
    retired = {
        "model_influence_config": {
            "models": {"open_quality_lightgbm": {"stage": "retired"}}
        }
    }
    active = {
        "model_influence_config": {
            "models": {"open_quality_lightgbm": {"stage": "demo_active"}}
        }
    }
    assert classify_governance_risk(absent, retired).risk_class == "risk_tightening"
    assert classify_governance_risk(absent, active).risk_class == "risk_expanding"


def test_governance_write_boundaries_have_no_legacy_direct_activation():
    root = Path(__file__).resolve().parents[1]
    api_source = (root / "backend/api/learning.py").read_text(encoding="utf-8")
    autonomous_source = (root / "backend/services/autonomous_learning.py").read_text(
        encoding="utf-8"
    )
    governor_source = (root / "research/learning/governor.py").read_text(
        encoding="utf-8"
    )
    shadow_source = (root / "backend/services/shadow_service.py").read_text(
        encoding="utf-8"
    )
    model_source = (root / "backend/services/model_influence_governance.py").read_text(
        encoding="utf-8"
    )

    manual_switch = api_source.split(
        "def apply_position_supervisor_template_switch", 1
    )[1].split("def run_position_supervisor_counterfactual", 1)[0]
    auto_switch = autonomous_source.split(
        "def _auto_apply_position_supervisor_template_suggestions", 1
    )[1].split("def _auto_rollback_position_supervisor_template", 1)[0]
    governor_reconcile = governor_source.split("def reconcile_application_effects", 1)[1]

    assert "RuntimeConfigMutationService" not in manual_switch
    assert "UPDATE policy_suggestion" not in manual_switch
    assert "RuntimeConfigMutationService" not in auto_switch
    assert "INSERT INTO learning_application_log" not in auto_switch
    assert "parameter_template_registry" not in governor_reconcile
    assert "sync_runtime_config(" not in governor_reconcile
    assert "prepare_promotion(" in shadow_source
    assert "FactorLifecycleStage.PROMOTION_PREPARED" in shadow_source
    assert "RegistryAdapter.shared().promote" not in shadow_source
    assert "ModelPolicyActivationPlan" in model_source

    for plan_type in (
        ParameterTemplateActivationPlan,
        PositionSupervisorTemplatePlan,
        ModelPolicyActivationPlan,
    ):
        assert "risk_reduction" not in {field.name for field in fields(plan_type)}
