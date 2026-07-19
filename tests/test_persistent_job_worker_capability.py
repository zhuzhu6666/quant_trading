from __future__ import annotations

import json
import sqlite3

from backend.jobs.capability import PersistentJobWorkerCapability, STATUS_KEY


def _db(tmp_path):
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE runtime_kv "
        "(key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL)"
    )
    conn.commit()
    conn.close()
    return path


def test_job_worker_capability_publishes_process_and_handler_facts(tmp_path):
    now = [1000.0]
    db_path = _db(tmp_path)
    capability = PersistentJobWorkerCapability(
        worker_id="worker-a",
        handler_kinds=("sync", "backtest"),
        db_path=db_path,
        now=lambda: now[0],
    )

    payload = capability.publish("running", force=True)

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT value_json, updated_at FROM runtime_kv WHERE key=?",
        (STATUS_KEY,),
    ).fetchone()
    conn.close()
    persisted = json.loads(row[0])
    assert persisted == payload
    assert persisted["worker_id"] == "worker-a"
    assert persisted["handler_kinds"] == ["backtest", "sync"]
    assert persisted["boot_id"]
    assert persisted["pid"] > 0
    assert persisted["process_static_feature_flags"]["fingerprint"]
    assert row[1] == 1000.0


def test_job_worker_capability_rate_limits_unchanged_status(tmp_path):
    now = [1000.0]
    capability = PersistentJobWorkerCapability(
        worker_id="worker-a",
        handler_kinds=("sync",),
        db_path=_db(tmp_path),
        now=lambda: now[0],
        heartbeat_interval_sec=10.0,
    )
    capability.publish("idle", force=True)
    now[0] = 1005.0

    assert capability.publish("idle") == {"ok": True, "status": "rate_limited"}
    now[0] = 1011.0
    assert capability.publish("idle")["updated_at"] == 1011.0


def test_job_worker_capability_rejects_empty_identity_or_handlers(tmp_path):
    db_path = _db(tmp_path)

    try:
        PersistentJobWorkerCapability(
            worker_id="",
            handler_kinds=("sync",),
            db_path=db_path,
        )
    except ValueError as exc:
        assert str(exc) == "worker_id_required"
    else:
        raise AssertionError("missing worker id must fail")

    try:
        PersistentJobWorkerCapability(
            worker_id="worker-a",
            handler_kinds=(),
            db_path=db_path,
        )
    except ValueError as exc:
        assert str(exc) == "handler_kinds_required"
    else:
        raise AssertionError("missing handlers must fail")
