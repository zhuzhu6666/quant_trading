from __future__ import annotations

import time

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
