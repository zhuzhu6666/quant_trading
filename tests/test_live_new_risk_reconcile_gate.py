from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.services import live_service
from backend.services.live_safety_state import no_new_risk_latch_status


@pytest.fixture(autouse=True)
def _isolated_open_admission(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_SAFETY_STATE_DIR", str(tmp_path / "safety"))
    monkeypatch.setattr(live_service, "no_new_risk_latched", lambda **_kwargs: False)
    monkeypatch.setattr(live_service, "_generation_controller_enabled", lambda: False)
    live_service._process_shutdown_requested = False
    live_service._live_state_update(
        loop_running=True,
        accepting_new_risk=True,
        session_state_status="available",
        circuit_breaker=False,
        account_reconciled=None,
        account_updated_at=None,
        account_reconcile_id=None,
        account_reconcile_failed_at=None,
        positions_reconciled=None,
        positions_updated_at=None,
        positions_reconcile_id=None,
        positions_reconcile_failed_at=None,
        new_risk_reconcile_blockers=[],
    )
    yield
    live_service._process_shutdown_requested = False
    live_service._live_state_update(
        loop_running=False,
        accepting_new_risk=False,
        session_state_status="unknown",
        account_reconciled=None,
        account_updated_at=None,
        account_reconcile_id=None,
        account_reconcile_failed_at=None,
        positions_reconciled=None,
        positions_updated_at=None,
        positions_reconcile_id=None,
        positions_reconcile_failed_at=None,
        new_risk_reconcile_blockers=[],
    )


def _publish_fresh_reconciles(now: float) -> None:
    live_service._live_state_update(
        account_reconciled={"ok": True, "balance": 1000.0, "equity": 1000.0},
        account_updated_at=now,
        account_reconcile_id="account-fresh",
        account_reconcile_failed_at=None,
        positions_reconciled=[],
        positions_updated_at=now,
        positions_reconcile_id="positions-fresh",
        positions_reconcile_failed_at=None,
    )


def _fresh_position_reconcile(*, now: float, positions: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        status="fresh",
        reconcile_id="positions-fresh",
        observed_at=now,
        positions=positions,
        components={},
    )


def test_final_open_admission_allows_only_fresh_identified_reconciles():
    now = time.time()
    _publish_fresh_reconciles(now)

    assert live_service._new_risk_reconciliation_blockers(now_ts=now) == []
    assert live_service._open_trade_draining() is False


def test_fresh_empty_reconcile_conflicting_with_recovery_blocks_new_risk(monkeypatch):
    now = time.time()
    _publish_fresh_reconciles(now)
    monkeypatch.setattr(
        live_service,
        "_enrich_positions_with_path_metrics",
        lambda positions, **_kwargs: positions,
    )
    monkeypatch.setattr(
        live_service,
        "_list_active_recovery_positions",
        lambda _broker: [{"position_id": 101}],
    )

    live_service._publish_fresh_position_reconcile(
        _fresh_position_reconcile(now=now, positions=[]),
        broker="ctrader",
    )

    assert live_service._new_risk_reconciliation_blockers(now_ts=now) == [
        "positions_reconcile_failed"
    ]
    assert live_service._live_state_get("accepting_new_risk") is False
    assert "broker_recovery_position_conflict:101" in str(
        live_service._live_state_get("positions_reconcile_error")
    )
    assert (
        "position_reconcile_conflict",
        "broker_recovery_state",
    ) in {
        (item["cause"], item["cause_id"])
        for item in no_new_risk_latch_status(fail_closed=True)["causes"]
    }


def test_fresh_reconcile_keeps_multiple_aligned_recovery_positions_open(monkeypatch):
    now = time.time()
    _publish_fresh_reconciles(now)
    monkeypatch.setattr(
        live_service,
        "_enrich_positions_with_path_metrics",
        lambda positions, **_kwargs: positions,
    )
    monkeypatch.setattr(
        live_service,
        "_list_active_recovery_positions",
        lambda _broker: [{"position_id": 101}, {"position_id": 202}],
    )

    positions = live_service._publish_fresh_position_reconcile(
        _fresh_position_reconcile(
            now=now,
            positions=[{"position_id": 101}, {"position_id": 202}],
        ),
        broker="ctrader",
    )

    assert [item["position_id"] for item in positions] == [101, 202]
    assert live_service._new_risk_reconciliation_blockers(now_ts=now) == []
    assert live_service._live_state_get("positions_reconcile_error") is None


def test_aligned_reconcile_releases_prior_recovery_conflict_latch(monkeypatch):
    now = time.time()
    _publish_fresh_reconciles(now)
    monkeypatch.setattr(
        live_service,
        "_enrich_positions_with_path_metrics",
        lambda positions, **_kwargs: positions,
    )
    monkeypatch.setattr(
        live_service,
        "_list_active_recovery_positions",
        lambda _broker: [{"position_id": 101}],
    )

    live_service._publish_fresh_position_reconcile(
        _fresh_position_reconcile(now=now, positions=[]),
        broker="ctrader",
    )
    live_service._publish_fresh_position_reconcile(
        _fresh_position_reconcile(now=now + 1.0, positions=[{"position_id": 101}]),
        broker="ctrader",
    )

    assert no_new_risk_latch_status(fail_closed=True)["active"] is False
    assert live_service._new_risk_reconciliation_blockers(now_ts=now + 1.0) == []


def test_final_open_admission_blocks_missing_reconcile_identity():
    now = time.time()
    _publish_fresh_reconciles(now)
    live_service._live_state_update(
        account_reconcile_id=None,
        positions_reconcile_id=None,
    )

    blockers = live_service._new_risk_reconciliation_blockers(now_ts=now)

    assert blockers == ["account_reconcile_unknown", "positions_reconcile_unknown"]
    assert live_service._open_trade_draining() is True

def test_final_open_admission_blocks_stale_or_newer_failed_reconcile():
    now = time.time()
    _publish_fresh_reconciles(now - 16.0)
    stale = live_service._new_risk_reconciliation_blockers(now_ts=now)
    assert stale == ["account_reconcile_stale", "positions_reconcile_stale"]

    _publish_fresh_reconciles(now)
    live_service._live_state_update(
        account_reconcile_failed_at=now + 0.1,
        positions_reconcile_failed_at=now + 0.1,
    )
    failed = live_service._new_risk_reconciliation_blockers(now_ts=now + 0.2)

    assert failed == ["account_reconcile_failed", "positions_reconcile_failed"]
    assert live_service._open_trade_draining() is True


def test_final_open_admission_preserves_specific_reconcile_reason():
    now = time.time()
    _publish_fresh_reconciles(now - 16.0)

    blockers = live_service._open_trade_admission_blockers()

    assert blockers == (
        "account_reconcile_stale",
        "positions_reconcile_stale",
    )
    assert (
        live_service._open_admission_gate_reason(blockers)
        == "account_reconcile_stale"
    )


def test_open_pipeline_refreshes_stale_reconcile_before_candidate(monkeypatch):
    now = time.time()
    _publish_fresh_reconciles(now - 16.0)
    logs: list[str] = []
    refreshes: list[float] = []
    candidate = SimpleNamespace(order_block={"order_blocked": True})
    policy_gate = SimpleNamespace(passed=False, reason="policy_block")

    def _refresh(_bridge, _broker, **_kwargs):
        refreshes.append(time.time())
        _publish_fresh_reconciles(time.time())
        return True

    monkeypatch.setattr(
        live_service,
        "_refresh_account_positions_sync",
        _refresh,
    )
    monkeypatch.setattr(
        live_service,
        "_prepare_open_trade_candidate",
        lambda **_kwargs: candidate,
    )
    monkeypatch.setattr(
        live_service,
        "_record_open_trade_blocked_by_policy",
        lambda **_kwargs: policy_gate,
    )

    result = live_service._run_open_trade_pipeline(
        bridge=SimpleNamespace(is_connected=True),
        pipeline={},
        broker="ctrader",
        cfg=SimpleNamespace(),
        bar={"time": now},
        factor_values={},
        composite=SimpleNamespace(direction=1, score=0.8),
        gate_result=SimpleNamespace(passed=True, reason="passed"),
        account={"balance": 1000.0, "equity": 1000.0},
        positions=[],
        attr_engine=None,
        current_price=4000.0,
        atr_price=4.0,
        pending_open_attach_ids=[],
        send=True,
        tick=77,
        log=logs.append,
    )

    assert result is policy_gate
    assert len(refreshes) == 1
    assert any("refreshing final open reconciles" in item for item in logs)


def test_signal_pass_admission_block_is_audited_without_risk_verdict(monkeypatch):
    now = time.time()
    _publish_fresh_reconciles(now)
    monkeypatch.setattr(live_service, "no_new_risk_latched", lambda **_kwargs: True)
    monkeypatch.setattr(
        live_service,
        "_watchdog_freshness_retry_eligible",
        lambda _blockers: False,
    )

    class _Ledger:
        def __init__(self):
            self.calls = []

        def log_composite_decision(self, **payload):
            self.calls.append(payload)
            return "dec_admission_blocked"

    ledger = _Ledger()
    monkeypatch.setattr(live_service, "_LEDGER", ledger)
    prepare = MagicMock()
    monkeypatch.setattr(live_service, "_prepare_open_trade_candidate", prepare)

    result = live_service._run_open_trade_pipeline(
        bridge=SimpleNamespace(is_connected=True),
        pipeline={},
        broker="ctrader",
        cfg=SimpleNamespace(timeframe="M5"),
        bar={"time": now},
        factor_values={},
        composite=SimpleNamespace(direction=1, score=0.8),
        gate_result=SimpleNamespace(passed=True, reason="passed"),
        account={"balance": 1000.0, "equity": 1000.0},
        positions=[],
        attr_engine=None,
        current_price=4000.0,
        atr_price=4.0,
        pending_open_attach_ids=[],
        send=True,
        tick=1625,
        log=lambda _message: None,
    )

    assert result.passed is False
    assert result.reason == "no_new_risk_latched"
    assert len(ledger.calls) == 1
    action = ledger.calls[0]["action_json"]
    assert action["gate_passed"] is True
    assert action["skip_stage"] == "before_candidate"
    assert action["risk_stage"] == "not_reached"
    assert action["risk_policy_reached"] is False
    assert "risk_verdict" not in action
    assert "no_new_risk_latched" in action["blockers"]
    prepare.assert_not_called()
