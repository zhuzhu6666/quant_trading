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


def test_emergency_recovery_prefers_broker_contract_over_local_ledger():
    bridge = _RecoveryBridge(
        {"ready": True, "unresolved_count": 0, "recovered": []}
    )
    local_calls = []

    result = recover_emergency_execution_intents(
        bridge,
        enabled=True,
        read_local_unresolved=lambda: local_calls.append(True),
    )

    assert bridge.calls == 1
    assert local_calls == []
    assert result["ready"] is True
    assert result["unresolved_count"] == 0


def test_enabled_emergency_recovery_missing_bridge_contract_fails_closed():
    result = recover_emergency_execution_intents(
        object(),
        enabled=True,
        read_local_unresolved=lambda: [],
    )

    assert result["ready"] is False
    assert result["enabled"] is True
    assert result["unresolved_count"] is None
    assert result["error"] == "bridge_execution_recovery_contract_missing"


def test_disabled_compat_recovery_uses_fsynced_local_unknown_ledger():
    unresolved = [{"mutation_id": "unknown-1"}]
    result = recover_emergency_execution_intents(
        object(),
        enabled=False,
        read_local_unresolved=lambda: unresolved,
    )

    assert result["ready"] is False
    assert result["enabled"] is False
    assert result["unresolved_count"] == 1
    assert result["unresolved"] == unresolved


def test_local_emergency_recovery_failure_is_unknown_not_empty():
    def unavailable():
        raise OSError("ledger unreadable")

    result = recover_emergency_execution_intents(
        object(),
        enabled=False,
        read_local_unresolved=unavailable,
    )

    assert result["ready"] is False
    assert result["unresolved_count"] is None
    assert result["unresolved"] == []
    assert result["error"].startswith(
        "local_execution_recovery_unavailable:OSError:ledger unreadable"
    )
