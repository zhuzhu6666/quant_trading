"""Long-task management with local compatibility and durable PG workers."""
from backend.jobs.manager import JobManager, get_job_manager, shutdown_job_managers_for_tests
from backend.jobs.pg_queue import ClaimedJob, PgJobQueue
from backend.jobs.progress import ProgressCB, noop_progress
from backend.jobs.state import JobState, new_job_id

__all__ = [
    "JobManager", "get_job_manager", "shutdown_job_managers_for_tests",
    "ProgressCB", "noop_progress",
    "JobState", "new_job_id", "ClaimedJob", "PgJobQueue",
]
