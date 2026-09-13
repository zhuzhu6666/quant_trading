"""Ranged bar reads must return every row in range after month pruning."""
import duckdb
import pytest

from backend.core import db as core_db
from data.duckdb_store import DuckDBDataStore

SCHEMA = (
    "CREATE TABLE bars ("
    "symbol VARCHAR, timeframe VARCHAR, time BIGINT, "
    "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
    "volume DOUBLE, spread BIGINT"
    ")"
)
BARS = {
    "bars_2026_06.duckdb": [(1782000000, 2300.0)],          # 2026-06-21
    "bars_2026_07.duckdb": [(1783000000, 2400.0), (1783100000, 2401.0)],  # 2026-07
    "bars_2026_08.duckdb": [(1785700000, 2500.0)],          # 2026-08
}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monthly_dir = tmp_path / "bars_monthly"
    monthly_dir.mkdir()
    for name, rows in BARS.items():
        path = monthly_dir / name
        conn = duckdb.connect(str(path))
        conn.execute(SCHEMA)
        conn.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [("XAUUSD+", "M5", ts, px, px, px, px, 1.0, 0) for ts, px in rows],
        )
        conn.close()

    monkeypatch.setattr(core_db, "DUCKDB_BARS_MONTHLY_DIR", monthly_dir)
    monkeypatch.setattr(DuckDBDataStore, "_instance", None)
    monkeypatch.setattr(core_db, "refresh_current_bars_link", lambda *_a, **_k: None)
    return DuckDBDataStore(tmp_path / "bars.duckdb")


def test_ranged_read_keeps_every_row_in_range(store):
    inside_july = store.load_bars("XAUUSD+", "M5", start=1782950000, end=1783150000)
    assert len(inside_july) == 2
    assert list(inside_july["close"]) == [2400.0, 2401.0]

    # A range spanning the June/July boundary must keep both month databases.
    across_months = store.load_bars("XAUUSD+", "M5", start=1782000000, end=1783100000)
    assert len(across_months) == 3
    assert list(across_months["close"]) == [2300.0, 2400.0, 2401.0]
