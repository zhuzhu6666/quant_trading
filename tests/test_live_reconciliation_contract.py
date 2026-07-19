import time
from types import SimpleNamespace

from backend.services.live_reconciliation import explicit_account_reconcile
from execution.base import PositionReconcileResult


class _Bridge:
    is_connected = True

    def __init__(self) -> None:
        self.confirmed_empty_positions = None

    def reconcile_account(
        self,
        *,
        force=True,
        allow_cache_fallback=False,
        confirmed_empty_positions=None,
    ):
        self.confirmed_empty_positions = confirmed_empty_positions
        now = time.time()
        return SimpleNamespace(
            status="fresh",
            reconcile_id="account-1",
            observed_at=now,
            account={"balance": 1000.0, "equity": 1000.0},
        )


def test_explicit_account_reconcile_propagates_immutable_position_contract() -> None:
    now = time.time()
    positions = PositionReconcileResult(
        reconcile_id="positions-1",
        status="fresh",
        positions=(),
        observed_at=now,
        generated_at=now,
    )
    bridge = _Bridge()

    result = explicit_account_reconcile(
        bridge,
        positions_reconcile=positions,
    )

    assert result is not None
    assert bridge.confirmed_empty_positions is positions


def test_explicit_account_reconcile_does_not_upgrade_compat_projection() -> None:
    bridge = _Bridge()

    result = explicit_account_reconcile(
        bridge,
        positions_reconcile={
            "status": "fresh",
            "positions": [],
            "observed_at": time.time(),
        },
    )

    assert result is not None
    assert bridge.confirmed_empty_positions is None
