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
from backend.services.learning_application_store import LearningApplicationStore


def _init_state_db(tmp_path) -> None:
    from backend.core.db import STATE_DB_DDL, connect_sqlite

    conn = connect_sqlite(tmp_path / "state.db")
    try:
        conn.executescript(STATE_DB_DDL)
        conn.commit()
    finally:
        conn.close()
import pytest


@pytest.fixture(autouse=True)
def _reset_runtime_config():
    rc.reset_for_tests()
    yield
    rc.reset_for_tests()


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


def _batch_ref(candidate_id, action="promote_factor"):
    return {
        "candidate_id": candidate_id,
        "target_agent": "factor_governance",
        "scope_type": "factor_weight",
        "scope_key": candidate_id,
        "action": action,
        "execution_ready": True,
        "governance_eligible": True,
        "bridge_ready": True,
        "blocker_codes": [],
    }


def _delegate_batch(service, refs, **gate_extra):
    from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService

    assert isinstance(service, V16BrainOrchestratorService)
    return service.delegate_factor_governance_cycle(
        {
            "snapshot_id": "batch-1",
            "health_cycle_id": "health-1",
            "expansion_preflight": {
                "required": True,
                "candidate_count": len(refs),
                "reasons": {},
                "candidate_refs": refs,
            },
            **gate_extra,
        },
        persist=False,
    )


def test_batch_delegate_joins_ids_and_verdict_binds(tmp_path):
    """3 refs -> one command with joined candidate_id + max_apply_count 3;
    verdict binds fingerprint/count; over-cap batches refused."""
    from backend.runtime.factor_governance_orchestrator import (
        factor_batch_manifest_verdict,
    )
    from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService

    rc.reset_for_tests()
    service = V16BrainOrchestratorService(db_path=tmp_path / "state.db")
    refs = [_batch_ref("alpha_a"), _batch_ref("alpha_b"), _batch_ref("alpha_c")]
    delegated = _delegate_batch(service, refs)

    assert delegated["status"] == "delegated"
    command = delegated["command"]
    assert command["candidate_id"] == "alpha_a,alpha_b,alpha_c"
    assert command["max_apply_count"] == 3

    preflight = delegated["command"]["evidence"]["expansion_preflight"]
    assert factor_batch_manifest_verdict(
        {"evidence": delegated["command"]["evidence"]}, preflight
    )["allowed"] is True
    tampered = {**preflight, "candidate_count": 2}
    assert (
        factor_batch_manifest_verdict(
            {"evidence": delegated["command"]["evidence"]}, tampered
        )["status"]
        == "factor_batch_manifest_mismatch"
    )

    oversized = _delegate_batch(
        service, [_batch_ref(f"alpha_{idx}") for idx in range(6)]
    )
    assert oversized["status"] == "factor_candidate_contract_not_ready"
    assert oversized["reason"] == "candidate_batch_size_out_of_range"


def test_preflight_hands_first_n_and_defers_rest(monkeypatch):
    """batch_max=2 with 3 promotable -> 2 refs with planned deltas, 1 deferred
    entry keeping {candidate_id, action} shape."""
    import hashlib
    import time as _time
    from dataclasses import replace

    from alpha.factor_identity import (
        canonical_factor_id,
        factor_definition_fingerprint,
    )

    rc.reset_for_tests()
    rc.patch({"factor_governance_batch_max_candidates": 2})
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    expression = "ts_mean(close, 5)"

    def _item(factor_id):
        return {
            "factor_id": factor_id,
            "lifecycle_factor_id": canonical_factor_id(expression),
            "lifecycle_origin": "shadow",
            "lifecycle_status": "SHADOW",
            "lifecycle_expression": expression,
            "lifecycle_definition_fingerprint": factor_definition_fingerprint(
                expression
            ),
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
            "health_updated_at": _time.time(),
            "direction": 1,
            "normalizer": "zscore",
            "lifecycle_generation": 1,
            "lifecycle_config_hash": "c" * 64,
            "runtime_selection_fingerprint": "s" * 64,
            "lifecycle_mutation_id": "mutation-shadow",
            "runtime_admission": "blocked",
            "lifecycle_evidence": {
                "candidate_validation": {
                    "direction": 1,
                    "signed_ic_mean": 0.03,
                    "pit_passed": True,
                    "walk_forward_passed": True,
                    "multi_forward_passed": True,
                    "cost_test_passed": True,
                    "execution_evidence_complete": True,
                    "contamination_status": "clean",
                    "regime_ids": ["trend"],
                },
            },
            "loaded_projection": {},
        }

    catalog = [_item(f"batch_promo_{idx}") for idx in range(3)]
    monkeypatch.setattr(
        orch,
        "_prime_admission_evidence_count_cache",
        lambda ids: orch._admission_evidence_count_cache.update(
            {
                item: {
                    "governance_eligible_mature": 20,
                    "contaminated_or_ineligible": 0,
                    "status": "available",
                }
                for item in ids
            }
        ),
    )
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda _factor_id: False)
    monkeypatch.setattr(
        orch, "_posterior_expansion_guard", lambda *_args, **_kwargs: "posterior_ok"
    )
    monkeypatch.setattr(
        orch,
        "_current_market_regime_projection",
        lambda: {"regime_id": "", "confidence": 0.0},
    )
    profile = replace(
        orch._governance_profile(rc.shared()), name="strict_live", balanced_demo=False
    )

    preflight = orch._expansion_preflight(
        catalog,
        cfg=rc.shared(),
        profile=profile,
        redundancy_report={"group_count": 0, "groups": []},
    )

    assert preflight["candidate_count"] == 2
    assert [ref["candidate_id"] for ref in preflight["candidate_refs"]] == [
        "batch_promo_0",
        "batch_promo_1",
    ]
    assert all("planned_weight_delta" in ref for ref in preflight["candidate_refs"])
    assert preflight["deferred_candidates"] == [
        {"candidate_id": "batch_promo_2", "action": "promote_factor"}
    ]


def test_batch_guards_refuse_over_cap_and_redundant_follower():
    """Total delta over cap refuses the whole batch; a correlated follower is
    refused with patch-shaped evidence unless already leader-grouped."""
    rc.reset_for_tests()
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    cfg = rc.shared()
    manifest = {
        "alpha_a": {"candidate_id": "alpha_a", "planned_weight_delta": 0.20},
        "alpha_b": {"candidate_id": "alpha_b", "planned_weight_delta": 0.20},
    }

    over = orch._batch_manifest_guards(
        ["alpha_a", "alpha_b"], manifest, {"groups": []}, cfg
    )
    assert set(over) == {"alpha_a", "alpha_b"}
    assert over["alpha_a"]["reason"] == "batch_total_weight_delta_exceeded"

    small_manifest = {
        cid: {"candidate_id": cid, "planned_weight_delta": 0.01}
        for cid in ("alpha_a", "alpha_b")
    }
    report = {
        "groups": [{"group_id": "g1", "leader": "alpha_a",
                    "members": ["alpha_a", "alpha_b"]}]
    }
    pair = orch._batch_manifest_guards(
        ["alpha_a", "alpha_b"], small_manifest, report, cfg
    )
    assert list(pair) == ["alpha_b"]
    assert pair["alpha_b"]["reason"] == "batch_redundant_pair_refused"
    assert pair["alpha_b"]["evidence"]["redundancy_patch"] == {
        "alpha_b": pair["alpha_b"]["evidence"]["redundancy_patch"]["alpha_b"]
    }

    grouped_cfg = rc.shared()
    grouped_cfg.factor_signal_config = {
        "alpha_a": {"redundancy_group": "g1", "redundancy_leader": "alpha_a"},
        "alpha_b": {"redundancy_group": "g1", "redundancy_leader": "alpha_a"},
    }
    assert (
        orch._batch_manifest_guards(
            ["alpha_a", "alpha_b"], small_manifest, report, grouped_cfg
        )
        == {}
    )


def test_gate_reclaims_partial_batch_for_next_item(tmp_path):
    """One 2-apply command serves two per-item claims (a then b) and refuses
    a third; an off-manifest id never claims."""
    from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService
    from backend.services.v16_command_gate import V16CommandGate

    rc.reset_for_tests()
    db_path = tmp_path / "state.db"
    service = V16BrainOrchestratorService(db_path=db_path)
    delegated = service.delegate_factor_governance_cycle(
        {
            "snapshot_id": "batch-1",
            "health_cycle_id": "health-1",
            "expansion_preflight": {
                "required": True,
                "candidate_count": 2,
                "reasons": {},
                "candidate_refs": [_batch_ref("alpha_a"), _batch_ref("alpha_b")],
            },
        },
        persist=True,
    )
    assert delegated["status"] == "delegated"
    command_id = delegated["command"]["command_id"]

    def _claim(candidate_id):
        return V16CommandGate.claim(
            db_path,
            target_agent="factor_governance",
            scope_type="factor_weight",
            scope_key="alpha_weight_policy",
            action="factor_governance_cycle",
            command_id=command_id,
            candidate_id=candidate_id,
        )

    assert _claim("zzz").get("allowed") is not True
    first = _claim("alpha_a")
    assert first["allowed"] is True
    done = V16CommandGate.finalize(
        db_path,
        command_id=command_id,
        claim_token=first["claim_token"],
        mutation_id="mut-1",
        config_hash="cfg-1",
        domain_hash="dom-1",
    )
    assert done["allowed"] is True
    second = _claim("alpha_b")
    assert second["allowed"] is True
    done2 = V16CommandGate.finalize(
        db_path,
        command_id=command_id,
        claim_token=second["claim_token"],
        mutation_id="mut-2",
        config_hash="cfg-2",
        domain_hash="dom-2",
    )
    assert done2["allowed"] is True
    assert _claim("alpha_a").get("allowed") is not True


class _RevivalAdapter:
    """Minimal discovered-factor adapter: meta + registry projection hooks."""

    def __init__(self, name, expression):
        from alpha.registry import factor_registry

        self.meta = {
            name: {"source": "shadow", "description": expression,
                   "register_time": __import__("time").time()}
        }
        factor_registry._factors[name] = lambda df: df["close"]

    def get_meta(self, name):
        return dict(self.meta.get(name, {}))

    def register_runtime(self, name, func, source, description="", **_kwargs):
        from alpha.registry import factor_registry

        factor_registry._factors[name] = func
        self.meta[name] = {"source": source, "description": description,
                           "register_time": __import__("time").time()}
        return True

    def promote(self, name, new_source, reason=""):
        self.meta[name]["source"] = new_source
        return True

    def demote(self, name, new_source, reason=""):
        self.meta[name]["source"] = new_source
        return True

    def unregister(self, name, reason=""):
        from alpha.registry import factor_registry

        factor_registry._factors.pop(name, None)
        self.meta[name]["source"] = "removed"
        return True


def _retired_dsl(tmp_path, fname, *, cause, regime):
    from backend.services.factor_lifecycle_service import FactorLifecycleService

    expression = "ts_mean(close, 5) + delta(volume, 2)"
    service = FactorLifecycleService(
        tmp_path / f"{fname}.sqlite",
        adapter=_RevivalAdapter(fname, expression),
        projection_stale_after_sec=75,
        health_stale_after_sec=180,
    )
    registered = service.register_shadow(
        name=fname,
        expression=expression,
        evidence_refs={"candidate_validation": {"direction": 1,
                                                "signed_ic_mean": 0.03}},
    )
    assert registered["ok"] is True
    retired = service.retire(
        name=fname,
        evidence_refs={"retire_cause": cause, "regime_id": regime},
        idempotency_key=f"retire-{fname}",
    )
    assert retired["ok"] is True
    return service, fname


def _seed_experience(db_path, regimes):
    import sqlite3
    import time as _time

    now = _time.time()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS experience_memory "
            "(regime_id TEXT, created_at REAL, trade_id TEXT)"
        )
        conn.executemany(
            "INSERT INTO experience_memory (regime_id, created_at, trade_id)"
            " VALUES (?, ?, ?)",
            [(regime, now + idx, f"trade-{idx}") for idx, regime in enumerate(regimes)],
        )
        conn.commit()
    finally:
        conn.close()


def _revive(service, name, key="revive-1"):
    return service.reprepare_retired(
        name=name,
        actor="system:factor_governance",
        reason="regime back in fitting set",
        evidence_refs={},
        idempotency_key=key,
    )


def test_revival_reprepares_regime_mismatch_to_new_shadow(tmp_path):
    """RETIRED cause=regime_mismatch + matching current regime -> new SHADOW
    generation linked to the terminal mutation, admission blocked."""
    import json

    service, name = _retired_dsl(tmp_path, "revive_alpha", cause="regime_mismatch",
                                 regime="trend")
    _seed_experience(service.db_path, ["trend"] * 5)
    before = service.get_state(factor_name=name)
    assert before["lifecycle_stage"] == "RETIRED"

    result = _revive(service, name)

    assert result["ok"] is True
    assert result["lifecycle_stage"] == "SHADOW"
    after = service.get_state(factor_name=name)
    assert after["generation"] == int(before["generation"]) + 1
    assert after["runtime_admission"] == "blocked"
    metadata = json.loads(after["metadata_json"])
    assert metadata["reenrolled_from"]["mutation_id"] == before["mutation_id"]
    assert metadata["reenrolled_from"]["lifecycle_stage"] == "RETIRED"
    assert metadata["revival_regime_id"] == "trend"


def test_revival_rejects_dead_cause(tmp_path):
    service, name = _retired_dsl(tmp_path, "revive_dead", cause="dead",
                                 regime="trend")
    _seed_experience(service.db_path, ["trend"] * 5)

    result = _revive(service, name)

    assert result["ok"] is False
    assert result["reason"] == "revival_cause_ineligible"


def test_revival_fails_closed_on_unknown_regime(tmp_path):
    service, name = _retired_dsl(tmp_path, "revive_unknown",
                                 cause="regime_mismatch", regime="trend")

    result = _revive(service, name)

    assert result["ok"] is False
    assert result["reason"] == "revival_regime_unknown"


def test_revival_refuses_current_regime_outside_good_set(tmp_path):
    service, name = _retired_dsl(tmp_path, "revive_other",
                                 cause="regime_mismatch", regime="trend")
    _seed_experience(service.db_path, ["range"] * 5)

    result = _revive(service, name)

    assert result["ok"] is False
    assert result["reason"] == "revival_regime_mismatch"


def _seed_prior_rows(db_path, factor, regime, *, n=3, net=0.5, age_days=0.0):
    import sqlite3
    import time as _time

    now = _time.time()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS experience_memory "
            "(experience_id TEXT PRIMARY KEY, trade_id TEXT DEFAULT '', "
            "regime_id TEXT DEFAULT '', created_at REAL DEFAULT 0.0)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS factor_contribution_review "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT NOT NULL, "
            "trade_id TEXT DEFAULT '', factor TEXT NOT NULL, "
            "net_contribution REAL DEFAULT 0.0, confidence REAL DEFAULT 0.0)"
        )
        for idx in range(n):
            trade = f"{factor}-trade-{idx}"
            conn.execute(
                "INSERT INTO experience_memory (experience_id, trade_id, regime_id,"
                " created_at) VALUES (?, ?, ?, ?)",
                (f"exp-{factor}-{idx}", trade, regime, now - age_days * 86400.0),
            )
            conn.execute(
                "INSERT INTO factor_contribution_review (review_id, trade_id, factor,"
                " net_contribution, confidence) VALUES (?, ?, ?, ?, ?)",
                (f"rev-{factor}-{idx}", trade, factor, net, 0.8),
            )
        conn.commit()
    finally:
        conn.close()


def test_regime_prior_decays_and_reports_confidence(tmp_path):
    """Decayed positive memory returns a positive prior with sub-fresh
    confidence; unknown factor/regime is no-prior ({}), never zero."""
    from backend.services.market_regime import factor_regime_prior

    db_path = tmp_path / "memory.sqlite"
    _seed_prior_rows(db_path, "alpha_m", "trend", n=3, net=0.5)

    prior = factor_regime_prior(db_path, "alpha_m", "trend")

    assert prior["n_obs"] == 3
    assert prior["prior_weight"] > 0
    assert 0.0 < prior["confidence"] < 1.0
    assert factor_regime_prior(db_path, "alpha_m", "range") == {}
    assert factor_regime_prior(db_path, "nope", "trend") == {}


def test_revival_carries_prior_into_evidence(tmp_path):
    """A matching-regime revival with decayed positive memory carries the
    prior into re-preparation evidence for probation observers."""
    import json
    import sqlite3

    service, name = _retired_dsl(tmp_path, "revive_prior",
                                 cause="regime_mismatch", regime="trend")
    _seed_prior_rows(service.db_path, name, "trend", n=4, net=0.6)
    _seed_experience(service.db_path, ["trend"] * 5)

    result = _revive(service, name, key="revive-prior-1")

    assert result["ok"] is True
    row = sqlite3.connect(service.db_path).execute(
        "SELECT evidence_json FROM factor_lifecycle_state WHERE factor_name=?",
        (name,),
    ).fetchone()
    evidence = json.loads(row[0])
    assert evidence["revival_prior"]["n_obs"] == 4
    assert evidence["revival_prior"]["prior_weight"] > 0
    assert 0.0 < evidence["revival_prior"]["confidence"] < 1.0


def test_rollback_scan_covers_batch_budget(tmp_path):
    """12 failing applications with batch-sized budget 10 -> first scan
    takes 10, second scan takes the remaining 2 (kill-switch symmetry)."""
    rc.reset_for_tests()
    _init_state_db(tmp_path)
    db = tmp_path / "state.db"
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(db)
    store = LearningApplicationStore(str(db))
    for idx in range(12):
        application_id = store.prepare_application(
            scope_type="factor",
            scope_key=f"batch_factor_{idx:02d}",
            action="update_weight",
            status="applied",
            run_id="run-batch",
            source="test",
        )
        store.write_effect(
            application_id=application_id,
            scope_key=f"batch_factor_{idx:02d}",
            scope_type="factor",
            action="update_weight",
            status="applied",
            observed_trade_count=5,
            delta_avg_reward=-0.5,
        )

    first = orch._rollback_failed_actions({"run_id": "gov-batch-1"})
    second = orch._rollback_failed_actions({"run_id": "gov-batch-2"})

    assert len(first) == 10
    assert len(second) == 2


def test_end_to_end_retire_revive_batch_loop(monkeypatch, tmp_path):
    """Full acceleration loop on sqlite: register dsl -> normal retire with
    cause regime_mismatch -> regime flip in experience_memory -> reprepare a
    new SHADOW generation -> batch manifest of 3 binds with no guard
    refusals. (PG-only commit covered by the gate reclaim test.)"""
    import json
    import sqlite3
    from alpha.factor_identity import (
        canonical_factor_id,
        factor_definition_fingerprint,
    )
    from backend.services.factor_lifecycle_service import FactorLifecycleService

    rc.reset_for_tests()
    _init_state_db(tmp_path)
    db = tmp_path / "state.db"
    orch = FactorGovernanceOrchestrator(risk_policy=_AllowRisk())
    orch.overlay = RuntimeConfigOverlayService(db)

    expression = "rank(close)"
    name = "loop_alpha"
    adapter = _RevivalAdapter(name, expression)
    service = FactorLifecycleService(
        db,
        adapter=adapter,
        projection_stale_after_sec=75,
        health_stale_after_sec=180,
    )
    registered = service.register_shadow(
        name=name,
        expression=expression,
        evidence_refs={"candidate_validation": {"direction": 1,
                                                "signed_ic_mean": 0.03}},
    )
    assert registered["ok"] is True

    monkeypatch.setattr(
        "alpha.registry_adapter.RegistryAdapter.shared", classmethod(lambda cls: adapter)
    )
    monkeypatch.setattr(orch, "_factor_has_pending_effect", lambda _factor_id: False)
    monkeypatch.setattr(
        orch,
        "_current_market_regime_projection",
        lambda: {"regime_id": "trend", "confidence": 0.8, "source": "test"},
    )
    audited = []
    monkeypatch.setattr(
        orch,
        "_audit_action",
        lambda run, item, action, status, evidence, verdict, **kwargs: audited.append(
            {"factor_id": item["factor_id"], "action": action, "status": status,
             "evidence": evidence}
        ) or audited[-1],
    )
    catalog = [
        {
            "factor_id": name,
            "source": "discovered",
            "enabled": False,
            "health_score": 10.0,
            "health_status": "WEAK",
            "lifecycle_expression": expression,
            "lifecycle_factor_id": canonical_factor_id(expression),
            "lifecycle_definition_fingerprint": factor_definition_fingerprint(
                expression
            ),
            "lifecycle_artifact_hash": "b" * 64,
            "factor_governance_shadow": {
                "model_type": "factor_governance_lightgbm",
                "sample_count": 25,
                "weak_sample_count": 25,
                "weakness_score": 0.9,
                "avg_weakness_score": 0.9,
                "promotion_gate": {"passed": True},
                "result": {"mutation_eligible": True},
                "payload": {"features": {"same_regime_positive_rate": 0.9}},
            },
        }
    ]

    retire_actions = orch._retire_quarantined_discovered(
        catalog, {"run_id": "loop-retire"}
    )
    assert retire_actions[0]["status"] == "applied"
    assert retire_actions[0]["evidence"]["retire_cause"] == "regime_mismatch"
    assert retire_actions[0]["evidence"]["regime_id"] == "trend"
    terminal = service.get_state(factor_name=name)
    assert terminal["lifecycle_stage"] == "RETIRED"
    assert json.loads(terminal["metadata_json"])["retire_cause"] == "regime_mismatch"

    _seed_experience(db, ["trend"] * 5)
    revived = service.reprepare_retired(
        name=name,
        actor="system:factor_governance",
        reason="loop regime back",
        evidence_refs={},
        idempotency_key="loop-revive-1",
    )
    assert revived["ok"] is True
    assert revived["lifecycle_stage"] == "SHADOW"
    after = service.get_state(factor_name=name)
    assert after["generation"] == int(terminal["generation"]) + 1

    from backend.services.v16_brain_orchestrator import V16BrainOrchestratorService
    from backend.runtime.factor_governance_orchestrator import (
        factor_batch_manifest_verdict,
    )

    delegator = V16BrainOrchestratorService(db_path=db)
    refs = [
        {**_batch_ref(name, "promote_factor"), "planned_weight_delta": 0.01},
        {**_batch_ref("loop_beta", "promote_factor"), "planned_weight_delta": 0.01},
        {**_batch_ref("loop_gamma", "restore_factor_live"),
         "planned_weight_delta": 0.02},
    ]
    delegated = delegator.delegate_factor_governance_cycle(
        {
            "snapshot_id": "loop-1",
            "health_cycle_id": "health-1",
            "expansion_preflight": {
                "required": True,
                "candidate_count": 3,
                "reasons": {},
                "candidate_refs": refs,
            },
        },
        persist=False,
    )
    assert delegated["status"] == "delegated"
    assert delegated["command"]["candidate_id"] == f"{name},loop_beta,loop_gamma"
    preflight = delegated["command"]["evidence"]["expansion_preflight"]
    assert factor_batch_manifest_verdict(
        {"evidence": delegated["command"]["evidence"]}, preflight
    )["allowed"] is True
    manifest_by_id = {ref["candidate_id"]: ref for ref in refs}
    guards = orch._batch_manifest_guards(
        [name, "loop_beta", "loop_gamma"], manifest_by_id,
        {"groups": []}, rc.shared(),
    )
    assert guards == {}
