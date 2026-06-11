"""tests/alpha/search/test_operators.py — OperatorRegistry unit tests (Task 2.1.2)."""

import pytest

from alpha.search.operators import (
    OperatorRegistry,
    register_standard_ops,
)


# ── OperatorRegistry ──────────────────────────────────────────────────


def test_register_and_get():
    """register + get round-trip returns full metadata."""
    OperatorRegistry.reset_singleton()
    reg = OperatorRegistry.shared()
    reg.register("ts_mean", lambda: None, 2, "ts")
    meta = reg.get("ts_mean")
    assert meta is not None
    assert meta["category"] == "ts"
    assert meta["arity"] == 2
    assert "fn" in meta
    assert "description" in meta


def test_get_returns_none_for_unknown():
    """get() returns None for unregistered operator."""
    OperatorRegistry.reset_singleton()
    reg = OperatorRegistry.shared()
    assert reg.get("nonexistent") is None


def test_get_meta_compatible_with_allowed_ops():
    """get_meta returns (category, min_args, max_args) matching ALLOWED_OPS format."""
    OperatorRegistry.reset_singleton()
    reg = OperatorRegistry.shared()
    reg.register("ts_corr", lambda: None, 3, "ts")
    cat, min_a, max_a = reg.get_meta("ts_corr")
    assert cat == "ts"
    assert min_a == 3
    assert max_a == 3


def test_get_meta_returns_none_for_unknown():
    """get_meta returns None for unregistered operator."""
    OperatorRegistry.reset_singleton()
    reg = OperatorRegistry.shared()
    assert reg.get_meta("nonexistent") is None


def test_register_duplicate_raises():
    """Registering the same name twice raises ValueError."""
    OperatorRegistry.reset_singleton()
    reg = OperatorRegistry.shared()
    reg.register("test_op", lambda: None, 1, "math")
    with pytest.raises(ValueError, match="already registered"):
        reg.register("test_op", lambda: None, 1, "math")


def test_all_names():
    """all_names returns sorted list of registered names."""
    OperatorRegistry.reset_singleton()
    reg = OperatorRegistry.shared()
    reg.register("b", lambda: None, 1, "cs")
    reg.register("a", lambda: None, 1, "ts")
    assert reg.all_names() == ["a", "b"]


def test_all_names_empty():
    """all_names returns empty list for fresh registry."""
    OperatorRegistry.reset_singleton()
    reg = OperatorRegistry.shared()
    assert reg.all_names() == []


# ── Singleton ─────────────────────────────────────────────────────────


def test_shared_returns_same_instance():
    """shared() always returns the same singleton instance."""
    OperatorRegistry.reset_singleton()
    r1 = OperatorRegistry.shared()
    r2 = OperatorRegistry.shared()
    assert r1 is r2


def test_reset_singleton_creates_new_instance():
    """After reset_singleton, shared() returns a fresh instance."""
    OperatorRegistry.reset_singleton()
    r1 = OperatorRegistry.shared()
    r1.register("x", lambda: None, 1, "math")
    OperatorRegistry.reset_singleton()
    r2 = OperatorRegistry.shared()
    assert r2.all_names() == []
    assert r1 is not r2


# ── register_standard_ops ────────────────────────────────────────────


def test_register_standard_ops():
    """register_standard_ops registers all expected operators."""
    OperatorRegistry.reset_singleton()
    register_standard_ops()
    reg = OperatorRegistry.shared()
    names = reg.all_names()

    # Check a few representative operators are present
    assert "ts_mean" in names
    assert "ts_std" in names
    assert "ts_sum" in names
    assert "ts_min" in names
    assert "ts_max" in names
    assert "ts_corr" in names
    assert "ts_rank" in names
    assert "delta" in names
    assert "delay" in names
    assert "ts_decay_linear" in names
    assert "rank" in names
    assert "normalize" in names
    assert "quantile" in names
    assert "sign" in names
    assert "abs" in names
    assert "log" in names
    assert "sqrt" in names
    assert "power" in names

    # Check new operators
    assert "signed_log" in names
    assert "zscore" in names

    # Total: 18 existing from ALLOWED_OPS + 2 new = 20
    assert len(names) == 20


def test_register_standard_ops_idempotent():
    """Multiple calls to register_standard_ops are safe (no-op on repeat)."""
    OperatorRegistry.reset_singleton()
    register_standard_ops()
    count = len(OperatorRegistry.shared().all_names())
    register_standard_ops()  # second call
    assert len(OperatorRegistry.shared().all_names()) == count


def test_register_standard_ops_metadata():
    """Each operator registered by register_standard_ops has valid metadata."""
    OperatorRegistry.reset_singleton()
    register_standard_ops()
    reg = OperatorRegistry.shared()

    # Check get_meta compatibility with ALLOWED_OPS format
    cat, min_a, max_a = reg.get_meta("ts_mean")
    assert cat == "ts"
    assert min_a == 2
    assert max_a == 2

    cat, min_a, max_a = reg.get_meta("ts_corr")
    assert cat == "ts"
    assert min_a == 3
    assert max_a == 3

    cat, min_a, max_a = reg.get_meta("rank")
    assert cat == "cs"
    assert min_a == 1
    assert max_a == 1

    cat, min_a, max_a = reg.get_meta("power")
    assert cat == "math"
    assert min_a == 2
    assert max_a == 2

    # New operators
    cat, min_a, max_a = reg.get_meta("signed_log")
    assert cat == "math"
    assert min_a == 1
    assert max_a == 1

    cat, min_a, max_a = reg.get_meta("zscore")
    assert cat == "ts"
    assert min_a == 1
    assert max_a == 1


def test_get_returns_full_record():
    """get() returns all metadata fields for registered operator."""
    OperatorRegistry.reset_singleton()
    register_standard_ops()
    reg = OperatorRegistry.shared()
    meta = reg.get("ts_mean")
    assert meta is not None
    assert "fn" in meta
    assert "arity" in meta
    assert "category" in meta
    assert "description" in meta
    assert meta["arity"] == 2
    assert meta["category"] == "ts"
    assert "Rolling mean" in meta["description"]
