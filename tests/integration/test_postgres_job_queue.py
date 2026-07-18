from __future__ import annotations

import concurrent.futures
import os
import threading
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from backend.jobs.pg_queue import PgJobQueue


pytestmark = pytest.mark.postgres_integration


@pytest.fixture()
def pg_job_queue():
    from backend.core.db import state_pg_dsn

    dsn = state_pg_dsn().strip()
    if not dsn:
        pytest.skip("QUANT_STATE_PG_DSN is not configured")
    if os.environ.get("CI", "").lower() != "true" and os.environ.get(
        "QUANT_ALLOW_ISOLATED_PG_TESTS", ""
    ) != "1":
        pytest.skip("isolated PostgreSQL queue tests require CI or explicit opt-in")

    schema = f"pytest_job_queue_{uuid.uuid4().hex}"
    admin = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    admin.execute(
        sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
    )
    admin.execute(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            params_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            progress DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            error TEXT NOT NULL DEFAULT '',
            created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            priority INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            available_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            claimed_by TEXT NOT NULL DEFAULT '',
            claim_token TEXT NOT NULL DEFAULT '',
            claimed_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            heartbeat_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            lease_expires_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            idempotency_key TEXT NOT NULL DEFAULT '',
            current_step TEXT NOT NULL DEFAULT '',
            log_tail_json TEXT NOT NULL DEFAULT '[]',
            finished_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            handler_version TEXT NOT NULL DEFAULT 'legacy'
        )
        """
    )
    admin.execute(
        "CREATE UNIQUE INDEX idx_jobs_kind_idempotency ON jobs(kind, idempotency_key) "
        "WHERE idempotency_key <> ''"
    )
    admin.commit()

    def connect():
        conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
        conn.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
        )
        return conn

    clock = [1_000.0]
    try:
        yield PgJobQueue(conn_factory=connect, clock=lambda: clock[0]), clock
    finally:
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.commit()
        admin.close()


def test_claim_is_exclusive_under_concurrent_workers(pg_job_queue) -> None:
    queue, _clock = pg_job_queue
    queued = queue.enqueue("backtest", {"symbol": "XAUUSD+"})
    barrier = threading.Barrier(2)

    def claim(worker_id: str):
        barrier.wait(timeout=2.0)
        return queue.claim(
            worker_id=worker_id,
            supported_kinds=("backtest",),
            global_limit=2,
            kind_limits={"backtest": 2},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("worker-a", "worker-b")))

    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1
    assert claimed[0].state.id == queued.id
    assert claimed[0].state.attempt_count == 1


def test_global_and_per_kind_claim_limits(pg_job_queue) -> None:
    queue, _clock = pg_job_queue
    first_backtest = queue.enqueue("backtest", {"n": 1}, priority=2)
    queue.enqueue("backtest", {"n": 2}, priority=1)
    discover = queue.enqueue("discover", {"n": 3})

    first = queue.claim(
        worker_id="worker-a",
        supported_kinds=("backtest", "discover"),
        global_limit=2,
        kind_limits={"backtest": 1, "discover": 1},
    )
    second = queue.claim(
        worker_id="worker-b",
        supported_kinds=("backtest", "discover"),
        global_limit=2,
        kind_limits={"backtest": 1, "discover": 1},
    )
    third = queue.claim(
        worker_id="worker-c",
        supported_kinds=("backtest", "discover"),
        global_limit=2,
        kind_limits={"backtest": 1, "discover": 1},
    )

    assert first is not None and first.state.id == first_backtest.id
    assert second is not None and second.state.id == discover.id
    assert third is None


def test_idempotent_enqueue_and_cancel_lifecycle(pg_job_queue) -> None:
    queue, _clock = pg_job_queue
    first = queue.enqueue("tuning", {"grid": [1]}, idempotency_key="request-42")
    duplicate = queue.enqueue("tuning", {"grid": [2]}, idempotency_key="request-42")
    assert duplicate.id == first.id
    assert duplicate.params == {"grid": [1]}

    queued = queue.enqueue("discover", {"n": 10})
    assert queue.request_cancel(queued.id) is True
    assert queue.get(queued.id).status == "cancelled"

    running = queue.enqueue("backtest", {"n": 20})
    claim = queue.claim(
        worker_id="worker-a",
        supported_kinds=("backtest",),
        global_limit=1,
    )
    assert claim is not None and claim.state.id == running.id
    assert queue.request_cancel(running.id) is True
    heartbeat = queue.heartbeat(running.id, claim.claim_token)
    assert heartbeat == {
        "ok": True,
        "cancel_requested": True,
        "heartbeat_at": 1_000.0,
    }
    assert queue.acknowledge_cancel(running.id, claim.claim_token) is True
    assert queue.get(running.id).status == "cancelled"


def test_expired_claim_retries_then_fails_at_attempt_limit(pg_job_queue) -> None:
    queue, clock = pg_job_queue
    queued = queue.enqueue("ab_test", {}, max_attempts=2)
    first = queue.claim(
        worker_id="worker-a",
        supported_kinds=("ab_test",),
        lease_sec=5,
        global_limit=1,
    )
    assert first is not None and first.state.id == queued.id

    clock[0] = 1_006.0
    recovered = queue.recover_expired()
    assert recovered == {"cancelled": 0, "retried": 1, "failed": 0}
    assert queue.get(queued.id).status == "retry_wait"

    second = queue.claim(
        worker_id="worker-b",
        supported_kinds=("ab_test",),
        lease_sec=5,
        global_limit=1,
    )
    assert second is not None
    assert second.claim_token != first.claim_token
    assert second.state.attempt_count == 2

    clock[0] = 1_012.0
    exhausted = queue.recover_expired()
    assert exhausted == {"cancelled": 0, "retried": 0, "failed": 1}
    state = queue.get(queued.id)
    assert state.status == "error"
    assert state.error == "worker_lease_expired_max_attempts"


def test_successful_retry_clears_previous_attempt_error(pg_job_queue) -> None:
    queue, _clock = pg_job_queue
    queued = queue.enqueue("backtest", {}, max_attempts=2)
    first = queue.claim(
        worker_id="worker-a",
        supported_kinds=("backtest",),
        global_limit=1,
    )
    assert first is not None and first.state.id == queued.id
    assert queue.fail(
        queued.id,
        first.claim_token,
        "transient_failure",
        retryable=True,
        retry_delay_sec=0.0,
    ) == "retry_wait"

    second = queue.claim(
        worker_id="worker-b",
        supported_kinds=("backtest",),
        global_limit=1,
    )
    assert second is not None and second.state.error == "transient_failure"
    assert queue.complete(queued.id, second.claim_token, {"ok": True}) == "done"

    completed = queue.get(queued.id)
    assert completed is not None
    assert completed.status == "done"
    assert completed.error is None


def test_worker_never_claims_legacy_pre_migration_rows(pg_job_queue) -> None:
    queue, _clock = pg_job_queue
    legacy = queue.enqueue("backtest", {}, handler_version="legacy")

    claim = queue.claim(
        worker_id="worker-a",
        supported_kinds=("backtest",),
        global_limit=1,
    )

    assert claim is None
    assert queue.get(legacy.id).status == "queued"
