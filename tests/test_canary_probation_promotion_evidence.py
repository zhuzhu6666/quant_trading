"""Regression: canary PROBATION counts as completed evidence for promotion prep.

Root cause (2026-08-22): `_promotion_evidence` required canary stage == ACTIVE,
but the D1 gate requires committed ACTIVE backing in factor_lifecycle_state
before canary may enter ACTIVE — and lifecycle activation is itself produced by
this same promotion path.  The circular requirement starved the pipeline: 13
fully-qualified GP factors sat in CANARY_5 with no way to advance.

Contract after fix:
  - SHADOW / CANARY_5 / CANARY_20 / CANARY_50 -> bar_oos_canary_incomplete
  - PROBATION / ACTIVE -> canary blocker cleared (other gates unchanged)
"""

from __future__ import annotations

import time

import pytest

import backend.runtime.factor_governance_orchestrator as governance_module
from backend.runtime.factor_governance_orchestrator import FactorGovernanceOrchestrator
from config import runtime_config as rc
from config.runtime_config import RuntimeConfig


def _catalog_item(canary_stage: str) -> dict:
    now = time.time()
    return {
        "factor_id": "dsl_auto_deadlock_probe",
        "lifecycle_factor_id": "dsl_auto_deadlock_probe",
        "lifecycle_origin": "dsl",
        "lifecycle_status": "SHADOW",
        "runtime_admission": "projection_acknowledged",
        "activation_canary": True,
        "health_rolling_ic": 0.05,
        "health_status": "WATCH",
        "health_score": 62.0,
        "health_n_obs": 2000,
        "health_updated_at": now,
        "canary": {"stage": canary_stage},
        "shadow_perf": {
            "oos_bars": 1358,
            "n_valid": 1599,
            "evidence_hash": "f" * 64,
            "dataset_hash": "0" * 64,
            "cumulative_pnl": 0.049,
            "hit_rate": 0.54,
            "max_drawdown": 0.008,
        },
    }


def _cfg() -> RuntimeConfig:
    return RuntimeConfig(
        autonomy_mode="demo_autonomous",
        factor_signal_config={
            "dsl_auto_deadlock_probe": {
                "enabled": True,
                "lifecycle_status": "SHADOW",
                "role": "alpha",
                "source": "discovered",
            },
        },
        factor_portfolio_weights={"dsl_auto_deadlock_probe": 0.0},
        factor_health_min_n_obs=100,
    )


def test_probation_canary_satisfies_promotion_evidence(monkeypatch) -> None:
    rc.reset_for_tests()
    orchestrator = FactorGovernanceOrchestrator()
    item = _catalog_item("PROBATION")
    cfg = _cfg()

    evidence = orchestrator._promotion_evidence(item, cfg)
    assert "bar_oos_canary_incomplete" not in evidence["blocker_codes"], (
        "PROBATION is the terminal canary evidence stage; requiring ACTIVE here "
        "inverts the D1 backing dependency"
    )


@pytest.mark.parametrize("stage", ["SHADOW", "CANARY_5", "CANARY_20", "CANARY_50"])
def test_intermediate_canary_stages_still_blocked(stage: str, monkeypatch) -> None:
    rc.reset_for_tests()
    orchestrator = FactorGovernanceOrchestrator()
    item = _catalog_item(stage)
    cfg = _cfg()

    evidence = orchestrator._promotion_evidence(item, cfg)
    assert "bar_oos_canary_incomplete" in evidence["blocker_codes"]
