"""alpha/search/elite_archive.py — Persistent elite storage for GP search (Task 2.1.1).

Stores discovered factor expressions so subsequent GP runs can warmstart from prior elites.
Replaces the ephemeral GP search that lost all results between runs.
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class EliteRecord:
    """A single elite factor expression discovered during GP search."""
    expr_hash: str            # hash of the expression string
    expr: str                 # DSL expression text
    score: float              # IC-based score
    generation_added: int     # which generation this was added
    source_run_id: str        # GP run ID that produced it
    last_promoted: Optional[float] = None  # timestamp of last canary promotion


class EliteArchive:
    """Persistent archive of elite factor expressions.

    Stores records in a JSONL file for durability across GP runs.
    Supports warmstarting GP populations from prior elites.
    """

    def __init__(self, path: str = "data/charts/elite_archive.jsonl"):
        self._records: dict[str, EliteRecord] = {}
        self._path = path
        self._is_memory = path == ":memory:"

    # ── public API ──────────────────────────────────────────────────

    def add(self, record: EliteRecord) -> None:
        """Add or update a record. If expr_hash exists, keep the higher score."""
        existing = self._records.get(record.expr_hash)
        if existing is not None and existing.score >= record.score:
            # Existing record has equal or higher score; keep existing.
            return
        self._records[record.expr_hash] = record
        # Append to JSONL file for durability (skip for :memory: mode).
        if not self._is_memory:
            self._append_line(record)

    def top_k(self, k: int) -> list[EliteRecord]:
        """Return top-k by score descending."""
        sorted_records = sorted(
            self._records.values(), key=lambda r: r.score, reverse=True
        )
        return sorted_records[:k]

    def warmstart_seed(self, n: int) -> list[EliteRecord]:
        """Return n records for warmstarting GP population.

        Selects from diverse cells: top score + random from different score tiers.
        If fewer than n records exist, returns all records.
        """
        if len(self._records) == 0:
            return []
        if len(self._records) <= n:
            return list(self._records.values())

        sorted_records = sorted(
            self._records.values(), key=lambda r: r.score, reverse=True
        )

        # Always include the top scorer.
        selected = [sorted_records[0]]

        # Divide the remaining into n-1 tiers, pick one random from each.
        remaining = sorted_records[1:]
        tier_size = max(1, len(remaining) // (n - 1))
        for i in range(n - 1):
            start = i * tier_size
            end = min(start + tier_size, len(remaining))
            if start >= len(remaining):
                break
            tier = remaining[start:end]
            selected.append(random.choice(tier))

        return selected[:n]

    def load(self) -> None:
        """Restore from jsonl file.

        Reads all lines, merging by expr_hash (latest wins).
        If the file does not exist, this is a no-op.
        """
        if self._is_memory:
            return

        path = Path(self._path)
        if not path.exists():
            logger.info("EliteArchive: no existing archive at %s, starting fresh", self._path)
            return

        records: dict[str, EliteRecord] = {}
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    record = EliteRecord(**data)
                    records[record.expr_hash] = record
                except (json.JSONDecodeError, TypeError, KeyError) as e:
                    logger.warning("EliteArchive: skipping malformed line: %s", e)

        self._records = records
        logger.info("EliteArchive: loaded %d records from %s", len(records), self._path)

    def save_snapshot(self) -> None:
        """Write full current state to jsonl (overwrite)."""
        if self._is_memory:
            return

        path = Path(self._path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for record in self._records.values():
                f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        logger.info("EliteArchive: saved snapshot with %d records to %s", len(self._records), self._path)

    def __len__(self) -> int:
        return len(self._records)

    @property
    def records(self) -> dict[str, EliteRecord]:
        return dict(self._records)

    # ── internal helpers ────────────────────────────────────────────

    def _append_line(self, record: EliteRecord) -> None:
        """Append a single record line to the JSONL file."""
        path = Path(self._path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
