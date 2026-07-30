from __future__ import annotations

from types import SimpleNamespace

from backend.services.runtime_factor_selection_projection import (
    RuntimeFactorSelectionProjectionService,
)
from config import runtime_config
from config.runtime_config import RuntimeConfig


def test_runtime_factor_selection_projection_exposes_governance_counts(
    tmp_path,
    monkeypatch,
):
    runtime_config.reset_for_tests()
    runtime_config.replace(
        RuntimeConfig(
            autonomy_mode="demo_autonomous",
            factor_signal_config={
                "alpha_a": {
                    "enabled": True,
                    "lifecycle_status": "ACTIVE",
                    "role": "alpha",
                },
                "context_a": {
                    "enabled": True,
                    "lifecycle_status": "ACTIVE",
                    "role": "context",
                },
                "old_alpha": {
                    "enabled": False,
                    "lifecycle_status": "QUARANTINED",
                    "role": "alpha",
                },
            },
            factor_portfolio_weights={
                "alpha_a": 0.05,
                "context_a": 0.0,
                "old_alpha": 0.0,
            },
        )
    )
    monkeypatch.setattr(
        runtime_config,
        "bounded_demo_mode_active",
        lambda _cfg=None: True,
    )
    selection = SimpleNamespace(
        selected_factor_ids=["alpha_a", "context_a"],
        excluded_factor_ids=["old_alpha"],
        reason_excluded={"old_alpha": "lifecycle_not_live"},
    )
    service = RuntimeFactorSelectionProjectionService(tmp_path / "state.db")

    published = service.publish(selection)
    latest = service.latest()

    assert published["governance_profile"] == "balanced_demo"
    assert published["alpha_voter_count"] == 1
    assert published["context_count"] == 1
    assert published["gate_count"] == 0
    assert published["selected_factor_roles"] == {
        "alpha_a": "alpha",
        "context_a": "context",
    }
    assert published["selected_factor_weights"] == {
        "alpha_a": 0.05,
        "context_a": 0.0,
    }
    assert published["exclusion_reason_counts"] == {"lifecycle_not_live": 1}
    assert published["hard_quarantined_factors"] == ["old_alpha"]
    assert published["signal_thresholds"]["evidence_version"].endswith(".v2")
    assert published["process_boot_id"].startswith(
        "live-factor-selection:"
    )
    assert published["config_hash"]
    assert published["selection_fingerprint"]
    assert published["heartbeat_at"] == published["published_at"]
    assert latest["ok"] is True
    assert latest["evidence_maturity"]["factors"] == {}
