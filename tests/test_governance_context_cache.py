from __future__ import annotations

import time

import pytest

from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.agent_scorecard import AgentScorecardService
from backend.services.canonical_v2 import (
    ensure_sqlite_schema,
    put_payload,
    read_payload,
    record_review,
)
from backend.services.trade_lesson_memory import upsert_trade_lesson_memory
from backend.services.canonical_v2_reader import review_row


class _CountingConn:
    """sqlite3 wrapper that counts execute() calls through read_payload."""

    def __init__(self, conn):
        self._conn = conn
        self.execute_calls = 0

    def execute(self, sql, params=()):
        self.execute_calls += 1
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture()
def _sqlite_conn(tmp_path):
    conn = connect_sqlite(tmp_path / "state.db")
    conn.executescript(STATE_DB_DDL)
    ensure_sqlite_schema(conn)
    yield conn
    conn.close()


def test_read_payload_cache_skips_sql_and_decodes_once(_sqlite_conn):
    """Blobs are content-addressed and integrity-verified: after the first
    verified read, repeated reads must not re-fetch or re-decompress.  This
    dedupe is what turns the ~58x-per-cycle agent-context rebuilds into one
    set of blob decodes."""
    from backend.services import canonical_v2

    canonical_v2._payload_text_cache_clear()
    value = {"after": {"weight": 0.3}, "before": {"weight": 0.0}}
    ref = put_payload(
        _sqlite_conn, value, payload_kind="factor_state", schema_version="v1"
    )
    assert read_payload(_sqlite_conn, ref.payload_hash) == value  # warm

    counting = _CountingConn(_sqlite_conn)
    assert read_payload(counting, ref.payload_hash) == value
    assert counting.execute_calls == 0


def test_read_payload_cache_never_serves_other_hashes(_sqlite_conn):
    from backend.services import canonical_v2

    canonical_v2._payload_text_cache_clear()
    first = put_payload(
        _sqlite_conn,
        {"v": 1},
        payload_kind="factor_state",
        schema_version="v1",
    )
    second = put_payload(
        _sqlite_conn,
        {"v": 2},
        payload_kind="factor_state",
        schema_version="v1",
    )
    assert read_payload(_sqlite_conn, first.payload_hash) == {"v": 1}
    assert read_payload(_sqlite_conn, second.payload_hash) == {"v": 2}


def test_latest_trade_attributions_cache_collapses_repeat_scans(
    tmp_path, monkeypatch
):
    """The governance cycle audits ~58 actions per run and each audit used to
    rebuild the attribution map from scratch (full recent-review blob scan).
    Within the TTL the second call must not rescan."""
    import backend.services.agent_scorecard as scorecard_module

    canonical_v2_module = pytest.importorskip("backend.services.canonical_v2")
    canonical_v2_module._payload_text_cache_clear()

    db_path = tmp_path / "state.db"
    now = time.time()
    conn = connect_sqlite(db_path)
    conn.executescript(STATE_DB_DDL)
    ensure_sqlite_schema(conn)
    try:
        record_review(
            conn,
            review_id="review_cache_1",
            trade_id="trade_cache_1",
            position_id="pos_cache_1",
            pnl=-5.0,
            mae=-6.0,
            mfe=1.0,
            outcome_label="bad_loss",
            failure_tags=["weak_entry_signal"],
            summary_text="cache check",
            review={"primary_responsibility": "signal_quality"},
            created_at=now,
        )
        row = review_row(conn, "review_cache_1")
        upsert_trade_lesson_memory(conn, row)
        conn.commit()
    finally:
        conn.close()

    calls = {"n": 0}
    real_iter = scorecard_module.iter_review_rows

    def counting_iter(conn, *, limit):
        calls["n"] += 1
        return real_iter(conn, limit=limit)

    monkeypatch.setattr(scorecard_module, "iter_review_rows", counting_iter)

    service = AgentScorecardService(db_path)
    first = service.latest_trade_attributions(
        limit=10, include_external_links=False
    )
    second = service.latest_trade_attributions(
        limit=10, include_external_links=False
    )
    assert calls["n"] == 1
    assert first == second
    assert first["items"][0]["review_id"] == "review_cache_1"
