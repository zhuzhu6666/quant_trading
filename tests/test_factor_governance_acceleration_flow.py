"""Factor governance acceleration flow (sqlite fixtures, no prod mutation).

Created in Phase 2, extended per phase; each phase asserts its own slice
green before the next begins. Final gate: prepare dsl -> age past the
stale-evidence window -> fast-lane retire -> regime flip -> reprepare new
SHADOW generation -> batch manifest commits admitted items -> rollback scan
covers the batch.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import backend.runtime.factor_governance_orchestrator as governance_module
from backend.runtime.factor_governance_orchestrator import FactorGovernanceOrchestrator
from backend.services.runtime_config_overlay import RuntimeConfigOverlayService
from config import runtime_config as rc


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


def _stale_shadow(name: str, age_hours: float) -> dict:
    ts = time.time() - age_hours * 3600.0
    return {
        "factor_id": name,
        "source": "shadow",
        "enabled": False,
        "lifecycle_status": "SHADOW",
        "lifecycle_factor_id": f"dsl:{name}",
        "lifecycle_origin": "dsl",
        "health_status": "UNKNOWN",
        "health_score": 0.0,
        "canary": {"stage": "SHADOW", "fresh_evidence_bars": 0, "updated_at": ts},
        "last_action_ts": ts,
        "lifecycle_updated_at": ts,
    }


def test_fast_lane_retires_budget_and_leaves_normal_quota(monkeypatch, tmp_path):
    """3 zero-progress over-age SHADOW rows with fast budget 2 -> exactly the
    2 oldest retire with cause dead; the normal retire quota still admits."""
    rc.reset_for_tests()
    rc.patch({"factor_governance_fast_retire_per_cycle": 2})
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(tmp_path / "state.db")
    retired = []

    class _Adapter:
        def get_meta(self, name):
            return {"source": "shadow", "description": f"rank({name})"}

    class _Lifecycle:
        def __init__(self, _db_path, adapter):
            self.adapter = adapter

        def retire(self, **kwargs):
            retired.append(kwargs)
            return {"ok": True, "lifecycle_stage": "RETIRED", "mutation_id": "m-fast"}

    monkeypatch.setattr(
        "alpha.registry_adapter.RegistryAdapter.shared",
        classmethod(lambda cls: _Adapter()),
    )
    monkeypatch.setattr(governance_module, "FactorLifecycleService", _Lifecycle)
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda _factor_id: False)
    monkeypatch.setattr(
        orch,
        "_current_market_regime_projection",
        lambda: {"regime_id": "trend", "confidence": 0.8, "source": "test"},
    )
    monkeypatch.setattr(
        orch,
        "_audit_action",
        lambda run, item, action, status, evidence, verdict, **kwargs: {
            "factor_id": item["factor_id"],
            "action": action,
            "status": status,
            "evidence": evidence,
        },
    )

    catalog = [
        _stale_shadow("stale_a", 500),
        _stale_shadow("stale_b", 450),
        _stale_shadow("stale_c", 400),
    ]
    fresh = _stale_shadow("fresh_d", 10)
    catalog.append(fresh)

    fast = orch._retire_zero_progress_shadow(
        catalog, {"run_id": "flow-fast"}, cfg=rc.shared()
    )

    assert [kwargs["name"] for kwargs in retired] == ["stale_a", "stale_b"]
    assert [action["status"] for action in fast] == ["applied", "applied"]
    assert retired[0]["evidence_refs"]["retire_cause"] == "dead"
    assert retired[0]["evidence_refs"]["regime_id"] == "trend"

    from alpha.factor_identity import (
        canonical_factor_id,
        factor_definition_fingerprint,
    )

    expression = "rank(close)"
    normal = orch._retire_quarantined_discovered(
        [
            {
                "factor_id": "weak_discovered",
                "source": "discovered",
                "enabled": False,
                "health_score": 10.0,
                "health_status": "WEAK",
                "lifecycle_expression": expression,
                "lifecycle_factor_id": canonical_factor_id(expression),
                "lifecycle_definition_fingerprint": factor_definition_fingerprint(
                    expression
                ),
                "lifecycle_artifact_hash": "c" * 64,
            }
        ],
        {"run_id": "flow-normal"},
    )

    assert normal[0]["status"] == "applied"
    assert retired[-1]["name"] == "weak_discovered"
