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
        """Create/migrate the canonical structured experiment table."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS experiments (
                        run_id TEXT PRIMARY KEY,
                        experiment_type TEXT,
                        params_json TEXT DEFAULT '{}',
                        metrics_json TEXT DEFAULT '{}',
                        tags_json TEXT DEFAULT '[]',
                        artifacts_json TEXT DEFAULT '[]',
                        status TEXT DEFAULT 'running',
                        timestamp REAL,
                        created_at REAL
                    )
                    """
                )
                columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(experiments)").fetchall()}
                for name, ddl in {
                    "experiment_type": "experiment_type TEXT",
                    "params_json": "params_json TEXT DEFAULT '{}'",
                    "metrics_json": "metrics_json TEXT DEFAULT '{}'",
                    "tags_json": "tags_json TEXT DEFAULT '[]'",
                    "artifacts_json": "artifacts_json TEXT DEFAULT '[]'",
                    "status": "status TEXT DEFAULT 'running'",
                    "timestamp": "timestamp REAL",
                    "created_at": "created_at REAL",
                }.items():
                    if name not in columns:
                        conn.execute(f"ALTER TABLE experiments ADD COLUMN {name} {ddl}")
                # One older tracker stored a JSON blob in `data`.  Migrate it
                # in place so both historical rows and the canonical schema
                # remain readable without creating a second source of truth.
                columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(experiments)").fetchall()}
                if "data" in columns:
                    rows = conn.execute(
                        "SELECT run_id, data FROM experiments WHERE (experiment_type IS NULL OR experiment_type='') AND data IS NOT NULL"
                    ).fetchall()
                    for row in rows:
                        try:
                            payload = json.loads(row["data"] or "{}")
                        except Exception:
                            continue
                        conn.execute(
                            """UPDATE experiments SET experiment_type=?, params_json=?, metrics_json=?,
                               tags_json=?, artifacts_json=?, status=?, timestamp=?, created_at=?
                               WHERE run_id=?""",
                            (
                                str(payload.get("experiment_type") or "unknown"),
                                json.dumps(payload.get("params") or {}),
                                json.dumps(payload.get("metrics") or {}),
                                json.dumps(payload.get("tags") or []),
                                json.dumps(payload.get("artifacts") or []),
                                str(payload.get("status") or "running"),
                                float(payload.get("timestamp") or self._now()),
                                float(payload.get("timestamp") or self._now()),
                                row["run_id"],
                            ),
                        )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_experiments_type ON experiments(experiment_type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(status)")
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
        keys = set(row.keys())
        if "experiment_type" in keys and row["experiment_type"]:
            return Experiment(
                run_id=str(row["run_id"]),
                timestamp=float(row["timestamp"] or row["created_at"] or 0.0),
                experiment_type=str(row["experiment_type"]),
                params=json.loads(row["params_json"] or "{}"),
                metrics=json.loads(row["metrics_json"] or "{}"),
                tags=json.loads(row["tags_json"] or "[]"),
                artifacts=json.loads(row["artifacts_json"] or "[]"),
                status=str(row["status"] or "running"),
            )
        data = json.loads(row["data"] or "{}")
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
                    """INSERT INTO experiments
                       (run_id, experiment_type, params_json, metrics_json, tags_json,
                        artifacts_json, status, timestamp, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(run_id) DO UPDATE SET
                         experiment_type=excluded.experiment_type,
                         params_json=excluded.params_json,
                         metrics_json=excluded.metrics_json,
                         tags_json=excluded.tags_json,
                         artifacts_json=excluded.artifacts_json,
                         status=excluded.status,
                         timestamp=excluded.timestamp""",
                    (
                        exp.run_id,
                        exp.experiment_type,
                        json.dumps(exp.params, ensure_ascii=False),
                        json.dumps(exp.metrics, ensure_ascii=False),
                        json.dumps(exp.tags, ensure_ascii=False),
                        json.dumps(exp.artifacts, ensure_ascii=False),
                        exp.status,
                        exp.timestamp,
                        exp.timestamp,
                    ),
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
        run_id: str | None = None,
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
        run_id = str(run_id or uuid.uuid4().hex)
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

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        metrics: dict[str, Any] | None = None,
        artifacts: list[str] | None = None,
    ) -> Experiment:
        """Update a run through the canonical store while preserving its API."""
        exp = self.get_run(run_id)
        if exp is None:
            raise ValueError(f"Run {run_id} not found — cannot update.")
        if status is not None:
            exp.status = str(status)
        if metrics:
            exp.metrics.update(metrics)
        if artifacts:
            exp.artifacts.extend(str(item) for item in artifacts)
        self._upsert(exp)
        return exp

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
                    "SELECT * FROM experiments WHERE run_id = ?", (run_id,)
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
                    "SELECT * FROM experiments ORDER BY COALESCE(timestamp, created_at, 0) DESC"
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
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    continue
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
