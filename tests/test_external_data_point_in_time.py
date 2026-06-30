from __future__ import annotations

import pandas as pd

from backend.core.db import connect_duckdb
from data.external_loader import ExternalDataLoader
from data.external_schema import ensure_external_schema, etf_release_at


def _bars(*dates: str) -> pd.DataFrame:
    idx = pd.to_datetime(list(dates))
    return pd.DataFrame({"close": range(len(idx))}, index=idx)


def test_cot_release_at_prevents_future_visibility(tmp_path):
    external = tmp_path / "external.duckdb"
    events = tmp_path / "events.duckdb"
    ensure_external_schema(external)
    con = connect_duckdb(external)
    try:
        con.execute(
            """
            INSERT INTO cot_gold
            (report_date, open_interest, mm_long, mm_short, mm_spread,
             pm_long, pm_short, swap_long, swap_short, other_long, other_short,
             release_at, fetched_at, source)
            VALUES ('2026-01-06', 1000, 700, 200, 0, 100, 300, 0, 0, 0, 0,
                    ?, 1, 'test')
            """,
            [pd.Timestamp("2026-01-09 21:30:00").timestamp()],
        )
    finally:
        con.close()
    connect_duckdb(events).execute("CREATE TABLE events(date VARCHAR, type VARCHAR, description VARCHAR, importance INTEGER)").close()

    out = ExternalDataLoader(external, events).align_to_bars(
        _bars("2026-01-09 20:00:00", "2026-01-09 22:00:00")
    )

    assert pd.isna(out.iloc[0]["cot_mm_net"])
    assert out.iloc[1]["cot_mm_net"] == 500


def test_etf_filing_date_prevents_future_visibility(tmp_path):
    external = tmp_path / "external.duckdb"
    events = tmp_path / "events.duckdb"
    ensure_external_schema(external)
    con = connect_duckdb(external)
    try:
        con.execute(
            """
            INSERT INTO etf_holdings
            (symbol, date, total_tonnes, total_shares, aum_usd, release_at, fetched_at, source)
            VALUES ('GLD', '2026-03-31', 100.5, 10, NULL, ?, 1, 'test')
            """,
            [etf_release_at("2026-05-05", "2026-03-31")],
        )
    finally:
        con.close()
    connect_duckdb(events).execute("CREATE TABLE events(date VARCHAR, type VARCHAR, description VARCHAR, importance INTEGER)").close()

    out = ExternalDataLoader(external, events).align_to_bars(
        _bars("2026-05-05 20:00:00", "2026-05-05 22:00:00")
    )

    assert pd.isna(out.iloc[0]["GLD_tonnes"])
    assert out.iloc[1]["GLD_tonnes"] == 100.5


def test_external_loader_does_not_backfill_before_first_release(tmp_path):
    external = tmp_path / "external.duckdb"
    events = tmp_path / "events.duckdb"
    ensure_external_schema(external)
    con = connect_duckdb(external)
    try:
        con.execute(
            """
            INSERT INTO macro_daily
            (series, date, value, release_at, fetched_at, source)
            VALUES ('DFII10', '2026-01-05', 1.25, ?, 1, 'test')
            """,
            [pd.Timestamp("2026-01-06 00:00:00").timestamp()],
        )
    finally:
        con.close()
    connect_duckdb(events).execute("CREATE TABLE events(date VARCHAR, type VARCHAR, description VARCHAR, importance INTEGER)").close()

    out = ExternalDataLoader(external, events).align_to_bars(
        _bars("2026-01-01", "2026-01-06")
    )

    assert pd.isna(out.iloc[0]["real_yield_10y"])
    assert out.iloc[1]["real_yield_10y"] == 1.25


def test_event_flags_do_not_forward_fill_forever(tmp_path):
    external = tmp_path / "external.duckdb"
    events = tmp_path / "events.duckdb"
    ensure_external_schema(external)
    con = connect_duckdb(events)
    try:
        con.execute("CREATE TABLE events(date VARCHAR, type VARCHAR, description VARCHAR, importance INTEGER)")
        con.execute("INSERT INTO events VALUES ('2026-01-02', 'FOMC', 'FOMC', 3)")
    finally:
        con.close()

    out = ExternalDataLoader(external, events).align_to_bars(
        _bars("2026-01-02 12:00:00", "2026-01-03 12:00:00")
    )

    assert out.iloc[0]["evt_fomc"] == 1
    assert out.iloc[1]["evt_fomc"] == 0


def test_event_hour_buckets_are_signed_and_windowed(tmp_path):
    external = tmp_path / "external.duckdb"
    events = tmp_path / "events.duckdb"
    ensure_external_schema(external)
    con = connect_duckdb(events)
    try:
        con.execute("CREATE TABLE events(date VARCHAR, type VARCHAR, description VARCHAR, importance INTEGER)")
        con.execute("INSERT INTO events VALUES ('2026-01-02', 'FOMC', 'FOMC decision', 3)")
    finally:
        con.close()

    out = ExternalDataLoader(external, events, event_times={"FOMC": "19:00"}).align_to_bars(
        _bars(
            "2026-01-01 20:00:00",
            "2026-01-02 19:30:00",
            "2026-01-03 20:00:00",
            "2026-01-05 20:00:00",
        )
    )

    assert out.iloc[0]["hours_to_fomc"] == -24
    assert out.iloc[1]["hours_to_fomc"] == 0
    assert out.iloc[2]["hours_to_fomc"] == 48
    assert pd.isna(out.iloc[3]["hours_to_fomc"])


def test_precomputed_macro_derived_columns_are_point_in_time(tmp_path):
    external = tmp_path / "external.duckdb"
    events = tmp_path / "events.duckdb"
    ensure_external_schema(external)
    con = connect_duckdb(external)
    try:
        rows = []
        for i in range(6):
            release = pd.Timestamp(f"2026-01-{i + 2:02d} 00:00:00").timestamp()
            rows.append(("DFII10", f"2026-01-{i + 1:02d}", i / 100.0, release, 1, "test"))
        con.executemany(
            """
            INSERT INTO macro_daily (series, date, value, release_at, fetched_at, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    finally:
        con.close()
    connect_duckdb(events).execute("CREATE TABLE events(date VARCHAR, type VARCHAR, description VARCHAR, importance INTEGER)").close()

    out = ExternalDataLoader(external, events).align_to_bars(
        _bars("2026-01-06 23:00:00", "2026-01-07 01:00:00")
    )

    assert pd.isna(out.iloc[0]["real_yield_chg_5d"])
    assert out.iloc[1]["real_yield_chg_5d"] == 5.0


def test_fred_without_key_is_skip_not_failure(monkeypatch):
    import scripts.refresh_external_data as refresh

    finishes = []
    monkeypatch.delenv("QUANT_FRED_API_KEY", raising=False)
    monkeypatch.setattr(refresh, "_env_value", lambda name: "")
    monkeypatch.setattr(refresh, "start_refresh_audit", lambda source: "fred_test")
    monkeypatch.setattr(refresh, "finish_refresh_audit", lambda run_id, **kwargs: finishes.append(kwargs))
    monkeypatch.setattr(refresh, "_get_store", lambda: object())
    monkeypatch.setattr(refresh, "_status_fred", lambda store: {"latest_effective_date": None, "latest_release_at": None, "stale": True, "latest": "空表"})

    assert refresh.refresh_fred(force=True) is True
    assert finishes[-1]["status"] == "skipped"
    assert "QUANT_FRED_API_KEY" in finishes[-1]["error"]


def test_status_latest_timestamp_reads_snapshot_when_duckdb_locked(tmp_path):
    import scripts.refresh_external_data as refresh

    db = tmp_path / "locked.duckdb"
    ensure_external_schema(db)
    seed = connect_duckdb(db)
    try:
        seed.execute(
            "INSERT INTO macro_daily (series, date, value, release_at, fetched_at, source) VALUES ('DFII10', '2026-01-01', 1.0, 1, 1, 'test')"
        )
    finally:
        seed.close()

    writer = connect_duckdb(db)
    try:
        latest = refresh._get_latest_timestamp(object(), "macro_daily", "date", db)
    finally:
        writer.close()

    assert latest.strftime("%Y-%m-%d") == "2026-01-01"
