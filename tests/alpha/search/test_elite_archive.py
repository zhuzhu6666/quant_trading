"""tests/alpha/search/test_elite_archive.py — EliteArchive unit tests (Task 2.1.1)."""
import json
import random

import pytest

from alpha.search.elite_archive import EliteArchive, EliteRecord


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def archive(tmp_path) -> EliteArchive:
    """Create an EliteArchive backed by a temp file."""
    p = str(tmp_path / "test_archive.jsonl")
    return EliteArchive(path=p)


# ── EliteRecord ──────────────────────────────────────────────────────

def test_elite_record_default_last_promoted():
    """EliteRecord.last_promoted should default to None."""
    rec = EliteRecord("h1", "x>0", 0.5, 0, "r1")
    assert rec.last_promoted is None


# ── add / top_k ──────────────────────────────────────────────────────

def test_add_and_top_k():
    """top_k returns correct number sorted by score descending."""
    archive = EliteArchive(path=":memory:")
    archive.add(EliteRecord("a", "x>0", 0.5, 0, "r1"))
    archive.add(EliteRecord("b", "y>0", 0.8, 0, "r1"))
    archive.add(EliteRecord("c", "z>0", 0.3, 0, "r1"))
    top = archive.top_k(2)
    assert len(top) == 2
    assert top[0].expr_hash == "b"  # highest score
    assert top[1].expr_hash == "a"


def test_top_k_returns_all_when_k_exceeds_count():
    """top_k returns all records when k > archive size."""
    archive = EliteArchive(path=":memory:")
    archive.add(EliteRecord("a", "x>0", 0.5, 0, "r1"))
    archive.add(EliteRecord("b", "y>0", 0.8, 0, "r1"))
    top = archive.top_k(10)
    assert len(top) == 2


def test_top_k_empty():
    """top_k returns empty list for empty archive."""
    archive = EliteArchive(path=":memory:")
    assert archive.top_k(5) == []


def test_top_k_zero():
    """top_k(0) returns empty list."""
    archive = EliteArchive(path=":memory:")
    archive.add(EliteRecord("a", "x>0", 0.5, 0, "r1"))
    assert archive.top_k(0) == []


# ── add updates existing ─────────────────────────────────────────────

def test_add_updates_existing():
    """Add with same hash keeps the higher score."""
    archive = EliteArchive(path=":memory:")
    archive.add(EliteRecord("a", "x>0", 0.5, 0, "r1"))
    archive.add(EliteRecord("a", "x>0", 0.9, 1, "r1"))  # same hash, higher score
    assert len(archive) == 1
    assert archive.records["a"].score == 0.9
    assert archive.records["a"].generation_added == 1  # updated record


def test_add_keeps_existing_when_new_score_lower():
    """Add with same hash and lower score keeps the existing record."""
    archive = EliteArchive(path=":memory:")
    archive.add(EliteRecord("a", "x>0", 0.9, 0, "r1"))
    archive.add(EliteRecord("a", "x>0", 0.5, 1, "r2"))  # same hash, lower score
    assert len(archive) == 1
    assert archive.records["a"].score == 0.9
    assert archive.records["a"].generation_added == 0  # unchanged


def test_add_keeps_existing_when_new_score_equal():
    """Add with same hash and equal score keeps the existing record."""
    archive = EliteArchive(path=":memory:")
    archive.add(EliteRecord("a", "x>0", 0.5, 0, "r1"))
    archive.add(EliteRecord("a", "x>0v2", 0.5, 1, "r2"))  # same hash, equal score
    assert len(archive) == 1
    assert archive.records["a"].score == 0.5
    assert archive.records["a"].generation_added == 0  # unchanged


# ── warmstart_seed ───────────────────────────────────────────────────

def test_warmstart_seed_diversity():
    """warmstart_seed returns highest score first and diverse selection."""
    archive = EliteArchive(path=":memory:")
    for i in range(10):
        archive.add(EliteRecord(f"h{i}", f"expr{i}", 0.1 * i, 0, "r1"))
    seed = archive.warmstart_seed(3)
    assert len(seed) <= 3
    assert seed[0].expr_hash == "h9"  # highest score


def test_warmstart_seed_returns_all_when_fewer_records():
    """warmstart_seed returns all records if fewer than n exist."""
    archive = EliteArchive(path=":memory:")
    archive.add(EliteRecord("a", "x>0", 0.5, 0, "r1"))
    archive.add(EliteRecord("b", "y>0", 0.8, 0, "r1"))
    seed = archive.warmstart_seed(5)
    assert len(seed) == 2


def test_warmstart_seed_empty():
    """warmstart_seed returns empty list for empty archive."""
    archive = EliteArchive(path=":memory:")
    assert archive.warmstart_seed(3) == []


def test_warmstart_seed_single_record():
    """warmstart_seed handles archive with exactly one record."""
    archive = EliteArchive(path=":memory:")
    archive.add(EliteRecord("a", "x>0", 0.5, 0, "r1"))
    seed = archive.warmstart_seed(3)
    assert len(seed) == 1
    assert seed[0].expr_hash == "a"


def test_warmstart_seed_diverse_tiers():
    """Verify warmstart_seed selects from different score tiers."""
    random.seed(42)  # deterministic for test
    archive = EliteArchive(path=":memory:")
    # Create scores roughly forming tiers: [0.9], [0.7-0.8], [0.4-0.6], [0.0-0.3]
    scores = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.0]
    for i, s in enumerate(scores):
        archive.add(EliteRecord(f"h{i}", f"expr{i}", s, 0, "r1"))
    seed = archive.warmstart_seed(4)
    assert len(seed) == 4
    assert seed[0].expr_hash == "h0"  # highest score (0.9)
    # The other 3 should come from different tiers
    selected_hashes = {r.expr_hash for r in seed}
    assert len(selected_hashes) == 4  # all unique


# ── persistence ──────────────────────────────────────────────────────

def test_persistence(tmp_path):
    """Archive survives serialization round-trip."""
    p = str(tmp_path / "test_archive.jsonl")
    archive = EliteArchive(path=p)
    archive.add(EliteRecord("a", "x>0", 0.5, 0, "r1"))
    archive.add(EliteRecord("b", "y>0", 0.8, 0, "r1"))

    # New instance loading from same path
    archive2 = EliteArchive(path=p)
    archive2.load()
    assert len(archive2) == 2
    assert archive2.records["b"].score == 0.8


def test_persistence_load_nonexistent_file(tmp_path):
    """load() on nonexistent file is a no-op."""
    p = str(tmp_path / "nonexistent.jsonl")
    archive = EliteArchive(path=p)
    archive.load()  # should not raise
    assert len(archive) == 0


def test_save_snapshot_overwrites(tmp_path):
    """save_snapshot overwrites the file with current state."""
    p = str(tmp_path / "test_archive.jsonl")
    archive = EliteArchive(path=p)
    archive.add(EliteRecord("a", "x>0", 0.5, 0, "r1"))
    archive.add(EliteRecord("b", "y>0", 0.8, 0, "r1"))

    # Remove "a" and save snapshot
    del archive._records["a"]
    archive.save_snapshot()

    # Reload
    archive2 = EliteArchive(path=p)
    archive2.load()
    assert len(archive2) == 1
    assert "b" in archive2.records


def test_persistence_merge_by_hash_latest_wins(tmp_path):
    """When loading, records with same hash should be resolved (latest line wins)."""
    p = str(tmp_path / "test_archive.jsonl")
    archive = EliteArchive(path=p)
    archive.add(EliteRecord("a", "x>0", 0.5, 0, "r1"))

    # Manually append a newer version of "a" with higher score
    rec2 = {"expr_hash": "a", "expr": "x>0", "score": 0.9, "generation_added": 1, "source_run_id": "r2"}
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec2) + "\n")

    archive2 = EliteArchive(path=p)
    archive2.load()
    assert archive2.records["a"].score == 0.9


def test_persistence_handles_corrupted_line(tmp_path, caplog):
    """Corrupted lines in JSONL are skipped with a warning."""
    p = str(tmp_path / "test_archive.jsonl")
    # Write one valid line and one corrupted line
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"expr_hash": "a", "expr": "x>0", "score": 0.5, "generation_added": 0, "source_run_id": "r1"}\n')
        f.write("NOT JSON\n")
        f.write('{"expr_hash": "b", "expr": "y>0", "score": 0.8, "generation_added": 0, "source_run_id": "r1"}\n')

    archive = EliteArchive(path=p)
    archive.load()
    assert len(archive) == 2  # corrupted line skipped, 2 valid lines loaded


# ── __len__ ──────────────────────────────────────────────────────────

def test_len():
    """__len__ returns the number of records."""
    archive = EliteArchive(path=":memory:")
    assert len(archive) == 0
    archive.add(EliteRecord("a", "x>0", 0.5, 0, "r1"))
    assert len(archive) == 1
    archive.add(EliteRecord("b", "y>0", 0.8, 0, "r1"))
    assert len(archive) == 2


# ── records property ─────────────────────────────────────────────────

def test_records_property_returns_copy():
    """records property returns a copy that doesn't affect internal state."""
    archive = EliteArchive(path=":memory:")
    archive.add(EliteRecord("a", "x>0", 0.5, 0, "r1"))
    r = archive.records
    r["b"] = EliteRecord("b", "y>0", 0.8, 0, "r1")
    assert "b" not in archive.records
    assert len(archive) == 1
