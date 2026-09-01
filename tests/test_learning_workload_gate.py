from backend.services import learning_workload_gate as gate


class _Health:
    def __init__(self, payload):
        self.payload = payload

    def latest(self, **kwargs):
        return dict(self.payload)


class _Watermark:
    def __init__(self, payload):
        self.payload = payload

    def evaluate(self):
        return dict(self.payload)


def _patch(monkeypatch, *, health, watermark, pending=(False, "")):
    monkeypatch.setattr(gate, "RuntimeHealthProjectionService", lambda _db: _Health(health))
    monkeypatch.setattr(gate, "LearningCycleWatermarkService", lambda _db: _Watermark(watermark))
    monkeypatch.setattr(gate, "_pending_governance", lambda _db: pending)


def test_closed_without_new_facts_is_skipped(monkeypatch, tmp_path):
    _patch(
        monkeypatch,
        health={"ok": True, "market_session": {"status": "closed_confirmed", "can_open_positions": False}},
        watermark={"ok": True, "should_run": False},
    )
    result = gate.LearningWorkloadGate(tmp_path / "state.db").evaluate()
    assert result["status"] == gate.SKIP_CLOSED_NO_NEW_FACTS


def test_closed_with_pending_governance_runs_only_governance(monkeypatch, tmp_path):
    _patch(
        monkeypatch,
        health={"ok": True, "market_session": {"status": "closed_confirmed", "can_open_positions": False}},
        watermark={"ok": True, "should_run": False},
        pending=(True, "approved_policy_suggestion"),
    )
    result = gate.LearningWorkloadGate(tmp_path / "state.db").evaluate()
    assert result["status"] == gate.RUN_PENDING_GOVERNANCE
    assert result["pending_governance"] is True


def test_unknown_market_or_watermark_does_not_skip(monkeypatch, tmp_path):
    _patch(
        monkeypatch,
        health={"ok": False, "market_session": {"status": "closed_confirmed", "can_open_positions": False}},
        watermark={"ok": True, "should_run": False},
    )
    result = gate.LearningWorkloadGate(tmp_path / "state.db").evaluate()
    assert result["status"] == gate.RUN_UNKNOWN


def test_new_facts_run_even_when_market_is_closed(monkeypatch, tmp_path):
    _patch(
        monkeypatch,
        health={"ok": True, "market_session": {"status": "closed_confirmed", "can_open_positions": False}},
        watermark={"ok": True, "should_run": True},
    )
    result = gate.LearningWorkloadGate(tmp_path / "state.db").evaluate()
    assert result["status"] == gate.RUN_NEW_FACTS
