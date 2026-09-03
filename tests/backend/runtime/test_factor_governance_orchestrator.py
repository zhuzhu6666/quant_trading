import hashlib
from dataclasses import replace
from types import SimpleNamespace
import time

import pytest

import backend.runtime.factor_governance_orchestrator as governance_module
from backend.runtime.factor_governance_orchestrator import FactorGovernanceOrchestrator
from alpha.factor_identity import (
    canonical_factor_id,
    factor_definition_fingerprint,
)
from backend.services.runtime_config_overlay import RuntimeConfigOverlayService
from backend.services.learning_application_store import LearningApplicationStore
from config import runtime_config as rc


def _init_state_db(tmp_path) -> None:
    """Seed a bare overlay sqlite with the full runtime schema (lean DDL)."""
    from backend.core.db import STATE_DB_DDL, connect_sqlite

    conn = connect_sqlite(tmp_path / "state.db")
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()


class _AllowRisk:
    def evaluate(self, action, context):
        return SimpleNamespace(
            allowed=True,
            reason="ok",
            severity="info",
            required_mode=context.get("required_mode", "autonomous_governance"),
            audit_payload={"action": action},
            to_dict=lambda: {"allowed": True, "reason": "ok", "action": action},
        )


def _strict_profile(orch):
    profile = orch._governance_profile(rc.shared())
    return replace(profile, name="strict_live", balanced_demo=False)


def _with_candidate_admission(item: dict, *, prepared: bool = False) -> dict:
    artifact_hash = str(item["lifecycle_artifact_hash"])
    validation = {
        "direction": 1,
        "signed_ic_mean": 0.03,
        "pit_passed": True,
        "walk_forward_passed": True,
        "multi_forward_passed": True,
        "cost_test_passed": True,
        "execution_evidence_complete": True,
        "contamination_status": "clean",
        "regime_ids": ["trend"],
    }
    return {
        **item,
        "direction": 1,
        "normalizer": "zscore",
        "lifecycle_generation": 1,
        "lifecycle_config_hash": "c" * 64,
        "runtime_selection_fingerprint": "s" * 64,
        "lifecycle_mutation_id": "mutation-prepared" if prepared else "mutation-shadow",
        "runtime_admission": "projection_acknowledged" if prepared else "blocked",
        "lifecycle_evidence": {
            "candidate_validation": validation,
            "v16": (
                {"command_id": "v16-prepare", "candidate_id": "candidate-1"}
                if prepared
                else {}
            ),
        },
        "loaded_projection": (
            {
                "loaded": True,
                "status": "loaded",
                "generation": 1,
                "artifact_hash": artifact_hash,
            }
            if prepared
            else {}
        ),
    }


def _mature_clean_counts(_factor_id: str) -> dict:
    return {
        "governance_eligible_mature": 20,
        "contaminated_or_ineligible": 0,
        "status": "available",
    }


def test_primes_admission_evidence_cache_with_one_run_projection(monkeypatch):
    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    calls = []

    class _Provider:
        def __init__(self, _db_path):
            pass

        def factor_evidence_summary(self, factor_ids):
            calls.append(list(factor_ids))
            return {
                factor_id: _mature_clean_counts(factor_id)
                for factor_id in factor_ids
            }

    monkeypatch.setattr(
        "research.features.feature_provider.LearningFeatureProvider",
        _Provider,
    )

    factor_ids = [f"factor-{idx}" for idx in range(401)]
    orch._prime_admission_evidence_count_cache(factor_ids)

    assert calls == [factor_ids]
    assert (
        orch._factor_admission_evidence_counts("factor-3")[
            "governance_eligible_mature"
        ]
        == 20
    )


def test_expansion_preflight_reuses_batch_admission_evidence(monkeypatch):
    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    expression = "ts_mean(close, 5)"
    factor_id = "shadow_batch_candidate"
    catalog = [
        _with_candidate_admission({
            "factor_id": factor_id,
            "lifecycle_factor_id": canonical_factor_id(expression),
            "lifecycle_origin": "shadow",
            "lifecycle_status": "SHADOW",
            "lifecycle_expression": expression,
            "lifecycle_definition_fingerprint": factor_definition_fingerprint(expression),
            "lifecycle_artifact_hash": hashlib.sha256(expression.encode()).hexdigest(),
            "source": "shadow",
            "role": "alpha",
            "canary": {"stage": "ACTIVE"},
            "shadow_perf": {
                "oos_bars": 120,
                "n_valid": 100,
                "cumulative_pnl": 1.2,
                "hit_rate": 0.55,
                "max_drawdown": 0.01,
            },
            "health_status": "HEALTHY",
            "health_score": 80.0,
            "health_updated_at": time.time(),
        })
    ]
    primed = []

    def _prime(ids):
        primed.append(list(ids))
        orch._admission_evidence_count_cache.update(
            {item: _mature_clean_counts(item) for item in ids}
        )

    monkeypatch.setattr(orch, "_prime_admission_evidence_count_cache", _prime)
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda _factor_id: False)
    monkeypatch.setattr(orch, "_posterior_expansion_guard", lambda *_args, **_kwargs: "posterior_ok")
    monkeypatch.setattr(
        orch,
        "_current_market_regime_projection",
        lambda: {"regime_id": "", "confidence": 0.0},
    )

    preflight = orch._expansion_preflight(
        catalog,
        cfg=rc.shared(),
        profile=_strict_profile(orch),
        redundancy_report={"group_count": 0, "groups": []},
    )

    assert primed == [[factor_id]]
    assert preflight["reasons"]["shadow_promotion"] == [factor_id]


def test_orchestrator_prepares_eligible_shadow_through_lifecycle_service(monkeypatch, tmp_path):
    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(tmp_path / "state.db")
    prepared = []
    audited = []

    class _Adapter:
        def get_meta(self, name):
            return {"source": "shadow", "description": "ts_mean(close, 5)"}

    class _Lifecycle:
        def __init__(self, _db_path, *, adapter, health_stale_after_sec=None):
            self.adapter = adapter

        def get_state(self, *, factor_name):
            return {}

        def prepare_promotion(self, **kwargs):
            prepared.append(kwargs)
            return {
                "ok": True,
                "status": "committed",
                "lifecycle_stage": "PROMOTION_PREPARED",
                "mutation_id": "mutation-prepare-1",
            }

    monkeypatch.setattr(
        "alpha.registry_adapter.RegistryAdapter.shared",
        classmethod(lambda cls: _Adapter()),
    )
    monkeypatch.setattr(governance_module, "FactorLifecycleService", _Lifecycle)
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda _factor_id: False)
    monkeypatch.setattr(
        orch,
        "_factor_admission_evidence_counts",
        _mature_clean_counts,
    )
    monkeypatch.setattr(
        "backend.services.factor_weight_change.FactorWeightChangeService._replay_admission",
        lambda _self, _decisions: {
            "required": True,
            "allowed": True,
            "max_delta": 0.3,
            "replay_run_id": "replay-test",
            "evidence_grade": "A",
        },
    )
    monkeypatch.setattr(
        orch,
        "_audit_action",
        lambda run, item, action, status, evidence, verdict, **kwargs: audited.append(
            {"factor_id": item["factor_id"], "action": action, "status": status, "evidence": evidence}
        ) or audited[-1],
    )

    catalog = [_with_candidate_admission({
        "factor_id": "shadow_alpha_1",
        "lifecycle_factor_id": canonical_factor_id("ts_mean(close, 5)"),
        "lifecycle_origin": "shadow",
        "lifecycle_status": "SHADOW",
        "lifecycle_expression": "ts_mean(close, 5)",
        "lifecycle_definition_fingerprint": factor_definition_fingerprint("ts_mean(close, 5)"),
        "lifecycle_artifact_hash": hashlib.sha256(b"ts_mean(close, 5)").hexdigest(),
        "source": "shadow",
        "role": "alpha",
        "canary": {"stage": "ACTIVE"},
        "shadow_perf": {
            "oos_bars": 120,
            "n_valid": 100,
            "cumulative_pnl": 1.2,
            "hit_rate": 0.55,
            "max_drawdown": 0.01,
        },
        "health_status": "HEALTHY",
        "health_score": 80.0,
        "health_updated_at": time.time(),
    })]

    actions = orch._promote_shadow_candidates(catalog, {"run_id": "test-run"})

    assert len(prepared) == 1
    assert prepared[0]["name"] == "shadow_alpha_1"
    assert prepared[0]["expression"] == "ts_mean(close, 5)"
    assert actions[0]["status"] == "promotion_prepared"
    assert "shadow_alpha_1" not in rc.shared().factor_portfolio_weights


def test_orchestrator_activates_only_prepared_factor_with_explicit_weight(monkeypatch, tmp_path):
    rc.reset_for_tests()
    rc.patch({"factor_governance_new_factor_weight": 0.3})
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(tmp_path / "state.db")
    activated = []

    class _Adapter:
        pass

    class _Lifecycle:
        def __init__(self, _db_path, *, adapter, health_stale_after_sec=None):
            self.adapter = adapter

        def get_state(self, *, factor_name):
            return {"factor_name": factor_name, "lifecycle_stage": "PROMOTION_PREPARED"}

        def activate(self, **kwargs):
            activated.append(kwargs)
            return {
                "ok": True,
                "status": "committed",
                "lifecycle_stage": "ACTIVE",
                "mutation_id": "mutation-active-1",
            }

    monkeypatch.setattr(
        "alpha.registry_adapter.RegistryAdapter.shared",
        classmethod(lambda cls: _Adapter()),
    )
    monkeypatch.setattr(governance_module, "FactorLifecycleService", _Lifecycle)
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda _factor_id: False)
    monkeypatch.setattr(
        orch,
        "_factor_admission_evidence_counts",
        _mature_clean_counts,
    )
    monkeypatch.setattr(
        orch,
        "_audit_action",
        lambda _run, item, action, status, *_args, **_kwargs: {
            "factor_id": item["factor_id"], "action": action, "status": status
        },
    )

    catalog = [_with_candidate_admission({
        "factor_id": "shadow_alpha_1",
        "lifecycle_factor_id": canonical_factor_id("ts_mean(close, 5)"),
        "lifecycle_origin": "shadow",
        "lifecycle_status": "PROMOTION_PREPARED",
        "runtime_admission": "projection_acknowledged",
        "lifecycle_expression": "ts_mean(close, 5)",
        "lifecycle_definition_fingerprint": factor_definition_fingerprint("ts_mean(close, 5)"),
        "lifecycle_artifact_hash": hashlib.sha256(b"ts_mean(close, 5)").hexdigest(),
        "source": "shadow",
        "role": "alpha",
        "canary": {"stage": "ACTIVE"},
        "shadow_perf": {
            "oos_bars": 120,
            "n_valid": 100,
            "cumulative_pnl": 1.2,
            "hit_rate": 0.55,
            "max_drawdown": 0.01,
        },
        "health_status": "HEALTHY",
        "health_score": 80.0,
        "health_updated_at": time.time(),
    }, prepared=True)]

    actions = orch._promote_shadow_candidates(
        catalog,
        {"run_id": "activation-run"},
        v16_authority={
            "command_id": "v16-command-1",
            "target_agent": "factor_governance",
            "evidence_fingerprint": "evidence-1",
        },
    )

    assert actions[0]["status"] == "applied"
    assert activated[0]["name"] == "shadow_alpha_1"
    assert activated[0]["weight"] == rc.shared().factor_governance_new_factor_weight
    assert activated[0]["v16"].command_id == "v16-command-1"
    assert activated[0]["v16"].evidence_fingerprint == "evidence-1"


def test_orchestrator_does_not_promote_shadow_without_evidence():
    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    catalog = [{
        "factor_id": "weak_shadow",
        "source": "shadow",
        "role": "alpha",
        "shadow_perf": {"oos_bars": 10, "n_valid": 10, "cumulative_pnl": -1.0},
        "health_status": "UNKNOWN",
        "health_score": 0.0,
    }]

    assert orch._promote_shadow_candidates(catalog, {"run_id": "test-run"}) == []


def test_orchestrator_ignores_shadow_without_durable_lifecycle_identity(monkeypatch):
    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda _factor_id: False)
    profile = _strict_profile(orch)
    catalog = [{
        "factor_id": "pca_18",
        "source": "shadow",
        "role": "alpha",
        "canary": {"stage": "ACTIVE"},
        "shadow_perf": {
            "oos_bars": 500,
            "n_valid": 500,
            "cumulative_pnl": 2.0,
            "hit_rate": 0.70,
            "max_drawdown": 0.01,
        },
        "health_status": "HEALTHY",
        "health_score": 90.0,
    }]

    preflight = orch._expansion_preflight(
        catalog,
        cfg=rc.shared(),
        profile=profile,
        redundancy_report={"group_count": 0, "groups": []},
    )

    assert preflight["required"] is False
    assert preflight["reasons"]["shadow_promotion"] == []
    assert orch._promote_shadow_candidates(catalog, {"run_id": "test-run"}) == []


def test_v16_delegates_only_concrete_factor_expansion_preflight(tmp_path):
    from backend.services.v16_brain_orchestrator import (
        V16BrainOrchestratorService,
    )

    service = V16BrainOrchestratorService(db_path=tmp_path / "state.db")
    missing = service.delegate_factor_governance_cycle(
        {
            "snapshot_id": "brain-1",
            "expansion_preflight": {
                "required": False,
                "candidate_count": 0,
            },
        },
        persist=False,
    )
    missing_refs = service.delegate_factor_governance_cycle(
        {
            "snapshot_id": "brain-1",
            "health_cycle_id": "factor_health:1",
            "expansion_preflight": {
                "required": True,
                "candidate_count": 1,
                "reasons": {"shadow_promotion": ["shadow-alpha"]},
            },
        },
        persist=False,
    )

    delegated = service.delegate_factor_governance_cycle(
        {
            "snapshot_id": "brain-1",
            "health_cycle_id": "factor_health:1",
            "expansion_preflight": {
                "required": True,
                "candidate_count": 1,
                "reasons": {"shadow_promotion": ["shadow-alpha"]},
                "candidate_refs": [
                    {
                        "candidate_id": "shadow-alpha",
                        "scope_type": "factor_weight",
                        "scope_key": "shadow-alpha",
                        "action": "promote_factor",
                        "execution_ready": True,
                        "blocker_codes": [],
                    }
                ],
            },
        },
        persist=False,
    )

    assert missing["status"] == "factor_expansion_evidence_not_ready"
    assert missing_refs["status"] == "factor_candidate_contract_not_ready"
    assert delegated["status"] == "delegated"
    command = delegated["command"]
    assert command["decision"] == "delegate"
    assert command["target_agent"] == "factor_governance"
    assert command["action"] == "factor_governance_cycle"
    assert command["candidate_id"] == "shadow-alpha"
    assert command["evidence"]["expansion_preflight"]["candidate_count"] == 1
    assert command["evidence"]["batch_manifest"]["fixed_after_issue"] is True
    assert command["delegation"]["authorization_granularity"] == "run_batch_fixed_manifest"


def test_audit_action_uses_overlay_db_for_snapshot_and_decision(
    monkeypatch, tmp_path
):
    import backend.services.evolution_ledger as evolution_ledger

    rc.reset_for_tests()
    local_db = tmp_path / "state.db"
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(local_db)
    snapshot_calls = []
    decision_calls = []

    def _persist_snapshot(_config, **kwargs):
        snapshot_calls.append(kwargs)
        return {"config_version": 1, "config_hash": "config-hash"}

    def _record_decision(**kwargs):
        decision_calls.append(kwargs)
        return "decision-id"

    monkeypatch.setattr(
        evolution_ledger,
        "persist_runtime_config_snapshot",
        _persist_snapshot,
    )
    monkeypatch.setattr(
        evolution_ledger,
        "record_evolution_decision",
        _record_decision,
    )
    monkeypatch.setattr(orch, "_record_policy_suggestion", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        orch,
        "_record_learning_application",
        lambda *args, **kwargs: None,
    )

    orch._audit_action(
        {"run_id": "local-run"},
        {"factor_id": "factor"},
        "update_weight",
        "blocked_by_risk",
        {},
        _AllowRisk().evaluate("update_weight", {}),
    )

    assert snapshot_calls[0]["db_path"] == local_db
    assert decision_calls[0]["db_path"] == local_db


def test_factor_batch_manifest_must_match_current_preflight():
    from backend.runtime.factor_governance_orchestrator import factor_batch_manifest_verdict
    from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService

    preflight = {
        "required": True,
        "candidate_count": 1,
        "reasons": {"shadow_promotion": ["shadow-alpha"]},
        "candidate_refs": [
            {
                "candidate_id": "shadow-alpha",
                "scope_type": "factor_weight",
                "scope_key": "shadow-alpha",
                "action": "promote_factor",
                "execution_ready": True,
                "blocker_codes": [],
            }
        ],
    }
    delegated = V16BrainOrchestratorService().delegate_factor_governance_cycle(
        {
            "snapshot_id": "brain-1",
            "health_cycle_id": "factor_health:1",
            "expansion_preflight": preflight,
        },
        persist=False,
    )
    authority = {"evidence": delegated["command"]["evidence"]}
    assert factor_batch_manifest_verdict(authority, preflight)["allowed"] is True
    mismatch = {**preflight, "candidate_count": 2}
    assert factor_batch_manifest_verdict(authority, mismatch)["status"] == "factor_batch_manifest_mismatch"
    missing_refs = {key: value for key, value in preflight.items() if key != "candidate_refs"}
    assert factor_batch_manifest_verdict(
        authority,
        missing_refs,
    )["status"] == "factor_candidate_contract_not_ready"


def test_factor_governance_cycle_authorizes_shadow_enrollment_step():
    from backend.services.v16_command_gate import V16CommandGate

    assert V16CommandGate._action_matches(
        {"action": "factor_governance_cycle"},
        "register_shadow_factor",
    )


def test_run_cycle_executes_tightening_before_expansion_freeze(monkeypatch):
    import backend.services.evolution_ledger as evolution_ledger

    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    catalog = [{"factor_id": "weak", "source": "builtin", "role": "alpha"}]
    monkeypatch.setattr(
        evolution_ledger,
        "start_evolution_run",
        lambda **_kwargs: {"run_id": "tightening-before-freeze"},
    )
    monkeypatch.setattr(evolution_ledger, "finish_evolution_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        governance_module, "build_factor_catalog", lambda *_args, **_kwargs: list(catalog)
    )
    monkeypatch.setattr(
        governance_module,
        "persist_factor_catalog_snapshot",
        lambda *_args, **_kwargs: {"snapshot_id": "snapshot-1"},
    )
    monkeypatch.setattr(orch, "_rollback_failed_actions", lambda *_args: [])
    monkeypatch.setattr(orch, "_rollback_canary_regressions", lambda *_args: [])
    monkeypatch.setattr(
        orch,
        "_downweight_weak_alpha",
        lambda *_args, **_kwargs: [{"action": "downweight", "status": "applied"}],
    )
    monkeypatch.setattr(
        orch,
        "_disable_weak_live_alpha",
        lambda *_args, **_kwargs: [{"action": "quarantine", "status": "applied"}],
    )
    monkeypatch.setattr(
        orch,
        "_retire_quarantined_discovered",
        lambda *_args: [{"action": "retire", "status": "applied"}],
    )
    monkeypatch.setattr(orch, "_autonomy_posture", lambda: "frozen")
    monkeypatch.setattr(
        orch,
        "_activate_healthy_builtin_shadow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("expansion must remain frozen")
        ),
    )

    result = orch.run_cycle(trigger_source="pytest")

    assert result["status"] == "observation_only"
    assert [item["action"] for item in result["actions"]] == [
        "downweight",
        "quarantine",
        "retire",
    ]


def test_run_cycle_does_not_rebuild_catalog_for_non_mutating_prior_action(monkeypatch):
    """A prior audit item must not make every later stage rebuild the catalog."""
    import backend.services.evolution_ledger as evolution_ledger

    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    monkeypatch.setattr(
        evolution_ledger,
        "start_evolution_run",
        lambda **_kwargs: {"run_id": "catalog-refresh-local-actions"},
    )
    monkeypatch.setattr(evolution_ledger, "finish_evolution_run", lambda *_args, **_kwargs: None)
    build_calls = []

    def _build_catalog(*_args, **_kwargs):
        build_calls.append(1)
        return []

    monkeypatch.setattr(governance_module, "build_factor_catalog", _build_catalog)
    monkeypatch.setattr(
        governance_module,
        "persist_factor_catalog_snapshot",
        lambda *_args, **_kwargs: {"snapshot_id": "snapshot-refresh"},
    )
    monkeypatch.setattr(
        orch,
        "_rollback_failed_actions",
        lambda *_args: [{"action": "rollback_factor_action", "status": "superseded"}],
    )
    monkeypatch.setattr(orch, "_rollback_canary_regressions", lambda *_args: [])
    monkeypatch.setattr(orch, "_demote_invalid_candidate_evidence", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(orch, "_downweight_weak_alpha", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(orch, "_disable_weak_live_alpha", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(orch, "_retire_quarantined_discovered", lambda *_args: [])
    monkeypatch.setattr(orch, "_autonomy_posture", lambda: "frozen")

    result = orch.run_cycle(trigger_source="pytest")

    assert result["status"] == "observation_only"
    assert len(build_calls) == 1


def test_run_cycle_does_not_claim_v16_without_expansion_work(monkeypatch):
    import backend.services.evolution_ledger as evolution_ledger
    import backend.services.v16_command_gate as v16_gate

    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    monkeypatch.setattr(
        evolution_ledger,
        "start_evolution_run",
        lambda **_kwargs: {"run_id": "idle-no-expansion"},
    )
    finished = []
    monkeypatch.setattr(
        evolution_ledger,
        "finish_evolution_run",
        lambda *args, **kwargs: finished.append((args, kwargs)),
    )
    monkeypatch.setattr(
        governance_module, "build_factor_catalog", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        governance_module,
        "persist_factor_catalog_snapshot",
        lambda *_args, **_kwargs: {"snapshot_id": "snapshot-idle"},
    )
    monkeypatch.setattr(orch, "_rollback_failed_actions", lambda *_args: [])
    monkeypatch.setattr(orch, "_rollback_canary_regressions", lambda *_args: [])
    monkeypatch.setattr(orch, "_downweight_weak_alpha", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(orch, "_disable_weak_live_alpha", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(orch, "_retire_quarantined_discovered", lambda *_args: [])
    monkeypatch.setattr(orch, "_autonomy_posture", lambda: "ready")
    monkeypatch.setattr(
        governance_module.runtime_config,
        "autonomy_expansion_freeze_applies",
        lambda _cfg: False,
    )
    monkeypatch.setattr(
        governance_module.RedundancyDetector,
        "build_report",
        lambda *_args, **_kwargs: {"group_count": 0, "groups": []},
    )
    monkeypatch.setattr(orch, "_apply_parameter_template_actions", lambda *_args: [])
    monkeypatch.setattr(
        v16_gate.V16CommandGate,
        "authorize",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("idle cycle must not claim V16 authority")
        ),
    )

    result = orch.run_cycle(trigger_source="pytest")

    assert result["status"] == "idle_no_expansion_action"
    assert result["reason"] == "no_factor_expansion_actionable"
    assert result["expansion_preflight"]["required"] is False
    assert finished[-1][1]["status"] == "completed"


def test_expansion_preflight_finds_fresh_builtin_activation(monkeypatch):
    rc.reset_for_tests()
    rc.patch(
        {
            "factor_signal_config": {
                "fresh_shadow": {
                    "enabled": True,
                    "role": "alpha",
                    "autonomous_activation": True,
                }
            },
            "factor_portfolio_weights": {"fresh_shadow": 0.0},
            "factor_governance_builtin_activation_weight": 0.1,
        }
    )
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda _factor_id: False)
    profile = replace(
        _strict_profile(orch),
        builtin_activation_min_health_score=60.0,
        builtin_activation_min_n_obs=500,
        health_max_age_seconds=300.0,
    )

    result = orch._expansion_preflight(
        [
            {
                "factor_id": "fresh_shadow",
                "source": "builtin",
                "role": "alpha",
                "enabled": True,
                "lifecycle_status": "SHADOW",
                "health_status": "HEALTHY",
                "health_score": 80.0,
                "health_n_obs": 2000,
                "health_updated_at": time.time(),
                "factor_governance_shadow": {},
            }
        ],
        cfg=rc.shared(),
        profile=profile,
        redundancy_report={"group_count": 0, "groups": []},
    )

    assert result["required"] is True
    assert result["reasons"]["builtin_activation"] == ["fresh_shadow"]


def test_expansion_preflight_freezes_one_concrete_redundancy_mutation():
    """Redundancy groups are one config mutation, not one V16 candidate per group."""
    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    report = {
        "group_count": 1,
        "groups": [
            {
                "group_id": "redundancy:auto:rsi_14",
                "leader": "rsi_14",
                "members": ["rsi_14", "stoch_k"],
                "correlations": {"rsi_14:stoch_k": 0.95},
                "sample_count": 500,
            }
        ],
    }

    result = orch._expansion_preflight(
        [],
        cfg=rc.shared(),
        profile=_strict_profile(orch),
        redundancy_report=report,
    )

    assert result["required"] is True
    assert result["candidate_count"] == 1
    candidate = result["candidate_refs"][0]
    assert candidate["candidate_id"] == "redundancy"
    assert candidate["action"] == "update_redundancy_groups"
    assert candidate["scope_key"] == "alpha_weight_policy"
    assert candidate["execution_ready"] is True
    assert candidate["evidence_refs"]["groups"] == report["groups"]
    assert candidate["evidence_refs"]["patch_fingerprint"]


def test_redundancy_preflight_satisfies_v16_fixed_manifest_contract(tmp_path):
    """A redundancy-only change must be executable by the strict V16 gate."""
    from backend.runtime.factor_governance_orchestrator import factor_batch_manifest_verdict
    from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService

    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    preflight = orch._expansion_preflight(
        [],
        cfg=rc.shared(),
        profile=_strict_profile(orch),
        redundancy_report={
            "group_count": 1,
            "groups": [
                {
                    "group_id": "redundancy:auto:rsi_14",
                    "leader": "rsi_14",
                    "members": ["rsi_14", "stoch_k"],
                }
            ],
        },
    )
    delegated = V16BrainOrchestratorService(db_path=tmp_path / "state.db").delegate_factor_governance_cycle(
        {
            "snapshot_id": "catalog-1",
            "health_cycle_id": "health-1",
            "expansion_preflight": preflight,
        },
        persist=False,
    )

    assert delegated["status"] == "delegated"
    assert delegated["command"]["candidate_id"] == "redundancy"
    assert factor_batch_manifest_verdict(
        {"evidence": delegated["command"]["evidence"]},
        preflight,
    )["allowed"] is True


def test_orchestrator_requires_active_canary_before_shadow_promotion():
    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    catalog = [{
        "factor_id": "premature_shadow",
        "source": "shadow",
        "role": "alpha",
        "canary": {"stage": "CANARY_50"},
        "shadow_perf": {
            "oos_bars": 500, "n_valid": 500, "cumulative_pnl": 2.0,
            "hit_rate": 0.70, "max_drawdown": 0.01,
        },
        "health_status": "HEALTHY",
        "health_score": 90.0,
    }]

    assert orch._promote_shadow_candidates(catalog, {"run_id": "test-run"}) == []


@pytest.mark.parametrize(
    ("health_status", "health_score", "health_age", "blocker"),
    [
        # UNKNOWN is deliberately absent: an UNKNOWN row means the factor was
        # never health-monitored, and at canary PROBATION/ACTIVE the ladder
        # substitutes for that absent evidence (see
        # test_promotion_evidence_waives_absent_health_for_canary_ladder_top).
        ("DECAYING", 90.0, 0.0, "factor_health_decaying"),
        ("HEALTHY", 90.0, 86_400.0, "factor_health_stale"),
        ("WATCH", 39.0, 0.0, "factor_health_watch_below_threshold"),
    ],
)
def test_promotion_evidence_fails_closed_for_unhealthy_or_stale_facts(
    health_status,
    health_score,
    health_age,
    blocker,
):
    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    item = {
        "health_status": health_status,
        "health_score": health_score,
        "health_updated_at": time.time() - health_age,
        "canary": {"stage": "ACTIVE"},
        "shadow_perf": {
            "oos_bars": 500,
            "n_valid": 500,
            "cumulative_pnl": 2.0,
            "hit_rate": 0.70,
            "max_drawdown": 0.01,
        },
    }

    evidence = orch._promotion_evidence(item, rc.shared())

    assert evidence["eligible"] is False
    assert blocker in evidence["blocker_codes"]


def test_promotion_evidence_accepts_fresh_watch_at_existing_threshold():
    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    expression = "ts_mean(close, 5)"
    item = _with_candidate_admission({
        "factor_id": "watch_candidate",
        "lifecycle_factor_id": canonical_factor_id(expression),
        "lifecycle_origin": "shadow",
        "lifecycle_status": "SHADOW",
        "lifecycle_expression": expression,
        "lifecycle_definition_fingerprint": factor_definition_fingerprint(expression),
        "lifecycle_artifact_hash": hashlib.sha256(expression.encode()).hexdigest(),
        "health_status": "WATCH",
        "health_score": rc.shared().factor_health_watch_threshold,
        "health_updated_at": time.time(),
        "canary": {"stage": "ACTIVE"},
        "shadow_perf": {
            "oos_bars": 500,
            "n_valid": 500,
            "cumulative_pnl": 2.0,
            "hit_rate": 0.70,
            "max_drawdown": 0.01,
        },
    })
    orch._factor_admission_evidence_counts = _mature_clean_counts

    evidence = orch._promotion_evidence(item, rc.shared())

    assert evidence["eligible"] is True
    assert evidence["blocker_codes"] == []


def test_orchestrator_downweights_from_model_weakness_evidence(monkeypatch, tmp_path):
    rc.reset_for_tests()
    rc.patch({
        "factor_signal_config": {
            "model_weak_factor": {"role": "alpha", "enabled": True, "tags": ["技术"]},
        },
        "factor_portfolio_weights": {"model_weak_factor": 0.3},
        "factor_governance_model_min_samples": 3,
        "factor_governance_model_weakness_threshold": 0.65,
        "awe_max_single_change": 0.15,
    })
    _init_state_db(tmp_path)
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(tmp_path / "state.db")
    audited = []
    monkeypatch.setattr(
        orch,
        "_audit_action",
        lambda run, item, action, status, evidence, verdict, **kwargs: audited.append(
            {"factor_id": item["factor_id"], "action": action, "status": status, "evidence": evidence}
        ) or audited[-1],
    )
    catalog = [{
        "factor_id": "model_weak_factor",
        "source": "builtin",
        "role": "alpha",
        "enabled": True,
        "eligible_for_live": True,
        "used_in_score": True,
        "weight": 0.3,
        "health_score": 0.0,
        "health_status": "UNKNOWN",
        "factor_governance_shadow": {
            "sample_count": 20,
            "weak_sample_count": 20,
            "avg_weakness_score": 0.78,
            "weakness_score": 0.8,
            "model_type": "factor_governance_lightgbm",
            "latest_inference_id": "fg_weak_1",
            "result": {
                "promotion_gate": {"passed": True, "reason": "promotion_gate_passed"},
                "mutation_eligible": True,
                "artifact_sha256": "test-artifact",
                "factor_generation": "runtime_bounded_v1",
                "lineage_hash": "test-lineage",
            },
        },
    }]

    actions = orch._downweight_weak_alpha(catalog, {"run_id": "test-run"})

    assert actions[0]["status"] == "applied"
    assert rc.shared().factor_portfolio_weights["model_weak_factor"] == 0.255
    evidence = audited[0]["evidence"]
    assert evidence["health_weak"] is False
    assert evidence["model_governance"]["weak_for_downweight"] is True


def test_orchestrator_blocks_model_mutation_before_quality_gate_or_coverage(monkeypatch, tmp_path):
    rc.reset_for_tests()
    rc.patch({
        "factor_signal_config": {
            "model_weak_factor": {"role": "alpha", "enabled": True, "tags": ["技术"]},
        },
        "factor_portfolio_weights": {"model_weak_factor": 0.3},
        "factor_governance_model_min_samples": 3,
        "factor_governance_model_min_factor_samples": 20,
        "factor_governance_model_weakness_threshold": 0.65,
    })
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(tmp_path / "state.db")
    item = {
        "factor_id": "model_weak_factor",
        "source": "builtin",
        "role": "alpha",
        "enabled": True,
        "eligible_for_live": True,
        "used_in_score": True,
        "weight": 0.3,
        "health_score": 0.0,
        "health_status": "UNKNOWN",
        "factor_governance_shadow": {
            "sample_count": 19,
            "weak_sample_count": 19,
            "avg_weakness_score": 0.95,
            "weakness_score": 0.95,
            "model_type": "factor_governance_lightgbm",
            "result": {
                "promotion_gate": {
                    "passed": False,
                    "reason": "promotion_gate_failed",
                },
                "mutation_eligible": False,
                "artifact_sha256": "test-artifact",
                "factor_generation": "runtime_bounded_v1",
                "lineage_hash": "test-lineage",
            },
        },
    }

    evidence = orch._model_governance_evidence(item, rc.shared())
    actions = orch._downweight_weak_alpha([item], {"run_id": "test-run"})

    assert evidence["mutation_eligible"] is False
    assert evidence["weak_for_downweight"] is False
    assert actions == []
    assert rc.shared().factor_portfolio_weights["model_weak_factor"] == 0.3


def test_orchestrator_defers_factor_mutation_while_effect_is_pending(monkeypatch):
    rc.reset_for_tests()
    rc.patch({"factor_portfolio_weights": {"model_weak_factor": 0.3}})
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda factor_id: factor_id == "model_weak_factor")
    catalog = [{
        "factor_id": "model_weak_factor",
        "source": "builtin",
        "role": "alpha",
        "used_in_score": True,
        "weight": 0.3,
        "health_score": 10.0,
        "health_status": "DECAYING",
        "factor_governance_shadow": {},
    }]

    assert orch._downweight_weak_alpha(catalog, {"run_id": "test-run"}) == []
    assert rc.shared().factor_portfolio_weights["model_weak_factor"] == 0.3


def test_orchestrator_does_not_audit_decision_policy_noop(monkeypatch):
    rc.reset_for_tests()
    rc.patch({"factor_portfolio_weights": {"minimal_factor": 0.01}})
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    applied = []
    audited = []

    class _NoopDecision:
        old_weight = 0.01
        new_weight = 0.01

        @staticmethod
        def to_api():
            return {"old_weight": 0.01, "new_weight": 0.01}

    class _NoopPolicy:
        def __init__(self, **kwargs):
            pass

        def fast_decide(self, **kwargs):
            return {"minimal_factor": _NoopDecision()}

        @staticmethod
        def to_weights(decisions):
            return {name: decision.new_weight for name, decision in decisions.items()}

    monkeypatch.setattr("backend.runtime.factor_governance_orchestrator.DecisionPolicy", _NoopPolicy)
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda factor_id: False)
    monkeypatch.setattr(orch, "_apply_runtime_patch", lambda *args, **kwargs: applied.append((args, kwargs)))
    monkeypatch.setattr(orch, "_audit_action", lambda *args, **kwargs: audited.append((args, kwargs)))

    catalog = [{
        "factor_id": "minimal_factor",
        "source": "builtin",
        "role": "alpha",
        "used_in_score": True,
        "weight": 0.01,
        "health_score": 10.0,
        "health_status": "DECAYING",
        "factor_governance_shadow": {},
    }]

    assert orch._downweight_weak_alpha(catalog, {"run_id": "test-run"}) == []
    assert applied == []
    assert audited == []


def test_pending_effect_gate_releases_factor_after_final_effect(tmp_path):
    _init_state_db(tmp_path)
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(tmp_path / "state.db")
    store = LearningApplicationStore(tmp_path / "state.db")
    fid = "rsi_14"

    # No ledger rows -> no pending experiment.
    assert orch._factor_has_pending_effect(fid) is False

    # Terminal effect (effective) releases the scope even while the
    # application log keeps an active "applied" status.
    app = store.prepare_application(
        scope_type="factor", scope_key=fid, action="update_weight", status="applied"
    )
    store.write_effect(
        application_id=app, scope_key=fid, scope_type="factor", status="effective",
        observed_trade_count=5, delta_avg_reward=0.2,
    )
    assert orch._factor_has_pending_effect(fid) is False

    # An active/supervising effect keeps the factor pending until it matures.
    app2 = store.prepare_application(
        scope_type="factor", scope_key=fid, action="update_weight", status="applied"
    )
    store.write_effect(
        application_id=app2, scope_key=fid, scope_type="factor", status="observing",
        observed_trade_count=1, delta_avg_reward=0.0,
    )
    assert orch._factor_has_pending_effect(fid) is True


def test_scoped_factor_rollback_preserves_unrelated_runtime_config():
    patch = FactorGovernanceOrchestrator._scoped_factor_rollback_patch(
        "rsi_14",
        {
            "factor_signal_config": {"rsi_14": {"enabled": True}},
            "factor_portfolio_weights": {"rsi_14": 0.4},
        },
        {
            "factor_signal_config": {"rsi_14": {"enabled": False}, "ema_slope": {"enabled": False}},
            "factor_portfolio_weights": {"rsi_14": 0.1, "ema_slope": 0.8},
        },
    )

    assert patch["factor_signal_config"]["rsi_14"]["enabled"] is True
    assert "ema_slope" not in patch["factor_signal_config"]
    assert patch["factor_portfolio_weights"] == {"rsi_14": 0.4}


def test_discovered_disable_uses_lifecycle_coordinator_in_enforce(
    monkeypatch, tmp_path
):
    import backend.services.governance_control_plans as control_plans

    rc.reset_for_tests()
    rc.patch({
        "factor_signal_config": {
            "weak_discovered": {
                "enabled": True,
                "lifecycle_status": "ACTIVE",
            }
        },
        "factor_portfolio_weights": {"weak_discovered": 0.2},
    })
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(tmp_path / "state.db")
    quarantined = []
    audits = []

    class _Adapter:
        def get_meta(self, name):
            assert name == "weak_discovered"
            return {
                "source": "discovered",
                "description": "rank(close)",
                "artifact_hash": "a" * 64,
            }

    class _Lifecycle:
        def __init__(self, _db_path, *, adapter):
            assert isinstance(adapter, _Adapter)

        def quarantine(self, **kwargs):
            quarantined.append(kwargs)
            return {
                "ok": True,
                "status": "committed",
                "mutation_id": "mutation-quarantine",
                "lifecycle_stage": "QUARANTINED",
            }

    monkeypatch.setattr(control_plans, "governance_coordinator_mode", lambda: "enforce")
    monkeypatch.setattr(
        governance_module.RegistryAdapter,
        "shared",
        classmethod(lambda cls: _Adapter()),
    )
    monkeypatch.setattr(governance_module, "FactorLifecycleService", _Lifecycle)
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda _factor_id: False)
    monkeypatch.setattr(
        orch,
        "_apply_runtime_patch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct runtime lifecycle patch used")
        ),
    )
    monkeypatch.setattr(
        orch,
        "_audit_action",
        lambda _run, _item, action, status, *_args, **kwargs: audits.append(
            {"action": action, "status": status, "result": kwargs.get("result")}
        ) or audits[-1],
    )

    actions = orch._disable_weak_live_alpha(
        [{
            "factor_id": "weak_discovered",
            "source": "discovered",
            "role": "alpha",
            "eligible_for_live": True,
            "health_score": 10.0,
            "health_status": "DECAYING",
        }],
        {"run_id": "disable-run"},
        profile=_strict_profile(orch),
    )

    assert len(quarantined) == 1
    assert quarantined[0]["name"] == "weak_discovered"
    assert actions[0]["status"] == "applied"
    assert audits[0]["result"]["mutation_id"] == "mutation-quarantine"


def test_failed_disable_is_never_audited_as_applied(monkeypatch, tmp_path):
    rc.reset_for_tests()
    rc.patch({
        "factor_signal_config": {"weak_builtin": {"enabled": True}},
        "factor_portfolio_weights": {"weak_builtin": 0.2},
    })
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(tmp_path / "state.db")
    audits = []
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda _factor_id: False)
    monkeypatch.setattr(
        orch,
        "_apply_runtime_patch",
        lambda *_args, **_kwargs: {
            "ok": False,
            "status": "blocked_v16_command_required",
        },
    )
    monkeypatch.setattr(
        orch,
        "_audit_action",
        lambda _run, _item, action, status, *_args, **kwargs: audits.append(
            {"action": action, "status": status, "result": kwargs.get("result")}
        ) or audits[-1],
    )

    actions = orch._disable_weak_live_alpha(
        [{
            "factor_id": "weak_builtin",
            "source": "builtin",
            "role": "alpha",
            "eligible_for_live": True,
            "health_score": 10.0,
            "health_status": "DECAYING",
        }],
        {"run_id": "blocked-disable"},
        profile=_strict_profile(orch),
    )

    assert actions[0]["status"] == "blocked_by_evidence"
    assert audits[0]["result"]["durably_committed"] is False


def test_coordinator_audit_does_not_create_second_executable_fact(
    monkeypatch, tmp_path
):
    import backend.services.governance_control_plans as control_plans

    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(tmp_path / "state.db")
    monkeypatch.setattr(control_plans, "governance_coordinator_mode", lambda: "enforce")
    monkeypatch.setattr(
        governance_module,
        "get_state_pg_conn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("post-commit executable audit write attempted")
        ),
    )

    suggestion_id = orch._record_policy_suggestion(
        "factor",
        "promote_factor",
        "applied",
        {},
        "decision-1",
    )
    orch._record_learning_application(
        "factor",
        "promote_factor",
        "applied",
        suggestion_id,
        {},
        {},
        {"mutation_id": "mutation-1"},
    )

    assert suggestion_id == ""


# ── 批次 C: 降权条件化 (当前 regime 下弱才降权, 否则 regime_mismatch 不动权重) ──────────

def test_regime_mismatch_verdict_marks_when_global_weak_but_current_regime_fit_good():
    """全局模型弱点高、但当前 regime 条件适配好时可暂停降权 (声明确认工具)."""
    verdict = FactorGovernanceOrchestrator._regime_mismatch_verdict(
        True,
        current_regime_id="trend=strong|volatility=high",
        regime_fit_score=0.82,
        regime_fit_ok_threshold=0.5,
    )

    assert verdict["regime_mismatch"] is True
    assert verdict["reason"] == "current_regime_fit_ok"
    assert verdict["current_regime_id"] == "trend=strong|volatility=high"


def test_regime_mismatch_verdict_allows_downweight_when_current_regime_weak_too():
    """当前 regime 下适配同样弱时, 不标记 mismatch, 保留降权路径."""
    verdict = FactorGovernanceOrchestrator._regime_mismatch_verdict(
        True,
        current_regime_id="trend=weak|volatility=low",
        regime_fit_score=0.15,
        regime_fit_ok_threshold=0.5,
    )

    assert verdict["regime_mismatch"] is False
    assert verdict["reason"] == "current_regime_weak_too"


def test_regime_mismatch_verdict_is_fail_safe_when_no_regime_evidence():
    """无当前 regime 投影或无 regime 适配证据时, 不得侵蚀安全: 沿用全局降权路径."""
    no_regime = FactorGovernanceOrchestrator._regime_mismatch_verdict(
        True,
        current_regime_id="",
        regime_fit_score=0.9,
    )
    assert no_regime["regime_mismatch"] is False
    assert no_regime["reason"] == "no_current_regime"

    no_fit = FactorGovernanceOrchestrator._regime_mismatch_verdict(
        True,
        current_regime_id="trend=strong|volatility=high",
        regime_fit_score=None,
    )
    assert no_fit["regime_mismatch"] is False
    assert no_fit["reason"] == "no_regime_fit_evidence"


def test_regime_mismatch_verdict_ignores_when_not_globally_weak():
    """因子全局并不弱时, 直接放行降权路径不做 mismatch 判定."""
    verdict = FactorGovernanceOrchestrator._regime_mismatch_verdict(
        False,
        current_regime_id="trend=strong|volatility=high",
        regime_fit_score=0.1,
    )

    assert verdict["regime_mismatch"] is False
    assert verdict["reason"] == "not_globally_weak"


# ── 批次 D: 条件化恢复 (当前 regime 下弱则不恢复; 与后验闸正交) ──────────────

def test_regime_suitable_for_restore_permits_when_fit_good():
    """当前 regime 适配好 -> 允许恢复."""
    verdict = FactorGovernanceOrchestrator._regime_suitable_for_restore(
        current_regime_id="trend=strong|volatility=high",
        regime_fit_score=0.9,
        regime_fit_ok_threshold=0.5,
    )
    assert verdict["suitable"] is True
    assert verdict["reason"] == "current_regime_fit_ok"


def test_regime_suitable_for_restore_blocks_when_current_regime_weak():
    """当前 regime 下适配弱 -> 不恢复 (条件化恢复核心)."""
    verdict = FactorGovernanceOrchestrator._regime_suitable_for_restore(
        current_regime_id="trend=weak|volatility=low",
        regime_fit_score=0.1,
        regime_fit_ok_threshold=0.5,
    )
    assert verdict["suitable"] is False
    assert verdict["reason"] == "current_regime_weak_too"


def test_regime_suitable_for_restore_fail_open_when_no_evidence():
    """无当前 regime 投影或无数值证据 -> fail-open 允许恢复 (不误伤既有恢复路径)."""
    no_regime = FactorGovernanceOrchestrator._regime_suitable_for_restore(
        current_regime_id="",
        regime_fit_score=0.9,
    )
    assert no_regime["suitable"] is True
    assert no_regime["reason"] == "no_current_regime"

    no_fit = FactorGovernanceOrchestrator._regime_suitable_for_restore(
        current_regime_id="trend=strong|volatility=high",
        regime_fit_score=None,
    )
    assert no_fit["suitable"] is True
    assert no_fit["reason"] == "no_regime_fit_evidence"


def test_shadow_regime_fit_score_prefers_same_regime_conditional():
    """批次F: 条件化闸优先消费因子×regime 条件胜率 (same_regime_positive_rate),
    而非交易级 fit_score —— 因子级特征能区分因子, 交易级不能."""
    item = {
        "factor_governance_shadow": {
            "payload": {
                "features": {
                    "same_regime_positive_rate": 0.8,
                    "current_regime_fit_score": 0.2,
                }
            }
        }
    }
    score = FactorGovernanceOrchestrator._shadow_regime_fit_score(item)
    assert score == 0.8


def test_shadow_regime_fit_score_falls_back_to_trade_level_fit():
    """旧 schema (v5.0 及之前) 无 same_regime_* 特征 -> 回退交易级 fit_score."""
    item = {
        "factor_governance_shadow": {
            "payload": {
                "features": {
                    "current_regime_fit_score": 0.32,
                }
            }
        }
    }
    score = FactorGovernanceOrchestrator._shadow_regime_fit_score(item)
    assert score == 0.32


def test_shadow_regime_fit_score_none_when_no_evidence():
    """无任何 regime 证据 -> None (调用方 fail-safe/fail-open)."""
    assert FactorGovernanceOrchestrator._shadow_regime_fit_score({}) is None
    item = {"factor_governance_shadow": {"payload": {"features": {}}}}
    assert FactorGovernanceOrchestrator._shadow_regime_fit_score(item) is None
    item = {"factor_governance_shadow": {"payload": {"features": {"current_regime_fit_score": "nan"}}}}
    assert FactorGovernanceOrchestrator._shadow_regime_fit_score(item) is None


def test_downweight_skipped_when_current_regime_fit_good_for_weak_factor(monkeypatch, tmp_path):
    """批C接入: 模型判弱但当前 regime 适配好 -> regime_mismatch, 权重不变."""
    rc.reset_for_tests()
    rc.patch({
        "factor_signal_config": {
            "regime_ok_factor": {"role": "alpha", "enabled": True, "tags": ["技术"]},
        },
        "factor_portfolio_weights": {"regime_ok_factor": 0.3},
        "factor_governance_model_min_samples": 3,
        "factor_governance_model_weakness_threshold": 0.65,
        "awe_max_single_change": 0.15,
    })
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(tmp_path / "state.db")

    # setup local sqlite state with experience_memory carrying current regime
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(tmp_path / "state.db")
    conn.executescript(
        """
        CREATE TABLE experience_memory (
            regime_id TEXT,
            created_at REAL,
            trade_id TEXT
        );
        INSERT INTO experience_memory (regime_id, created_at, trade_id)
        VALUES ('trend=strong|volatility=high', 1600000.0, 't1'),
               ('trend=strong|volatility=high', 1600010.0, 't2'),
               ('trend=strong|volatility=high', 1600020.0, 't3');
        """
    )
    conn.commit()
    conn.close()

    catalog = [{
        "factor_id": "regime_ok_factor",
        "source": "builtin",
        "role": "alpha",
        "enabled": True,
        "eligible_for_live": True,
        "used_in_score": True,
        "weight": 0.3,
        "health_score": 0.0,
        "health_status": "UNKNOWN",
        "factor_governance_shadow": {
            "sample_count": 20,
            "weak_sample_count": 20,
            "avg_weakness_score": 0.78,
            "weakness_score": 0.8,
            "model_type": "factor_governance_lightgbm",
            "latest_inference_id": "fg_weak_2",
            "result": {
                "promotion_gate": {"passed": True, "reason": "promotion_gate_passed"},
                "mutation_eligible": True,
                "artifact_sha256": "test-artifact",
                "factor_generation": "runtime_bounded_v1",
                "lineage_hash": "test-lineage",
            },
            "payload": {"features": {"current_regime_fit_score": 0.85}},
        },
    }]

    actions = orch._downweight_weak_alpha(catalog, {"run_id": "test-run"})

    # no downweight patch applied -> weight unchanged
    assert rc.shared().factor_portfolio_weights.get("regime_ok_factor", 0.3) == 0.3
    assert not any(a["status"] == "applied" for a in actions)


def test_downweight_applied_when_current_regime_weak_too(monkeypatch, tmp_path):
    """批C接入: 当前 regime 适配也弱 -> 保留降权路径."""
    rc.reset_for_tests()
    rc.patch({
        "factor_signal_config": {
            "weak_in_current_regime": {"role": "alpha", "enabled": True, "tags": ["技术"]},
        },
        "factor_portfolio_weights": {"weak_in_current_regime": 0.3},
        "factor_governance_model_min_samples": 3,
        "factor_governance_model_weakness_threshold": 0.65,
        "awe_max_single_change": 0.15,
    })
    _init_state_db(tmp_path)
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(tmp_path / "state.db")
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(tmp_path / "state.db")
    for _ts in (1600000.0, 1600010.0, 1600020.0):
        conn.execute(
            "INSERT INTO experience_memory (regime_id, created_at, trade_id) VALUES (?, ?, ?)",
            ("trend=weak|volatility=low", _ts, "t1"),
        )
    conn.commit()
    conn.close()

    catalog = [{
        "factor_id": "weak_in_current_regime",
        "source": "builtin",
        "role": "alpha",
        "enabled": True,
        "eligible_for_live": True,
        "used_in_score": True,
        "weight": 0.3,
        "health_score": 0.0,
        "health_status": "UNKNOWN",
        "factor_governance_shadow": {
            "sample_count": 20,
            "weak_sample_count": 20,
            "avg_weakness_score": 0.78,
            "weakness_score": 0.8,
            "model_type": "factor_governance_lightgbm",
            "latest_inference_id": "fg_weak_3",
            "result": {
                "promotion_gate": {"passed": True, "reason": "promotion_gate_passed"},
                "mutation_eligible": True,
                "artifact_sha256": "test-artifact",
                "factor_generation": "runtime_bounded_v1",
                "lineage_hash": "test-lineage",
            },
            "payload": {"features": {"current_regime_fit_score": 0.1}},
        },
    }]

    actions = orch._downweight_weak_alpha(catalog, {"run_id": "test-run"})

    assert any(a["status"] == "applied" for a in actions)
    assert rc.shared().factor_portfolio_weights.get("weak_in_current_regime") == 0.255


def test_expansion_preflight_blocks_restore_when_current_regime_weak(monkeypatch, tmp_path):
    """批D接入: 当前 regime 下适配弱的 zero-weight 恢复候选不进 preflight 候选集."""
    rc.reset_for_tests()
    rc.patch({
        "factor_signal_config": {
            "regime_weak_restore_cand": {
                "role": "alpha", "enabled": True, "tags": ["技术"],
                "autonomous_activation": True,
            },
        },
        "factor_portfolio_weights": {"regime_weak_restore_cand": 0.0},
        "factor_governance_builtin_activation_enabled": False,
        "factor_governance_auto_restore_enabled": True,
    })
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(tmp_path / "state.db")

    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(tmp_path / "state.db")
    conn.executescript(
        """
        CREATE TABLE experience_memory (
            regime_id TEXT, created_at REAL, trade_id TEXT
        );
        INSERT INTO experience_memory (regime_id, created_at, trade_id)
        VALUES ('trend=weak|volatility=low', 1600000.0, 't1'),
               ('trend=weak|volatility=low', 1600010.0, 't2'),
               ('trend=weak|volatility=low', 1600020.0, 't3');
        """
    )
    conn.commit()
    conn.close()

    profile = replace(
        orch._governance_profile(rc.shared()),
        name="balanced_demo",
        balanced_demo=True,
        restore_min_health_score=0.0,
        restore_min_n_obs=0,
        restore_model_min_samples=0,
        restore_max_weakness=1.0,
        restore_cooldown_seconds=0.0,
        health_max_age_seconds=1e18,
        min_live_weight=0.05,
    )
    now = time.time()
    catalog = [{
        "factor_id": "regime_weak_restore_cand",
        "source": "builtin",
        "role": "alpha",
        "enabled": True,
        "eligible_for_live": True,
        "used_in_score": True,
        "weight": 0.0,
        "lifecycle_origin": "builtin",
        "lifecycle_status": "ACTIVE",
        "health_updated_at": now,
        "health_status": "HEALTHY",
        "health_score": 85.0,
        "health_n_obs": 40,
        "factor_governance_shadow": {
            "sample_count": 20,
            "weak_sample_count": 20,
            "avg_weakness_score": 0.78,
            "weakness_score": 0.8,
            "model_type": "factor_governance_lightgbm",
            "latest_inference_id": "fg_restore_1",
            "result": {
                "promotion_gate": {"passed": True, "reason": "promotion_gate_passed"},
                "mutation_eligible": True,
                "artifact_sha256": "test-artifact",
                "factor_generation": "runtime_bounded_v1",
                "lineage_hash": "test-lineage",
            },
            "payload": {"features": {"current_regime_fit_score": 0.1}},
        },
    }]

    preflight = orch._expansion_preflight(
        catalog,
        cfg=rc.shared(),
        profile=profile,
        redundancy_report={"group_count": 0, "groups": []},
    )

    assert "regime_weak_restore_cand" not in preflight["reasons"]["active_zero_weight_restore"]
    assert "regime_weak_restore_cand" not in preflight["reasons"]["builtin_restore"]


def test_rollback_missing_payload_adjudicated_once(tmp_path):
    """A rollback candidate whose decision payload is gone is adjudicated
    exactly once: the first scan records one superseded audit plus a one-way
    runtime_kv marker; later scans must not re-enter it (no replayed audit,
    rollback budget freed) and the application status stays untouched so the
    negative delta remains visible to the posterior expansion gate."""
    from backend.services.runtime_kv_store import RuntimeKVStore

    rc.reset_for_tests()
    _init_state_db(tmp_path)
    db = tmp_path / "state.db"
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(db)
    store = LearningApplicationStore(str(db))
    application_id = store.prepare_application(
        scope_type="factor",
        scope_key="pin_bar",
        action="update_weight",
        status="applied",
        run_id="run-1",
        source="test",
    )
    store.write_effect(
        application_id=application_id,
        scope_key="pin_bar",
        scope_type="factor",
        action="update_weight",
        status="applied",
        observed_trade_count=5,
        delta_avg_reward=-0.5,
    )

    first = orch._rollback_failed_actions({"run_id": "gov-run-1"})
    assert [item["status"] for item in first] == ["superseded"]

    marker = RuntimeKVStore(str(db)).get(
        f"factor_governance.rollback.adjudicated.{application_id}"
    )
    assert marker["reason"] == "missing_rollback_config"

    second = orch._rollback_failed_actions({"run_id": "gov-run-2"})
    assert second == []

    app = store.get_application(application_id)
    assert app["status"] == "applied"


def test_expansion_preflight_hands_single_highest_priority_candidate(monkeypatch):
    """The V16 delegate and the batch manifest verdict both require exactly
    one frozen execution-ready candidate, so the preflight must narrow its
    handoff to the next action in run_cycle execution order and report the
    rest as deferred; otherwise the delegate can never issue a command."""
    rc.reset_for_tests()
    rc.patch(
        {
            "factor_signal_config": {
                "fresh_shadow": {
                    "enabled": True,
                    "role": "alpha",
                    "autonomous_activation": True,
                },
                "stale_member": {"enabled": True, "role": "alpha"},
            },
            "factor_portfolio_weights": {"fresh_shadow": 0.0, "stale_member": 0.6},
            "factor_governance_builtin_activation_weight": 0.1,
        }
    )
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda _factor_id: False)
    profile = replace(
        _strict_profile(orch),
        builtin_activation_min_health_score=60.0,
        builtin_activation_min_n_obs=500,
        health_max_age_seconds=300.0,
    )
    report = {
        "group_count": 1,
        "groups": [
            {
                "group_id": "redundancy:auto:stale_member",
                "leader": "stale_member",
                "members": ["stale_member", "macd_hist"],
                "correlations": {"stale_member:macd_hist": 0.95},
                "sample_count": 500,
            }
        ],
    }

    result = orch._expansion_preflight(
        [
            {
                "factor_id": "fresh_shadow",
                "source": "builtin",
                "role": "alpha",
                "enabled": True,
                "lifecycle_status": "SHADOW",
                "health_status": "HEALTHY",
                "health_score": 80.0,
                "health_n_obs": 2000,
                "health_updated_at": time.time(),
                "factor_governance_shadow": {},
            }
        ],
        cfg=rc.shared(),
        profile=profile,
        redundancy_report=report,
    )

    assert result["required"] is True
    assert result["candidate_count"] == 1
    assert result["candidate_refs"][0]["candidate_id"] == "fresh_shadow"
    assert result["candidate_refs"][0]["action"] == "promote_factor"
    assert result["deferred_candidates"] == [
        {"candidate_id": "redundancy", "action": "update_redundancy_groups"}
    ]


def test_prepared_lease_demotes_stale_builtin_and_spares_active_builtin(tmp_path, monkeypatch):
    """PROMOTION_PREPARED must not be a parking state: a prepared candidate
    older than the lease window is demoted back to SHADOW with prepared_stale
    even while its evidence reads eligible; builtin ACTIVE alphas stay under
    the downweight/disable paths and are never touched by this scan."""
    rc.reset_for_tests()
    rc.patch({"factor_governance_promotion_prepared_max_age_hours": 168})
    _init_state_db(tmp_path)
    db = tmp_path / "state.db"
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(db)
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda _factor_id: False)
    monkeypatch.setattr(
        orch,
        "_promotion_evidence",
        lambda item, cfg: {"eligible": True, "blocker_codes": []},
    )

    demoted = []

    class _Lifecycle:
        def __init__(self, _db_path, *, adapter=None, health_stale_after_sec=None):
            pass

        def demote_to_shadow(self, *, name, reason, **_kwargs):
            demoted.append((name, reason))
            return {"ok": True, "lifecycle_stage": "SHADOW"}

    monkeypatch.setattr(governance_module, "FactorLifecycleService", _Lifecycle)

    now = time.time()
    catalog = [
        {
            "factor_id": "stale_builtin",
            "lifecycle_origin": "builtin",
            "lifecycle_status": "PROMOTION_PREPARED",
            "lifecycle_updated_at": now - 200 * 3600,
            "lifecycle_evidence": {},
        },
        {
            "factor_id": "fresh_dsl",
            "lifecycle_origin": "dsl",
            "lifecycle_status": "PROMOTION_PREPARED",
            "lifecycle_updated_at": now,
            "lifecycle_evidence": {},
        },
        {
            "factor_id": "live_builtin",
            "lifecycle_origin": "builtin",
            "lifecycle_status": "ACTIVE",
            "lifecycle_evidence": {},
        },
    ]

    orch._demote_invalid_candidate_evidence(
        catalog,
        {"run_id": "lease-run"},
        cfg=rc.shared(),
    )

    assert demoted == [("stale_builtin", "prepared_stale")]


def test_promotion_evidence_waives_absent_health_for_canary_ladder_top():
    """A canary PROBATION candidate was never traded, so it has no
    factor_health row (UNKNOWN forever).  The completed ladder substitutes for
    that absent evidence; a SHADOW-stage candidate and a real DECAYING row
    still block."""
    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    cfg = rc.shared()

    def _item(canary_stage, health_status=None):
        item = {
            "factor_id": "dsl_auto_abc",
            "source": "discovered",
            "role": "alpha",
            "lifecycle_status": "SHADOW",
            "canary": {"stage": canary_stage},
            "shadow_perf": {
                "oos_bars": 1200,
                "n_valid": 90,
                "cumulative_pnl": 0.04,
                "hit_rate": 0.52,
                "max_drawdown": 0.02,
            },
        }
        if health_status:
            item["health_status"] = health_status
            item["health_score"] = 30.0
            item["health_updated_at"] = time.time()
        return item

    probation = orch._promotion_evidence(_item("PROBATION"), cfg)
    assert "factor_health_unknown" not in probation["blocker_codes"]
    assert "factor_health_invalid_or_stale" not in probation["blocker_codes"]
    assert probation["health_evidence_source"] == "canary_ladder"

    shadow_stage = orch._promotion_evidence(_item("SHADOW"), cfg)
    assert "factor_health_unknown" in shadow_stage["blocker_codes"]

    decaying = orch._promotion_evidence(_item("PROBATION", "DECAYING"), cfg)
    assert "factor_health_decaying" in decaying["blocker_codes"]
    assert decaying["health_evidence_source"] == "factor_health"


def test_preflight_backoff_defers_recently_blocked_candidates(monkeypatch):
    """Single-candidate handoff must not let a permanently failing
    high-priority candidate starve ready lower-priority ones: a candidate
    audited blocked recently is deferred below eligible ones."""
    rc.reset_for_tests()
    rc.patch(
        {
            "factor_signal_config": {
                "blocked_activation": {
                    "enabled": True,
                    "role": "alpha",
                    "autonomous_activation": True,
                },
            },
            "factor_portfolio_weights": {"blocked_activation": 0.0},
            "factor_governance_builtin_activation_weight": 0.1,
        }
    )
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda _factor_id: False)
    monkeypatch.setattr(
        orch,
        "_recently_blocked_expansion_candidates",
        lambda: {"blocked_activation"},
    )
    profile = replace(
        _strict_profile(orch),
        builtin_activation_min_health_score=60.0,
        builtin_activation_min_n_obs=500,
        health_max_age_seconds=300.0,
    )
    now = time.time()
    catalog = [
        {
            "factor_id": "blocked_activation",
            "source": "builtin",
            "role": "alpha",
            "enabled": True,
            "lifecycle_status": "SHADOW",
            "lifecycle_origin": "builtin",
            "health_status": "HEALTHY",
            "health_score": 80.0,
            "health_n_obs": 2000,
            "health_updated_at": now,
            "factor_governance_shadow": {},
        },
        {
            "factor_id": "dsl_promo",
            "source": "discovered",
            "role": "alpha",
            "direction": 1,
            "enabled": True,
            "lifecycle_status": "SHADOW",
            "lifecycle_origin": "dsl",
            "lifecycle_expression": "rank(close)",
            "lifecycle_factor_id": canonical_factor_id("rank(close)"),
            "lifecycle_generation": 1,
            "lifecycle_artifact_hash": "a" * 64,
            "lifecycle_definition_fingerprint": factor_definition_fingerprint(
                "rank(close)"
            ),
            "lifecycle_config_hash": "c" * 64,
            "runtime_selection_fingerprint": "f" * 64,
            "health_rolling_ic": 0.03,
            "canary": {"stage": "PROBATION"},
            "shadow_perf": {
                "oos_bars": 1200,
                "n_valid": 90,
                "cumulative_pnl": 0.04,
                "hit_rate": 0.52,
                "max_drawdown": 0.02,
                "evidence_hash": "d" * 64,
                "dataset_hash": "e" * 64,
            },
        },
    ]

    result = orch._expansion_preflight(
        catalog,
        cfg=rc.shared(),
        profile=profile,
        redundancy_report={"group_count": 0, "groups": []},
    )

    assert result["candidate_count"] == 1
    assert result["candidate_refs"][0]["candidate_id"] == "dsl_promo"
    assert result["deferred_candidates"] == [
        {"candidate_id": "blocked_activation", "action": "promote_factor"}
    ]
