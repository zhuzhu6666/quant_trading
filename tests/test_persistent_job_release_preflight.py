from __future__ import annotations

from backend.jobs import release_preflight


class _Cursor:
    description = None

    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = ""
        self.closed = False

    def execute(self, sql):
        self.executed = str(sql)

    def fetchall(self):
        return list(self.rows)

    def close(self):
        self.closed = True


class _Conn:
    def __init__(self, rows):
        self.cursor_value = _Cursor(rows)
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def close(self):
        self.closed = True


def test_persistent_job_release_preflight_passes_read_only_complete_registry(monkeypatch):
    monkeypatch.setattr(
        release_preflight,
        "validate_persistent_job_worker_startup",
        lambda: None,
    )
    conn = _Conn(
        [
            {
                "kind": "backtest",
                "status": "queued",
                "row_count": 2,
                "active_lease_count": 0,
            }
        ]
    )

    result = release_preflight.collect_persistent_job_worker_release_preflight(
        conn_factory=lambda: conn
    )

    assert result["ok"] is True
    assert result["active_lease_count"] == 0
    assert result["unsupported_runnable_kinds"] == []
    assert "SELECT kind, status" in conn.cursor_value.executed
    assert conn.cursor_value.closed is True
    assert conn.closed is True


def test_persistent_job_release_preflight_blocks_active_or_unsupported_work(monkeypatch):
    monkeypatch.setattr(
        release_preflight,
        "validate_persistent_job_worker_startup",
        lambda: None,
    )
    conn = _Conn(
        [
            {
                "kind": "unknown_heavy_job",
                "status": "running",
                "row_count": 1,
                "active_lease_count": 1,
            }
        ]
    )

    result = release_preflight.collect_persistent_job_worker_release_preflight(
        conn_factory=lambda: conn
    )

    assert result["ok"] is False
    assert result["blockers"] == [
        "persistent_job_active_lease_exists_before_enable",
        "persistent_job_kind_unsupported",
    ]
    assert result["unsupported_runnable_kinds"] == ["unknown_heavy_job"]


def test_persistent_job_release_preflight_fails_closed_when_startup_fact_errors(
    monkeypatch,
):
    def fail():
        raise RuntimeError("schema_unavailable")

    monkeypatch.setattr(
        release_preflight,
        "validate_persistent_job_worker_startup",
        fail,
    )

    result = release_preflight.collect_persistent_job_worker_release_preflight(
        conn_factory=lambda: (_ for _ in ()).throw(AssertionError("must_not_connect"))
    )

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["blockers"] == ["persistent_job_worker_preflight_error"]
