"""alpha/search/map_elites.py — MAP-Elites grid for novelty injection in GP search (Task 2.1.3).

Maintains a 3-dimensional grid (depth x abs_ic_bin x vol_bucket) where each
cell stores the best-scoring expression for that behavioral combination.
Used to inject novelty into GP populations by sampling from sparsely
occupied cells.
"""
from __future__ import annotations

import random
import threading
from dataclasses import dataclass
from typing import Optional


@dataclass
class CellRecord:
    """An individual occupying a MAP-Elites cell."""
    expr_hash: str
    expr: str
    score: float
    depth: int          # 1-5
    abs_ic_bin: int     # 0-4
    vol_bucket: int     # 0=HIGH, 1=MEDIUM, 2=LOW
    generation: int


class MAPElites:
    """MAP-Elites grid for novelty injection in GP search.

    Grid: depth (1-5) x abs_ic_bin (0-4) x vol_bucket (0-2) = 75 cells.
    """

    def __init__(self) -> None:
        self._grid: dict[tuple[int, int, int], CellRecord] = {}
        self._lock = threading.Lock()

    # ── public API ─────────────────────────────────────────────────────

    def add(self, record: CellRecord) -> None:
        """Add/update a cell. Higher score replaces existing occupant.

        Key is (depth, abs_ic_bin, vol_bucket).
        """
        key = (record.depth, record.abs_ic_bin, record.vol_bucket)
        with self._lock:
            if key not in self._grid or record.score > self._grid[key].score:
                self._grid[key] = record

    def get(self, depth: int, abs_ic_bin: int, vol_bucket: int) -> Optional[CellRecord]:
        """Retrieve the occupant of a specific cell, or None if empty."""
        key = (depth, abs_ic_bin, vol_bucket)
        return self._grid.get(key)

    def novelty_cells(self, n: int) -> list[CellRecord]:
        """Return up to n randomly selected records from occupied cells.

        Since each cell has at most one occupant, this is a uniform
        random sample across all occupied cells. Returns empty list
        if no cells are occupied.
        """
        with self._lock:
            if not self._grid:
                return []
            cells = list(self._grid.values())
            sample_size = min(n, len(cells))
            return random.sample(cells, sample_size)

    @property
    def occupancy(self) -> int:
        """Number of occupied cells."""
        return len(self._grid)

    @property
    def grid_shape(self) -> tuple[int, int, int]:
        return (5, 5, 3)

    def clear(self) -> None:
        with self._lock:
            self._grid.clear()

    # ── static classifiers ─────────────────────────────────────────────

    @staticmethod
    def classify_vol_bucket(atr_percentile: float) -> int:
        """Classify volatility bucket from ATR percentile (0-100).

        0=HIGH (>= 67th), 1=MEDIUM (33-67), 2=LOW (< 33).
        """
        if atr_percentile >= 67:
            return 0  # HIGH
        elif atr_percentile >= 33:
            return 1  # MEDIUM
        else:
            return 2  # LOW

    @staticmethod
    def classify_abs_ic_bin(abs_ic: float) -> int:
        """Classify absolute IC into 5 quantile bins (0-4)."""
        if abs_ic >= 0.08:
            return 4
        elif abs_ic >= 0.05:
            return 3
        elif abs_ic >= 0.02:
            return 2
        elif abs_ic >= 0.01:
            return 1
        else:
            return 0
