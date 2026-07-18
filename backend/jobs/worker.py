"""Process-owned runner for leased PostgreSQL research jobs."""
from __future__ import annotations

import concurrent.futures
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from loguru import logger

from backend.jobs.handlers import JobHandler, persistent_job_handlers
from backend.jobs.pg_queue import ClaimedJob, PgJobQueue


class JobCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerRunResult:
    claimed: bool
    job_id: str = ""
    kind: str = ""
    status: str = "idle"


class PersistentJobWorker:
    """Claim one job at a time while maintaining its lease from this process."""

    def __init__(
        self,
        *,
        queue: PgJobQueue,
        worker_id: str,
        handlers: Mapping[str, JobHandler] | None = None,
        lease_sec: float = 60.0,
        heartbeat_interval_sec: float = 10.0,
        poll_interval_sec: float = 2.0,
        global_limit: int = 2,
        kind_limits: Mapping[str, int] | None = None,
        retry_delay_sec: float = 5.0,
        executor_factory: Callable[[], concurrent.futures.Executor] | None = None,
    ) -> None:
        self.queue = queue
        self.worker_id = str(worker_id or "").strip()
        if not self.worker_id:
            raise ValueError("worker_id_required")
        self.handlers = dict(handlers or persistent_job_handlers())
        self.lease_sec = max(5.0, float(lease_sec))
        self.heartbeat_interval_sec = max(
            0.5,
            min(float(heartbeat_interval_sec), self.lease_sec / 2.0),
        )
        self.poll_interval_sec = max(0.1, float(poll_interval_sec))
        self.global_limit = max(1, int(global_limit))
        configured_kind_limits = {
            str(kind): max(0, int(limit))
            for kind, limit in dict(kind_limits or {}).items()
        }
        # A newly registered heavy handler must never inherit the broader
        # global ceiling by omission.  Default every supported kind to one;
        # release configuration may raise it explicitly, or set zero to keep
        # that kind disabled on this worker generation.
        self.kind_limits = {
            kind: configured_kind_limits.get(kind, 1)
            for kind in self.handlers
        }
        self.retry_delay_sec = max(0.0, float(retry_delay_sec))
        self._executor_factory = executor_factory or (
            lambda: concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="pg-job-handler",
            )
        )

    def _claim(self) -> ClaimedJob | None:
        return self.queue.claim(
            worker_id=self.worker_id,
            supported_kinds=tuple(self.handlers),
            lease_sec=self.lease_sec,
            global_limit=self.global_limit,
            kind_limits=self.kind_limits,
            retry_delay_sec=self.retry_delay_sec,
        )

    def run_once(self, *, stop_event: threading.Event | None = None) -> WorkerRunResult:
        stop_event = stop_event or threading.Event()
        claim = self._claim()
        if claim is None:
            return WorkerRunResult(claimed=False)
        job = claim.state
        handler = self.handlers.get(job.kind)
        if handler is None:
            status = self.queue.fail(
                job.id,
                claim.claim_token,
                f"unsupported_job_kind:{job.kind}",
                retryable=False,
            )
            return WorkerRunResult(True, job.id, job.kind, status)

        cancel_event = threading.Event()
        claim_lost_event = threading.Event()

        def progress(step: str, pct: float, message: str) -> None:
            if cancel_event.is_set():
                raise JobCancelled("job_cancel_requested")
            heartbeat = self.queue.heartbeat(
                job.id,
                claim.claim_token,
                lease_sec=self.lease_sec,
                progress_pct=pct,
                current_step=step,
                log_message=f"[{step} {pct:.0f}%] {message}",
            )
            if not heartbeat.get("ok"):
                claim_lost_event.set()
                cancel_event.set()
                raise JobCancelled(str(heartbeat.get("reason") or "claim_not_owned"))
            if heartbeat.get("cancel_requested"):
                cancel_event.set()
                raise JobCancelled(str(heartbeat.get("reason") or "job_cancel_requested"))

        executor = self._executor_factory()
        future = executor.submit(handler, dict(job.params), progress)
        stopping = False
        heartbeat_retry = False
        try:
            while not future.done():
                wait_sec = (
                    min(1.0, max(0.1, self.heartbeat_interval_sec / 2.0))
                    if heartbeat_retry
                    else self.heartbeat_interval_sec
                )
                if stopping:
                    time.sleep(wait_sec)
                elif stop_event.wait(wait_sec):
                    # A graceful stop drains the current handler while keeping
                    # its lease alive.  A hard process death is recovered by
                    # lease expiry, so two workers never intentionally execute
                    # the same leased job at the same time.
                    stopping = True
                try:
                    heartbeat = self.queue.heartbeat(
                        job.id,
                        claim.claim_token,
                        lease_sec=self.lease_sec,
                    )
                except Exception as exc:
                    # A single connection failure must not abandon the handler
                    # thread and silently stop lease renewal.  Retry at a short
                    # cadence; hard process death is still recovered by lease
                    # expiry in PgJobQueue.
                    heartbeat_retry = True
                    logger.warning(
                        "[job_worker] heartbeat failed job={} kind={}; retrying: {}",
                        job.id,
                        job.kind,
                        exc,
                    )
                    continue
                heartbeat_retry = False
                if not heartbeat.get("ok"):
                    claim_lost_event.set()
                    cancel_event.set()
                elif heartbeat.get("cancel_requested"):
                    cancel_event.set()

            try:
                result = future.result()
            except JobCancelled:
                if claim_lost_event.is_set():
                    return WorkerRunResult(True, job.id, job.kind, "claim_lost")
                self.queue.acknowledge_cancel(job.id, claim.claim_token)
                return WorkerRunResult(True, job.id, job.kind, "cancelled")
            except Exception as exc:
                if claim_lost_event.is_set():
                    return WorkerRunResult(True, job.id, job.kind, "claim_lost")
                error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1000:]}"
                status = self.queue.fail(
                    job.id,
                    claim.claim_token,
                    error,
                    retryable=True,
                    retry_delay_sec=self.retry_delay_sec,
                )
                logger.error("[job_worker] job={} kind={} failed status={}: {}", job.id, job.kind, status, exc)
                return WorkerRunResult(True, job.id, job.kind, status)

            if claim_lost_event.is_set():
                return WorkerRunResult(True, job.id, job.kind, "claim_lost")
            if cancel_event.is_set():
                self.queue.acknowledge_cancel(job.id, claim.claim_token)
                return WorkerRunResult(True, job.id, job.kind, "cancelled")
            status = self.queue.complete(job.id, claim.claim_token, result)
            return WorkerRunResult(True, job.id, job.kind, status)
        finally:
            # Never abandon a running Python thread: ThreadPoolExecutor cannot
            # kill one, and returning early would leave an untracked handler
            # mutating state after its lease was reassigned.
            executor.shutdown(wait=True, cancel_futures=False)

    def run_forever(self, *, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                result = self.run_once(stop_event=stop_event)
                if not result.claimed:
                    stop_event.wait(self.poll_interval_sec)
            except Exception as exc:
                logger.exception("[job_worker] claim/run cycle failed: {}", exc)
                stop_event.wait(self.poll_interval_sec)
