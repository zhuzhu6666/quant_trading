from __future__ import annotations

from backend.services.live_execution_recovery import (
    recover_emergency_execution_intents,
)


class _RecoveryBridge:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def recover_execution_intents(self):
        self.calls += 1
        return self.payload


def test_emergency_recovery_uses_broker_contract_without_local_fallback():
    bridge = _RecoveryBridge(
        {"ready": True, "unresolved_count": 0, "recovered": []}
    )
    result = recover_emergency_execution_intents(bridge)

    assert bridge.calls == 1
    assert result["ready"] is True
    assert result["unresolved_count"] == 0


def test_missing_broker_recovery_contract_fails_closed():
    result = recover_emergency_execution_intents(object())

    assert result["ready"] is False
    assert result["enabled"] is True
    assert result["unresolved_count"] is None
    assert result["error"] == "bridge_execution_recovery_contract_missing"


def test_emergency_recovery_preserves_broker_unknown_state():
    payload = {
        "ready": False,
        "enabled": True,
        "unresolved_count": 1,
        "unresolved": [{"mutation_id": "unknown-1"}],
        "error": "broker_recovery_pending",
    }
    bridge = _RecoveryBridge(payload)
    result = recover_emergency_execution_intents(bridge)

    assert bridge.calls == 1
    assert result == payload
