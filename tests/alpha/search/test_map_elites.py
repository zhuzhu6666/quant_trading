"""tests/alpha/search/test_map_elites.py — MAP-Elites unit tests (Task 2.1.3)."""
import random

import pytest

from alpha.search.map_elites import MAPElites, CellRecord


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def grid() -> MAPElites:
    """Create a fresh MAP-Elites grid for each test."""
    return MAPElites()


# ── CellRecord ───────────────────────────────────────────────────────

def test_cell_record_fields():
    """CellRecord stores all fields correctly."""
    rec = CellRecord("h1", "x>0", 0.8, 3, 2, 1, 0)
    assert rec.expr_hash == "h1"
    assert rec.expr == "x>0"
    assert rec.score == 0.8
    assert rec.depth == 3
    assert rec.abs_ic_bin == 2
    assert rec.vol_bucket == 1
    assert rec.generation == 0


# ── add / get / occupancy ────────────────────────────────────────────

def test_add_and_get(grid):
    """Adding a record and retrieving it by key returns the same record."""
    rec = CellRecord("h1", "x>0", 0.8, 3, 2, 1, 0)
    grid.add(rec)
    assert grid.occupancy == 1
    retrieved = grid.get(3, 2, 1)
    assert retrieved is not None
    assert retrieved.expr_hash == "h1"


def test_get_returns_none_for_empty_cell(grid):
    """get() returns None when no record occupies that cell."""
    assert grid.get(1, 0, 0) is None


def test_add_updates_keeps_best(grid):
    """Adding two records to the same cell keeps the higher score."""
    grid.add(CellRecord("h1", "x>0", 0.5, 3, 2, 1, 0))
    grid.add(CellRecord("h2", "y>0", 0.9, 3, 2, 1, 0))
    assert grid.occupancy == 1
    retrieved = grid.get(3, 2, 1)
    assert retrieved is not None
    assert retrieved.score == 0.9
    assert retrieved.expr_hash == "h2"


def test_add_same_cell_lower_score_does_not_replace(grid):
    """Adding a lower score to an occupied cell keeps the higher score."""
    grid.add(CellRecord("h1", "x>0", 0.8, 3, 2, 1, 0))
    grid.add(CellRecord("h2", "y>0", 0.3, 3, 2, 1, 0))
    assert grid.occupancy == 1
    retrieved = grid.get(3, 2, 1)
    assert retrieved is not None
    assert retrieved.score == 0.8
    assert retrieved.expr_hash == "h1"


def test_add_equal_score_keeps_existing(grid):
    """Adding a record with score equal to existing keeps the existing."""
    grid.add(CellRecord("h1", "x>0", 0.5, 3, 2, 1, 0))
    grid.add(CellRecord("h2", "y>0", 0.5, 3, 2, 1, 0))
    assert grid.occupancy == 1
    retrieved = grid.get(3, 2, 1)
    assert retrieved is not None
    assert retrieved.expr_hash == "h1"  # original kept (not >)


def test_add_different_cells_independent(grid):
    """Records in different cells don't interfere."""
    grid.add(CellRecord("h1", "x>0", 0.5, 1, 0, 0, 0))
    grid.add(CellRecord("h2", "y>0", 0.8, 5, 4, 2, 0))
    assert grid.occupancy == 2
    assert grid.get(1, 0, 0) is not None
    assert grid.get(5, 4, 2) is not None


# ── novelty_cells ────────────────────────────────────────────────────

def test_novelty_cells_returns_empty_when_empty(grid):
    """novelty_cells returns [] when grid is empty."""
    assert grid.novelty_cells(5) == []


def test_novelty_cells_returns_requested_count(grid):
    """novelty_cells returns up to n records from occupied cells."""
    for i in range(10):
        for j in range(3):
            grid.add(CellRecord(f"h{i}_{j}", f"expr{i}", 0.5, 1, i % 5, j, 0))
    result = grid.novelty_cells(3)
    assert len(result) <= 3
    assert len(result) > 0


def test_novelty_cells_does_not_exceed_grid_size(grid):
    """novelty_cells returns at most occupancy records."""
    grid.add(CellRecord("h1", "x>0", 0.8, 3, 2, 1, 0))
    grid.add(CellRecord("h2", "y>0", 0.6, 1, 0, 2, 0))
    result = grid.novelty_cells(10)
    assert len(result) == 2


def test_novelty_cells_returns_unique_records(grid):
    """novelty_cells does not return duplicates."""
    for i in range(5):
        grid.add(CellRecord(f"h{i}", f"expr{i}", 0.5, 1, 0, 0, 0))
    result = grid.novelty_cells(5)
    hashes = [r.expr_hash for r in result]
    assert len(set(hashes)) == len(hashes)


# ── classify_vol_bucket ──────────────────────────────────────────────

def test_classify_vol_bucket():
    """Vol bucket classification matches threshold rules."""
    assert MAPElites.classify_vol_bucket(80) == 0  # HIGH
    assert MAPElites.classify_vol_bucket(50) == 1  # MEDIUM
    assert MAPElites.classify_vol_bucket(20) == 2  # LOW
    assert MAPElites.classify_vol_bucket(67) == 0  # boundary >= 67
    assert MAPElites.classify_vol_bucket(33) == 1  # boundary >= 33
    assert MAPElites.classify_vol_bucket(32) == 2  # just below MEDIUM
    assert MAPElites.classify_vol_bucket(66) == 1  # just below HIGH
    assert MAPElites.classify_vol_bucket(100) == 0  # max
    assert MAPElites.classify_vol_bucket(0) == 2    # min


# ── classify_abs_ic_bin ──────────────────────────────────────────────

def test_classify_abs_ic_bin():
    """Absolute IC bin classification matches thresholds."""
    assert MAPElites.classify_abs_ic_bin(0.10) == 4
    assert MAPElites.classify_abs_ic_bin(0.08) == 4  # boundary >= 0.08
    assert MAPElites.classify_abs_ic_bin(0.06) == 3
    assert MAPElites.classify_abs_ic_bin(0.05) == 3  # boundary >= 0.05
    assert MAPElites.classify_abs_ic_bin(0.03) == 2
    assert MAPElites.classify_abs_ic_bin(0.02) == 2  # boundary >= 0.02
    assert MAPElites.classify_abs_ic_bin(0.015) == 1
    assert MAPElites.classify_abs_ic_bin(0.01) == 1  # boundary >= 0.01
    assert MAPElites.classify_abs_ic_bin(0.005) == 0
    assert MAPElites.classify_abs_ic_bin(0.0) == 0
    assert MAPElites.classify_abs_ic_bin(-0.01) == 0  # negative (rare)


# ── grid_shape ───────────────────────────────────────────────────────

def test_grid_shape(grid):
    """grid_shape reports the expected 75-cell shape."""
    assert grid.grid_shape == (5, 5, 3)


# ── clear ────────────────────────────────────────────────────────────

def test_clear(grid):
    """clear() removes all records from the grid."""
    grid.add(CellRecord("h1", "x>0", 0.8, 3, 2, 1, 0))
    grid.add(CellRecord("h2", "y>0", 0.6, 1, 0, 2, 0))
    assert grid.occupancy == 2
    grid.clear()
    assert grid.occupancy == 0


# ── concurrency safety (basic) ───────────────────────────────────────

def test_concurrent_add_same_cell(grid):
    """Adding to the same cell from multiple sources is safe."""
    import threading

    def add_record(score: float, result: list) -> None:
        rec = CellRecord(f"h{score}", f"expr{score}", score, 2, 1, 0, 0)
        grid.add(rec)
        result.append(True)

    threads = []
    results = []
    for s in [0.9, 0.3, 0.7, 0.5, 0.8]:
        t = threading.Thread(target=add_record, args=(s, results))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    assert grid.occupancy == 1  # same cell (2, 1, 0)
    retrieved = grid.get(2, 1, 0)
    assert retrieved is not None
    assert retrieved.score == 0.9  # highest wins
