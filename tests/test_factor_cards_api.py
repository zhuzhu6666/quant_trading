import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pandas as pd
import pytest

from alpha.registry_adapter import reset_shared
from backend.api import learning as learning_api
from backend.core.db import STATE_DB_DDL
from backend.services.factor_cards import (
    FactorCardService,
    build_factor_admission_evidence,
)
from backend.services.parameter_templates import ParameterTemplateService
from backend.services.parameter_template_validation import (
    ParameterTemplateValidationService,
    run_parameter_template_offline_validation,
)
from backend.services.research_evidence import ResearchEvidenceRejected
from config import runtime_config as rc
from research.learning.governor import RuleEvolutionGovernor


def _valid_parameter_template_research_evidence() -> dict:
    binding_inputs = {
        "config_hash": "a" * 64,
        "data_hash": "b" * 64,
        "code_hash": "c" * 64,
        "artifact_hash": "d" * 64,
    }
    binding_hash = hashlib.sha256(
        json.dumps(
            binding_inputs,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "parity_replay_report.v1",
        "contract": "parity_replay_contract.v1",
        "status": "parity_verified",
        "engine": "live_parity_replay_v1",
        "evidence_class": "live_parity",
        "live_parity": True,
        "governance_eligible": True,
        "deployable_candidate": True,
        "bindings": {**binding_inputs, "binding_hash": binding_hash},
        "binding_verification": {
            "expected": dict(binding_inputs),
            "required_expected_names": list(binding_inputs),
            "missing_expected": [],
            "verified": True,
            "mismatches": [],
        },
        "data_source": {"source": "monthly_pit_bars", "point_in_time": True},
        "causality": {
            "closed_bar_only": True,
            "next_bar_execution": True,
            "native_bid_ask": True,
        },
        "components": {
            name: {"reuse": "exact", "verified": True}
            for name in (
                "factor_frame",
                "runtime_selector",
                "streaming_factor_engine",
                "normalizer",
                "compositor",
                "execution_gate",
                "risk_policy",
                "position_path_metrics",
                "safety_arbitration",
                "supervisor",
                "trailing",
                "protection_planner",
                "cost_model",
                "lifecycle",
            )
        },
        "diagnostic_reasons": [],
    }


def _seed_factor_card_state(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO factor_health
            (factor, score, status, section, components_json, n_obs, rolling_ic, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rsi_14",
                72.5,
                "HEALTHY",
                "momentum",
                json.dumps({"ic": 0.14}),
                128,
                0.14,
                1_800_000.0,
            ),
        )
        conn.execute(
            """
            INSERT INTO decision_factor_snapshot
            (decision_id, factor, source, raw_value, normalized_value, direction,
             base_weight, policy_weight, shadow_score, health_score, gated,
             gated_reason, contribution_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "dec_1",
                "rsi_14",
                "registry",
                61.0,
                0.64,
                1.0,
                0.25,
                0.2,
                0.18,
                72.5,
                0,
                "",
                0.12,
            ),
        )
        conn.execute(
            """
            INSERT INTO trade_outcome_review
            (review_id, trade_id, position_id, outcome_label, review_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "rev_1",
                "t1",
                "p1",
                "bad_loss",
                json.dumps({"primary_responsibility": "parameter"}),
                1_850_000.0,
            ),
        )
        conn.execute(
            """
            INSERT INTO factor_contribution_review
            (review_id, trade_id, factor, entry_contribution, hold_contribution,
             exit_contribution, net_contribution, confidence, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rev_1",
                "t1",
                "rsi_14",
                0.31,
                -0.11,
                -0.42,
                -0.22,
                0.76,
                json.dumps(
                    {
                        "source": "rule_review",
                        "primary_responsibility": "parameter",
                        "responsibility_labels": [
                            "factor_logic_ok_but_param_suspect",
                            "holding_too_long",
                        ],
                        "factor_role": "harmful",
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, reviewed_at, review_note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "s1",
                "factor",
                "rsi_14",
                "downweight",
                0.83,
                "parameter drift",
                json.dumps({"reason": "Phase E seed"}),
                "approved",
                1_860_000.0,
                "",
                1_860_000.0,
            ),
        )
        conn.execute(
            """
            INSERT INTO learning_application_log
            (application_id, cycle_ts, scope_type, scope_key, action, bias_multiplier,
             old_weight, new_weight, suggestion_ids_json, status, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "app_1",
                1_861_000.0,
                "factor",
                "rsi_14",
                "downweight",
                0.8,
                0.25,
                0.2,
                json.dumps(["s1"]),
                "applied",
                json.dumps({"reason": "approved"}),
                1_861_000.0,
            ),
        )
        conn.execute(
            """
            INSERT INTO learning_application_effect
            (application_id, scope_type, scope_key, action, status, observed_trade_count,
             baseline_trade_count, post_avg_reward, baseline_avg_reward, delta_avg_reward,
             post_win_rate, baseline_win_rate, decision_json, last_review_at, updated_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "app_1",
                "factor",
                "rsi_14",
                "downweight",
                "observing",
                3,
                8,
                -0.02,
                -0.11,
                0.09,
                0.5,
                0.25,
                json.dumps({"note": "watch"}),
                1_862_000.0,
                1_862_000.0,
                1_861_000.0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _complete_candidate_catalog(now: float, *, role: str = "alpha") -> dict:
    return {
        "role": role,
        "direction": 1,
        "normalizer": "robust_zscore",
        "lifecycle_factor_id": "dsl:candidate",
        "lifecycle_status": "PROMOTION_PREPARED",
        "lifecycle_generation": 3,
        "lifecycle_artifact_hash": "a" * 64,
        "lifecycle_definition_fingerprint": "d" * 64,
        "lifecycle_config_hash": "c" * 64,
        "lifecycle_mutation_id": "mutation-3",
        "runtime_admission": "projection_acknowledged",
        "runtime_selection_fingerprint": "s" * 64,
        "loaded_projection": {
            "loaded": True,
            "status": "loaded",
            "generation": 3,
            "artifact_hash": "a" * 64,
            "projection_id": "projection-3",
            "process_role": "live_alpha",
        },
        "health_status": "HEALTHY",
        "health_score": 85.0,
        "health_n_obs": 500,
        "health_updated_at": now,
        "shadow_perf": {
            "n_valid": 20,
            "oos_bars": 600,
            "evidence_hash": "evidence-hash",
            "dataset_hash": "dataset-hash",
        },
        "canary": {"stage": "ACTIVE", "oos_bars": 600},
        "lifecycle_evidence": {
            "candidate_validation": {
                "signed_ic_mean": 0.04,
                "pit_passed": True,
                "walk_forward_passed": True,
                "multi_forward_passed": True,
                "cost_test_passed": True,
                "execution_evidence_complete": True,
                "contamination_status": "clean",
                "regime_ids": ["trend", "range"],
            },
            "v16": {"command_id": "cmd-3", "candidate_id": "candidate-3"},
        },
    }


def _complete_evidence_counts() -> dict:
    return {
        "governance_eligible_mature": 20,
        "contaminated_or_ineligible": 0,
    }


def test_candidate_card_signed_ic_direction_mismatch_is_fail_closed():
    now = time.time()
    catalog = _complete_candidate_catalog(now)
    catalog["direction"] = -1

    evidence = build_factor_admission_evidence(
        factor_id="candidate",
        catalog_item=catalog,
        evidence_counts=_complete_evidence_counts(),
        governance={},
        now_ts=now,
    )

    assert evidence["direction"]["status"] == "signed_ic_direction_mismatch"
    assert evidence["eligible_for_preparation"] is False
    assert "direction_contract_invalid" in evidence["preflight_blocker_codes"]


@pytest.mark.parametrize("role", ["context", "gate"])
def test_candidate_card_non_alpha_roles_never_publish_directional_vote(role):
    now = time.time()
    evidence = build_factor_admission_evidence(
        factor_id=f"{role}-candidate",
        catalog_item=_complete_candidate_catalog(now, role=role),
        evidence_counts=_complete_evidence_counts(),
        governance={},
        now_ts=now,
    )

    assert evidence["direction"]["directional_vote_allowed"] is False
    assert evidence["direction"]["direction"] is None
    assert evidence["direction"]["polarity"] is None
    assert evidence["direction"]["status"] == "non_directional"
    assert "direction_contract_invalid" not in evidence["preflight_blocker_codes"]


def test_candidate_card_complete_prepared_evidence_is_activation_eligible():
    now = time.time()
    evidence = build_factor_admission_evidence(
        factor_id="candidate",
        catalog_item=_complete_candidate_catalog(now),
        evidence_counts=_complete_evidence_counts(),
        governance={},
        now_ts=now,
    )

    assert evidence["eligible_for_preparation"] is True
    assert evidence["eligible_for_activation"] is True
    assert evidence["activation_blocker_codes"] == []
    assert evidence["validation"]["bar_oos"]["research_only"] is True


def test_candidate_card_active_canary_waits_for_mature_positive_real_effect():
    now = time.time()
    catalog = _complete_candidate_catalog(now)
    catalog["lifecycle_status"] = "ACTIVE"
    catalog["activation_canary"] = True
    observing = build_factor_admission_evidence(
        factor_id="candidate",
        catalog_item=catalog,
        evidence_counts=_complete_evidence_counts(),
        governance={
            "latest_application_id": "app-1",
            "latest_application_status": "applied",
            "application_effect_status": "observing",
            "application_effect_trade_count": 7,
        },
        now_ts=now,
    )
    effective = build_factor_admission_evidence(
        factor_id="candidate",
        catalog_item=catalog,
        evidence_counts=_complete_evidence_counts(),
        governance={
            "latest_application_id": "app-1",
            "latest_application_status": "applied",
            "application_effect_status": "effective",
            "application_effect_trade_count": 20,
            "application_effect_decision": {
                "evidence_quality": {"bounded_attribution_allowed": True}
            },
        },
        now_ts=now,
    )

    assert observing["eligible_for_weight_expansion"] is False
    assert "application_effect_not_mature_positive" in observing["weight_expansion_blocker_codes"]
    assert effective["eligible_for_weight_expansion"] is True
    assert effective["weight_expansion_blocker_codes"] == []


def test_factor_card_service_assembles_governance_and_responsibility_evidence(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    service = FactorCardService(db_path)
    card = service.list_cards(factor_id="rsi_14", limit=5)[0]

    assert card["schema_version"] == "factor_card.v1"
    assert card["factor_id"] == "rsi_14"
    assert card["factor_family"] == "momentum_oscillator"
    assert card["source"] == "builtin"
    assert card["formula_version"] == "registry_builtin.v1"
    assert card["parameter_version"] == "default.v1"
    assert card["parameters"] == {"length": 14}
    assert card["governance_state"]["weight_state"] == "downweighted"
    assert card["governance_state"]["review_status"] == "approved"
    assert card["governance_state"]["latest_suggestion_action"] == "downweight"
    assert card["governance_state"]["latest_template_recommendation"]["recommendation_id"]
    assert card["governance_state"]["latest_template_recommendation"]["recommended_action"] in {"suggest_switch", "offline_validate"}
    assert card["evidence_summary"]["health_score"] == 72.5
    assert card["evidence_summary"]["shadow_score"] == 0.18
    assert card["evidence_summary"]["last_primary_responsibility"] == "parameter"
    assert "factor_logic_ok_but_param_suspect" in card["failure_modes"]
    assert "holding_too_long" in card["evidence_summary"]["recent_responsibility_labels"]
    assert card["updated_at"].endswith("Z")


def test_parameter_template_activation_syncs_runtime_signal_config(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    rc.reset_for_tests()
    _seed_factor_card_state(db_path)

    service = ParameterTemplateService(db_path)
    item = service.upsert_template(
        {
            "factor_id": "rsi_14",
            "regime_key": "range",
            "factor_family": "momentum_oscillator",
            "template_version": "runtime_range.v1",
            "template_role": "default",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 10, "upper_band": 68, "lower_band": 32},
            "applicable_regimes": ["range"],
            "avoid_regimes": ["strong_trend"],
            "holding_profile_hint": {"style": "short_swing", "min_bars": 2, "max_bars": 8},
            "evidence": {"note": "runtime sync"},
        },
        source="manual",
    )
    suggestion = service.create_switch_suggestion(
        factor_id="rsi_14",
        template_id=item["template_id"],
        regime_key="range",
        note="runtime sync approve",
    )
    RuleEvolutionGovernor(db_path).set_status(suggestion["suggestion_id"], "approved", "manual approve")
    applied = service.activate_template(
        factor_id="rsi_14",
        template_id=item["template_id"],
        regime_key="range",
        suggestion_id=suggestion["suggestion_id"],
        note="apply approved template",
    )

    cfg = rc.shared()

    assert applied["ok"] is True
    assert cfg.factor_signal_config["rsi_14"]["parameter_template_version"] == "runtime_range.v1"
    assert cfg.factor_signal_config["rsi_14"]["parameter_overrides"]["length"] == 10
    assert cfg.extra["active_parameter_templates"]["rsi_14:range"]["template_id"] == item["template_id"]


def test_runtime_tunable_derived_template_activation_syncs_keltner_runtime_config(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    rc.reset_for_tests()
    _seed_factor_card_state(db_path)

    service = ParameterTemplateService(db_path)
    item = service.upsert_template(
        {
            "factor_id": "keltner_width",
            "regime_key": "",
            "factor_family": "volatility",
            "template_version": "keltner_runtime.v1",
            "template_role": "conservative",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"ema_length": 24, "atr_multiplier": 1.8},
            "applicable_regimes": ["breakout", "high_vol"],
            "avoid_regimes": ["range"],
            "holding_profile_hint": {"style": "confirmation", "min_bars": 2, "max_bars": 10},
            "evidence": {"note": "keltner runtime sync"},
        },
        source="manual",
    )
    suggestion = service.create_switch_suggestion(
        factor_id="keltner_width",
        template_id=item["template_id"],
        note="keltner runtime sync approve",
    )
    RuleEvolutionGovernor(db_path).set_status(suggestion["suggestion_id"], "approved", "manual approve")
    applied = service.activate_template(
        factor_id="keltner_width",
        template_id=item["template_id"],
        suggestion_id=suggestion["suggestion_id"],
        note="apply approved keltner template",
    )

    cfg = rc.shared()

    assert applied["ok"] is True
    assert applied["boundary"]["recommended_scope"] == "online_light"
    assert cfg.factor_signal_config["keltner_width"]["parameter_template_version"] == "keltner_runtime.v1"
    assert cfg.factor_signal_config["keltner_width"]["parameter_overrides"]["ema_length"] == 24
    assert cfg.factor_signal_config["keltner_width"]["parameter_overrides"]["atr_multiplier"] == 1.8
    assert cfg.extra["active_parameter_templates"]["keltner_width:default"]["template_id"] == item["template_id"]


def test_learning_factor_cards_endpoint_returns_filtered_items(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    class BoundFactorCardService(FactorCardService):
        def __init__(self):
            super().__init__(db_path)

    monkeypatch.setattr(learning_api, "FactorCardService", BoundFactorCardService)

    result = learning_api.get_factor_cards(None, factor_id="rsi_14", limit=10)

    assert len(result["items"]) == 1
    assert result["items"][0]["factor_id"] == "rsi_14"
    assert result["items"][0]["governance_state"]["application_effect_status"] == "observing"


def test_factor_cards_prefers_latest_catalog_snapshot(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    import backend.services.factor_catalog as factor_catalog

    monkeypatch.setattr(
        factor_catalog,
        "latest_factor_catalog_snapshot",
        lambda _db_path: {
            "ok": True,
            "items": [
                {
                    "factor_id": "rsi_14",
                    "source": "builtin",
                    "lifecycle_status": "ACTIVE",
                }
            ],
        },
    )
    monkeypatch.setattr(
        factor_catalog,
        "build_factor_catalog",
        lambda *_args, **_kwargs: pytest.fail("live catalog rebuild should be skipped"),
    )

    cards = FactorCardService(db_path).list_cards(factor_id="rsi_14", limit=1)

    assert len(cards) == 1
    assert cards[0]["factor_id"] == "rsi_14"


def test_factor_card_surfaces_catalog_governance_shadow_evidence(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE factor_governance_shadow_audit (
                inference_id TEXT PRIMARY KEY,
                model_type TEXT NOT NULL,
                model_version TEXT DEFAULT '',
                artifact_path TEXT DEFAULT '',
                review_id TEXT DEFAULT '',
                trade_id TEXT DEFAULT '',
                position_id TEXT DEFAULT '',
                factor TEXT DEFAULT '',
                mode TEXT DEFAULT 'shadow',
                positive_score REAL DEFAULT 0.0,
                weakness_score REAL DEFAULT 0.0,
                prediction INTEGER DEFAULT 0,
                payload_json TEXT DEFAULT '{}',
                result_json TEXT DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO factor_governance_shadow_audit
            (inference_id, model_type, model_version, factor, mode,
             positive_score, weakness_score, prediction, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("fg_card_1", "factor_governance_lightgbm", "1.0", "rsi_14", "shadow", 0.1, 0.82, 0, 2_000_000.0),
        )
        conn.commit()
    finally:
        conn.close()

    card = FactorCardService(db_path).list_cards(factor_id="rsi_14", limit=5)[0]

    assert card["role"] == "alpha"
    assert card["evidence_summary"]["model_weakness_score"] == 0.82
    assert card["evidence_summary"]["factor_governance_shadow"]["latest_inference_id"] == "fg_card_1"


def test_parameter_template_service_derives_default_and_regime_aware_variants(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    service = ParameterTemplateService(db_path)
    items = service.list_templates(factor_id="rsi_14", limit=10)

    assert [item["template_role"] for item in items] == [
        "aggressive",
        "conservative",
        "default",
    ]
    default_item = next(item for item in items if item["template_role"] == "default")
    conservative = next(item for item in items if item["template_role"] == "conservative")
    aggressive = next(item for item in items if item["template_role"] == "aggressive")

    assert default_item["source"] == "manual_library"
    assert default_item["parameters"] == {"length": 14, "upper_band": 70, "lower_band": 30}
    assert conservative["parameters"] == {"length": 21, "upper_band": 74, "lower_band": 26}
    assert aggressive["parameters"] == {"length": 9, "upper_band": 65, "lower_band": 35}
    assert conservative["applicable_regimes"] == ["strong_trend", "low_vol"]
    assert aggressive["evidence"]["last_primary_responsibility"] == "parameter"


def test_learning_parameter_templates_endpoint_filters_by_regime(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    class BoundParameterTemplateService(ParameterTemplateService):
        def __init__(self):
            super().__init__(db_path)

    monkeypatch.setattr(learning_api, "ParameterTemplateService", BoundParameterTemplateService)

    result = learning_api.get_parameter_templates(None, factor_id="rsi_14", regime="strong_trend", limit=10)

    assert len(result["items"]) == 1
    assert result["items"][0]["template_role"] == "conservative"


def test_learning_review_suggestion_returns_result_display(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    class BoundGovernor(RuleEvolutionGovernor):
        def __init__(self):
            super().__init__(db_path)

    monkeypatch.setattr(learning_api, "RuleEvolutionGovernor", BoundGovernor)

    approved = learning_api.review_suggestion(
        None,
        learning_api.ReviewRequest(
            suggestion_id="s1",
            status="approved",
            note="approve display",
        ),
    )

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "s_reject",
                "factor",
                "rsi_14",
                "downweight",
                0.5,
                "reject display",
                "{}",
                "proposed",
                1_870_000.0,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    rejected = learning_api.review_suggestion(
        None,
        learning_api.ReviewRequest(
            suggestion_id="s_reject",
            status="rejected",
            note="reject display",
        ),
    )

    assert approved["ok"] is True
    assert approved["result_label"] == "已批准建议"
    assert "等待应用" in approved["result_summary"]
    assert rejected["ok"] is True
    assert rejected["result_label"] == "已拒绝建议"
    assert "保留证据" in rejected["result_summary"]


def test_learning_governance_run_returns_result_display(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    class BoundGovernor(RuleEvolutionGovernor):
        def __init__(self):
            super().__init__(db_path)

    monkeypatch.setattr(learning_api, "RuleEvolutionGovernor", BoundGovernor)
    monkeypatch.setattr(
        learning_api,
        "_risk_verdict",
        lambda action, context=None: {"allowed": True, "reason": "ok"},
    )

    result = learning_api.run_governance(None)

    assert result["auto_actions"] >= 0
    assert result["result_label"]
    assert result["result_summary"] == result["message"]


def test_parameter_template_recommendations_surface_parameter_suspicion(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    class BoundParameterTemplateService(ParameterTemplateService):
        def __init__(self):
            super().__init__(db_path)

    monkeypatch.setattr(learning_api, "ParameterTemplateService", BoundParameterTemplateService)

    result = learning_api.get_parameter_template_recommendations(None, factor_id="rsi_14", limit=10)

    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["factor_id"] == "rsi_14"
    assert item["responsibility"]["primary_responsibility"] == "parameter"
    assert "factor_logic_ok_but_param_suspect" in item["responsibility"]["responsibility_labels"]
    assert item["boundary"]["recommended_scope"] == "online_light"
    assert item["recommended_action"] == "suggest_switch"
    assert item["trace_locator"]["position_id"] == "p1"
    assert item["trace_locator"]["review_id"] == "rev_1"
    assert item["governance"]["status_label"] == "在线轻调"
    assert item["governance"]["stage_tone"] == "positive"
    assert item["governance"]["action_button_text"] == "生成治理建议"
    assert item["governance"]["action_summary"] == "可直接进入受控 suggestion -> apply-switch 链路。"
    assert "suggestion 审批" in item["governance"]["followup_hint"]


def test_parameter_template_recommendations_can_surface_offline_validate_path(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    service = ParameterTemplateService(db_path)
    service.activate_template(
        factor_id="rsi_14",
        template_id="rsi_14:default.v1:default",
        regime_key="",
        note="seed default active for offline recommendation",
    )

    items = service.list_recommendations(factor_id="rsi_14", limit=10)

    assert len(items) == 1
    assert items[0]["boundary"]["recommended_scope"] == "offline_deep"
    assert items[0]["recommended_action"] == "offline_validate"
    assert "parameter_delta_too_large" in items[0]["boundary"]["reasons"]


def test_parameter_template_switch_suggestion_carries_factor_card_parameter_evidence(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    service = ParameterTemplateService(db_path)
    template = service.upsert_template(
        {
            "factor_id": "rsi_14",
            "regime_key": "",
            "factor_family": "momentum_oscillator",
            "template_version": "rsi_reco.v1",
            "template_role": "conservative",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 18, "upper_band": 72, "lower_band": 28},
            "applicable_regimes": ["range"],
            "avoid_regimes": ["strong_trend"],
            "holding_profile_hint": {"style": "short_swing", "min_bars": 2, "max_bars": 10},
            "evidence": {"note": "recommendation evidence"},
        },
        source="manual",
    )

    suggestion = service.create_switch_suggestion(
        factor_id="rsi_14",
        template_id=template["template_id"],
        note="carry factor-card evidence",
        evidence_context={"source": "recommendation"},
    )

    assert suggestion["evidence"]["factor_card_evidence"]["last_primary_responsibility"] == "parameter"
    assert "factor_logic_ok_but_param_suspect" in suggestion["evidence"]["factor_card_evidence"]["recent_responsibility_labels"]
    assert suggestion["evidence"]["evidence_context"]["source"] == "recommendation"


def test_parameter_template_recommendation_can_materialize_governance_suggestion(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    class BoundParameterTemplateService(ParameterTemplateService):
        def __init__(self):
            super().__init__(db_path)

    monkeypatch.setattr(learning_api, "ParameterTemplateService", BoundParameterTemplateService)

    recommendations = learning_api.get_parameter_template_recommendations(None, factor_id="rsi_14", limit=10)
    recommendation_id = recommendations["items"][0]["recommendation_id"]
    result = learning_api.materialize_parameter_template_recommendation(
        None,
        learning_api.ParameterTemplateRecommendationActionRequest(
            recommendation_id=recommendation_id,
            note="materialize recommendation test",
        ),
    )

    assert result["ok"] is True
    assert result["mode"] == "suggest_switch"
    assert result["result_label"] == "已生成治理建议"
    assert "等待 governor 审批" in result["result_summary"]
    assert result["recommendation"]["recommendation_id"] == recommendation_id
    assert result["item"]["action"] == "switch_parameter_template"
    assert result["item"]["evidence"]["evidence_context"]["source"] == "parameter_template_recommendation"
    assert result["item"]["evidence"]["evidence_context"]["recommendation_id"] == recommendation_id


def test_parameter_template_recommendation_can_materialize_offline_validation_job(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    class BoundParameterTemplateService(ParameterTemplateService):
        def __init__(self):
            super().__init__(db_path)

    monkeypatch.setattr(learning_api, "ParameterTemplateService", BoundParameterTemplateService)

    service = BoundParameterTemplateService()
    service.activate_template(
        factor_id="rsi_14",
        template_id="rsi_14:default.v1:default",
        regime_key="",
        note="seed active default for offline recommendation job",
    )

    captured: dict[str, object] = {}

    class FakeJob:
        id = "job_ptv_reco_1"
        status = "queued"

    class FakeJobManager:
        def submit(self, kind, params, fn):
            captured["kind"] = kind
            captured["params"] = params
            captured["callable"] = fn
            return FakeJob()

    monkeypatch.setattr(learning_api, "get_job_manager", lambda: FakeJobManager())

    recommendations = learning_api.get_parameter_template_recommendations(None, factor_id="rsi_14", limit=10)
    recommendation_id = recommendations["items"][0]["recommendation_id"]
    result = learning_api.materialize_parameter_template_recommendation(
        None,
        learning_api.ParameterTemplateRecommendationActionRequest(
            recommendation_id=recommendation_id,
            note="materialize offline recommendation test",
            symbol="XAUUSD+",
            timeframe="M15",
        ),
    )

    assert result["ok"] is True
    assert result["mode"] == "offline_validate"
    assert result["result_label"] == "已创建离线验证"
    assert "登记灰度候选" in result["result_summary"]
    assert result["job_id"] == "job_ptv_reco_1"
    assert result["boundary"]["recommended_scope"] == "offline_deep"
    assert captured["kind"] == "parameter_template_validation"
    assert captured["params"]["factor_id"] == "rsi_14"
    assert captured["params"]["template_id"] == result["recommendation"]["target_template_id"]
    assert captured["params"]["recommendation_context"]["source"] == "parameter_template_recommendation"
    assert captured["params"]["recommendation_context"]["recommendation_id"] == recommendation_id


def test_parameter_template_service_persists_activation_and_switch_log(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    service = ParameterTemplateService(db_path)
    item = service.upsert_template(
        {
            "factor_id": "rsi_14",
            "regime_key": "range",
            "factor_family": "momentum_oscillator",
            "template_version": "manual_range.v1",
            "template_role": "default",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 10, "upper_band": 68, "lower_band": 32},
            "applicable_regimes": ["range"],
            "avoid_regimes": ["strong_trend"],
            "holding_profile_hint": {"style": "short_swing", "min_bars": 2, "max_bars": 8},
            "evidence": {"note": "manual override"},
        },
        source="manual",
    )
    suggestion = service.create_switch_suggestion(
        factor_id="rsi_14",
        template_id=item["template_id"],
        regime_key="range",
        note="approve switch",
    )
    RuleEvolutionGovernor(db_path).set_status(suggestion["suggestion_id"], "approved", "manual approve")
    result = service.activate_template(
        factor_id="rsi_14",
        template_id=item["template_id"],
        regime_key="range",
        suggestion_id=suggestion["suggestion_id"],
        note="apply approved template",
    )

    assert result["ok"] is True
    active = service.get_active_template(factor_id="rsi_14", regime_key="range")
    assert active["template_id"] == item["template_id"]
    logs = service.list_switch_logs(factor_id="rsi_14")
    assert logs[0]["new_template_id"] == item["template_id"]
    assert logs[0]["suggestion_id"] == suggestion["suggestion_id"]


def test_learning_parameter_template_management_endpoints_work_end_to_end(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    class BoundParameterTemplateService(ParameterTemplateService):
        def __init__(self):
            super().__init__(db_path)

    monkeypatch.setattr(learning_api, "ParameterTemplateService", BoundParameterTemplateService)

    upsert = learning_api.upsert_parameter_template(
        None,
        learning_api.ParameterTemplateUpsertRequest(
            template={
                "factor_id": "rsi_14",
                "regime_key": "range",
                "factor_family": "momentum_oscillator",
                "template_version": "manual_range.v2",
                "template_role": "conservative",
                "formula_version": "registry_builtin.v1",
                "base_parameter_version": "default.v1",
                "parameters": {"length": 18},
                "applicable_regimes": ["range"],
                "avoid_regimes": ["strong_trend"],
                "holding_profile_hint": {"style": "short_swing", "min_bars": 2, "max_bars": 14},
                "evidence": {"note": "api upsert"},
            },
            source="manual",
            activate=False,
        ),
    )
    template_id = upsert["item"]["template_id"]
    suggestion = learning_api.suggest_parameter_template_switch(
        None,
        learning_api.ParameterTemplateSuggestSwitchRequest(
            factor_id="rsi_14",
            regime_key="range",
            template_id=template_id,
            note="submit for approval",
        ),
    )
    RuleEvolutionGovernor(db_path).set_status(suggestion["item"]["suggestion_id"], "approved", "ok")
    applied = learning_api.apply_parameter_template_switch(
        None,
        learning_api.ParameterTemplateApplySwitchRequest(
            factor_id="rsi_14",
            regime_key="range",
            template_id=template_id,
            suggestion_id=suggestion["item"]["suggestion_id"],
            note="apply",
        ),
    )
    active = learning_api.get_active_parameter_templates(None, factor_id="rsi_14")
    logs = learning_api.get_parameter_template_switch_logs(None, factor_id="rsi_14", limit=10)

    assert applied["ok"] is True
    assert any(item["template_id"] == template_id for item in active["items"])
    assert logs["items"][0]["new_template_id"] == template_id


def test_parameter_template_boundary_check_distinguishes_online_and_offline(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    rc.reset_for_tests()
    _seed_factor_card_state(db_path)

    class BoundParameterTemplateService(ParameterTemplateService):
        def __init__(self):
            super().__init__(db_path)

    monkeypatch.setattr(learning_api, "ParameterTemplateService", BoundParameterTemplateService)

    service = BoundParameterTemplateService()
    online_template = service.upsert_template(
        {
            "factor_id": "rsi_14",
            "regime_key": "range",
            "factor_family": "momentum_oscillator",
            "template_version": "range_small_shift.v1",
            "template_role": "conservative",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 18, "upper_band": 72, "lower_band": 28},
            "applicable_regimes": ["range"],
            "avoid_regimes": ["strong_trend"],
            "holding_profile_hint": {"style": "short_swing", "min_bars": 2, "max_bars": 10},
            "evidence": {"note": "small shift"},
        },
        source="manual",
    )
    offline_template = service.upsert_template(
        {
            "factor_id": "bb_width",
            "regime_key": "",
            "factor_family": "volatility",
            "template_version": "bb_custom.v1",
            "template_role": "experimental",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 40, "stddev": 3},
            "applicable_regimes": ["high_vol"],
            "avoid_regimes": ["range"],
            "holding_profile_hint": {"style": "confirmation", "min_bars": 2, "max_bars": 8},
            "evidence": {"note": "unsupported role"},
        },
        source="manual",
    )

    online = learning_api.assess_parameter_template_boundary(
        None,
        learning_api.ParameterTemplateBoundaryRequest(
            factor_id="rsi_14",
            regime_key="range",
            template_id=online_template["template_id"],
        ),
    )
    offline = learning_api.assess_parameter_template_boundary(
        None,
        learning_api.ParameterTemplateBoundaryRequest(
            factor_id="bb_width",
            template_id=offline_template["template_id"],
        ),
    )

    assert online["item"]["recommended_scope"] == "online_light"
    assert "fits_runtime_guardrail" in online["item"]["reasons"]
    assert offline["item"]["recommended_scope"] == "offline_deep"
    assert "unsupported_template_role" in offline["item"]["reasons"]


def test_runtime_tunable_supertrend_template_is_online_light(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    service = ParameterTemplateService(db_path)
    template = service.upsert_template(
        {
            "factor_id": "supertrend_str",
            "regime_key": "",
            "factor_family": "trend",
            "template_version": "supertrend_online.v1",
            "template_role": "conservative",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"atr_length": 12, "multiplier": 2.8},
            "applicable_regimes": ["trend", "breakout"],
            "avoid_regimes": ["range"],
            "holding_profile_hint": {"style": "trend_follow", "min_bars": 4, "max_bars": 20},
            "evidence": {"note": "supertrend online light"},
        },
        source="manual",
    )

    boundary = service.assess_template_change(
        factor_id="supertrend_str",
        target_template_id=template["template_id"],
    )
    suggestion = service.create_switch_suggestion(
        factor_id="supertrend_str",
        template_id=template["template_id"],
        note="supertrend boundary path",
    )

    assert boundary["recommended_scope"] == "online_light"
    assert "fits_runtime_guardrail" in boundary["reasons"]
    assert suggestion["evidence"]["boundary"]["recommended_scope"] == "online_light"
    assert suggestion["evidence"]["approval_path"] == "governed_apply_switch"


def test_runtime_tunable_ema_slope_template_is_online_light_and_syncs_runtime_config(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    rc.reset_for_tests()
    _seed_factor_card_state(db_path)

    service = ParameterTemplateService(db_path)
    template = service.upsert_template(
        {
            "factor_id": "ema_slope",
            "regime_key": "",
            "factor_family": "trend",
            "template_version": "ema_slope_online.v1",
            "template_role": "conservative",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"period": 18, "lookback": 4},
            "applicable_regimes": ["trend", "breakout"],
            "avoid_regimes": ["range"],
            "holding_profile_hint": {"style": "trend_follow", "min_bars": 3, "max_bars": 18},
            "evidence": {"note": "ema slope online"},
        },
        source="manual",
    )

    boundary = service.assess_template_change(
        factor_id="ema_slope",
        target_template_id=template["template_id"],
    )
    suggestion = service.create_switch_suggestion(
        factor_id="ema_slope",
        template_id=template["template_id"],
        note="ema slope approve",
    )
    RuleEvolutionGovernor(db_path).set_status(suggestion["suggestion_id"], "approved", "manual approve")
    applied = service.activate_template(
        factor_id="ema_slope",
        template_id=template["template_id"],
        suggestion_id=suggestion["suggestion_id"],
        note="apply ema slope template",
    )
    cfg = rc.shared()

    assert boundary["recommended_scope"] == "online_light"
    assert suggestion["evidence"]["approval_path"] == "governed_apply_switch"
    assert applied["ok"] is True
    assert cfg.factor_signal_config["ema_slope"]["parameter_template_version"] == "ema_slope_online.v1"
    assert cfg.factor_signal_config["ema_slope"]["parameter_overrides"]["period"] == 18
    assert cfg.factor_signal_config["ema_slope"]["parameter_overrides"]["lookback"] == 4


def test_runtime_tunable_stoch_template_is_online_light(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    service = ParameterTemplateService(db_path)
    template = service.upsert_template(
        {
            "factor_id": "stoch_k",
            "regime_key": "",
            "factor_family": "momentum_oscillator",
            "template_version": "stoch_online.v1",
            "template_role": "aggressive",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"k_length": 10, "smooth_k": 3, "smooth_d": 3},
            "applicable_regimes": ["range", "breakout"],
            "avoid_regimes": ["strong_trend"],
            "holding_profile_hint": {"style": "fast_reversion", "min_bars": 1, "max_bars": 8},
            "evidence": {"note": "stoch online"},
        },
        source="manual",
    )

    boundary = service.assess_template_change(
        factor_id="stoch_k",
        target_template_id=template["template_id"],
    )
    suggestion = service.create_switch_suggestion(
        factor_id="stoch_k",
        template_id=template["template_id"],
        note="stoch boundary path",
    )

    assert boundary["recommended_scope"] == "online_light"
    assert "fits_runtime_guardrail" in boundary["reasons"]
    assert suggestion["evidence"]["boundary"]["recommended_scope"] == "online_light"


def test_runtime_tunable_bb_width_template_is_online_light_and_syncs_runtime_config(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    rc.reset_for_tests()
    _seed_factor_card_state(db_path)

    service = ParameterTemplateService(db_path)
    template = service.upsert_template(
        {
            "factor_id": "bb_width",
            "regime_key": "",
            "factor_family": "volatility",
            "template_version": "bb_online.v1",
            "template_role": "conservative",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 22, "stddev": 2.4},
            "applicable_regimes": ["high_vol", "breakout"],
            "avoid_regimes": ["range"],
            "holding_profile_hint": {"style": "confirmation", "min_bars": 2, "max_bars": 8},
            "evidence": {"note": "bb online"},
        },
        source="manual",
    )

    boundary = service.assess_template_change(
        factor_id="bb_width",
        target_template_id=template["template_id"],
    )
    suggestion = service.create_switch_suggestion(
        factor_id="bb_width",
        template_id=template["template_id"],
        note="bb approve",
    )
    RuleEvolutionGovernor(db_path).set_status(suggestion["suggestion_id"], "approved", "manual approve")
    applied = service.activate_template(
        factor_id="bb_width",
        template_id=template["template_id"],
        suggestion_id=suggestion["suggestion_id"],
        note="apply bb template",
    )
    cfg = rc.shared()

    assert boundary["recommended_scope"] == "online_light"
    assert suggestion["evidence"]["approval_path"] == "governed_apply_switch"
    assert applied["ok"] is True
    assert cfg.factor_signal_config["bb_width"]["parameter_template_version"] == "bb_online.v1"
    assert cfg.factor_signal_config["bb_width"]["parameter_overrides"]["length"] == 22
    assert cfg.factor_signal_config["bb_width"]["parameter_overrides"]["stddev"] == 2.4


def test_runtime_tunable_obv_template_is_online_light(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    service = ParameterTemplateService(db_path)
    template = service.upsert_template(
        {
            "factor_id": "obv_slope",
            "regime_key": "",
            "factor_family": "volume",
            "template_version": "obv_online.v1",
            "template_role": "conservative",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"lookback": 14},
            "applicable_regimes": ["breakout", "high_vol"],
            "avoid_regimes": ["low_vol"],
            "holding_profile_hint": {"style": "confirmation", "min_bars": 2, "max_bars": 10},
            "evidence": {"note": "obv online"},
        },
        source="manual",
    )

    boundary = service.assess_template_change(
        factor_id="obv_slope",
        target_template_id=template["template_id"],
    )
    suggestion = service.create_switch_suggestion(
        factor_id="obv_slope",
        template_id=template["template_id"],
        note="obv boundary path",
    )

    assert boundary["recommended_scope"] == "online_light"
    assert "fits_runtime_guardrail" in boundary["reasons"]
    assert suggestion["evidence"]["boundary"]["recommended_scope"] == "online_light"


def test_runtime_tunable_vol_ma_ratio_template_is_online_light_and_syncs_runtime_config(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    rc.reset_for_tests()
    _seed_factor_card_state(db_path)

    service = ParameterTemplateService(db_path)
    template = service.upsert_template(
        {
            "factor_id": "vol_ma_ratio",
            "regime_key": "",
            "factor_family": "volume",
            "template_version": "vol_ma_online.v1",
            "template_role": "aggressive",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"period": 12},
            "applicable_regimes": ["breakout", "high_vol"],
            "avoid_regimes": ["low_vol"],
            "holding_profile_hint": {"style": "confirmation", "min_bars": 1, "max_bars": 8},
            "evidence": {"note": "vol ma online"},
        },
        source="manual",
    )

    suggestion = service.create_switch_suggestion(
        factor_id="vol_ma_ratio",
        template_id=template["template_id"],
        note="vol ma approve",
    )
    RuleEvolutionGovernor(db_path).set_status(suggestion["suggestion_id"], "approved", "manual approve")
    applied = service.activate_template(
        factor_id="vol_ma_ratio",
        template_id=template["template_id"],
        suggestion_id=suggestion["suggestion_id"],
        note="apply vol ma template",
    )
    cfg = rc.shared()

    assert applied["ok"] is True
    assert applied["boundary"]["recommended_scope"] == "online_light"
    assert cfg.factor_signal_config["vol_ma_ratio"]["parameter_template_version"] == "vol_ma_online.v1"
    assert cfg.factor_signal_config["vol_ma_ratio"]["parameter_overrides"]["period"] == 12


def test_parameter_template_switch_suggestion_carries_boundary_and_blocks_offline_direct_apply(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    service = ParameterTemplateService(db_path)
    offline_template = service.upsert_template(
        {
            "factor_id": "bb_width",
            "regime_key": "",
            "factor_family": "volatility",
            "template_version": "bb_custom_guarded.v1",
            "template_role": "experimental",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 40, "stddev": 3},
            "applicable_regimes": ["high_vol"],
            "avoid_regimes": ["range"],
            "holding_profile_hint": {"style": "confirmation", "min_bars": 2, "max_bars": 8},
            "evidence": {"note": "unsupported role boundary guard"},
        },
        source="manual",
    )

    suggestion = service.create_switch_suggestion(
        factor_id="bb_width",
        template_id=offline_template["template_id"],
        note="should require offline validation",
    )
    RuleEvolutionGovernor(db_path).set_status(suggestion["suggestion_id"], "approved", "approve guarded switch")
    blocked = service.activate_template(
        factor_id="bb_width",
        template_id=offline_template["template_id"],
        suggestion_id=suggestion["suggestion_id"],
        note="try direct apply",
    )

    assert suggestion["boundary"]["recommended_scope"] == "offline_deep"
    assert suggestion["evidence"]["approval_path"] == "offline_validation_then_gray_release"
    assert "unsupported_template_role" in suggestion["evidence"]["boundary"]["reasons"]
    assert blocked["ok"] is False
    assert blocked["blocked"] is True
    assert blocked["error"] == "offline_deep_validation_required"
    assert blocked["boundary"]["recommended_scope"] == "offline_deep"


def test_parameter_template_offline_validation_runs_backtest_and_returns_plan(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    class BoundParameterTemplateService(ParameterTemplateService):
        def __init__(self):
            super().__init__(db_path)

    monkeypatch.setattr(
        "backend.services.parameter_template_validation.ParameterTemplateService",
        BoundParameterTemplateService,
    )
    monkeypatch.setattr(
        "backend.services.parameter_template_validation.run_backtest",
        lambda params, cb: {
            **_valid_parameter_template_research_evidence(),
            "engine": "live_parity_replay_v1",
            "metrics": {"bar_count": 260, "independent_trade_count": 4},
            "note": f"mocked {params['symbol']} {params['timeframe']}",
        },
    )
    monkeypatch.setattr(
        "backend.services.parameter_template_validation.MonthlyPITBarLoader.load",
        lambda _self, _request: (pd.DataFrame(
            {
                "open": [100 + i for i in range(260)],
                "high": [101 + i for i in range(260)],
                "low": [99 + i for i in range(260)],
                "close": [100 + i + (0.3 if i % 2 == 0 else -0.1) for i in range(260)],
                "volume": [1.0] * 260,
            }
        ), {"source": "fixture"}),
    )

    service = BoundParameterTemplateService()
    offline_template = service.upsert_template(
        {
            "factor_id": "bb_width",
            "regime_key": "",
            "factor_family": "volatility",
            "template_version": "bb_custom.v2",
            "template_role": "experimental",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 40, "stddev": 3},
            "applicable_regimes": ["high_vol"],
            "avoid_regimes": ["range"],
            "holding_profile_hint": {"style": "confirmation", "min_bars": 2, "max_bars": 8},
            "evidence": {"note": "offline validation unsupported role"},
        },
        source="manual",
    )

    events: list[tuple[str, float, str]] = []
    result = run_parameter_template_offline_validation(
        {
            "factor_id": "bb_width",
            "template_id": offline_template["template_id"],
            "symbol": "XAUUSD+",
            "timeframe": "M15",
            "recommendation_context": {
                "source": "parameter_template_recommendation",
                "recommendation_id": "ptr_test_bb_width",
                "responsibility": {
                    "primary_responsibility": "parameter",
                    "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
                },
            },
        },
        lambda step, pct, msg: events.append((step, pct, msg)),
    )

    assert result["ok"] is True
    assert result["mode"] == "offline_deep"
    assert result["boundary"]["recommended_scope"] == "offline_deep"
    assert "unsupported_template_role" in result["boundary"]["reasons"]
    assert result["validation_plan"][1]["stage"] == "parity_backtest"
    assert result["validation_plan"][1]["status"] == "completed"
    assert result["validation_plan"][2]["stage"] == "walk_forward_review"
    assert result["validation_plan"][2]["status"] == "completed"
    assert result["validation_plan"][3]["stage"] == "gray_release_review"
    assert result["validation_plan"][3]["status"] == "completed"
    assert result["backtest"]["metrics"]["bar_count"] == 260
    assert "candidate_summary" in result["walk_forward"]
    assert result["release_candidate"]["status"] == "pending_review"
    assert result["release_candidate"]["validation_summary"]["recommendation_source"]["recommendation_id"] == "ptr_test_bb_width"
    assert result["release_candidate"]["validation_summary"]["template_snapshot"]["template_id"] == offline_template["template_id"]
    assert Path(result["report_path"]).exists()
    assert events[0][0] == "planning"


def test_parameter_template_offline_validation_endpoint_submits_job(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    class BoundParameterTemplateService(ParameterTemplateService):
        def __init__(self):
            super().__init__(db_path)

    monkeypatch.setattr(learning_api, "ParameterTemplateService", BoundParameterTemplateService)

    captured: dict[str, object] = {}

    class FakeJob:
        id = "job_ptv_1"
        status = "queued"

    class FakeJobManager:
        def submit(self, kind, params, fn):
            captured["kind"] = kind
            captured["params"] = params
            captured["callable"] = fn
            return FakeJob()

    service = BoundParameterTemplateService()
    offline_template = service.upsert_template(
        {
            "factor_id": "bb_width",
            "regime_key": "",
            "factor_family": "volatility",
            "template_version": "bb_custom.v3",
            "template_role": "experimental",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 44, "stddev": 3},
            "applicable_regimes": ["high_vol"],
            "avoid_regimes": ["range"],
            "holding_profile_hint": {"style": "confirmation", "min_bars": 2, "max_bars": 8},
            "evidence": {"note": "offline endpoint unsupported role"},
        },
        source="manual",
    )
    monkeypatch.setattr(learning_api, "get_job_manager", lambda: FakeJobManager())

    result = learning_api.submit_parameter_template_offline_validation(
        None,
        learning_api.ParameterTemplateOfflineValidationRequest(
            factor_id="bb_width",
            template_id=offline_template["template_id"],
            symbol="XAUUSD+",
            timeframe="M15",
        ),
    )

    assert result["ok"] is True
    assert result["job_id"] == "job_ptv_1"
    assert result["boundary"]["recommended_scope"] == "offline_deep"
    assert "unsupported_template_role" in result["boundary"]["reasons"]
    assert captured["kind"] == "parameter_template_validation"
    assert captured["params"]["factor_id"] == "bb_width"


def test_parameter_template_offline_candidates_endpoint_lists_release_candidates(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    service = ParameterTemplateValidationService(db_path)
    service.register_release_candidate(
        factor_id="bb_width",
        template_id="bb_width:bb_custom.v4:default",
        regime_key="",
        boundary={"recommended_scope": "offline_deep", "reasons": ["factor_not_runtime_tunable"]},
        walk_forward={
            "passed": True,
            "candidate_summary": {"avg_ic": 0.12, "avg_directional_accuracy": 0.55},
            "baseline_summary": {"avg_ic": 0.08, "avg_directional_accuracy": 0.52},
            "config": {"n_folds": 3},
        },
        validation_report_path=str(tmp_path / "report.json"),
        research_evidence=_valid_parameter_template_research_evidence(),
        recommendation_context={
            "source": "parameter_template_recommendation",
            "recommendation_id": "ptr_candidate_list",
            "responsibility": {
                "primary_responsibility": "parameter",
                "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
            },
            "approval_path": "offline_validation_then_gray_release",
        },
    )

    class BoundValidationService(ParameterTemplateValidationService):
        def __init__(self):
            super().__init__(db_path)

    monkeypatch.setattr(learning_api, "ParameterTemplateValidationService", BoundValidationService)

    result = learning_api.list_parameter_template_offline_candidates(None, factor_id="bb_width", limit=10)

    assert len(result["items"]) == 1
    assert result["items"][0]["factor_id"] == "bb_width"
    assert result["items"][0]["status"] == "pending_review"
    assert result["items"][0]["governance"]["status_label"] == "待审候选"
    assert result["items"][0]["governance"]["stage_tone"] == "warning"
    assert result["items"][0]["governance"]["source_summary"] == "来源推荐 ptr_candidate_list · 参数问题"
    assert result["items"][0]["governance"]["approval_path_text"] == "先离线验证再灰度发布"
    assert result["items"][0]["governance"]["evidence_display"] == "Walk-forward IC 0.120，基线 0.080，Δ +0.040"
    assert result["items"][0]["governance"]["review_display"] == "等待系统规则审核"
    assert result["items"][0]["governance"]["deployment_display"] == "尚未发布"
    assert result["items"][0]["governance"]["rollback_display"] == ""
    assert result["items"][0]["governance"]["action_buttons"] == [
        {"key": "approve", "label": "批准候选", "tone": "primary", "disabled": False},
        {"key": "reject", "label": "拒绝候选", "tone": "secondary", "disabled": False},
    ]


def test_parameter_template_release_candidate_review_release_and_rollback(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    rc.reset_for_tests()
    _seed_factor_card_state(db_path)

    template_service = ParameterTemplateService(db_path)
    conservative = template_service.upsert_template(
        {
            "factor_id": "rsi_14",
            "regime_key": "",
            "factor_family": "momentum_oscillator",
            "template_version": "candidate_conservative.v1",
            "template_role": "conservative",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 18, "upper_band": 72, "lower_band": 28},
            "applicable_regimes": ["range"],
            "avoid_regimes": ["strong_trend"],
            "holding_profile_hint": {"style": "short_swing", "min_bars": 2, "max_bars": 10},
            "evidence": {"note": "candidate conservative"},
        },
        source="manual",
    )
    template_service.activate_template(
        factor_id="rsi_14",
        template_id="rsi_14:default.v1:default",
        regime_key="",
        note="seed default active",
    )

    validation_service = ParameterTemplateValidationService(db_path)
    candidate = validation_service.register_release_candidate(
        factor_id="rsi_14",
        template_id=conservative["template_id"],
        regime_key="",
        boundary={"recommended_scope": "offline_deep", "reasons": ["parameter_delta_too_large"]},
        walk_forward={
            "passed": True,
            "candidate_summary": {"avg_ic": 0.12, "avg_directional_accuracy": 0.56},
            "baseline_summary": {"avg_ic": 0.08, "avg_directional_accuracy": 0.52},
            "config": {"n_folds": 3},
        },
        validation_report_path=str(tmp_path / "report.json"),
        research_evidence=_valid_parameter_template_research_evidence(),
    )

    reviewed = validation_service.review_release_candidate(
        candidate_id=candidate["candidate_id"],
        status="approved",
        note="looks good",
    )
    deployed = validation_service.deploy_release_candidate(
        candidate_id=candidate["candidate_id"],
        note="deploy candidate",
    )
    rolled_back = validation_service.rollback_release_candidate(
        candidate_id=candidate["candidate_id"],
        note="rollback candidate",
    )

    active = template_service.get_active_template(factor_id="rsi_14", regime_key="")

    assert reviewed["status"] == "approved"
    assert deployed["ok"] is True
    assert deployed["candidate"]["status"] == "deployed"
    assert deployed["release_result"]["new_template_id"] == conservative["template_id"]
    assert rolled_back["ok"] is True
    assert rolled_back["candidate"]["status"] == "rolled_back"
    assert active["template_id"] == "rsi_14:default.v1:default"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        events = conn.execute(
            """
            SELECT event, status, source
            FROM lifecycle_events
            WHERE factor=?
            ORDER BY id ASC
            """,
            ("rsi_14",),
        ).fetchall()
    finally:
        conn.close()

    assert [row["event"] for row in events[-4:]] == [
        "parameter_template_candidate_registered",
        "parameter_template_candidate_reviewed",
        "parameter_template_candidate_deployed",
        "parameter_template_candidate_rolled_back",
    ]
    assert [row["status"] for row in events[-4:]] == [
        "pending_review",
        "approved",
        "deployed",
        "rolled_back",
    ]
    assert all(row["source"] == "parameter_template" for row in events[-4:])


def test_parameter_template_legacy_candidate_requires_revalidation_before_deploy(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    rc.reset_for_tests()
    _seed_factor_card_state(db_path)

    template_service = ParameterTemplateService(db_path)
    validation_service = ParameterTemplateValidationService(db_path)
    template_service.activate_template(
        factor_id="rsi_14",
        template_id="rsi_14:default.v1:default",
        regime_key="",
        note="seed default active",
    )
    snapshot = {
        "factor_id": "rsi_14",
        "regime_key": "",
        "factor_family": "momentum_oscillator",
        "template_version": "candidate_snapshot.v1",
        "template_role": "conservative",
        "formula_version": "registry_builtin.v1",
        "base_parameter_version": "default.v1",
        "parameters": {"length": 18, "upper_band": 72, "lower_band": 28},
        "applicable_regimes": ["range"],
        "avoid_regimes": ["strong_trend"],
        "holding_profile_hint": {"style": "short_swing", "min_bars": 2, "max_bars": 10},
        "evidence": {"note": "legacy release candidate snapshot"},
    }
    template_id = "rsi_14:candidate_snapshot.v1:default"
    assert template_service.get_template(template_id=template_id) is None

    now = time.time()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO parameter_template_release_candidate
            (candidate_id, factor_id, template_id, regime_key, status,
             boundary_json, validation_summary_json, validation_report_path,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, 'approved', ?, ?, ?, ?, ?)
            """,
            (
                "ptrc_legacy_snapshot",
                "rsi_14",
                template_id,
                "",
                json.dumps(
                    {
                        "recommended_scope": "offline_deep",
                        "reasons": ["parameter_delta_too_large"],
                        "target_template": {**snapshot, "template_id": template_id},
                    }
                ),
                json.dumps({"walk_forward_passed": True, "candidate_avg_ic": 0.11}),
                str(tmp_path / "legacy_report.json"),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    legacy = validation_service.get_release_candidate("ptrc_legacy_snapshot")
    assert legacy is not None
    assert legacy["status"] == "legacy_quarantined"
    assert legacy["require_revalidation"] is True
    with pytest.raises(ResearchEvidenceRejected):
        validation_service.deploy_release_candidate(
            candidate_id="ptrc_legacy_snapshot",
            note="deploy legacy snapshot",
        )
    assert template_service.get_template(template_id=template_id) is None

    refreshed = validation_service.register_release_candidate(
        factor_id="rsi_14",
        template_id=template_id,
        regime_key="",
        boundary={
            "recommended_scope": "offline_deep",
            "reasons": ["parameter_delta_too_large"],
            "target_template": {**snapshot, "template_id": template_id},
        },
        walk_forward={
            "passed": True,
            "candidate_summary": {"avg_ic": 0.11},
            "baseline_summary": {"avg_ic": 0.08},
            "config": {"n_folds": 3},
        },
        validation_report_path=str(tmp_path / "verified_report.json"),
        template_snapshot={**snapshot, "template_id": template_id},
        research_evidence=_valid_parameter_template_research_evidence(),
    )
    assert refreshed["candidate_id"] == "ptrc_legacy_snapshot"
    assert refreshed["status"] == "pending_review"
    validation_service.review_release_candidate(
        candidate_id="ptrc_legacy_snapshot",
        status="approved",
        note="revalidated",
    )
    deployed = validation_service.deploy_release_candidate(
        candidate_id="ptrc_legacy_snapshot",
        note="deploy revalidated snapshot",
    )
    materialized = template_service.get_template(template_id=template_id)

    assert deployed["ok"] is True
    assert deployed["release_result"]["new_template_id"] == template_id
    assert materialized is not None
    assert materialized["source"] == "offline_validation_candidate"
    assert materialized["parameters"]["length"] == 18


def test_parameter_template_release_candidate_api_endpoints_work(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    rc.reset_for_tests()
    _seed_factor_card_state(db_path)

    class BoundParameterTemplateValidationService(ParameterTemplateValidationService):
        def __init__(self):
            super().__init__(db_path)

    class BoundParameterTemplateService(ParameterTemplateService):
        def __init__(self):
            super().__init__(db_path)

    monkeypatch.setattr(learning_api, "ParameterTemplateValidationService", BoundParameterTemplateValidationService)
    monkeypatch.setattr(learning_api, "ParameterTemplateService", BoundParameterTemplateService)

    template_service = BoundParameterTemplateService()
    candidate_template = template_service.upsert_template(
        {
            "factor_id": "rsi_14",
            "regime_key": "",
            "factor_family": "momentum_oscillator",
            "template_version": "candidate_api.v1",
            "template_role": "conservative",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 17, "upper_band": 71, "lower_band": 29},
            "applicable_regimes": ["range"],
            "avoid_regimes": ["strong_trend"],
            "holding_profile_hint": {"style": "short_swing", "min_bars": 2, "max_bars": 10},
            "evidence": {"note": "candidate api"},
        },
        source="manual",
    )
    template_service.activate_template(
        factor_id="rsi_14",
        template_id="rsi_14:default.v1:default",
        regime_key="",
        note="seed default active api",
    )
    validation_service = BoundParameterTemplateValidationService()
    candidate = validation_service.register_release_candidate(
        factor_id="rsi_14",
        template_id=candidate_template["template_id"],
        regime_key="",
        boundary={"recommended_scope": "offline_deep", "reasons": ["parameter_delta_too_large"]},
        walk_forward={
            "passed": True,
            "candidate_summary": {"avg_ic": 0.15, "avg_directional_accuracy": 0.58},
            "baseline_summary": {"avg_ic": 0.07, "avg_directional_accuracy": 0.51},
            "config": {"n_folds": 3},
        },
        validation_report_path=str(tmp_path / "report_api.json"),
        research_evidence=_valid_parameter_template_research_evidence(),
    )

    reviewed = learning_api.review_parameter_template_offline_candidate(
        None,
        learning_api.ParameterTemplateOfflineCandidateReviewRequest(
            candidate_id=candidate["candidate_id"],
            status="approved",
            note="approve api candidate",
        ),
    )
    released = learning_api.release_parameter_template_offline_candidate(
        None,
        learning_api.ParameterTemplateOfflineCandidateActionRequest(
            candidate_id=candidate["candidate_id"],
            note="release api candidate",
        ),
    )
    rolled_back = learning_api.rollback_parameter_template_offline_candidate(
        None,
        learning_api.ParameterTemplateOfflineCandidateActionRequest(
            candidate_id=candidate["candidate_id"],
            note="rollback api candidate",
        ),
    )

    assert reviewed["ok"] is True
    assert reviewed["item"]["status"] == "approved"
    assert reviewed["result_label"] == "已批准候选"
    assert "进入灰度发布" in reviewed["result_summary"]
    assert released["ok"] is True
    assert released["candidate"]["status"] == "deployed"
    assert released["result_label"] == "已执行发布"
    assert "观察后验效果" in released["result_summary"]
    assert rolled_back["ok"] is True
    assert rolled_back["candidate"]["status"] == "rolled_back"
    assert rolled_back["result_label"] == "已执行回滚"
    assert "复核回滚原因" in rolled_back["result_summary"]


def test_learning_summary_includes_parameter_template_candidate_stats(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    validation_service = ParameterTemplateValidationService(db_path)
    validation_service.register_release_candidate(
        factor_id="rsi_14",
        template_id="rsi_14:candidate_summary.v1:default",
        regime_key="",
        boundary={"recommended_scope": "offline_deep", "reasons": ["parameter_delta_too_large"]},
        walk_forward={
            "passed": True,
            "candidate_summary": {"avg_ic": 0.11, "avg_directional_accuracy": 0.57},
            "baseline_summary": {"avg_ic": 0.07, "avg_directional_accuracy": 0.51},
            "config": {"n_folds": 3},
        },
        validation_report_path=str(tmp_path / "summary_report.json"),
        recommendation_context={"source": "parameter_template_recommendation", "recommendation_id": "ptr_summary_rsi"},
        research_evidence=_valid_parameter_template_research_evidence(),
    )
    validation_service.register_release_candidate(
        factor_id="adx",
        template_id="adx:candidate_summary.v1:default",
        regime_key="",
        boundary={"recommended_scope": "offline_deep", "reasons": ["parameter_delta_too_large"]},
        walk_forward={
            "passed": True,
            "candidate_summary": {"avg_ic": 0.09, "avg_directional_accuracy": 0.55},
            "baseline_summary": {"avg_ic": 0.06, "avg_directional_accuracy": 0.5},
            "config": {"n_folds": 3},
        },
        validation_report_path=str(tmp_path / "summary_report_2.json"),
        recommendation_context={"source": "parameter_template_recommendation", "recommendation_id": "ptr_summary_adx"},
        research_evidence=_valid_parameter_template_research_evidence(),
    )

    class BoundValidationService(ParameterTemplateValidationService):
        def __init__(self):
            super().__init__(db_path)

    reviewed = BoundValidationService().list_release_candidates(limit=10)[0]
    BoundValidationService().review_release_candidate(
        candidate_id=reviewed["candidate_id"],
        status="approved",
        note="approve for summary",
    )
    learning_api._LEARNING_CACHE.clear()
    learning_api._LEARNING_LAST_GOOD.clear()
    ParameterTemplateService(db_path).list_recommendations(limit=20)
    import backend.core.db as core_db

    original_get_state_conn = core_db.get_state_conn
    original_state_db = core_db.STATE_DB
    core_db.STATE_DB = Path(db_path)

    def _temp_conn():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    core_db.get_state_conn = _temp_conn
    try:
        summary = learning_api.get_learning_summary(None)
    finally:
        core_db.get_state_conn = original_get_state_conn
        core_db.STATE_DB = original_state_db

    assert summary["parameter_template_candidates"]["pending_review"] == 1
    assert summary["parameter_template_candidates"]["approved"] == 1
    assert summary["parameter_template_recommendations"]["total"] >= 1
    assert summary["parameter_template_todo"]["stage_tone"] == "positive"
    assert summary["parameter_template_todo"]["entry_type"] == "candidate"
    assert summary["parameter_template_overview"]["headline"]["label"] == "待审候选"
    assert summary["parameter_template_overview"]["headline"]["tone"] == "warning"
    assert "优先等待系统规则审核" in summary["parameter_template_overview"]["headline"]["summary"]
    assert summary["parameter_template_overview"]["pending_candidate_hint"]["action_label"] == "去审候选"
    assert summary["parameter_template_overview"]["online_light_hint"]["action_label"] == "去审建议"
    assert summary["parameter_template_overview"]["offline_deep_hint"] is None
    assert summary["parameter_template_empty_states"]["offline_candidates"] == "还没有参数模板候选"
    assert summary["parameter_template_empty_states"]["lifecycle"] == "还没有参数治理轨迹"
    assert summary["parameter_template_empty_states"]["recommendations"] == "还没有参数模板建议"
    assert [item["id"] for item in summary["parameter_template_task_cards"]] == [
        "template",
        "template-lifecycle",
        "template-reco",
    ]
    template_card = summary["parameter_template_task_cards"][0]
    assert template_card["title"] == "模板候选"
    assert summary["parameter_template_todo"]["action_label"] in template_card["note"]
    assert template_card["tone"] == summary["parameter_template_todo"]["stage_tone"]
    assert summary["parameter_template_task_cards"][1]["title"] == "治理轨迹"
    assert summary["parameter_template_task_cards"][2]["title"] == "参数模板建议"
    offline_overview = learning_api._build_parameter_template_overview(
        suggestion_counts={},
        first_pending_candidate=None,
        first_online_recommendation=None,
        first_offline_recommendation={
            "factor_id": "rsi_14",
            "recommendation_id": "ptr_offline_hint",
            "boundary": {"recommended_scope": "offline_deep"},
        },
    )
    assert offline_overview["offline_deep_hint"]["action_label"] == "去做验证"
    assert summary["latest_parameter_template_candidate"]["candidate_id"]
    assert summary["latest_parameter_template_candidate"]["validation_summary"]["fold_count"] == 3
    assert summary["latest_parameter_template_candidate"]["validation_summary"]["recommendation_source"]["recommendation_id"] in {"ptr_summary_rsi", "ptr_summary_adx"}
    assert summary["latest_parameter_template_candidate_trace"]["recommendation_id"] in {"ptr_summary_rsi", "ptr_summary_adx"}
    assert "trace_locator" in summary["latest_parameter_template_candidate_trace"]
    assert "参数治理最新进展" in summary["parameter_template_ops_summary"]
    assert "在线" in summary["parameter_template_ops_summary"]
    assert summary["latest_parameter_template_recommendation"]["factor_id"] == "rsi_14"
    assert summary["stale"] is False
    assert summary["recommendations_source"] == "cache"


def test_learning_summary_returns_last_good_when_state_db_locked(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)
    learning_api._LEARNING_CACHE.clear()
    learning_api._LEARNING_LAST_GOOD.clear()

    import backend.core.db as core_db

    monkeypatch.setattr(core_db, "STATE_DB", Path(db_path))
    good_summary = learning_api.get_learning_summary(None)
    assert good_summary["stale"] is False
    learning_api._LEARNING_CACHE.clear()

    def _locked_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(core_db, "connect_sqlite", _locked_connect)
    stale_summary = learning_api.get_learning_summary(None)

    assert stale_summary["stale"] is True
    assert stale_summary["stale_reason"] == "database_locked"
    assert stale_summary["suggestions"] == good_summary["suggestions"]
    assert stale_summary["applications"] == good_summary["applications"]


def test_parameter_template_release_candidate_registration_is_idempotent(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)
    validation_service = ParameterTemplateValidationService(db_path)
    kwargs = {
        "factor_id": "bb_width",
        "template_id": "bb_width:bb_custom.v2:default",
        "regime_key": "",
        "boundary": {"recommended_scope": "offline_deep"},
        "walk_forward": {
            "passed": True,
            "candidate_summary": {"avg_ic": 0.04, "avg_directional_accuracy": 0.6},
            "baseline_summary": {"avg_ic": -0.01, "avg_directional_accuracy": 0.52},
            "config": {"n_folds": 3},
        },
        "validation_report_path": str(tmp_path / "report.json"),
        "recommendation_context": {
            "source": "parameter_template_recommendation",
            "recommendation_id": "ptr_test_bb_width",
        },
    }

    first = validation_service.register_release_candidate(**kwargs)
    second = validation_service.register_release_candidate(**kwargs)
    items = validation_service.list_release_candidates(limit=10)

    assert second["candidate_id"] == first["candidate_id"]
    assert [
        item for item in items
        if item["factor_id"] == "bb_width" and item["template_id"] == "bb_width:bb_custom.v2:default"
    ] == [first]


def test_factor_card_template_state_reflects_release_candidate_status(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    rc.reset_for_tests()
    _seed_factor_card_state(db_path)

    template_service = ParameterTemplateService(db_path)
    candidate_template = template_service.upsert_template(
        {
            "factor_id": "rsi_14",
            "regime_key": "",
            "factor_family": "momentum_oscillator",
            "template_version": "candidate_card_state.v1",
            "template_role": "conservative",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 18, "upper_band": 72, "lower_band": 28},
            "applicable_regimes": ["range"],
            "avoid_regimes": ["strong_trend"],
            "holding_profile_hint": {"style": "short_swing", "min_bars": 2, "max_bars": 10},
            "evidence": {"note": "candidate card state"},
        },
        source="manual",
    )
    template_service.activate_template(
        factor_id="rsi_14",
        template_id="rsi_14:default.v1:default",
        regime_key="",
        note="seed default active for factor card",
    )
    validation_service = ParameterTemplateValidationService(db_path)
    candidate = validation_service.register_release_candidate(
        factor_id="rsi_14",
        template_id=candidate_template["template_id"],
        regime_key="",
        boundary={"recommended_scope": "offline_deep", "reasons": ["parameter_delta_too_large"]},
        walk_forward={
            "passed": True,
            "candidate_summary": {"avg_ic": 0.13, "avg_directional_accuracy": 0.57},
            "baseline_summary": {"avg_ic": 0.08, "avg_directional_accuracy": 0.52},
            "config": {"n_folds": 3},
        },
        validation_report_path=str(tmp_path / "card_state_report.json"),
        research_evidence=_valid_parameter_template_research_evidence(),
    )

    service = FactorCardService(db_path)
    pending_card = service.list_cards(factor_id="rsi_14", limit=1)[0]
    assert pending_card["governance_state"]["template_state"] == "review_pending"

    validation_service.review_release_candidate(
        candidate_id=candidate["candidate_id"],
        status="approved",
        note="approve for factor card state",
    )
    approved_card = service.list_cards(factor_id="rsi_14", limit=1)[0]
    assert approved_card["governance_state"]["template_state"] == "review_approved"

    validation_service.deploy_release_candidate(
        candidate_id=candidate["candidate_id"],
        note="deploy for factor card state",
    )
    deployed_card = service.list_cards(factor_id="rsi_14", limit=1)[0]
    assert deployed_card["governance_state"]["template_state"] == "deployed"

    validation_service.rollback_release_candidate(
        candidate_id=candidate["candidate_id"],
        note="rollback for factor card state",
    )
    rolled_back_card = service.list_cards(factor_id="rsi_14", limit=1)[0]
    assert rolled_back_card["governance_state"]["template_state"] == "rolled_back"


def test_factor_card_governance_state_includes_candidate_trace(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    rc.reset_for_tests()
    _seed_factor_card_state(db_path)

    template_service = ParameterTemplateService(db_path)
    candidate_template = template_service.upsert_template(
        {
            "factor_id": "rsi_14",
            "regime_key": "",
            "factor_family": "momentum_oscillator",
            "template_version": "candidate_trace.v1",
            "template_role": "conservative",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 18, "upper_band": 72, "lower_band": 28},
            "applicable_regimes": ["range"],
            "avoid_regimes": ["strong_trend"],
            "holding_profile_hint": {"style": "short_swing", "min_bars": 2, "max_bars": 10},
            "evidence": {"note": "candidate trace"},
        },
        source="manual",
    )
    validation_service = ParameterTemplateValidationService(db_path)
    validation_service.register_release_candidate(
        factor_id="rsi_14",
        template_id=candidate_template["template_id"],
        regime_key="",
        boundary={"recommended_scope": "offline_deep", "reasons": ["parameter_delta_too_large"]},
        walk_forward={
            "passed": True,
            "candidate_summary": {"avg_ic": 0.13, "avg_directional_accuracy": 0.57},
            "baseline_summary": {"avg_ic": 0.08, "avg_directional_accuracy": 0.52},
            "config": {"n_folds": 3},
        },
        validation_report_path=str(tmp_path / "candidate_trace_report.json"),
        research_evidence=_valid_parameter_template_research_evidence(),
        recommendation_context={
            "source": "parameter_template_recommendation",
            "recommendation_id": "ptr_factor_card_trace",
            "reason": "factor logic looks usable but current parameters appear mismatched",
            "responsibility": {
                "primary_responsibility": "parameter",
                "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
            },
            "approval_path": "offline_validation_then_gray_release",
        },
    )

    card = FactorCardService(db_path).list_cards(factor_id="rsi_14", limit=1)[0]

    assert card["governance_state"]["latest_template_candidate_trace"]["recommendation_id"] == "ptr_factor_card_trace"
    assert card["governance_state"]["latest_template_candidate_trace"]["primary_responsibility"] == "parameter"
    assert "factor_logic_ok_but_param_suspect" in card["governance_state"]["latest_template_candidate_trace"]["responsibility_labels"]


def test_learning_lifecycle_includes_parameter_template_candidate_events(tmp_path):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    rc.reset_for_tests()
    _seed_factor_card_state(db_path)

    template_service = ParameterTemplateService(db_path)
    template = template_service.upsert_template(
        {
            "factor_id": "rsi_14",
            "regime_key": "",
            "factor_family": "momentum_oscillator",
            "template_version": "candidate_lifecycle.v1",
            "template_role": "conservative",
            "formula_version": "registry_builtin.v1",
            "base_parameter_version": "default.v1",
            "parameters": {"length": 19, "upper_band": 73, "lower_band": 27},
            "applicable_regimes": ["range"],
            "avoid_regimes": ["strong_trend"],
            "holding_profile_hint": {"style": "short_swing", "min_bars": 2, "max_bars": 10},
            "evidence": {"note": "candidate lifecycle"},
        },
        source="manual",
    )
    template_service.activate_template(
        factor_id="rsi_14",
        template_id="rsi_14:default.v1:default",
        regime_key="",
        note="seed default active lifecycle",
    )
    validation_service = ParameterTemplateValidationService(db_path)
    candidate = validation_service.register_release_candidate(
        factor_id="rsi_14",
        template_id=template["template_id"],
        regime_key="",
        boundary={"recommended_scope": "offline_deep", "reasons": ["parameter_delta_too_large"]},
        walk_forward={
            "passed": True,
            "candidate_summary": {"avg_ic": 0.14, "avg_directional_accuracy": 0.58},
            "baseline_summary": {"avg_ic": 0.08, "avg_directional_accuracy": 0.52},
            "config": {"n_folds": 3},
        },
        validation_report_path=str(tmp_path / "lifecycle_report.json"),
        research_evidence=_valid_parameter_template_research_evidence(),
        recommendation_context={
            "source": "parameter_template_recommendation",
            "recommendation_id": "ptr_lifecycle_rsi",
            "reason": "factor logic looks usable but current parameters appear mismatched",
            "responsibility": {
                "primary_responsibility": "parameter",
                "responsibility_labels": ["factor_logic_ok_but_param_suspect"],
            },
            "approval_path": "offline_validation_then_gray_release",
        },
    )
    validation_service.review_release_candidate(
        candidate_id=candidate["candidate_id"],
        status="approved",
        note="approve lifecycle candidate",
    )

    import backend.core.db as core_db

    original_get_state_conn = core_db.get_state_conn
    original_state_db = core_db.STATE_DB
    core_db.STATE_DB = Path(db_path)

    def _temp_conn():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    core_db.get_state_conn = _temp_conn
    try:
        lifecycle = learning_api.get_lifecycle(None, limit=10)
    finally:
        core_db.get_state_conn = original_get_state_conn
        core_db.STATE_DB = original_state_db

    assert lifecycle["items"]
    assert lifecycle["items"][0]["kind"] == "factor_lifecycle"
    assert lifecycle["items"][0]["source"] == "parameter_template"
    assert lifecycle["items"][0]["event"] == "parameter_template_candidate_reviewed"
    assert lifecycle["items"][1]["event"] == "parameter_template_candidate_registered"
    assert lifecycle["items"][0]["governance"]["status_label"] == "等待发布"
    assert lifecycle["items"][0]["governance"]["stage_tone"] == "positive"
    assert lifecycle["items"][0]["governance"]["action_label"] == "去发布"
    assert lifecycle["items"][0]["governance"]["target_type"] == "模板候选"
    assert lifecycle["items"][0]["governance"]["button_text"] == "查看对应候选"
    assert lifecycle["items"][0]["governance"]["source_summary"] == "来源推荐 ptr_lifecycle_rsi · 参数问题"
    assert lifecycle["items"][0]["governance"]["approval_path_text"] == "先离线验证再灰度发布"
    assert lifecycle["items"][1]["governance"]["status_label"] == "待审候选"
    assert lifecycle["items"][1]["governance"]["stage_tone"] == "warning"
    assert lifecycle["items"][1]["governance"]["action_label"] == "去审候选"
    assert lifecycle["items"][1]["governance"]["button_text"] == "查看对应候选"
    assert lifecycle["items"][1]["governance"]["source_summary"] == "来源推荐 ptr_lifecycle_rsi · 参数问题"
    assert lifecycle["items"][1]["governance"]["approval_path_text"] == "先离线验证再灰度发布"
    assert lifecycle["items"][0]["metrics"]["candidate_trace"]["recommendation_id"] == "ptr_lifecycle_rsi"
    assert lifecycle["items"][1]["metrics"]["candidate_trace"]["responsibility"]["primary_responsibility"] == "parameter"
    assert lifecycle["items"][0]["metrics"]["candidate_trace"]["trace_locator"]["review_id"] == "rev_1"


def test_offline_candidates_endpoint_includes_trace_locator(tmp_path, monkeypatch):
    db_path = str(tmp_path / "state.db")
    reset_shared()
    _seed_factor_card_state(db_path)

    class BoundParameterTemplateValidationService(ParameterTemplateValidationService):
        def __init__(self):
            super().__init__(db_path)

    validation_service = BoundParameterTemplateValidationService()
    validation_service.register_release_candidate(
        factor_id="rsi_14",
        template_id="rsi_14:candidate_trace_link.v1:default",
        regime_key="",
        boundary={"recommended_scope": "offline_deep", "reasons": ["parameter_delta_too_large"]},
        walk_forward={
            "passed": True,
            "candidate_summary": {"avg_ic": 0.1, "avg_directional_accuracy": 0.55},
            "baseline_summary": {"avg_ic": 0.06, "avg_directional_accuracy": 0.5},
            "config": {"n_folds": 3},
        },
        validation_report_path=str(tmp_path / "offline_trace_report.json"),
        research_evidence=_valid_parameter_template_research_evidence(),
    )

    monkeypatch.setattr(learning_api, "ParameterTemplateValidationService", BoundParameterTemplateValidationService)
    import backend.core.db as core_db

    original_get_state_conn = core_db.get_state_conn
    original_state_db = core_db.STATE_DB
    core_db.STATE_DB = Path(db_path)

    def _temp_conn():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    core_db.get_state_conn = _temp_conn
    try:
        result = learning_api.list_parameter_template_offline_candidates(None, factor_id="rsi_14", limit=10)
    finally:
        core_db.get_state_conn = original_get_state_conn
        core_db.STATE_DB = original_state_db

    assert result["items"]
    assert result["items"][0]["trace_locator"]["review_id"] == "rev_1"
    assert result["items"][0]["trace_locator"]["position_id"] == "p1"


def test_apply_position_supervisor_template_switch_requires_approved_suggestion(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO policy_suggestion
            (suggestion_id, scope_type, scope_key, action, confidence, reason,
             evidence_json, status, created_at)
            VALUES (?, 'position_supervisor_template', ?, 'relax_thesis_break',
                    0.8, 'test approved switch', ?, 'approved', ?)
            """,
                (
                    "psv_test_apply",
                    "position_supervisor:conservative.v1",
                    json.dumps(
                        {
                            "replay_summary": {"sample_count": 3},
                            "counterfactual_summary": {"sample_count": 3},
                        },
                        ensure_ascii=False,
                    ),
                    time.time(),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(learning_api, "STATE_DB", db_path)
    rc.reset_for_tests()
    try:
        result = learning_api.apply_position_supervisor_template_switch(
            None,
            learning_api.PositionSupervisorTemplateApplySwitchRequest(
                suggestion_id="psv_test_apply",
                note="pytest apply",
            ),
        )
        assert result["blocked"] is False
        assert result["previous_template_id"] == "position_supervisor:default.v1"
        assert result["target_template_id"] == "position_supervisor:conservative.v1"
        assert rc.shared().position_supervisor_template_id == "position_supervisor:conservative.v1"

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            suggestion = conn.execute("SELECT status FROM policy_suggestion WHERE suggestion_id='psv_test_apply'").fetchone()
            application = conn.execute("SELECT * FROM learning_application_log").fetchone()
            overlay = conn.execute("SELECT overlay_json FROM runtime_config_overlay").fetchone()
        finally:
            conn.close()
        assert suggestion["status"] == "applied"
        assert application["scope_type"] == "position_supervisor_template"
        overlay_json = json.loads(overlay["overlay_json"])
        assert overlay_json["position_supervisor_template_id"] == "position_supervisor:conservative.v1"
    finally:
        rc.reset_for_tests()
