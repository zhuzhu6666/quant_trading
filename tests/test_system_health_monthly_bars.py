import duckdb

from backend.core import db as core_db
from backend.services import live_service, market_session
from monitor import system_health


def test_system_health_reads_previous_month_when_current_month_is_empty(
    monkeypatch, tmp_path
):
    monthly_dir = tmp_path / "bars_monthly"
    monthly_dir.mkdir()
    current_path = monthly_dir / "bars_2026_08.duckdb"
    previous_path = monthly_dir / "bars_2026_07.duckdb"
    schema = (
        "CREATE TABLE bars ("
        "symbol VARCHAR, timeframe VARCHAR, time BIGINT, "
        "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE"
        ")"
    )

    for path in (current_path, previous_path):
        conn = duckdb.connect(str(path))
        conn.execute(schema)
        conn.close()
    conn = duckdb.connect(str(previous_path))
    conn.executemany(
        "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("XAUUSD+", "M1", 1785531000, 2400.0, 2401.0, 2399.0, 2400.5, 10.0),
            ("XAUUSD+", "M5", 1785531300, 2401.0, 2402.0, 2400.0, 2401.5, 11.0),
        ],
    )
    conn.close()

    monkeypatch.setattr(core_db, "DUCKDB_BARS_MONTHLY_DIR", monthly_dir)
    monkeypatch.setattr(core_db, "DUCKDB_BARS", current_path)
    monkeypatch.setattr(
        live_service, "_market_session_snapshot", lambda _generation_id: {}
    )
    monkeypatch.setattr(
        system_health,
        "_market_closed_for_freshness",
        lambda _now, _latest_ts: (True, "test_closed"),
    )
    monkeypatch.setattr(
        market_session,
        "maintenance_wait_evidence",
        lambda *_args, **_kwargs: {"active": False, "remaining_seconds": 0},
    )

    components = {}
    errors = []
    system_health.SystemHealth()._check_data_freshness(
        components,
        errors,
        report=system_health.HealthReport(),
    )

    assert components["bar_m1"].status == "ok"
    assert components["bar_m5"].status == "ok"
    assert errors == []
