from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from backend.services import live_service


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


def test_final_open_admission_allows_only_fresh_identified_reconciles():
    now = time.time()
    _publish_fresh_reconciles(now)

    assert live_service._new_risk_reconciliation_blockers(now_ts=now) == []
    assert live_service._open_trade_draining() is False


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
