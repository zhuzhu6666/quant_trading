from __future__ import annotations

import sqlite3
from dataclasses import replace

from backend.services.evolution_ledger import persist_runtime_config_snapshot
from config import runtime_config


def test_unchanged_runtime_config_preserves_occurrences_and_real_change_writes_once(
    tmp_path,
):
    db_path = tmp_path / "evolution.sqlite"
    runtime_config.reset_for_tests()
    current = runtime_config.shared()

    first = persist_runtime_config_snapshot(
        current,
        source="initial",
        run_id="cycle-1",
        db_path=db_path,
    )
    blocked = persist_runtime_config_snapshot(
        current,
        source="blocked_by_evidence",
        run_id="cycle-2",
        db_path=db_path,
    )
    no_change = persist_runtime_config_snapshot(
        current,
        source="no_change",
        run_id="cycle-3",
        db_path=db_path,
    )
    changed = persist_runtime_config_snapshot(
        replace(current, risk_max_daily_trades=9),
        source="governed_change",
        run_id="cycle-4",
        db_path=db_path,
    )
    repeated_change = persist_runtime_config_snapshot(
        replace(current, risk_max_daily_trades=9),
        source="duplicate_candidate",
        run_id="cycle-5",
        db_path=db_path,
    )

    assert blocked["config_version"] == first["config_version"] + 1
    assert blocked["reused"] is False
    assert no_change["config_version"] == blocked["config_version"] + 1
    assert no_change["reused"] is False
    assert changed["config_version"] == no_change["config_version"] + 1
    assert changed["reused"] is False
    assert repeated_change["config_version"] == changed["config_version"] + 1
    assert repeated_change["reused"] is False
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT config_version, source FROM runtime_config_snapshot ORDER BY config_version"
        ).fetchall()
    finally:
        conn.close()
        runtime_config.reset_for_tests()
    assert rows == [
        (first["config_version"], "initial"),
        (blocked["config_version"], "blocked_by_evidence"),
        (no_change["config_version"], "no_change"),
        (changed["config_version"], "governed_change"),
        (repeated_change["config_version"], "duplicate_candidate"),
    ]
