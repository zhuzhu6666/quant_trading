import sqlite3

from backend.core.db import STATE_DB_DDL
from backend.services.nursery_exploration_budget import NurseryExplorationBudgetService


def _service(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.executescript(STATE_DB_DDL)
    conn.close()
    return NurseryExplorationBudgetService(path), path


def _reserve(service, *, reason="weak_signal", setup="setup-a", now=1_700_000_000.0,
             per_reason=5, global_limit=15, setup_limit=1):
    return service.reserve(
        reasons=[reason],
        setup_fingerprint=setup,
        per_reason_limit=per_reason,
        global_limit=global_limit,
        setup_limit=setup_limit,
        ttl_seconds=300,
        now=now,
    )


def test_same_setup_is_limited_and_release_restores_capacity(tmp_path):
    service, _ = _service(tmp_path)
    first = _reserve(service)
    assert first["allowed"] is True
    assert _reserve(service)["status"] == "setup_budget_exhausted"

    service.finalize(first["reservation_id"], consumed=False)
    assert _reserve(service)["allowed"] is True


def test_reason_and_global_limits_count_consumed_reservations(tmp_path):
    service, _ = _service(tmp_path)
    for index in range(2):
        reservation = _reserve(
            service,
            setup=f"setup-{index}",
            per_reason=2,
            global_limit=3,
        )
        service.finalize(reservation["reservation_id"], consumed=True)

    exhausted = _reserve(service, setup="setup-3", per_reason=2, global_limit=3)
    assert exhausted["status"] == "reason_budget_exhausted"

    other = _reserve(service, reason="low_volatility", setup="setup-4", per_reason=5, global_limit=2)
    assert other["status"] == "global_budget_exhausted"


def test_expired_reservation_does_not_consume_budget(tmp_path):
    service, _ = _service(tmp_path)
    first = _reserve(service, setup="setup-a", now=1_700_000_000.0, global_limit=1)
    assert first["allowed"] is True
    later = _reserve(service, setup="setup-b", now=1_700_000_400.0, global_limit=1)
    assert later["allowed"] is True


def test_count_accepts_postgres_dict_rows():
    assert NurseryExplorationBudgetService._count({"count": 3}) == 3
    assert NurseryExplorationBudgetService._count((4,)) == 4
