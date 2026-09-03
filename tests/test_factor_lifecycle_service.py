from __future__ import annotations

import inspect
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from alpha.registry import factor_registry
from alpha.registry_adapter import (
    SOURCE_BUILTIN,
    SOURCE_DISCOVERED,
    SOURCE_REMOVED,
    SOURCE_SHADOW,
)
from alpha.streaming_factor_engine import StreamingFactorEngine
from alpha.factor_identity import factor_definition_fingerprint
from backend.services.factor_lifecycle_service import (
    ALLOWED_TRANSITIONS,
    FactorLifecycleService,
    FactorLifecycleStage,
)
from backend.services.learning_application_store import LearningApplicationStore
from backend.services.governance_mutation_coordinator import (
    GovernanceMutationCoordinator,
    GovernanceMutationPlan,
    classify_governance_risk,
)
from config import runtime_config
from config.runtime_config import RuntimeConfig


class FakeAdapter:
    def __init__(self, name: str, expression: str) -> None:
        self.name = name
        self.meta = {
            name: {
                "source": SOURCE_SHADOW,
                "description": expression,
                "register_time": time.time(),
            }
        }
        self.promote_calls = 0
        self.register_calls = 0
        self.register_log_events: list[bool] = []
        self.unregister_calls = 0
        self.fail_promote = False

    def get_meta(self, name: str) -> dict:
        return dict(self.meta.get(name, {}))

    def register_runtime(
        self,
        name: str,
        func,
        source: str,
        description: str = "",
        **_kwargs,
    ) -> bool:
        self.register_calls += 1
        self.register_log_events.append(bool(_kwargs.get("log_event", True)))
        factor_registry._factors[name] = func
        self.meta[name] = {
            "source": source,
            "description": description,
            "register_time": time.time(),
        }
        return True

    def promote(self, name: str, new_source: str, reason: str = "") -> bool:
        self.promote_calls += 1
        if self.fail_promote:
            return False
        self.meta[name]["source"] = new_source
        return True

    def demote(self, name: str, new_source: str, reason: str = "") -> bool:
        self.meta[name]["source"] = new_source
        return True

    def unregister(self, name: str, reason: str = "") -> bool:
        self.unregister_calls += 1
        factor_registry._factors.pop(name, None)
        self.meta[name]["source"] = SOURCE_REMOVED
        return True


@pytest.fixture(autouse=True)
def _reset_runtime_config():
    runtime_config.reset_for_tests()
    yield
    runtime_config.reset_for_tests()


@pytest.fixture
def lifecycle(tmp_path: Path):
    name = "phase3_lifecycle_alpha"
    expression = "ts_mean(close, 5) + delta(volume, 2)"

    def factor(df):
        return df["close"]

    factor_registry._factors[name] = factor
    adapter = FakeAdapter(name, expression)
    service = FactorLifecycleService(
        tmp_path / "lifecycle.sqlite",
        adapter=adapter,  # type: ignore[arg-type]
        projection_stale_after_sec=75,
        health_stale_after_sec=180,
    )
    registered = service.register_shadow(
        name=name,
        expression=expression,
        evidence_refs={
            "candidate_validation": {
                "direction": 1,
                "signed_ic_mean": 0.03,
            }
        },
    )
    assert registered["ok"] is True
    yield service, adapter, name, expression
    factor_registry._factors.pop(name, None)


def _candidate_admission_refs() -> dict:
    return {
        "admission_evidence": {
            "schema_version": "factor_admission_evidence.v1",
            "direction": {"direction": 1, "status": "validated"},
            "eligible_for_preparation": True,
            "eligible_for_activation": True,
            "preflight_blocker_codes": [],
            "activation_blocker_codes": [],
        }
    }


def _prepare_candidate(service: FactorLifecycleService, name: str) -> dict:
    return service.prepare_promotion(
        name=name,
        evidence_refs=_candidate_admission_refs(),
    )


def _activate_candidate(
    service: FactorLifecycleService,
    name: str,
    *,
    weight,
    now: float | None = None,
) -> dict:
    return service.activate(
        name=name,
        weight=weight,
        now=now,
        evidence_refs=_candidate_admission_refs(),
    )


def _prepare_and_ack(service: FactorLifecycleService, name: str, *, now: float) -> dict:
    prepared = _prepare_candidate(service, name)
    assert prepared["ok"] is True
    assert prepared["lifecycle_stage"] == "PROMOTION_PREPARED"
    state = service.get_state(factor_name=name)
    acknowledged = service.acknowledge_projection(
        factor_id=state["factor_id"],
        process_role="live_alpha",
        process_id="worker-1",
        boot_id="boot-1",
        artifact_hash=state["artifact_hash"],
        generation=state["generation"],
        mutation_id=state["mutation_id"],
        loaded=True,
        status="loaded",
        observed_at=now,
    )
    assert acknowledged["ok"] is True
    return state


def _write_health(service: FactorLifecycleService, name: str, *, now: float) -> None:
    _write_health_with(service, name, now=now, score=85.0, status="HEALTHY")


def _write_health_with(
    service: FactorLifecycleService,
    name: str,
    *,
    now: float,
    score: float,
    status: str,
) -> None:
    conn = sqlite3.connect(service.db_path)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO factor_health
               (factor, score, status, n_obs, rolling_ic, updated_at)
               VALUES (?, ?, ?, 250, 0.03, ?)""",
            (name, score, status, now),
        )
        conn.commit()
    finally:
        conn.close()


def test_state_machine_has_linear_promotion_and_terminal_states():
    assert FactorLifecycleStage.ACTIVE not in ALLOWED_TRANSITIONS[FactorLifecycleStage.SHADOW]
    assert FactorLifecycleStage.ACTIVE in ALLOWED_TRANSITIONS[FactorLifecycleStage.PROMOTION_PREPARED]
    assert ALLOWED_TRANSITIONS[FactorLifecycleStage.QUARANTINED] == frozenset()
    assert ALLOWED_TRANSITIONS[FactorLifecycleStage.RETIRED] == frozenset()


def test_prepare_uses_canonical_sha256_and_never_promotes_registry(lifecycle):
    service, adapter, name, expression = lifecycle
    result = _prepare_candidate(service, name)

    assert result["ok"] is True
    assert result["lifecycle_stage"] == "PROMOTION_PREPARED"
    assert adapter.promote_calls == 0
    assert adapter.get_meta(name)["source"] == SOURCE_SHADOW
    state = service.get_state(factor_name=name)
    fingerprint = factor_definition_fingerprint(expression)
    assert state["factor_id"] == f"dsl:{fingerprint}"
    assert state["definition_fingerprint"] == fingerprint
    assert state["artifact_hash"] == fingerprint
    assert state["runtime_admission"] == "awaiting_projection_ack"
    assert runtime_config.shared().factor_signal_config[name]["enabled"] is False
    assert float(runtime_config.shared().factor_portfolio_weights.get(name, 0.0) or 0.0) == 0.0
    intents = sqlite3.connect(service.db_path).execute(
        "SELECT action, risk_class, status FROM governance_mutation_intent ORDER BY created_at"
    ).fetchall()
    assert intents == [
        ("register_shadow_factor", "risk_tightening", "committed"),
        ("promote_factor", "risk_expanding", "committed"),
    ]


def test_candidate_prepare_preflight_failure_has_no_governance_side_effect(lifecycle):
    service, adapter, name, _expression = lifecycle
    conn = sqlite3.connect(service.db_path)
    try:
        before = {
            "intents": conn.execute(
                "SELECT COUNT(*) FROM governance_mutation_intent"
            ).fetchone()[0],
            "snapshots": conn.execute(
                "SELECT COUNT(*) FROM runtime_config_snapshot"
            ).fetchone()[0],
        }
    finally:
        conn.close()

    result = service.prepare_promotion(name=name, evidence_refs={})

    assert result["ok"] is False
    assert result["reason"] == "candidate_admission_evidence_missing"
    assert adapter.promote_calls == 0
    assert service.get_state(factor_name=name)["lifecycle_stage"] == "SHADOW"
    conn = sqlite3.connect(service.db_path)
    try:
        after = {
            "intents": conn.execute(
                "SELECT COUNT(*) FROM governance_mutation_intent"
            ).fetchone()[0],
            "snapshots": conn.execute(
                "SELECT COUNT(*) FROM runtime_config_snapshot"
            ).fetchone()[0],
        }
    finally:
        conn.close()
    assert after == before


def test_coordinator_projection_uses_stable_identity_and_prunes_pid_rows(
    lifecycle,
):
    service, _adapter, name, _expression = lifecycle
    assert _prepare_candidate(service, name)["ok"] is True
    state = service.get_state(factor_name=name)
    conn = sqlite3.connect(service.db_path)
    try:
        conn.execute(
            """
            INSERT INTO factor_runtime_projection
            (projection_id, factor_id, factor_name, process_role, process_id,
             boot_id, generation, artifact_hash, mutation_id, config_version,
             config_hash, loaded, status, error_message, heartbeat_at,
             loaded_at, created_at, updated_at)
            VALUES ('legacy-pid-row', ?, ?, 'governance_coordinator',
                    '12345', 'process', ?, ?, ?, 0, '', 0, 'current', '',
                    1, 0, 1, 1)
            """,
            (
                state["factor_id"],
                state["factor_name"],
                state["generation"],
                state["artifact_hash"],
                state["mutation_id"],
            ),
        )
        conn.execute(
            """
            INSERT INTO factor_runtime_projection
            (projection_id, factor_id, factor_name, process_role, process_id,
             boot_id, generation, artifact_hash, mutation_id, config_version,
             config_hash, loaded, status, error_message, heartbeat_at,
             loaded_at, created_at, updated_at)
            VALUES ('other-legacy-pid-row', 'dsl:other', 'other',
                    'governance_coordinator', '67890', 'process', 1, '',
                    '', 0, '', 0, 'current', '', 1, 0, 1, 1)
            """
        )
        conn.commit()
    finally:
        conn.close()

    service._record_projection_result(
        state,
        loaded=False,
        status="current",
        error_message="",
    )

    rows = sqlite3.connect(service.db_path).execute(
        """
        SELECT process_id, boot_id, status
        FROM factor_runtime_projection
        WHERE factor_id=? AND process_role='governance_coordinator'
        """,
        (state["factor_id"],),
    ).fetchall()
    assert rows == [("factor_lifecycle_service", "canonical", "current")]
    assert service._prune_legacy_coordinator_projections() == 1
    coordinator_rows = sqlite3.connect(service.db_path).execute(
        """
        SELECT process_id, boot_id
        FROM factor_runtime_projection
        WHERE process_role='governance_coordinator'
        """
    ).fetchall()
    assert coordinator_rows == [
        ("factor_lifecycle_service", "canonical")
    ]


def test_activation_fails_closed_without_projection_health_or_explicit_weight(lifecycle):
    service, _adapter, name, _expression = lifecycle
    prepared = _prepare_candidate(service, name)
    assert prepared["ok"] is True

    missing_weight = _activate_candidate(service, name, weight=None)
    assert missing_weight["ok"] is False
    assert missing_weight["reason"] == "explicit_positive_weight_required"

    missing_projection = _activate_candidate(service, name, weight=0.2)
    assert missing_projection["ok"] is False
    assert missing_projection["reason"] == "fresh_loaded_projection_ack_required"
    assert service.get_state(factor_name=name)["lifecycle_stage"] == "PROMOTION_PREPARED"


def test_live_ack_loads_committed_prepared_dsl_when_registry_is_cold(
    lifecycle, monkeypatch
):
    service, adapter, name, _expression = lifecycle
    assert _prepare_candidate(service, name)["ok"] is True

    # Simulate a live process that booted before this committed definition was
    # projected.  The ACK owner must load the committed shadow callable, but
    # it must still keep the factor out of the voting list.
    factor_registry._factors.pop(name, None)
    adapter.meta.pop(name, None)
    monkeypatch.setattr(
        "alpha.registry_adapter.RegistryAdapter.shared",
        classmethod(lambda cls: adapter),
    )
    engine = StreamingFactorEngine(
        max_buffer=80,
        factor_runtime_config=runtime_config.shared().factor_signal_config,
    )
    engine.warmup_bars(
        [
            {
                "open": 1900.0 + idx,
                "high": 1901.0 + idx,
                "low": 1899.0 + idx,
                "close": 1900.5 + idx,
                "volume": 100.0 + idx,
                "time": float(idx + 1),
                "complete": True,
            }
            for idx in range(60)
        ]
    )

    result = service.acknowledge_loaded_prepared_factors(
        engine=engine,
        boot_id="live-generation-cold-registry",
    )

    assert result["acknowledged_count"] == 1
    assert result["blocked_count"] == 0
    assert adapter.get_meta(name)["source"] == SOURCE_SHADOW
    assert name not in engine.voting_factor_ids


def test_live_warm_engine_acknowledges_prepared_without_adding_it_to_votes(
    lifecycle, monkeypatch
):
    service, adapter, name, _expression = lifecycle
    assert _prepare_candidate(service, name)["ok"] is True
    monkeypatch.setattr(
        "alpha.registry_adapter.RegistryAdapter.shared",
        classmethod(lambda cls: adapter),
    )
    engine = StreamingFactorEngine(
        max_buffer=80,
        factor_runtime_config=runtime_config.shared().factor_signal_config,
    )
    bars = [
        {
            "open": 1900.0 + idx,
            "high": 1901.0 + idx,
            "low": 1899.0 + idx,
            "close": 1900.5 + idx,
            "volume": 100.0 + idx,
            "time": float(idx + 1),
            "complete": True,
        }
        for idx in range(60)
    ]
    engine.warmup_bars(bars)

    assert engine.is_warm is True
    assert name not in engine.voting_factor_ids
    result = service.acknowledge_loaded_prepared_factors(
        engine=engine,
        boot_id="live-generation-7",
        process_id="live-process-1",
    )

    assert result["ok"] is True
    assert result["acknowledged_count"] == 1
    assert result["blocked_count"] == 0
    assert result["results"][0]["load_validation"]["voting_admitted"] is False
    row = sqlite3.connect(service.db_path).execute(
        """SELECT generation, artifact_hash, mutation_id, loaded, status, boot_id
           FROM factor_runtime_projection
           WHERE factor_id=? AND process_role='live_alpha'""",
        (service.get_state(factor_name=name)["factor_id"],),
    ).fetchone()
    state = service.get_state(factor_name=name)
    assert row == (
        state["generation"],
        state["artifact_hash"],
        state["mutation_id"],
        1,
        "loaded",
        "live-generation-7",
    )


def test_wrong_artifact_and_old_generation_cannot_ack(lifecycle):
    service, _adapter, name, _expression = lifecycle
    assert _prepare_candidate(service, name)["ok"] is True
    state = service.get_state(factor_name=name)

    wrong_artifact = service.acknowledge_projection(
        factor_id=state["factor_id"],
        process_role="live_alpha",
        process_id="worker",
        boot_id="boot",
        artifact_hash="f" * 64,
        generation=state["generation"],
        mutation_id=state["mutation_id"],
        loaded=True,
        status="loaded",
    )
    assert wrong_artifact["ok"] is False
    assert wrong_artifact["reason"] == "projection_artifact_mismatch"

    old_generation = service.acknowledge_projection(
        factor_id=state["factor_id"],
        process_role="live_alpha",
        process_id="worker",
        boot_id="boot",
        artifact_hash=state["artifact_hash"],
        generation=state["generation"] - 1,
        mutation_id=state["mutation_id"],
        loaded=True,
        status="loaded",
    )
    assert old_generation["ok"] is False
    assert old_generation["reason"] == "projection_generation_mismatch"
    assert _activate_candidate(service, name, weight=0.2)["ok"] is False


def test_registry_artifact_mismatch_blocks_automatic_live_ack(lifecycle, monkeypatch):
    service, adapter, name, _expression = lifecycle
    assert _prepare_candidate(service, name)["ok"] is True
    adapter.meta[name]["artifact_hash"] = "e" * 64
    monkeypatch.setattr(
        "alpha.registry_adapter.RegistryAdapter.shared",
        classmethod(lambda cls: adapter),
    )
    engine = StreamingFactorEngine(
        max_buffer=80,
        factor_runtime_config=runtime_config.shared().factor_signal_config,
    )
    engine.warmup_bars(
        [
            {
                "open": float(idx),
                "high": float(idx + 1),
                "low": float(idx - 1),
                "close": float(idx) + 0.5,
                "volume": 100.0,
                "time": float(idx + 1),
            }
            for idx in range(60)
        ]
    )

    result = service.acknowledge_loaded_prepared_factors(
        engine=engine,
        boot_id="live-generation-8",
    )

    assert result["acknowledged_count"] == 0
    assert result["blocked_count"] == 1
    assert result["results"][0]["reason"] == "registry_factor_artifact_mismatch"


def test_activation_requires_fresh_bound_projection_and_health(lifecycle):
    service, adapter, name, _expression = lifecycle
    now = time.time()
    _prepare_and_ack(service, name, now=now)
    _write_health(service, name, now=now)

    result = _activate_candidate(service, name, weight=0.25, now=now)

    assert result["ok"] is True
    assert result["risk_classification"]["risk_class"] == "risk_expanding"
    assert result["v16_authority"]["status"] == "isolated_test_state"
    assert adapter.promote_calls == 1
    assert adapter.get_meta(name)["source"] == SOURCE_DISCOVERED
    state = service.get_state(factor_name=name)
    assert state["lifecycle_stage"] == "ACTIVE"
    assert state["runtime_admission"] == "admitted"
    cfg = runtime_config.shared()
    assert cfg.factor_signal_config[name]["enabled"] is True
    assert cfg.factor_portfolio_weights[name] == 0.25


def test_candidate_activation_creates_exactly_one_observing_application(lifecycle):
    service, _adapter, name, _expression = lifecycle
    now = time.time()
    _prepare_and_ack(service, name, now=now)
    _write_health(service, name, now=now)

    first = _activate_candidate(service, name, weight=0.25, now=now)
    second = _activate_candidate(service, name, weight=0.25, now=now)

    assert first["ok"] is True
    assert first["application_id"]
    assert second["ok"] is True
    assert second["status"] == "already_active"
    store = LearningApplicationStore(str(service.db_path))
    latest = store.latest_application(scope_type="factor", scope_key=name)
    effects = [
        e for e in store.iter_effects(scope_key=name, scope_type="factor")
        if e.get("application_id") == first["application_id"]
    ]
    assert latest is not None
    assert latest["application_id"] == first["application_id"]
    assert latest["status"] == "applied"
    assert len(effects) == 1
    assert effects[0]["status"] == "observing"


def test_demote_to_shadow_preserves_generation_and_terminalizes_effect(lifecycle):
    service, adapter, name, _expression = lifecycle
    now = time.time()
    _prepare_and_ack(service, name, now=now)
    _write_health(service, name, now=now)
    activated = _activate_candidate(service, name, weight=0.25, now=now)
    assert activated["ok"] is True
    active_state = service.get_state(factor_name=name)

    demoted = service.demote_to_shadow(
        name=name,
        reason="legacy_evidence_incomplete",
        evidence_refs={"blocker_code": "legacy_evidence_incomplete"},
    )

    assert demoted["ok"] is True
    shadow_state = service.get_state(factor_name=name)
    assert shadow_state["lifecycle_stage"] == "SHADOW"
    assert shadow_state["generation"] == active_state["generation"]
    assert adapter.get_meta(name)["source"] == SOURCE_SHADOW
    cfg = runtime_config.shared()
    assert cfg.factor_signal_config[name]["enabled"] is False
    assert cfg.factor_signal_config[name]["activation_canary"] is False
    assert cfg.factor_portfolio_weights[name] == 0.0
    store = LearningApplicationStore(str(service.db_path))
    application = store.get_application(activated["application_id"])
    effects = [
        e for e in store.iter_effects(scope_key=name, scope_type="factor")
        if e.get("application_id") == activated["application_id"]
    ]
    assert application is not None
    assert application["status"] == "rolled_back"
    assert len(effects) == 1
    assert effects[0]["status"] == "rolled_back"


def test_activation_accepts_fresh_watch_health_above_watch_threshold(lifecycle):
    """Activation must align with promotion evidence: WATCH + score >= watch
    threshold (40) is acceptable, not only HEALTHY + >=70. IC/n_obs/freshness
    hard checks remain.
    """
    service, adapter, name, _expression = lifecycle
    now = time.time()
    _prepare_and_ack(service, name, now=now)
    _write_health_with(service, name, now=now, score=55.0, status="WATCH")

    result = _activate_candidate(service, name, weight=0.25, now=now)

    assert result["ok"] is True
    assert adapter.promote_calls == 1
    state = service.get_state(factor_name=name)
    assert state["lifecycle_stage"] == "ACTIVE"
    assert state["runtime_admission"] == "admitted"


def test_activation_rejects_watch_health_below_watch_threshold(lifecycle):
    service, adapter, name, _expression = lifecycle
    now = time.time()
    _prepare_and_ack(service, name, now=now)
    _write_health_with(service, name, now=now, score=35.0, status="WATCH")

    result = _activate_candidate(service, name, weight=0.25, now=now)

    assert result["ok"] is False
    assert result["reason"] == "fresh_valid_factor_health_required"
    assert adapter.promote_calls == 0


def test_fresh_live_process_acknowledges_active_discovered_generation(
    lifecycle, monkeypatch
):
    service, adapter, name, _expression = lifecycle
    now = time.time()
    _prepare_and_ack(service, name, now=now)
    _write_health(service, name, now=now)
    assert _activate_candidate(service, name, weight=0.25, now=now)["ok"] is True
    monkeypatch.setattr(
        "alpha.registry_adapter.RegistryAdapter.shared",
        classmethod(lambda cls: adapter),
    )
    engine = StreamingFactorEngine(
        max_buffer=80,
        factor_runtime_config=runtime_config.shared().factor_signal_config,
    )
    engine.warmup_bars(
        [
            {
                "open": 1900.0 + idx,
                "high": 1901.0 + idx,
                "low": 1899.0 + idx,
                "close": 1900.5 + idx,
                "volume": 100.0 + idx,
                "time": float(idx + 1),
                "complete": True,
            }
            for idx in range(60)
        ]
    )

    result = service.acknowledge_loaded_prepared_factors(
        engine=engine,
        boot_id="live-generation-after-restart",
        process_id="live-process-after-restart",
        observed_at=now + 1.0,
    )

    assert result["acknowledged_count"] == 1
    assert result["active_count"] == 1
    assert result["prepared_count"] == 0
    assert result["results"][0]["lifecycle_stage"] == "ACTIVE"
    # The first boot proof is allowed before selector admission; publishing
    # this current-process ACK is what lets the next canonical selection
    # include the ACTIVE factor without trusting an old PID.
    assert result["results"][0]["load_validation"]["voting_admitted"] is False
    row = sqlite3.connect(service.db_path).execute(
        """SELECT loaded, status, boot_id FROM factor_runtime_projection
           WHERE factor_id=? AND process_role='live_alpha' AND process_id=?""",
        (service.get_state(factor_name=name)["factor_id"], "live-process-after-restart"),
    ).fetchone()
    assert row == (1, "loaded", "live-generation-after-restart")


def test_backend_bootstrap_rebuilds_active_registry_from_committed_state_only(
    lifecycle,
):
    service, adapter, name, _expression = lifecycle
    now = time.time()
    _prepare_and_ack(service, name, now=now)
    _write_health(service, name, now=now)
    assert _activate_candidate(service, name, weight=0.25, now=now)["ok"] is True

    # Simulate a fresh process: the durable rows remain, process-local
    # Registry/meta do not.  No retired lifecycle fallback is involved.
    factor_registry._factors.pop(name, None)
    adapter.meta.pop(name, None)
    register_before = adapter.register_calls
    promote_before = adapter.promote_calls

    result = service.restore_committed_registry(process_role="backend")

    assert result["ok"] is True
    assert result["attempted_count"] == 1
    assert adapter.register_calls == register_before + 1
    assert adapter.register_log_events[-1] is False
    assert adapter.promote_calls == promote_before + 1
    assert adapter.get_meta(name)["source"] == SOURCE_DISCOVERED
    assert name in factor_registry


def test_backend_bootstrap_prioritizes_prepared_over_recent_shadow_volume(
    lifecycle, monkeypatch
):
    service, _adapter, name, _expression = lifecycle
    assert _prepare_candidate(service, name)["ok"] is True

    now = time.time()
    conn = sqlite3.connect(service.db_path)
    try:
        for idx in range(101):
            mutation_id = f"cold-shadow-mutation-{idx}"
            factor_id = f"dsl:{idx:064x}"
            conn.execute(
                """INSERT INTO governance_mutation_intent
                   (mutation_id, idempotency_key, control_surface, status,
                    created_at, updated_at)
                   VALUES (?, ?, 'factor_lifecycle', 'committed', ?, ?)""",
                (mutation_id, f"cold-shadow-key-{idx}", now, now),
            )
            conn.execute(
                """INSERT INTO factor_lifecycle_state
                   (factor_id, factor_name, definition_fingerprint,
                    artifact_hash, origin, lifecycle_stage, generation,
                    runtime_admission, mutation_id, metadata_json, updated_at)
                   VALUES (?, ?, ?, ?, 'dsl', 'SHADOW', 1, 'blocked', ?, ?, ?)""",
                (
                    factor_id,
                    f"cold_shadow_{idx}",
                    "a" * 64,
                    "a" * 64,
                    mutation_id,
                    '{"expression":"rank(close)"}',
                    now + idx + 1.0,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    projected: list[str] = []
    monkeypatch.setattr(
        service,
        "_project_registry",
        lambda state, **_kwargs: projected.append(str(state["factor_name"])),
    )
    monkeypatch.setattr(service, "_record_projection_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_set_runtime_admission", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        service.coordinator,
        "_record_projection",
        lambda *args, **kwargs: None,
    )

    result = service.restore_committed_registry(process_role="backend", limit=1)

    assert result["attempted_count"] == 1
    assert result["current_count"] == 1
    assert projected == [name]


def test_backend_bootstrap_rejects_non_backend_process(lifecycle):
    service, _adapter, _name, _expression = lifecycle

    result = service.restore_committed_registry(process_role="learning_worker")

    assert result["ok"] is False
    assert result["status"] == "backend_process_required"
    assert result["attempted_count"] == 0


def test_stale_projection_or_health_blocks_activation(lifecycle):
    service, _adapter, name, _expression = lifecycle
    now = time.time()
    _prepare_and_ack(service, name, now=now - 80)
    _write_health(service, name, now=now)

    stale_projection = _activate_candidate(service, name, weight=0.2, now=now)
    assert stale_projection["ok"] is False
    assert stale_projection["reason"] == "projection_ack_stale"


def test_quarantine_is_v16_exempt_and_registry_projection_is_post_commit(lifecycle):
    service, adapter, name, _expression = lifecycle
    now = time.time()
    _prepare_and_ack(service, name, now=now)
    _write_health(service, name, now=now)
    assert _activate_candidate(service, name, weight=0.2, now=now)["ok"] is True

    result = service.quarantine(name=name, reason="health decay")

    assert result["ok"] is True
    assert result["risk_classification"]["risk_class"] == "risk_tightening"
    assert result["v16_authority"]["status"] == "risk_tightening_exempt"
    assert adapter.unregister_calls == 1
    state = service.get_state(factor_name=name)
    assert state["lifecycle_stage"] == "QUARANTINED"
    assert state["runtime_admission"] == "blocked"
    assert runtime_config.shared().factor_signal_config[name]["enabled"] is False
    assert runtime_config.shared().factor_portfolio_weights[name] == 0.0


def test_builtin_lifecycle_governs_admission_without_mutating_code_registry(
    tmp_path, monkeypatch
):
    name = "pytest_builtin_lifecycle"

    def builtin_factor(df):
        return df["close"]

    factor_registry._factors[name] = builtin_factor
    adapter = FakeAdapter(name, name)
    adapter.meta[name]["source"] = SOURCE_BUILTIN
    runtime_config.replace(
        RuntimeConfig(
            factor_signal_config={
                name: {
                    "enabled": True,
                    "lifecycle_status": "SHADOW",
                    "role": "alpha",
                    "source": SOURCE_BUILTIN,
                }
            },
            factor_portfolio_weights={name: 0.0},
        )
    )
    service = FactorLifecycleService(tmp_path / "builtin.sqlite", adapter=adapter)
    try:
        shadow = service.register_shadow(name=name, expression=name)
        assert shadow["ok"] is True, shadow
        state = service.get_state(factor_name=name)
        assert state["origin"] == SOURCE_BUILTIN
        assert len(state["artifact_hash"]) == 64
        assert runtime_config.shared().factor_signal_config[name]["enabled"] is True

        prepared = service.prepare_promotion(name=name, expression=name)
        assert prepared["ok"] is True
        assert runtime_config.shared().factor_signal_config[name]["enabled"] is True
        now = time.time()
        monkeypatch.setattr(
            "alpha.registry_adapter.RegistryAdapter.shared",
            classmethod(lambda cls: adapter),
        )
        engine = StreamingFactorEngine(
            max_buffer=80,
            factor_runtime_config=runtime_config.shared().factor_signal_config,
        )
        engine.warmup_bars(
            [
                {
                    "open": 1900.0 + idx,
                    "high": 1901.0 + idx,
                    "low": 1899.0 + idx,
                    "close": 1900.5 + idx,
                    "volume": 100.0 + idx,
                    "time": float(idx + 1),
                    "complete": True,
                }
                for idx in range(60)
            ]
        )
        acknowledged = service.acknowledge_loaded_prepared_factors(
            engine=engine,
            boot_id="boot-1",
            process_id="worker-1",
            observed_at=now,
        )
        assert acknowledged["ok"] is True
        assert acknowledged["acknowledged_count"] == 1
        assert acknowledged["results"][0]["load_validation"]["voting_admitted"] is False
        _write_health(service, name, now=now)
        assert service.activate(name=name, weight=0.05, now=now)["ok"] is True
        assert adapter.promote_calls == 0

        quarantined = service.quarantine(name=name, reason="native health decay")
        assert quarantined["ok"] is True
        assert adapter.unregister_calls == 0
        assert name in factor_registry
        assert runtime_config.shared().factor_signal_config[name]["enabled"] is False
        assert runtime_config.shared().factor_portfolio_weights[name] == 0.0

        terminal_state = service.get_state(factor_name=name)
        reenrolled = service.reenroll_quarantined_builtin(
            name=name,
            actor="system:test",
            reason="fresh Demo recovery evidence",
            evidence_refs={"health_score": 65.0, "health_n_obs": 1000},
            idempotency_key="pytest-builtin-reenroll",
        )
        assert reenrolled["ok"] is True, reenrolled
        state = service.get_state(factor_name=name)
        assert state["lifecycle_stage"] == "SHADOW"
        assert state["generation"] == terminal_state["generation"] + 1
        assert runtime_config.shared().factor_signal_config[name]["enabled"] is True
        assert runtime_config.shared().factor_signal_config[name]["lifecycle_status"] == "SHADOW"
        assert runtime_config.shared().factor_signal_config[name]["source"] == SOURCE_BUILTIN
        assert runtime_config.shared().factor_signal_config[name]["autonomous_activation"] is True
        assert runtime_config.shared().factor_portfolio_weights[name] == 0.0
        metadata = json.loads(state["metadata_json"])
        assert metadata["reenrolled_from"]["lifecycle_stage"] == "QUARANTINED"
        assert (
            metadata["reenrolled_from"]["mutation_id"]
            == terminal_state["mutation_id"]
        )
    finally:
        factor_registry._factors.pop(name, None)


def test_registry_projection_failure_keeps_commit_and_marks_recovery(lifecycle):
    service, adapter, name, _expression = lifecycle
    now = time.time()
    _prepare_and_ack(service, name, now=now)
    _write_health(service, name, now=now)
    adapter.fail_promote = True

    result = _activate_candidate(service, name, weight=0.2, now=now)

    assert result["ok"] is False
    assert result["status"] == "committed_projection_degraded"
    assert result["projection_status"] == "degraded"
    state = service.get_state(factor_name=name)
    assert state["lifecycle_stage"] == "ACTIVE"
    assert state["runtime_admission"] == "degraded"
    row = sqlite3.connect(service.db_path).execute(
        "SELECT projection_status FROM governance_mutation_intent WHERE mutation_id=?",
        (result["mutation_id"],),
    ).fetchone()
    assert row == ("degraded",)
    generic_replay = GovernanceMutationCoordinator(service.db_path).replay_projection(
        result["mutation_id"]
    )
    assert generic_replay["status"] == "factor_projection_requires_domain_publisher"

    later = GovernanceMutationCoordinator(service.db_path).execute(
        GovernanceMutationPlan(
            patch={"position_supervisor_template_id": "position_supervisor:later.v2"},
            source="pytest_later_runtime_config",
            action="switch_position_supervisor_template",
            control_surface="supervisor_template",
            scope_type="supervisor_template",
            scope_key="position_supervisor",
            idempotency_key="later-config-after-factor-commit",
        )
    )
    assert later["projection_status"] == "current"

    adapter.fail_promote = False
    blocked_worker_recovery = service.recover_committed_projections(process_role="learning_worker")
    assert blocked_worker_recovery["status"] == "backend_process_required"

    recovered = service.recover_committed_projections(process_role="backend")
    assert recovered["ok"] is True
    assert recovered["attempted_count"] == 1
    assert recovered["current_count"] == 1
    assert service.get_state(factor_name=name)["runtime_admission"] == "admitted"
    assert adapter.get_meta(name)["source"] == SOURCE_DISCOVERED
    assert runtime_config.shared().position_supervisor_template_id == (
        "position_supervisor:later.v2"
    )


def test_restrictive_bootstrap_is_classified_without_v16():
    classification = classify_governance_risk(
        {"factor_signal_config": {}, "factor_portfolio_weights": {}},
        {
            "factor_signal_config": {
                "legacy": {"enabled": False, "lifecycle_status": "QUARANTINED"}
            },
            "factor_portfolio_weights": {"legacy": 0.0},
        },
    )
    assert classification.risk_class == "risk_tightening"
    assert classification.v16_required is False


def test_shadow_service_has_no_direct_registry_mutation():
    import backend.services.shadow_service as shadow_service

    source = inspect.getsource(shadow_service)
    assert ".promote(" not in source
    assert ".unregister(" not in source


def test_live_warmup_and_hot_reload_call_projection_ack_hook():
    bootstrap_source = Path(
        "backend/services/live_factor_bootstrap.py"
    ).read_text(encoding="utf-8")
    facade_source = Path("backend/services/live_service.py").read_text(
        encoding="utf-8"
    )

    assert bootstrap_source.count("runtime.acknowledge_projections(") >= 2
    assert "acknowledge_projections=_loop_ack_prepared_factor_projections" in (
        facade_source
    )


def test_shadow_route_forwards_additive_definition_and_v16(monkeypatch):
    import backend.api.shadow as shadow_api

    captured = {}

    def fake_promote(name, **kwargs):
        captured.update({"name": name, **kwargs})
        return {"ok": True, "lifecycle_stage": "PROMOTION_PREPARED"}

    monkeypatch.setattr(shadow_api, "promote", fake_promote)
    monkeypatch.setattr(shadow_api, "record_api_mutation", lambda **_kwargs: None)
    request = shadow_api.PromoteRequest(
        name="candidate",
        expression="ts_mean(close, 5)",
        artifact_hash="a" * 64,
        v16_command_id="cmd-1",
        v16_claim_token="claim-1",
    )

    result = shadow_api.promote_factor({"sub": "alice"}, request)

    assert result["ok"] is True
    assert captured["expression"] == "ts_mean(close, 5)"
    assert captured["artifact_hash"] == "a" * 64
    assert captured["actor"] == "operator:alice"
    assert captured["v16"].command_id == "cmd-1"
    assert captured["v16"].claim_token == "claim-1"


def test_discovered_factor_without_explicit_weight_never_gets_implicit_default(monkeypatch, tmp_path):
    import backend.services.factor_catalog as catalog

    name = "discovered_without_weight"

    def factor(_df):
        return 0.0

    factor_registry._factors[name] = factor
    cfg = SimpleNamespace(
        factor_signal_config={
            name: {
                "enabled": True,
                "role": "alpha",
                "lifecycle_status": "ACTIVE",
            }
        },
        factor_portfolio_weights={},
    )
    monkeypatch.setattr(catalog, "runtime_config", lambda: cfg)
    monkeypatch.setattr(catalog, "_health_by_factor", lambda _db: {})
    monkeypatch.setattr(catalog, "_canary_by_factor", lambda _db: {})
    monkeypatch.setattr(catalog, "_latest_policy_by_factor_for_db", lambda _db: {})
    monkeypatch.setattr(catalog, "_factor_governance_shadow_by_factor", lambda _db: {})
    monkeypatch.setattr(catalog, "_latest_catalog_snapshot_meta", lambda _db: {})
    monkeypatch.setattr(catalog, "_shadow_perf", lambda _name: {})
    try:
        item = next(row for row in catalog.build_factor_catalog(tmp_path / "catalog.sqlite") if row["factor_id"] == name)
    finally:
        factor_registry._factors.pop(name, None)

    assert item["weight"] == 0.0
    assert item["explicit_weight"] is False
    assert item["used_in_score"] is False


def test_legacy_compact_hash_artifact_still_acknowledges_builtin(tmp_path):
    """Pre-2026-08-31 preparations bound the compact-separator digest; the
    ack gate must accept it so the 08-31 hash consolidation does not orphan
    in-flight builtin promotions (harami et al stuck 9 days)."""
    from backend.services.factor_lifecycle_service import _compact_hash, _legacy_builtin_hash

    service = FactorLifecycleService(
        tmp_path / "lifecycle.sqlite",
        projection_stale_after_sec=75,
        health_stale_after_sec=180,
    )
    prepared = service.prepare_promotion(
        name="harami", evidence_refs=_candidate_admission_refs()
    )
    assert prepared["ok"] is True
    legacy = _legacy_builtin_hash("harami")
    assert legacy is not None
    state = service.get_state(factor_name="harami")
    assert state["artifact_hash"] != legacy
    # Pre-cutover rows carry a self-consistent legacy triple
    # (artifact/fingerprint/factor id all under the old contract).
    legacy_fp = _compact_hash(
        {"schema_version": "builtin_factor_identity.v1", "name": "harami", "artifact_hash": legacy}
    )
    conn = sqlite3.connect(tmp_path / "lifecycle.sqlite")
    try:
        conn.execute(
            "UPDATE factor_lifecycle_state SET artifact_hash=?, definition_fingerprint=?, factor_id=? WHERE factor_name=?",
            (legacy, legacy_fp, f"builtin:{legacy_fp}", "harami"),
        )
        conn.commit()
    finally:
        conn.close()
    engine = StreamingFactorEngine(
        max_buffer=80,
        factor_runtime_config=runtime_config.shared().factor_signal_config,
    )
    engine.warmup_bars(
        [
            {
                "open": 1900.0 + idx,
                "high": 1901.0 + idx,
                "low": 1899.0 + idx,
                "close": 1900.5 + idx,
                "volume": 100.0 + idx,
                "time": float(idx + 1),
                "complete": True,
            }
            for idx in range(60)
        ]
    )

    result = service.acknowledge_loaded_prepared_factors(
        engine=engine,
        boot_id="live-generation-legacy",
    )

    assert result["acknowledged_count"] == 1
    assert result["blocked_count"] == 0


def test_retire_threads_cause_keys_into_metadata(lifecycle):
    """Retire evidence carrying retire_cause + regime_id must land in
    metadata_json (additive keys) with stage RETIRED."""
    service, _adapter, name, _expression = lifecycle
    result = service.retire(
        name=name,
        evidence_refs={"retire_cause": "param_mismatch", "regime_id": "trend"},
        idempotency_key="retire-cause-once",
    )

    assert result["ok"] is True
    assert result["lifecycle_stage"] == FactorLifecycleStage.RETIRED.value
    state = service.get_state(factor_name=name)
    assert state["lifecycle_stage"] == FactorLifecycleStage.RETIRED.value
    metadata = json.loads(state["metadata_json"])
    assert metadata["retire_cause"] == "param_mismatch"
    assert metadata["regime_id"] == "trend"


def test_builtin_prepared_demotes_to_shadow_same_generation(tmp_path):
    """Stale builtin PREPARED exits via same-generation demote (prepared
    factors cannot vote, so the demote reduces nothing and risks nothing)."""
    adapter = FakeAdapter("harami", "harami")  # type: ignore[arg-type]
    adapter.meta["harami"]["source"] = SOURCE_BUILTIN
    service = FactorLifecycleService(
        tmp_path / "lifecycle.sqlite",
        adapter=adapter,  # type: ignore[arg-type]
        projection_stale_after_sec=75,
        health_stale_after_sec=180,
    )
    prepared = service.prepare_promotion(
        name="harami", evidence_refs=_candidate_admission_refs()
    )
    assert prepared["ok"] is True
    assert prepared["lifecycle_stage"] == "PROMOTION_PREPARED"
    generation = service.get_state(factor_name="harami")["generation"]

    demoted = service.demote_to_shadow(name="harami", reason="prepared_stale")

    assert demoted["ok"] is True
    state = service.get_state(factor_name="harami")
    assert state["lifecycle_stage"] == "SHADOW"
    assert state["generation"] == generation


def test_builtin_active_demote_stays_excluded(tmp_path):
    """ACTIVE builtins keep the demotion exclusion (downweight/disable own them)."""
    adapter = FakeAdapter("harami", "harami")  # type: ignore[arg-type]
    adapter.meta["harami"]["source"] = SOURCE_BUILTIN
    service = FactorLifecycleService(
        tmp_path / "lifecycle.sqlite",
        adapter=adapter,  # type: ignore[arg-type]
        projection_stale_after_sec=75,
        health_stale_after_sec=180,
    )
    now = time.time()
    _prepare_and_ack(service, "harami", now=now)
    _write_health(service, "harami", now=now)
    activated = _activate_candidate(service, "harami", weight=0.25, now=now)
    assert activated["ok"] is True

    demoted = service.demote_to_shadow(name="harami", reason="prepared_stale")

    assert demoted["ok"] is False
    assert demoted["reason"] == "builtin_factor_demotion_not_supported"
