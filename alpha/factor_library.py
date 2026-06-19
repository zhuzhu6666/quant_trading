"""
SQLite-backed historical factor archive.

Tracks every factor through its lifecycle: shadow -> canary -> active -> retired.
Stores full FactorRecord objects as JSON blobs in an SQLite database.

Integrates with:
  - evolution_story.jsonl for GP-discovered factors
  - factor_health reports for health history
  - RuntimeConfig for currently active factors
"""

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

from loguru import logger


@dataclass
class FactorRecord:
    """Full lifecycle record for a single factor."""

    name: str
    expression: str = ""
    source: str = "handcrafted"  # handcrafted | gp | ml
    discovery_date: float = 0.0  # unix timestamp
    stage: str = "shadow"  # shadow | canary | active | retired
    retirement_date: float | None = None
    retirement_reason: str = ""
    ic_history: list[dict] = field(default_factory=list)  # [{date, value}, ...]
    sharpe_history: list[dict] = field(default_factory=list)  # [{date, value}, ...]
    health_history: list[dict] = field(default_factory=list)  # [{date, value}, ...]
    current_health: float = 0.0
    current_status: str = "UNKNOWN"
    tags: list[str] = field(default_factory=list)


class FactorLibrary:
    """
    SQLite-backed archive of all factors (past and present).

    DB: data/factor_library.db (or custom path)
    Table: factors (TEXT name PK, TEXT json_data, REAL updated_at)
    """

    def __init__(self, db_path: str = "data/factor_library.db"):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Get or create the database connection."""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self):
        """Ensure the factors table exists."""
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS factors (
                name       TEXT PRIMARY KEY,
                data       TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()
        logger.debug("FactorLibrary DB initialised at {}", self.db_path)

    def _serialize(self, record: FactorRecord) -> str:
        """Serialize a FactorRecord to JSON string."""
        d = asdict(record)
        # Convert None retirement_date so json can handle it
        # (already None-compatible, but be explicit)
        return json.dumps(d, ensure_ascii=False, default=str)

    def _deserialize(self, name: str, json_str: str) -> FactorRecord:
        """Deserialize a JSON string back to a FactorRecord."""
        d = json.loads(json_str)
        # Ensure all fields present with defaults (graceful migration)
        d.setdefault("expression", "")
        d.setdefault("source", "handcrafted")
        d.setdefault("discovery_date", 0.0)
        d.setdefault("stage", "shadow")
        d.setdefault("retirement_date", None)
        d.setdefault("retirement_reason", "")
        d.setdefault("ic_history", [])
        d.setdefault("sharpe_history", [])
        d.setdefault("health_history", [])
        d.setdefault("current_health", 0.0)
        d.setdefault("current_status", "UNKNOWN")
        d.setdefault("tags", [])
        return FactorRecord(name=name, **{k: v for k, v in d.items() if k != "name"})

    def _now(self) -> float:
        """Current unix timestamp."""
        return datetime.now(timezone.utc).timestamp()

    @staticmethod
    def _validate_stage(stage: str):
        """Raise ValueError if stage is invalid."""
        valid = {"shadow", "canary", "active", "retired"}
        if stage not in valid:
            raise ValueError(
                f"Invalid stage '{stage}'. Must be one of {valid}."
            )

    def _upsert(self, record: FactorRecord):
        """Insert or replace a factor record in the database."""
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO factors (name, data, updated_at) VALUES (?, ?, ?)",
            (record.name, self._serialize(record), self._now()),
        )
        conn.commit()
        logger.debug("Factor '{}' upserted (stage={})", record.name, record.stage)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_factor(
        self,
        name: str,
        expression: str = "",
        source: str = "handcrafted",
        tags: list | None = None,
    ) -> bool:
        """
        Register a new factor.

        Returns False if a factor with this name already exists.
        """
        if self.get_factor(name) is not None:
            logger.warning("Factor '{}' already registered — skipping", name)
            return False

        record = FactorRecord(
            name=name,
            expression=expression,
            source=source,
            discovery_date=self._now(),
            tags=tags or [],
        )
        self._upsert(record)
        logger.info("Registered new factor '{}' (source={})", name, source)
        return True

    def update_stage(self, name: str, stage: str, reason: str = ""):
        """
        Update a factor's lifecycle stage.

        Allowed transitions: shadow -> canary -> active -> retired.
        Setting 'retired' auto-records the retirement date and reason.
        """
        self._validate_stage(stage)
        record = self.get_factor(name)
        if record is None:
            logger.error("Factor '{}' not found — cannot update stage", name)
            return

        record.stage = stage
        if stage == "retired":
            record.retirement_date = self._now()
            record.retirement_reason = reason

        self._upsert(record)
        logger.info("Factor '{}' moved to stage '{}'{}", name, stage,
                     f" ({reason})" if reason else "")

    # ---- observation recording ---------------------------------------

    def record_ic(self, name: str, ic_value: float):
        """Append an IC observation. Timestamp auto-generated."""
        record = self.get_factor(name)
        if record is None:
            logger.error("Factor '{}' not found — cannot record IC", name)
            return
        record.ic_history.append({"date": self._now(), "value": ic_value})
        self._upsert(record)
        logger.debug("Recorded IC={:.4f} for '{}'", ic_value, name)

    def record_health(self, name: str, score: float, status: str):
        """Record a health check snapshot."""
        record = self.get_factor(name)
        if record is None:
            logger.error("Factor '{}' not found — cannot record health", name)
            return
        record.health_history.append({"date": self._now(), "value": score})
        record.current_health = score
        record.current_status = status
        self._upsert(record)
        logger.info("Health={:.2f} status='{}' for '{}'", score, status, name)

    def record_sharpe(self, name: str, sharpe_value: float):
        """Record a Sharpe observation."""
        record = self.get_factor(name)
        if record is None:
            logger.error("Factor '{}' not found — cannot record Sharpe", name)
            return
        record.sharpe_history.append({"date": self._now(), "value": sharpe_value})
        self._upsert(record)
        logger.debug("Recorded Sharpe={:.4f} for '{}'", sharpe_value, name)

    # ---- retrieval ---------------------------------------------------

    def get_factor(self, name: str) -> FactorRecord | None:
        """Get the full record for a factor, or None if not found."""
        conn = self._connect()
        row = conn.execute(
            "SELECT data FROM factors WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        return self._deserialize(name, row["data"])

    def query(
        self,
        stage: str | None = None,
        source: str | None = None,
        min_ic: float | None = None,
        status: str | None = None,
    ) -> list[FactorRecord]:
        """
        Query factors with various filters.

        All filters are AND-combined.  ``min_ic`` checks the *last* IC value
        in a factor's history (if any); factors with empty IC history are
        excluded when this filter is active.
        """
        results: list[FactorRecord] = []

        conn = self._connect()
        rows = conn.execute("SELECT name, data FROM factors").fetchall()

        for row in rows:
            name = row["name"]
            rec = self._deserialize(name, row["data"])

            if stage is not None and rec.stage != stage:
                continue
            if source is not None and rec.source != source:
                continue
            if status is not None and rec.current_status != status:
                continue
            if min_ic is not None:
                if not rec.ic_history:
                    continue
                last_ic = rec.ic_history[-1]["value"]
                if last_ic < min_ic:
                    continue

            results.append(rec)

        return results

    def get_active_factors(self) -> list[FactorRecord]:
        """Convenience: all factors in ACTIVE stage."""
        return self.query(stage="active")

    def get_retired_factors(self) -> list[FactorRecord]:
        """Convenience: all factors in RETIRED stage."""
        return self.query(stage="retired")

    # ---- aggregates ---------------------------------------------------

    def get_summary_stats(self) -> dict:
        """
        Return aggregate statistics across all factors.

        Returns:
            dict with keys: total, by_stage, by_source, avg_health,
            avg_ic, avg_lifespan_days
        """
        all_recs = self.query()

        total = len(all_recs)
        by_stage: dict[str, int] = {}
        by_source: dict[str, int] = {}
        health_values: list[float] = []
        ic_values: list[float] = []
        lifespans: list[float] = []

        for rec in all_recs:
            by_stage[rec.stage] = by_stage.get(rec.stage, 0) + 1
            by_source[rec.source] = by_source.get(rec.source, 0) + 1

            if rec.current_health > 0:
                health_values.append(rec.current_health)

            if rec.ic_history:
                ic_values.append(rec.ic_history[-1]["value"])

            if rec.retirement_date and rec.discovery_date:
                lifespan_days = (rec.retirement_date - rec.discovery_date) / 86400.0
                lifespans.append(lifespan_days)

        avg_health = sum(health_values) / len(health_values) if health_values else 0.0
        avg_ic = sum(ic_values) / len(ic_values) if ic_values else 0.0
        avg_lifespan = sum(lifespans) / len(lifespans) if lifespans else 0.0

        return {
            "total": total,
            "by_stage": by_stage,
            "by_source": by_source,
            "avg_health": round(avg_health, 4),
            "avg_ic": round(avg_ic, 6),
            "avg_lifespan_days": round(avg_lifespan, 2),
        }

    # ---- import / export ---------------------------------------------

    def import_from_jsonl(self, jsonl_path: str) -> int:
        """
        Import factor records from an ``evolution_story.jsonl`` file.

        Each JSON line is expected to have at least a ``"name"`` key.
        Other recognised keys: expression, source, stage, tags, ic_history,
        sharpe_history, health_history, current_health, current_status.

        Returns the number of records imported (newly registered factors).
        """
        import_count = 0
        skipped_count = 0

        try:
            with open(jsonl_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("Skipping invalid JSON line in {}", jsonl_path)
                        continue

                    name = entry.get("name")
                    if not name:
                        logger.warning("Skipping entry without 'name' field")
                        skipped_count += 1
                        continue

                    # Register – skip if already exists
                    if not self.register_factor(
                        name=name,
                        expression=entry.get("expression", ""),
                        source=entry.get("source", "handcrafted"),
                        tags=entry.get("tags"),
                    ):
                        skipped_count += 1
                        continue

                    # Update optional fields on the newly created record
                    record = self.get_factor(name)
                    if record is None:
                        continue  # should not happen

                    record.stage = entry.get("stage", record.stage)
                    record.discovery_date = entry.get("discovery_date", record.discovery_date)
                    record.retirement_date = entry.get("retirement_date")
                    record.retirement_reason = entry.get("retirement_reason", "")
                    record.current_health = entry.get("current_health", record.current_health)
                    record.current_status = entry.get("current_status", record.current_status)

                    # Import history arrays if present
                    if "ic_history" in entry and isinstance(entry["ic_history"], list):
                        record.ic_history = entry["ic_history"]
                    if "sharpe_history" in entry and isinstance(entry["sharpe_history"], list):
                        record.sharpe_history = entry["sharpe_history"]
                    if "health_history" in entry and isinstance(entry["health_history"], list):
                        record.health_history = entry["health_history"]

                    self._upsert(record)
                    import_count += 1

            logger.info(
                "Import from {} complete: {} imported, {} skipped",
                jsonl_path, import_count, skipped_count,
            )
            return import_count

        except FileNotFoundError:
            logger.error("JSONL file not found: {}", jsonl_path)
            return 0
        except Exception as exc:
            logger.error("Failed to import from {}: {}", jsonl_path, exc)
            return 0

    def to_dataframe(self) -> "pd.DataFrame":
        """
        Export all factor records as a pandas DataFrame.

        Each row is one factor.  History columns (ic_history, sharpe_history,
        health_history) are kept as JSON strings for compactness.

        Raises ImportError if pandas is not installed.
        """
        try:
            import pandas as pd  # noqa: F811
        except ImportError:
            logger.error("pandas is required for to_dataframe()")
            raise

        conn = self._connect()
        rows = conn.execute("SELECT name, data FROM factors").fetchall()

        records = []
        for row in rows:
            rec = self._deserialize(row["name"], row["data"])
            d = asdict(rec)
            # Serialise list fields as JSON strings so the DataFrame is flat
            d["ic_history"] = json.dumps(d["ic_history"], ensure_ascii=False)
            d["sharpe_history"] = json.dumps(d["sharpe_history"], ensure_ascii=False)
            d["health_history"] = json.dumps(d["health_history"], ensure_ascii=False)
            d["tags"] = json.dumps(d["tags"], ensure_ascii=False)
            records.append(d)

        df = pd.DataFrame(records)
        logger.debug("Exported {} factor records as DataFrame", len(df))
        return df

    # ---- lifecycle ---------------------------------------------------

    def close(self):
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            logger.debug("FactorLibrary DB connection closed")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
