"""Posterior expansion guard: factor expansion must not repeat a
previously-applied autonomous factor action whose measured posterior effect
(delta_avg_reward in learning_application_effect) was negative.

Design (user decision A = mixed):
- enough samples + negative delta  -> blocked_by_posterior (candidate removed)
- not enough samples + negative delta -> posterior_degraded (candidate kept,
  flagged for degraded handling)
- no record or non-negative delta -> posterior_ok
"""
from __future__ import annotations

import time

import pytest

import backend.runtime.factor_governance_orchestrator as governance_module
from backend.runtime.factor_governance_orchestrator import (
    FactorGovernanceOrchestrator,
    posterior_degraded_target_weight,
    posterior_expansion_verdict,
)
from config import runtime_config as rc
from config.runtime_config import RuntimeConfig
from risk.policy_service import RiskVerdict


@pytest.fixture(autouse=True)
def _reset_runtime_config():
    rc.reset_for_tests()
    yield
    rc.reset_for_tests()


class _AllowRisk:
    def evaluate(self, _action: str, _context: dict) -> RiskVerdict:
        return RiskVerdict(True, "ok")


def _default_profile():
    cfg = RuntimeConfig(autonomy_mode="demo_autonomous")
    rc.replace(cfg)
    return FactorGovernanceOrchestrator(
        risk_policy=_AllowRisk()
    )._governance_profile(cfg)


# ---------------------------------------------------------------------------
# Pure verdict logic (mixed mode: block when evidence is enough,
# degrade when evidence is thin, allow otherwise)
# ---------------------------------------------------------------------------


def test_verdict_blocks_when_samples_enough_and_delta_negative():
    assert (
        posterior_expansion_verdict(
            delta_avg_reward=-0.08,
            observed_trade_count=25,
            block_delta=-0.05,
            min_samples=10,
        )
        == "blocked_by_posterior"
    )


def test_verdict_degrades_when_samples_insufficient_and_delta_negative():
    assert (
        posterior_expansion_verdict(
            delta_avg_reward=-0.08,
            observed_trade_count=3,
            block_delta=-0.05,
            min_samples=10,
        )
        == "posterior_degraded"
    )


def test_verdict_allows_when_delta_not_negative():
    assert (
        posterior_expansion_verdict(
            delta_avg_reward=0.02,
            observed_trade_count=25,
            block_delta=-0.05,
            min_samples=10,
        )
        == "posterior_ok"
    )


def test_verdict_allows_when_no_record():
    assert (
        posterior_expansion_verdict(
            delta_avg_reward=None,
            observed_trade_count=0,
            block_delta=-0.05,
            min_samples=10,
        )
        == "posterior_ok"
    )


def test_verdict_boundary_at_block_delta_is_still_ok():
    assert (
        posterior_expansion_verdict(
            delta_avg_reward=-0.05,
            observed_trade_count=25,
            block_delta=-0.05,
            min_samples=10,
        )
        == "posterior_ok"
    )


# ---------------------------------------------------------------------------
# Preflight integration: blocked candidates removed, degraded candidates kept
# but flagged, verdicts reported on the preflight result.
# ---------------------------------------------------------------------------


def _catalog_item(factor_id: str, **overrides) -> dict:
    now = time.time()
    item = {
        "factor_id": factor_id,
        "lifecycle_origin": "builtin",
        "lifecycle_status": "PROMOTION_PREPARED",
        "lifecycle_generation": 2,
        "role": "alpha",
        "enabled": True,
        "health_status": "HEALTHY",
        "health_score": 80.0,
        "health_n_obs": 2000,
        "health_updated_at": now,
        "runtime_admission": "projection_acknowledged",
        "loaded_projection": {
            "loaded": True,
            "generation": 2,
            "artifact_hash": "",
        },
    }
    item.update(overrides)
    return item


def test_preflight_excludes_factor_with_blocking_posterior(monkeypatch):
    cfg = RuntimeConfig(
        autonomy_mode="demo_autonomous",
        factor_signal_config={
            "f_blocked": {
                "enabled": True,
                "autonomous_activation": True,
                "lifecycle_status": "PROMOTION_PREPARED",
                "role": "alpha",
                "source": "builtin",
            },
            "f_clean": {
                "enabled": True,
                "autonomous_activation": True,
                "lifecycle_status": "PROMOTION_PREPARED",
                "role": "alpha",
                "source": "builtin",
            },
        },
        factor_portfolio_weights={"f_blocked": 0.0, "f_clean": 0.0},
        factor_governance_builtin_activation_weight=0.05,
    )
    rc.replace(cfg)
    orchestrator = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    monkeypatch.setattr(
        orchestrator,
        "_factor_has_pending_effect",
        lambda _factor_id: False,
    )
    monkeypatch.setattr(
        orchestrator,
        "_latest_posterior_effect",
        lambda factor_id: (
            {"delta_avg_reward": -0.12, "observed_trade_count": 30}
            if factor_id == "f_blocked"
            else None
        ),
    )
    catalog = [
        _catalog_item("f_blocked"),
        _catalog_item("f_clean"),
    ]

    preflight = orchestrator._expansion_preflight(
        catalog,
        cfg=cfg,
        profile=orchestrator._governance_profile(cfg),
        redundancy_report={},
    )

    assert preflight["reasons"]["builtin_activation"] == ["f_clean"]
    assert preflight["posterior_blocked_ids"] == ["f_blocked"]
    assert preflight["posterior_degraded_ids"] == []


def test_preflight_keeps_degraded_candidate_but_flags_it(monkeypatch):
    cfg = RuntimeConfig(
        autonomy_mode="demo_autonomous",
        factor_signal_config={
            "f_degraded": {
                "enabled": True,
                "autonomous_activation": True,
                "lifecycle_status": "PROMOTION_PREPARED",
                "role": "alpha",
                "source": "builtin",
            },
        },
        factor_portfolio_weights={"f_degraded": 0.0},
        factor_governance_builtin_activation_weight=0.05,
    )
    rc.replace(cfg)
    orchestrator = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    monkeypatch.setattr(
        orchestrator,
        "_factor_has_pending_effect",
        lambda _factor_id: False,
    )
    monkeypatch.setattr(
        orchestrator,
        "_latest_posterior_effect",
        lambda _factor_id: {
            "delta_avg_reward": -0.08,
            "observed_trade_count": 3,
        },
    )
    catalog = [_catalog_item("f_degraded")]

    preflight = orchestrator._expansion_preflight(
        catalog,
        cfg=cfg,
        profile=orchestrator._governance_profile(cfg),
        redundancy_report={},
    )

    assert preflight["reasons"]["builtin_activation"] == ["f_degraded"]
    assert preflight["posterior_blocked_ids"] == []
    assert preflight["posterior_degraded_ids"] == ["f_degraded"]


# ---------------------------------------------------------------------------
# Reduced-weight re-trial for posterior-degraded activations (2026-09-02)
# ---------------------------------------------------------------------------


def test_posterior_degraded_activation_halves_weight_by_default():
    assert posterior_degraded_target_weight(0.10) == 0.05
    assert posterior_degraded_target_weight(0.05) == 0.025


def test_posterior_degraded_activation_respects_configured_scale_and_caps():
    assert posterior_degraded_target_weight(0.10, scale=0.3) == 0.03
    # cap at the normal activation ceiling
    assert posterior_degraded_target_weight(0.90, scale=1.0) == 0.50
    # degenerate inputs stay bounded
    assert posterior_degraded_target_weight(0.10, scale=0.0) == 0.05
    assert posterior_degraded_target_weight(-1.0) == 0.0
