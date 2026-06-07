"""Long-task management (in-process queue, in-memory state)."""
from backend.jobs.manager import JobManager, get_job_manager
from backend.jobs.progress import ProgressCB, noop_progress
from backend.jobs.state import JobState, new_job_id

__all__ = [
    "JobManager", "get_job_manager",
    "ProgressCB", "noop_progress",
    "JobState", "new_job_id",
]
