from backend.runtime import evolution_orchestrator as evolution


def test_health_commit_immediately_hands_off_to_factor_governance(monkeypatch):
    report = evolution.EvolutionReport()
    report.factor_health_persisted = True
    report.factor_health_updated_at = 100.0
    report.factor_health_cycle_id = "factor_health:100000"
    monkeypatch.setattr(
        evolution,
        "scheduled_evolution_cycle",
        lambda: report,
    )
    calls = []

    import backend.services.autonomous_evolution_runner as nursery
    import backend.services.v16_brain_orchestrator as v16

    monkeypatch.setattr(
        nursery.AutonomousEvolutionNurseryRunner,
        "build_light_readiness",
        lambda _self: {"schema_version": "test.readiness.v1"},
    )
    monkeypatch.setattr(
        v16.V16BrainOrchestratorService,
        "run_once",
        lambda _self, **kwargs: (
            calls.append(("v16", kwargs["source"]))
            or {
                "status": "delegated",
                "snapshot_id": "brain-1",
                "delegated_count": 1,
                "command_count": 1,
            }
        ),
    )

    class _Governance:
        def run_cycle(self, *, trigger_source, v16_handoff):
            calls.append(("governance", trigger_source, v16_handoff))
            return {"status": "ok"}

    import backend.runtime.factor_governance_orchestrator as governance

    monkeypatch.setattr(
        governance.FactorGovernanceOrchestrator,
        "shared",
        classmethod(lambda _cls: _Governance()),
    )
    monkeypatch.setattr(evolution._time, "time", lambda: 110.0)

    result = evolution.scheduled_evolution_with_governance_handoff()

    assert calls == [
        (
            "v16",
            "system:factor_health_handoff.v16:factor_health:100000",
        ),
        (
            "governance",
            "factor_health_handoff:factor_health:100000",
            {
                "status": "delegated",
                "health_cycle_id": "factor_health:100000",
                "snapshot_id": "brain-1",
                "delegated_count": 1,
                "command_count": 1,
                "posterior_fingerprint": "",
            },
        ),
    ]
    assert result.factor_v16_handoff == {
        "status": "delegated",
        "health_cycle_id": "factor_health:100000",
        "snapshot_id": "brain-1",
        "delegated_count": 1,
        "command_count": 1,
    }
    assert result.factor_governance_handoff == {
        "status": "ok",
        "health_cycle_id": "factor_health:100000",
        "health_updated_at": 100.0,
        "started_at": 110.0,
        "delay_seconds": 10.0,
    }


def test_health_handoff_skips_when_persistence_failed(monkeypatch):
    report = evolution.EvolutionReport()
    monkeypatch.setattr(
        evolution,
        "scheduled_evolution_cycle",
        lambda: report,
    )

    calls = []

    class _Governance:
        def run_cycle(self, *, trigger_source, v16_handoff):
            calls.append((trigger_source, v16_handoff))
            return {"status": "waiting_v16_command"}

    import backend.runtime.factor_governance_orchestrator as governance

    monkeypatch.setattr(
        governance.FactorGovernanceOrchestrator,
        "shared",
        classmethod(lambda _cls: _Governance()),
    )

    result = evolution.scheduled_evolution_with_governance_handoff()

    assert result.factor_v16_handoff == {
        "status": "skipped",
        "health_cycle_id": "",
        "reason": "factor_health_not_persisted",
    }
    assert calls == [("evolution_health_unavailable", None)]
    assert result.factor_governance_handoff["status"] == "waiting_v16_command"


def test_closed_no_input_skips_governance_when_no_work_is_pending(monkeypatch):
    report = evolution.EvolutionReport()
    report.gp_skip_reason = "market_closed_no_new_input"
    monkeypatch.setattr(evolution, "scheduled_evolution_cycle", lambda: report)
    monkeypatch.setattr(evolution, "_has_pending_factor_governance_work", lambda: False)

    result = evolution.scheduled_evolution_with_governance_handoff()

    assert result.factor_v16_handoff["reason"] == "market_closed_no_new_input"
    assert result.factor_governance_handoff["status"] == "skipped"
