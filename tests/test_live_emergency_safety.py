import json
import threading
import time
from types import SimpleNamespace

import pytest

from backend.services import live_service
from backend.services.live_safety_state import (
    activate_no_new_risk_latch,
    no_new_risk_latch_status,
    reset_safety_state_for_tests,
    safety_outbox_path,
)


@pytest.fixture(autouse=True)
def _isolated_safety_state(monkeypatch, tmp_path):
    reset_safety_state_for_tests()
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path / "safety"))
    monkeypatch.setattr(live_service, "_process_shutdown_requested", False)
    monkeypatch.setattr(live_service, "_EMERGENCY_POST_RECONCILE_TIMEOUT_SEC", 0.0)
    monkeypatch.setattr(live_service, "_merge_recovery_position_meta", lambda *_args, **_kwargs: None)
    yield
    reset_safety_state_for_tests()


def test_latch_persistence_failure_blocks_new_risk_in_process(monkeypatch):
    monkeypatch.setattr(
        "backend.services.live_safety_state._append_fsynced",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(Exception):
        activate_no_new_risk_latch(reason="test_failure")

    assert no_new_risk_latch_status()["active"] is True
    assert no_new_risk_latch_status()["state"] == "persistence_failed_fail_closed"
    assert live_service._open_trade_draining() is True


def test_latch_and_outbox_persistence_failure_still_allows_emergency_close(monkeypatch):
    position = {
        "position_id": 40,
        "symbol": "XAUUSD+",
        "volume": 100.0,
        "open_timestamp": 1_700_000_000.0,
    }
    bridge = _Bridge([
        _reconcile(positions=(position,), reconcile_id="pre-disk-failure"),
        _reconcile(positions=(), reconcile_id="post-disk-failure"),
    ])
    _install(monkeypatch, bridge)
    monkeypatch.setattr(
        "backend.services.live_safety_state._append_fsynced",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    result = live_service.emergency_close("ctrader")

    assert result["status"] == "completed"
    assert result["ok"] is True
    assert result["resume_required"] is True
    assert result["no_new_risk_latch"]["state"] == "persistence_failed_fail_closed"
    assert bridge.close_calls == [(40, 100.0)]
    assert no_new_risk_latch_status()["active"] is True


def _reconcile(status="fresh", positions=(), reconcile_id="r1", error_message=""):
    return SimpleNamespace(
        reconcile_id=reconcile_id,
        status=status,
        positions=tuple(positions),
        observed_at=time.time(),
        generated_at=time.time(),
        error_code="reconcile_failed" if status == "failed" else "",
        error_message=error_message,
        success=status != "failed",
        fresh=status == "fresh",
        authoritative=status == "fresh",
    )


class _Bridge:
    is_connected = True

    def __init__(self, reconciles, close_result=None):
        self.reconciles = list(reconciles)
        self.close_result = close_result or SimpleNamespace(
            success=True,
            outcome="confirmed",
            error_code="",
            comment="",
        )
        self.close_calls = []

    def reconcile_positions(self, *, force=True, allow_cache_fallback=False):
        if len(self.reconciles) > 1:
            return self.reconciles.pop(0)
        return self.reconciles[0]

    def recover_execution_intents(self):
        return {
            "schema": "broker_execution_intent_recovery.v1",
            "ready": True,
            "unresolved_count": 0,
            "unresolved": [],
        }

    def close_position(self, position_id, volume=0.0):
        self.close_calls.append((position_id, volume))
        return self.close_result


class _Policy:
    def evaluate(self, action, context):
        return SimpleNamespace(
            allowed=True,
            reason="risk_reducing_action",
            audit_payload={"action": action, "position_id": context["position_id"]},
            to_dict=lambda: {
                "allowed": True,
                "reason": "risk_reducing_action",
                "audit_payload": {"action": action, "position_id": context["position_id"]},
            },
        )


def _install(monkeypatch, bridge):
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (bridge, None, False))
    monkeypatch.setattr(live_service, "_RISK_POLICY", _Policy())
    monkeypatch.setattr(
        live_service,
        "_lookup_open_decision_context",
        lambda _pid: {"entry_ts": 1.0, "timeframe": "M5", "source": "decision_ledger"},
    )


def test_emergency_latches_before_waiting_for_admitted_open_rpc(monkeypatch):
    bridge = _Bridge([_reconcile(positions=(), reconcile_id="pre")])
    _install(monkeypatch, bridge)
    admitted_rpc = threading.Event()
    release_rpc = threading.Event()
    result_holder = {}

    def _already_admitted_open():
        with live_service._OPEN_TRADE_ADMISSION_LOCK:
            admitted_rpc.set()
            release_rpc.wait(timeout=2.0)

    open_thread = threading.Thread(target=_already_admitted_open)
    open_thread.start()
    assert admitted_rpc.wait(timeout=1.0)

    emergency_thread = threading.Thread(
        target=lambda: result_holder.setdefault("result", live_service.emergency_close("ctrader")),
    )
    emergency_thread.start()
    deadline = time.time() + 1.0
    while not no_new_risk_latch_status().get("active") and time.time() < deadline:
        time.sleep(0.01)

    assert no_new_risk_latch_status()["active"] is True
    assert live_service._open_trade_draining() is True
    assert emergency_thread.is_alive()
    assert not bridge.close_calls

    release_rpc.set()
    open_thread.join(timeout=1.0)
    emergency_thread.join(timeout=1.0)
    assert result_holder["result"]["status"] == "no_positions"


def test_emergency_never_reports_no_positions_with_unresolved_open_intent(monkeypatch):
    class _UnknownOpenBridge(_Bridge):
        def recover_execution_intents(self):
            return {
                "schema": "broker_execution_intent_recovery.v1",
                "ready": False,
                "unresolved_count": 1,
                "unresolved": [{"intent_id": "open-unknown-1", "action": "market_open"}],
            }

    bridge = _UnknownOpenBridge([_reconcile(positions=(), reconcile_id="pre-empty")])
    _install(monkeypatch, bridge)

    result = live_service.emergency_close("ctrader")

    assert result["status"] == "outcome_unknown"
    assert result["ok"] is False
    assert result["pre_reconcile_id"] == "pre-empty"
    assert result["unknown_execution_intent_ids"] == ["open-unknown-1"]
    assert result["resume_required"] is True


def test_emergency_waits_for_delayed_open_then_closes_and_reconciles(monkeypatch):
    position = {"position_id": 401, "symbol": "XAUUSD+", "volume": 100.0}

    class _DelayedOpenBridge(_Bridge):
        def __init__(self):
            super().__init__([
                _reconcile(positions=(), reconcile_id="pre-empty"),
                _reconcile(positions=(position,), reconcile_id="delayed-fill"),
                _reconcile(positions=(), reconcile_id="post-close"),
            ])
            self.recovery_calls = 0

        def recover_execution_intents(self):
            self.recovery_calls += 1
            if self.recovery_calls == 1:
                return {
                    "schema": "broker_execution_intent_recovery.v1",
                    "ready": False,
                    "unresolved_count": 1,
                    "unresolved": [{"intent_id": "open-delayed-1", "action": "market_open"}],
                }
            return {
                "schema": "broker_execution_intent_recovery.v1",
                "ready": True,
                "unresolved_count": 0,
                "unresolved": [],
            }

    bridge = _DelayedOpenBridge()
    _install(monkeypatch, bridge)
    monkeypatch.setattr(live_service, "_EMERGENCY_POST_RECONCILE_TIMEOUT_SEC", 1.0)
    monkeypatch.setattr(live_service, "_EMERGENCY_POST_RECONCILE_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(live_service, "_EMERGENCY_SLEEP", lambda _seconds: None)

    result = live_service.emergency_close("ctrader")

    assert result["status"] == "completed"
    assert result["ok"] is True
    assert result["pre_reconcile_id"] == "delayed-fill"
    assert result["post_reconcile_id"] == "post-close"
    assert result["unknown_execution_intent_ids"] == []
    assert bridge.close_calls == [(401, 100.0)]


def test_emergency_never_completes_when_recovery_materializes_late_position(monkeypatch):
    original = {"position_id": 901, "symbol": "XAUUSD+", "volume": 100.0}
    delayed = {"position_id": 902, "symbol": "XAUUSD+", "volume": 50.0}

    class _RecoveryMaterializesPositionBridge(_Bridge):
        def __init__(self):
            super().__init__(
                [
                    _reconcile(positions=(original,), reconcile_id="pre-existing"),
                    _reconcile(positions=(), reconcile_id="post-original-close"),
                    _reconcile(positions=(delayed,), reconcile_id="post-recovery-late-fill"),
                ]
            )
            self.recovery_calls = 0

        def recover_execution_intents(self):
            self.recovery_calls += 1
            return {
                "schema": "broker_execution_intent_recovery.v1",
                "ready": True,
                "unresolved_count": 0,
                "unresolved": [],
            }

    bridge = _RecoveryMaterializesPositionBridge()
    _install(monkeypatch, bridge)

    result = live_service.emergency_close("ctrader")

    assert bridge.recovery_calls == 1
    assert bridge.close_calls == [(901, 100.0)]
    assert result["status"] == "outcome_unknown"
    assert result["ok"] is False
    assert result["post_reconcile_id"] == "post-recovery-late-fill"
    assert result["remaining_position_ids"] == [902]
    assert result["unknown_position_ids"] == [902]
    assert result["failures"][-1]["error_code"] == (
        "position_materialized_after_execution_recovery"
    )


def test_emergency_unknown_broker_still_latches_before_validation():
    result = live_service.emergency_close("unsupported")

    assert result["status"] == "reconciliation_failed"
    assert result["no_new_risk_latch"]["active"] is True
    assert no_new_risk_latch_status()["active"] is True


def test_emergency_requires_fresh_pre_reconcile(monkeypatch):
    bridge = _Bridge([_reconcile(status="cache", positions=(), reconcile_id="cache")])
    _install(monkeypatch, bridge)

    result = live_service.emergency_close("ctrader")

    assert result["status"] == "reconciliation_failed"
    assert result["ok"] is False
    assert result["pre_reconcile_id"] == "cache"
    assert not bridge.close_calls
    assert result["resume_required"] is True


def test_emergency_rejects_legacy_value_only_positions_contract(monkeypatch):
    class _LegacyBridge:
        is_connected = True

        def refresh_positions(self, **_kwargs):
            raise AssertionError("emergency must not call refresh_positions")

    bridge = _LegacyBridge()
    monkeypatch.setattr(live_service, "_get_ctrader", lambda: (bridge, None, False))

    result = live_service.emergency_close("ctrader")

    assert result["status"] == "reconciliation_failed"
    assert result["reconciliation"]["pre"]["error_code"] == "explicit_position_reconcile_missing"


def test_emergency_pg_and_audit_failures_do_not_change_broker_result(monkeypatch):
    position = {
        "position_id": 41,
        "symbol": "XAUUSD+",
        "volume": 100.0,
        "open_timestamp": 1_700_000_000.0,
    }
    bridge = _Bridge([
        _reconcile(positions=(position,), reconcile_id="pre"),
        _reconcile(positions=(), reconcile_id="post"),
    ])
    _install(monkeypatch, bridge)
    monkeypatch.setattr(
        live_service,
        "_lookup_open_decision_context",
        lambda _pid: (_ for _ in ()).throw(RuntimeError("postgres unavailable")),
    )
    monkeypatch.setattr(
        live_service,
        "_merge_recovery_position_meta",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    result = live_service.emergency_close("ctrader", "XAUUSD+")

    assert result["status"] == "completed"
    assert result["ok"] is True
    assert result["closed"] == 1
    assert result["remaining_position_ids"] == []
    assert bridge.close_calls == [(41, 100.0)]
    events = [json.loads(line) for line in safety_outbox_path().read_text(encoding="utf-8").splitlines()]
    assert {item["event_type"] for item in events} >= {
        "close_risk_context_enrichment_failed",
        "emergency_close_audit_deferred",
    }


def test_emergency_unknown_outcome_with_open_position_stays_unknown(monkeypatch):
    position = {"position_id": 42, "symbol": "XAUUSD+", "volume": 100.0}
    bridge = _Bridge(
        [
            _reconcile(positions=(position,), reconcile_id="pre"),
            _reconcile(positions=(position,), reconcile_id="post"),
        ],
        close_result=SimpleNamespace(
            success=False,
            outcome="unknown",
            error_code="ORDER_TIMEOUT",
            comment="result unknown",
        ),
    )
    _install(monkeypatch, bridge)

    result = live_service.emergency_close("ctrader")

    assert result["status"] == "outcome_unknown"
    assert result["closed"] == 0
    assert result["remaining_position_ids"] == [42]
    assert result["unknown_position_ids"] == [42]
    assert result["ok"] is False


@pytest.mark.parametrize("legacy_outcome", ["", "accepted", "broker_ok"])
def test_emergency_never_infers_confirmation_from_legacy_success(
    monkeypatch,
    legacy_outcome,
):
    position = {"position_id": 420, "symbol": "XAUUSD+", "volume": 100.0}
    bridge = _Bridge(
        [
            _reconcile(positions=(position,), reconcile_id="pre-legacy"),
            _reconcile(positions=(position,), reconcile_id="post-legacy"),
        ],
        close_result=SimpleNamespace(
            success=True,
            outcome=legacy_outcome,
            error_code="",
            comment="legacy result without immutable broker outcome",
        ),
    )
    _install(monkeypatch, bridge)

    result = live_service.emergency_close("ctrader")

    assert result["status"] == "outcome_unknown"
    assert result["remaining_position_ids"] == [420]
    assert result["unknown_position_ids"] == [420]
    assert result["ok"] is False


def test_emergency_unknown_rpc_is_confirmed_by_position_disappearance(monkeypatch):
    position = {"position_id": 43, "symbol": "XAUUSD+", "volume": 100.0}
    bridge = _Bridge(
        [
            _reconcile(positions=(position,), reconcile_id="pre"),
            _reconcile(positions=(), reconcile_id="post"),
        ],
        close_result=SimpleNamespace(
            success=False,
            outcome="unknown",
            error_code="ORDER_TIMEOUT",
            comment="result unknown",
        ),
    )
    _install(monkeypatch, bridge)

    result = live_service.emergency_close("ctrader")

    assert result["status"] == "completed"
    assert result["closed"] == 1
    assert result["unknown_position_ids"] == []


def test_emergency_post_reconcile_failure_never_reports_success(monkeypatch):
    position = {"position_id": 44, "symbol": "XAUUSD+", "volume": 100.0}
    bridge = _Bridge([
        _reconcile(positions=(position,), reconcile_id="pre"),
        _reconcile(status="failed", reconcile_id="post_failed", error_message="network down"),
    ])
    _install(monkeypatch, bridge)

    result = live_service.emergency_close("ctrader")

    assert result["status"] == "reconciliation_failed"
    assert result["post_reconcile_id"] == "post_failed"
    assert result["remaining_position_ids"] == [44]
    assert result["ok"] is False


def test_broker_open_timestamp_is_primary_when_pg_metadata_exists(monkeypatch):
    monkeypatch.setattr(
        live_service,
        "_lookup_open_decision_context",
        lambda _pid: {"entry_ts": 10.0, "timeframe": "M15", "source": "decision_ledger"},
    )

    context = live_service._build_close_position_risk_context(
        position_id=45,
        close_reason="emergency_close",
        position={"position_id": 45, "open_timestamp": 20.0},
        decision_ts=320.0,
    )

    assert context["entry_ts"] == 20.0
    assert context["entry_ts_source"] == "broker_position"
    assert context["temporal_context"]["timeframe"] == "M15"
