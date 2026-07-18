"""Durable PostgreSQL job queue with leased, idempotent claims."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from backend.jobs.state import JobState


JOB_CLAIM_ADVISORY_LOCK_ID = 0x51554A4F425131  # ASCII-ish: QUJOBQ1
CLAIMABLE_STATUSES = ("pending", "queued", "retry_wait")
TERMINAL_STATUSES = frozenset({"done", "error", "cancelled"})


def _state_conn():
    from backend.core.db import get_state_pg_conn

    return get_state_pg_conn()


def _json_load(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return dict(row)
    try:
        return dict(row)
    except (TypeError, ValueError):
        raise TypeError("PostgreSQL job queue requires mapping rows") from None


def _utc_datetime(epoch: Any, *, fallback: float | None = None) -> datetime:
    try:
        value = float(epoch or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        value = float(time.time() if fallback is None else fallback)
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _job_state(row: Any) -> JobState:
    item = _row_dict(row)
    status = str(item.get("status") or "queued")
    public_status = "queued" if status == "pending" else status
    finished_at = float(item.get("finished_at") or 0.0)
    result = _json_load(item.get("result_json"), None)
    if result == {} and public_status not in TERMINAL_STATUSES:
        result = None
    return JobState(
        id=str(item.get("id") or ""),
        kind=str(item.get("kind") or ""),
        status=public_status,  # type: ignore[arg-type]
        progress_pct=float(item.get("progress") or 0.0),
        current_step=str(item.get("current_step") or ""),
        started_at=_utc_datetime(item.get("created_at")),
        finished_at=_utc_datetime(finished_at) if finished_at > 0 else None,
        params=_json_load(item.get("params_json"), {}),
        result=result,
        error=str(item.get("error") or "") or None,
        log_tail=list(_json_load(item.get("log_tail_json"), []))[-50:],
        priority=int(item.get("priority") or 0),
        max_attempts=max(1, int(item.get("max_attempts") or 1)),
        attempt_count=max(0, int(item.get("attempt_count") or 0)),
        available_at=float(item.get("available_at") or 0.0),
        claimed_by=str(item.get("claimed_by") or ""),
        heartbeat_at=float(item.get("heartbeat_at") or 0.0),
        lease_expires_at=float(item.get("lease_expires_at") or 0.0),
        cancel_requested=bool(item.get("cancel_requested")),
        idempotency_key=str(item.get("idempotency_key") or ""),
        handler_version=str(item.get("handler_version") or "v1"),
    )


@dataclass(frozen=True)
class ClaimedJob:
    state: JobState
    claim_token: str
    worker_id: str


class PgJobQueue:
    """PostgreSQL queue using transaction leases and ``SKIP LOCKED`` claims."""

    def __init__(
        self,
        *,
        conn_factory: Callable[[], Any] = _state_conn,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._conn_factory = conn_factory
        self._clock = clock

    @staticmethod
    def _close(conn: Any) -> None:
        close = getattr(conn, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _rollback(conn: Any) -> None:
        rollback = getattr(conn, "rollback", None)
        if callable(rollback):
            rollback()

    def enqueue(
        self,
        kind: str,
        params: Mapping[str, Any],
        *,
        idempotency_key: str = "",
        priority: int = 0,
        max_attempts: int = 3,
        available_at: float | None = None,
        handler_version: str = "v1",
    ) -> JobState:
        normalized_kind = str(kind or "").strip()
        if not normalized_kind:
            raise ValueError("job_kind_required")
        normalized_key = str(idempotency_key or "").strip()
        now = float(self._clock())
        job_id = uuid.uuid4().hex
        payload = json.dumps(dict(params or {}), ensure_ascii=False, sort_keys=True, default=str)
        conn = self._conn_factory()
        try:
            row = conn.execute(
                """
                INSERT INTO jobs (
                    id, kind, status, params_json, result_json, progress, error,
                    created_at, updated_at, priority, max_attempts, attempt_count,
                    available_at, claimed_by, claim_token, claimed_at,
                    heartbeat_at, lease_expires_at, cancel_requested,
                    idempotency_key, current_step, log_tail_json, finished_at,
                    handler_version
                ) VALUES (
                    %s, %s, 'queued', %s, '{}', 0.0, '',
                    %s, %s, %s, %s, 0,
                    %s, '', '', 0.0,
                    0.0, 0.0, 0,
                    %s, '', '[]', 0.0,
                    %s
                )
                ON CONFLICT (kind, idempotency_key) WHERE idempotency_key <> ''
                DO NOTHING
                RETURNING *
                """,
                (
                    job_id,
                    normalized_kind,
                    payload,
                    now,
                    now,
                    int(priority),
                    max(1, int(max_attempts)),
                    float(now if available_at is None else available_at),
                    normalized_key,
                    str(handler_version or "v1"),
                ),
            ).fetchone()
            if row is None and normalized_key:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE kind=%s AND idempotency_key=%s",
                    (normalized_kind, normalized_key),
                ).fetchone()
            if row is None:
                raise RuntimeError("job_enqueue_did_not_return_row")
            conn.commit()
            return _job_state(row)
        except Exception:
            self._rollback(conn)
            raise
        finally:
            self._close(conn)

    def recover_expired(self, *, retry_delay_sec: float = 0.0) -> dict[str, int]:
        conn = self._conn_factory()
        try:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (JOB_CLAIM_ADVISORY_LOCK_ID,))
            result = self._recover_expired_on_conn(
                conn,
                now=float(self._clock()),
                retry_delay_sec=retry_delay_sec,
            )
            conn.commit()
            return result
        except Exception:
            self._rollback(conn)
            raise
        finally:
            self._close(conn)

    @staticmethod
    def _rowcount(cursor: Any) -> int:
        try:
            return max(0, int(cursor.rowcount or 0))
        except (TypeError, ValueError, AttributeError):
            return 0

    def _recover_expired_on_conn(
        self,
        conn: Any,
        *,
        now: float,
        retry_delay_sec: float,
    ) -> dict[str, int]:
        cancelled = self._rowcount(conn.execute(
            """
            UPDATE jobs
            SET status='cancelled', finished_at=%s, updated_at=%s,
                claimed_by='', claim_token='', lease_expires_at=0.0
            WHERE status='running' AND cancel_requested=1
              AND lease_expires_at > 0.0 AND lease_expires_at <= %s
            """,
            (now, now, now),
        ))
        retried = self._rowcount(conn.execute(
            """
            UPDATE jobs
            SET status='retry_wait', available_at=%s, updated_at=%s,
                claimed_by='', claim_token='', claimed_at=0.0,
                heartbeat_at=0.0, lease_expires_at=0.0,
                error=CASE WHEN error='' THEN 'worker_lease_expired' ELSE error END
            WHERE status='running' AND cancel_requested=0
              AND lease_expires_at > 0.0 AND lease_expires_at <= %s
              AND attempt_count < max_attempts
            """,
            (now + max(0.0, float(retry_delay_sec)), now, now),
        ))
        failed = self._rowcount(conn.execute(
            """
            UPDATE jobs
            SET status='error', finished_at=%s, updated_at=%s,
                claimed_by='', claim_token='', lease_expires_at=0.0,
                error='worker_lease_expired_max_attempts'
            WHERE status='running' AND cancel_requested=0
              AND lease_expires_at > 0.0 AND lease_expires_at <= %s
              AND attempt_count >= max_attempts
            """,
            (now, now, now),
        ))
        return {"cancelled": cancelled, "retried": retried, "failed": failed}

    def claim(
        self,
        *,
        worker_id: str,
        supported_kinds: Sequence[str],
        lease_sec: float = 60.0,
        global_limit: int = 2,
        kind_limits: Mapping[str, int] | None = None,
        retry_delay_sec: float = 0.0,
    ) -> ClaimedJob | None:
        normalized_worker = str(worker_id or "").strip()
        kinds = sorted({str(kind).strip() for kind in supported_kinds if str(kind).strip()})
        if not normalized_worker:
            raise ValueError("worker_id_required")
        if not kinds or int(global_limit) <= 0:
            return None
        limits = {str(key): max(0, int(value)) for key, value in dict(kind_limits or {}).items()}
        now = float(self._clock())
        conn = self._conn_factory()
        try:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (JOB_CLAIM_ADVISORY_LOCK_ID,))
            self._recover_expired_on_conn(
                conn,
                now=now,
                retry_delay_sec=retry_delay_sec,
            )
            rows = conn.execute(
                """
                SELECT kind, COUNT(*) AS count
                FROM jobs
                WHERE status='running' AND lease_expires_at > %s
                GROUP BY kind
                """,
                (now,),
            ).fetchall()
            running_by_kind = {
                str(_row_dict(row).get("kind") or ""): int(_row_dict(row).get("count") or 0)
                for row in rows
            }
            if sum(running_by_kind.values()) >= int(global_limit):
                conn.commit()
                return None
            allowed = [
                kind
                for kind in kinds
                if running_by_kind.get(kind, 0) < limits.get(kind, int(global_limit))
            ]
            if not allowed:
                conn.commit()
                return None
            row = conn.execute(
                """
                SELECT *
                FROM jobs
                WHERE status IN ('pending', 'queued', 'retry_wait')
                  AND cancel_requested=0
                  AND available_at <= %s
                  AND attempt_count < max_attempts
                  AND handler_version='v1'
                  AND kind = ANY(%s)
                ORDER BY priority DESC, created_at ASC, id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (now, allowed),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            job_id = str(_row_dict(row).get("id") or "")
            claim_token = uuid.uuid4().hex
            updated = conn.execute(
                """
                UPDATE jobs
                SET status='running', claimed_by=%s, claim_token=%s,
                    claimed_at=%s, heartbeat_at=%s, lease_expires_at=%s,
                    attempt_count=attempt_count+1, updated_at=%s
                WHERE id=%s AND status IN ('pending', 'queued', 'retry_wait')
                RETURNING *
                """,
                (
                    normalized_worker,
                    claim_token,
                    now,
                    now,
                    now + max(1.0, float(lease_sec)),
                    now,
                    job_id,
                ),
            ).fetchone()
            if updated is None:
                raise RuntimeError("job_claim_lost_locked_row")
            conn.commit()
            return ClaimedJob(
                state=_job_state(updated),
                claim_token=claim_token,
                worker_id=normalized_worker,
            )
        except Exception:
            self._rollback(conn)
            raise
        finally:
            self._close(conn)

    def heartbeat(
        self,
        job_id: str,
        claim_token: str,
        *,
        lease_sec: float = 60.0,
        progress_pct: float | None = None,
        current_step: str | None = None,
        log_message: str | None = None,
    ) -> dict[str, Any]:
        now = float(self._clock())
        conn = self._conn_factory()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=%s AND status='running' AND claim_token=%s FOR UPDATE",
                (str(job_id), str(claim_token)),
            ).fetchone()
            if row is None:
                conn.commit()
                return {"ok": False, "cancel_requested": False, "reason": "claim_not_owned"}
            item = _row_dict(row)
            logs = list(_json_load(item.get("log_tail_json"), []))
            if log_message:
                logs.append(str(log_message))
                logs = logs[-50:]
            progress = float(item.get("progress") or 0.0)
            if progress_pct is not None:
                progress = max(0.0, min(100.0, float(progress_pct)))
            step = str(item.get("current_step") or "") if current_step is None else str(current_step)
            updated = conn.execute(
                """
                UPDATE jobs
                SET heartbeat_at=%s, lease_expires_at=%s, progress=%s,
                    current_step=%s, log_tail_json=%s, updated_at=%s
                WHERE id=%s AND status='running' AND claim_token=%s
                RETURNING cancel_requested
                """,
                (
                    now,
                    now + max(1.0, float(lease_sec)),
                    progress,
                    step,
                    json.dumps(logs, ensure_ascii=False),
                    now,
                    str(job_id),
                    str(claim_token),
                ),
            ).fetchone()
            conn.commit()
            if updated is None:
                return {"ok": False, "cancel_requested": False, "reason": "claim_not_owned"}
            return {
                "ok": True,
                "cancel_requested": bool(_row_dict(updated).get("cancel_requested")),
                "heartbeat_at": now,
            }
        except Exception:
            self._rollback(conn)
            raise
        finally:
            self._close(conn)

    def complete(self, job_id: str, claim_token: str, result: Any) -> str:
        now = float(self._clock())
        payload = result if isinstance(result, Mapping) else {"value": result}
        conn = self._conn_factory()
        try:
            row = conn.execute(
                """
                UPDATE jobs
                SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'done' END,
                    result_json=%s, progress=CASE WHEN cancel_requested=1 THEN progress ELSE 100.0 END,
                    error=CASE WHEN cancel_requested=1 THEN error ELSE '' END,
                    finished_at=%s, updated_at=%s, heartbeat_at=%s,
                    claimed_by='', claim_token='', lease_expires_at=0.0
                WHERE id=%s AND status='running' AND claim_token=%s
                RETURNING status
                """,
                (
                    json.dumps(dict(payload), ensure_ascii=False, default=str),
                    now,
                    now,
                    now,
                    str(job_id),
                    str(claim_token),
                ),
            ).fetchone()
            conn.commit()
            if row is None:
                raise RuntimeError("job_complete_claim_not_owned")
            return str(_row_dict(row).get("status") or "done")
        except Exception:
            self._rollback(conn)
            raise
        finally:
            self._close(conn)

    def fail(
        self,
        job_id: str,
        claim_token: str,
        error: BaseException | str,
        *,
        retryable: bool = True,
        retry_delay_sec: float = 5.0,
    ) -> str:
        now = float(self._clock())
        message = str(error)[:2000]
        conn = self._conn_factory()
        try:
            row = conn.execute(
                """
                UPDATE jobs
                SET status=CASE
                        WHEN cancel_requested=1 THEN 'cancelled'
                        WHEN %s AND attempt_count < max_attempts THEN 'retry_wait'
                        ELSE 'error'
                    END,
                    available_at=CASE
                        WHEN %s AND cancel_requested=0 AND attempt_count < max_attempts THEN %s
                        ELSE available_at
                    END,
                    error=%s,
                    finished_at=CASE
                        WHEN cancel_requested=1 OR NOT %s OR attempt_count >= max_attempts THEN %s
                        ELSE 0.0
                    END,
                    updated_at=%s, claimed_by='', claim_token='',
                    heartbeat_at=0.0, lease_expires_at=0.0
                WHERE id=%s AND status='running' AND claim_token=%s
                RETURNING status
                """,
                (
                    bool(retryable),
                    bool(retryable),
                    now + max(0.0, float(retry_delay_sec)),
                    message,
                    bool(retryable),
                    now,
                    now,
                    str(job_id),
                    str(claim_token),
                ),
            ).fetchone()
            conn.commit()
            if row is None:
                raise RuntimeError("job_fail_claim_not_owned")
            return str(_row_dict(row).get("status") or "error")
        except Exception:
            self._rollback(conn)
            raise
        finally:
            self._close(conn)

    def acknowledge_cancel(self, job_id: str, claim_token: str) -> bool:
        now = float(self._clock())
        conn = self._conn_factory()
        try:
            row = conn.execute(
                """
                UPDATE jobs
                SET status='cancelled', cancel_requested=1, finished_at=%s,
                    updated_at=%s, claimed_by='', claim_token='', lease_expires_at=0.0
                WHERE id=%s AND status='running' AND claim_token=%s
                RETURNING id
                """,
                (now, now, str(job_id), str(claim_token)),
            ).fetchone()
            conn.commit()
            return row is not None
        except Exception:
            self._rollback(conn)
            raise
        finally:
            self._close(conn)

    def request_cancel(self, job_id: str) -> bool:
        now = float(self._clock())
        conn = self._conn_factory()
        try:
            row = conn.execute(
                """
                UPDATE jobs
                SET cancel_requested=1,
                    status=CASE
                        WHEN status IN ('pending', 'queued', 'retry_wait') THEN 'cancelled'
                        ELSE status
                    END,
                    finished_at=CASE
                        WHEN status IN ('pending', 'queued', 'retry_wait') THEN %s
                        ELSE finished_at
                    END,
                    updated_at=%s
                WHERE id=%s AND status NOT IN ('done', 'error', 'cancelled')
                RETURNING id
                """,
                (now, now, str(job_id)),
            ).fetchone()
            conn.commit()
            return row is not None
        except Exception:
            self._rollback(conn)
            raise
        finally:
            self._close(conn)

    def get(self, job_id: str) -> JobState | None:
        conn = self._conn_factory()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id=%s", (str(job_id),)).fetchone()
            return _job_state(row) if row is not None else None
        finally:
            self._close(conn)

    def list(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[JobState]:
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind=%s")
            params.append(str(kind))
        if status is not None:
            if status == "queued":
                clauses.append("status IN ('pending', 'queued')")
            else:
                clauses.append("status=%s")
                params.append(str(status))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 2000)))
        conn = self._conn_factory()
        try:
            rows = conn.execute(
                f"SELECT * FROM jobs{where} ORDER BY created_at DESC, id DESC LIMIT %s",
                tuple(params),
            ).fetchall()
            return [_job_state(row) for row in rows]
        finally:
            self._close(conn)
