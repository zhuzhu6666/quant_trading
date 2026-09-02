

def test_admission_waives_absent_validation_for_canary_ladder_top():
    """Bar-based candidates never receive enrollment-time validation
    artifacts; a completed canary ladder substitutes for the structurally
    absent cost/execution/contamination/regime/health evidence.  Absent is
    waived, negative still blocks."""
    from backend.services.factor_cards import build_factor_admission_evidence

    def _item(canary_stage="PROBATION", health_status=None, cost_test_passed=None):
        item = {
            "factor_id": "dsl_auto_ladder",
            "role": "alpha",
            "direction": 1,
            "health_rolling_ic": 0.03,
            "lifecycle_status": "SHADOW",
            "lifecycle_generation": 1,
            "lifecycle_artifact_hash": "a" * 64,
            "lifecycle_definition_fingerprint": "b" * 64,
            "lifecycle_config_hash": "c" * 64,
            "canary": {"stage": canary_stage},
            "shadow_perf": {
                "oos_bars": 1200,
                "n_valid": 90,
                "evidence_hash": "d" * 64,
                "dataset_hash": "e" * 64,
            },
        }
        if health_status:
            item["health_status"] = health_status
            item["health_score"] = 30.0
            item["health_updated_at"] = 0.0
        if cost_test_passed is not None:
            item["lifecycle_evidence"] = {
                "candidate_validation": {"cost_test_passed": cost_test_passed}
            }
        return item

    top = build_factor_admission_evidence(
        factor_id="dsl_auto_ladder",
        catalog_item=_item(),
        evidence_counts={},
        governance={},
    )
    assert top["eligible_for_preparation"] is True
    assert top["preflight_blocker_codes"] == []
    assert top["canary"]["evidence_source"] == "canary_ladder"

    shadow_stage = build_factor_admission_evidence(
        factor_id="dsl_auto_ladder",
        catalog_item=_item(canary_stage="SHADOW"),
        evidence_counts={},
        governance={},
    )
    assert "bar_oos_canary_incomplete" in shadow_stage["preflight_blocker_codes"]
    assert "cost_evidence_missing" in shadow_stage["preflight_blocker_codes"]

    decaying = build_factor_admission_evidence(
        factor_id="dsl_auto_ladder",
        catalog_item=_item(health_status="DECAYING"),
        evidence_counts={},
        governance={},
    )
    assert "factor_health_invalid_or_stale" in decaying["preflight_blocker_codes"]

    cost_failed = build_factor_admission_evidence(
        factor_id="dsl_auto_ladder",
        catalog_item=_item(cost_test_passed=False),
        evidence_counts={},
        governance={},
    )
    assert "cost_evidence_missing" in cost_failed["preflight_blocker_codes"]
