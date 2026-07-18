import pytest

from backend.core.static_feature_flags import StaticFeatureFlags


def test_static_flags_use_environment_precedence():
    flags = StaticFeatureFlags.from_sources(
        {
            "features": {
                "live_safety_plane_v2_mode": "off",
                "live_generation_controller_v2_enabled": False,
            }
        },
        {
            "QUANT_LIVE_SAFETY_PLANE_V2_MODE": "shadow",
            "QUANT_LIVE_GENERATION_CONTROLLER_V2_ENABLED": "1",
            "QUANT_CTRADER_EXECUTION_OUTCOME_V2_ENABLED": "true",
            "QUANT_GOVERNANCE_MUTATION_COORDINATOR_V2_MODE": "dual_record",
            "QUANT_PG_JOB_QUEUE_V2_ENABLED": "1",
        },
    )

    assert flags.live_safety_plane_v2_mode == "shadow"
    assert flags.live_generation_controller_v2_enabled is True
    assert flags.ctrader_execution_outcome_v2_enabled is True
    assert flags.governance_mutation_coordinator_v2_mode == "dual_record"
    assert flags.pg_job_queue_v2_enabled is True


def test_pg_job_queue_defaults_off():
    flags = StaticFeatureFlags.from_sources({}, {})

    assert flags.pg_job_queue_v2_enabled is False


def test_invalid_safety_mode_is_rejected_at_load():
    with pytest.raises(ValueError, match="invalid_live_safety_plane_v2_mode"):
        StaticFeatureFlags.from_sources(
            {"features": {"live_safety_plane_v2_mode": "typo"}},
            {},
        )


def test_invalid_governance_coordinator_mode_is_rejected_at_load():
    with pytest.raises(ValueError, match="invalid_governance_mutation_coordinator_v2_mode"):
        StaticFeatureFlags.from_sources(
            {"features": {"governance_mutation_coordinator_v2_mode": "typo"}},
            {},
        )
