from __future__ import annotations

import json

from backend.jobs import release_preflight
from backend.core.static_feature_flags import static_feature_flags_fingerprint


class _Cursor:
    description = None

    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = ""
        self.closed = False

    def execute(self, sql, params=None):
        self.executed = str(sql)

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

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


def test_persistent_job_worker_capability_requires_fresh_final_process(monkeypatch):
    from backend.jobs.manager import JobManager

    flags = {"pg_job_queue_v2_enabled": True}
    process_flags = {
        "schema_version": "static_feature_flags.v1",
        "values": flags,
        "fingerprint": static_feature_flags_fingerprint(flags),
        "pid": 4321,
        "process_started_at": 900.0,
    }
    payload = {
        "schema_version": "persistent_job_worker_capability.v1",
        "worker_id": "worker-a",
        "boot_id": "boot-a",
        "pid": 4321,
        "started_at": 900.0,
        "updated_at": 995.0,
        "status": "idle",
        "handler_kinds": sorted(JobManager.PERSISTENT_JOB_KINDS),
        "process_static_feature_flags": process_flags,
    }
    conn = _Conn([{"value_json": json.dumps(payload), "updated_at": 995.0}])

    result = release_preflight.collect_persistent_job_worker_capability(
        expected_flags=flags,
        conn_factory=lambda: conn,
        now=lambda: 1000.0,
    )

    assert result["ok"] is True
    assert result["age_seconds"] == 5.0
    assert result["handler_kinds"] == sorted(JobManager.PERSISTENT_JOB_KINDS)
    assert conn.cursor_value.closed is True
    assert conn.closed is True


def test_persistent_job_worker_capability_fails_closed_for_stale_or_wrong_flags():
    from backend.jobs.manager import JobManager

    expected_flags = {"pg_job_queue_v2_enabled": True}
    loaded_flags = {"pg_job_queue_v2_enabled": False}
    payload = {
        "schema_version": "persistent_job_worker_capability.v1",
        "worker_id": "worker-a",
        "boot_id": "boot-a",
        "pid": 4321,
        "started_at": 900.0,
        "updated_at": 900.0,
        "status": "idle",
        "handler_kinds": sorted(JobManager.PERSISTENT_JOB_KINDS),
        "process_static_feature_flags": {
            "schema_version": "static_feature_flags.v1",
            "values": loaded_flags,
            "fingerprint": static_feature_flags_fingerprint(loaded_flags),
            "pid": 4321,
            "process_started_at": 900.0,
        },
    }
    conn = _Conn([{"value_json": payload, "updated_at": 900.0}])

    result = release_preflight.collect_persistent_job_worker_capability(
        expected_flags=expected_flags,
        conn_factory=lambda: conn,
        now=lambda: 1000.0,
    )

    assert result["ok"] is False
    assert result["blockers"] == [
        "persistent_job_worker_capability_stale",
        "persistent_job_worker_static_flags_unconfirmed",
    ]


def test_persistent_job_worker_capability_reports_missing_record():
    conn = _Conn([])

    result = release_preflight.collect_persistent_job_worker_capability(
        expected_flags={"pg_job_queue_v2_enabled": True},
        conn_factory=lambda: conn,
    )

    assert result["ok"] is False
    assert result["blockers"] == ["persistent_job_worker_capability_missing"]
    assert result["age_seconds"] is None
