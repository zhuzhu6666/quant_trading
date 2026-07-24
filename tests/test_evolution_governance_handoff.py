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

    class _Governance:
        def run_cycle(self, *, trigger_source):
            calls.append(trigger_source)
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
        "factor_health_handoff:factor_health:100000"
    ]
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

    result = evolution.scheduled_evolution_with_governance_handoff()

    assert result.factor_governance_handoff == {
        "status": "skipped",
        "reason": "factor_health_not_persisted",
    }
