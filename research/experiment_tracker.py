"""
Lightweight experiment tracking for quant trading research.

SQLite-backed, no MLFlow dependency. Each experiment is stored as a JSON
blob in a TEXT column for maximum schema flexibility.
"""

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger


@dataclass
class Experiment:
    """Represents a single experiment run."""

    run_id: str
    timestamp: float
    experiment_type: str  # "gp_search" | "backtest" | "parameter_sweep" | "retrain_ml"
    params: dict
    metrics: dict
    tags: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    status: str = "running"  # running | completed | failed


class ExperimentTracker:
    """
    SQLite-backed experiment tracking. Lightweight replacement for MLFlow.

    DB: data/experiments.db
    Table: experiments (TEXT run_id PK, TEXT json_data, REAL updated_at)

    Each experiment is serialised to a JSON blob stored in the TEXT column,
    giving full schema flexibility without migrations.
    """

    def __init__(self, db_path: str = "data/experiments.db"):
        self._db = db_path
        self._init_db()
        logger.debug(f"ExperimentTracker initialised with db={self._db}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create the experiments table if it does not already exist."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS experiments (
                        run_id TEXT PRIMARY KEY,
                        data TEXT,
                        updated_at REAL
                    )
                    """
                )
            logger.debug("Database table guaranteed to exist.")
        except sqlite3.Error as exc:
            logger.error(f"Failed to initialise database: {exc}")
            raise

    def _connect(self) -> sqlite3.Connection:
        """Return a new sqlite3 connection (context-manager friendly).

        Concurrent-access handling: SQLite's default retry logic inside
        ``execute`` usually suffices for this lightweight use case.
        """
        conn = sqlite3.connect(self._db)
        conn.row_factory = sqlite3.Row
        return conn

    def _now(self) -> float:
        """Current UNIX timestamp (UTC) as a float."""
        return datetime.now(timezone.utc).timestamp()

    @staticmethod
    def _serialise(exp: Experiment) -> str:
        return json.dumps({
            "run_id": exp.run_id,
            "timestamp": exp.timestamp,
            "experiment_type": exp.experiment_type,
            "params": exp.params,
            "metrics": exp.metrics,
            "tags": exp.tags,
            "artifacts": exp.artifacts,
            "status": exp.status,
        })

    @staticmethod
    def _deserialise(row: sqlite3.Row) -> Experiment:
        data = json.loads(row["data"])
        return Experiment(
            run_id=data["run_id"],
            timestamp=data["timestamp"],
            experiment_type=data["experiment_type"],
            params=data["params"],
            metrics=data["metrics"],
            tags=data.get("tags", []),
            artifacts=data.get("artifacts", []),
            status=data.get("status", "running"),
        )

    def _upsert(self, exp: Experiment) -> None:
        """Insert or replace an experiment row."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO experiments (run_id, data, updated_at) VALUES (?, ?, ?)",
                    (exp.run_id, self._serialise(exp), self._now()),
                )
        except sqlite3.Error as exc:
            logger.error(f"Failed to upsert run {exp.run_id}: {exc}")
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_run(
        self,
        exp_type: str,
        params: Optional[dict] = None,
        tags: Optional[list] = None,
    ) -> str:
        """Create a new run and return its run_id.

        Parameters
        ----------
        exp_type : str
            Experiment category, e.g. ``"gp_search"``, ``"backtest"``,
            ``"parameter_sweep"``, ``"retrain_ml"``.
        params : dict or None
            Hyper-parameters / configuration dict for this run.
        tags : list[str] or None
            Optional tags to attach to the run.

        Returns
        -------
        str
            The newly created run ID (UUID v4 hex).
        """
        run_id = uuid.uuid4().hex
        exp = Experiment(
            run_id=run_id,
            timestamp=self._now(),
            experiment_type=exp_type,
            params=params or {},
            metrics={},
            tags=tags or [],
            artifacts=[],
            status="running",
        )
        self._upsert(exp)
        logger.info(f"Started run {run_id} ({exp_type})")
        return run_id

    def log_metric(self, run_id: str, key: str, value: float) -> None:
        """Log a single metric to an existing run.

        Metrics are stored as a flat dict. If the key already exists its
        value is overwritten.
        """
        exp = self.get_run(run_id)
        if exp is None:
            raise ValueError(f"Run {run_id} not found — cannot log metric.")
        exp.metrics[key] = value
        self._upsert(exp)
        logger.debug(f"Logged metric {key}={value} to run {run_id}")

    def log_artifact(self, run_id: str, file_path: str) -> None:
        """Record an artifact file path for a run.

        The path is appended to the artifacts list (duplicates are *not*
        automatically deduplicated).
        """
        exp = self.get_run(run_id)
        if exp is None:
            raise ValueError(f"Run {run_id} not found — cannot log artifact.")
        exp.artifacts.append(file_path)
        self._upsert(exp)
        logger.debug(f"Logged artifact {file_path} to run {run_id}")

    def finish_run(self, run_id: str, status: str = "completed") -> None:
        """Mark a run as completed or failed.

        Parameters
        ----------
        run_id : str
        status : str
            ``"completed"`` or ``"failed"`` (default ``"completed"``).
        """
        if status not in ("completed", "failed"):
            raise ValueError(f"Invalid finish status: {status!r}")
        exp = self.get_run(run_id)
        if exp is None:
            raise ValueError(f"Run {run_id} not found — cannot finish.")
        exp.status = status
        self._upsert(exp)
        logger.info(f"Finished run {run_id} with status={status}")

    def get_run(self, run_id: str) -> Optional[Experiment]:
        """Retrieve a single experiment by its run ID.

        Returns ``None`` if the run does not exist in the database.
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT data FROM experiments WHERE run_id = ?", (run_id,)
                ).fetchone()
            if row is None:
                logger.warning(f"Run {run_id} not found.")
                return None
            return self._deserialise(row)
        except sqlite3.Error as exc:
            logger.error(f"Failed to read run {run_id}: {exc}")
            raise

    def query(
        self,
        exp_type: Optional[str] = None,
        tags: Optional[list] = None,
        limit: int = 100,
    ) -> list[Experiment]:
        """Query experiments, optionally filtering by type and/or tags.

        Results are sorted by timestamp descending (most recent first).

        Parameters
        ----------
        exp_type : str or None
            If provided, only runs with this experiment type are returned.
        tags : list[str] or None
            If provided, only runs that contain **all** of the given tags
            are returned.
        limit : int
            Maximum number of results (default 100).

        Returns
        -------
        list[Experiment]
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT data FROM experiments ORDER BY updated_at DESC"
                ).fetchall()
        except sqlite3.Error as exc:
            logger.error(f"Failed to query experiments: {exc}")
            raise

        result: list[Experiment] = []
        for row in rows:
            exp = self._deserialise(row)

            # Filter by experiment type
            if exp_type is not None and exp.experiment_type != exp_type:
                continue

            # Filter by tags (all must be present)
            if tags:
                exp_tag_set = set(exp.tags)
                if not exp_tag_set.issuperset(tags):
                    continue

            result.append(exp)
            if len(result) >= limit:
                break

        return result

    def to_dataframe(self, exp_type: Optional[str] = None) -> "pd.DataFrame":
        """Return experiment data as a pandas DataFrame.

        pandas is a core dependency of this project, so this method will
        always work in production.
        """
        import pandas as pd  # type: ignore[import-untyped]

        runs = self.query(exp_type=exp_type, limit=10_000)
        records = []
        for r in runs:
            records.append({
                "run_id": r.run_id,
                "timestamp": r.timestamp,
                "experiment_type": r.experiment_type,
                "status": r.status,
                "params": r.params,
                **{f"metric_{k}": v for k, v in r.metrics.items()},
                "tags": r.tags,
                "artifacts": r.artifacts,
            })
        return pd.DataFrame(records)

    def get_summary_stats(self) -> dict:
        """Compute aggregate statistics across all tracked experiments.

        Returns
        -------
        dict with keys:
            total_runs, by_type, by_status, avg_metrics (per-type means)
        """
        runs = self.query(limit=10_000)

        total_runs = len(runs)
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        # Accumulate metric values per type for averaging
        metric_sums: dict[str, dict[str, float]] = {}
        metric_counts: dict[str, dict[str, int]] = {}

        for r in runs:
            by_type[r.experiment_type] = by_type.get(r.experiment_type, 0) + 1
            by_status[r.status] = by_status.get(r.status, 0) + 1

            t = r.experiment_type
            if t not in metric_sums:
                metric_sums[t] = {}
                metric_counts[t] = {}
            for k, v in r.metrics.items():
                metric_sums[t][k] = metric_sums[t].get(k, 0.0) + v
                metric_counts[t][k] = metric_counts[t].get(k, 0) + 1

        avg_metrics: dict[str, dict[str, float]] = {}
        for t, sums in metric_sums.items():
            avg_metrics[t] = {}
            for k, total in sums.items():
                avg_metrics[t][k] = round(total / metric_counts[t][k], 6)

        return {
            "total_runs": total_runs,
            "by_type": by_type,
            "by_status": by_status,
            "avg_metrics": avg_metrics,
        }
