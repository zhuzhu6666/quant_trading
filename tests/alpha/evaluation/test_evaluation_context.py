"""tests/alpha/evaluation/test_evaluation_context.py — Tests for EvaluationContext."""
import numpy as np
import pytest

from alpha.evaluation.evaluation_context import EvaluationContext, CVSplit


class TestEvaluationContext:
    """Tests for EvaluationContext dataclass and its methods."""

    def test_default_attributes(self):
        """Default embargo and purge should be zero."""
        ctx = EvaluationContext(train_bars=200, test_bars=50)
        assert ctx.train_bars == 200
        assert ctx.test_bars == 50
        assert ctx.embargo_bars == 0
        assert ctx.purge_bars == 0

    def test_custom_attributes(self):
        """All attributes should be settable."""
        ctx = EvaluationContext(train_bars=200, test_bars=50, embargo_bars=10, purge_bars=5)
        assert ctx.embargo_bars == 10
        assert ctx.purge_bars == 5

    def test_frozen(self):
        """EvaluationContext should be frozen (immutable)."""
        ctx = EvaluationContext(train_bars=200, test_bars=50)
        with pytest.raises(AttributeError):
            ctx.train_bars = 100

    def test_repr(self):
        """__repr__ should include all fields."""
        ctx = EvaluationContext(train_bars=200, test_bars=50, embargo_bars=10, purge_bars=5)
        r = repr(ctx)
        assert "train_bars=200" in r
        assert "test_bars=50" in r
        assert "embargo_bars=10" in r
        assert "purge_bars=5" in r

    def test_cvsplit_default_fold_id(self):
        """CVSplit fold_id should default to None."""
        split = CVSplit(
            train_indices=np.array([0, 1, 2]),
            test_indices=np.array([3, 4]),
        )
        assert split.fold_id is None

    def test_cvsplit_frozen(self):
        """CVSplit should be frozen."""
        split = CVSplit(np.array([0]), np.array([1]), fold_id=0)
        with pytest.raises(AttributeError):
            split.fold_id = 1

    # ── split() ─────────────────────────────────────────────────────

    def test_split_basic(self):
        """split() should produce correct number of folds."""
        ctx = EvaluationContext(train_bars=100, test_bars=20)
        splits = list(ctx.split(n_total=200, n_splits=3))
        assert len(splits) == 3

    def test_split_train_grows(self):
        """Training set should expand with each fold (expanding window)."""
        ctx = EvaluationContext(train_bars=100, test_bars=20)
        splits = list(ctx.split(n_total=200, n_splits=3))
        assert len(splits[0].train_indices) == 100
        assert len(splits[1].train_indices) == 120
        assert len(splits[2].train_indices) == 140

    def test_split_test_size_constant(self):
        """Test size should be constant across folds."""
        ctx = EvaluationContext(train_bars=100, test_bars=20)
        splits = list(ctx.split(n_total=200, n_splits=3))
        for s in splits:
            assert len(s.test_indices) == 20

    def test_split_test_start_advances(self):
        """Test window should advance by test_bars each fold."""
        ctx = EvaluationContext(train_bars=100, test_bars=20)
        splits = list(ctx.split(n_total=200, n_splits=3))
        assert splits[0].test_indices[0] == 100
        assert splits[1].test_indices[0] == 120
        assert splits[2].test_indices[0] == 140

    def test_split_auto_n_splits(self):
        """split() should infer n_splits when not provided."""
        ctx = EvaluationContext(train_bars=100, test_bars=20)
        splits = list(ctx.split(n_total=200))
        # (200 - 100) / 20 = 5 folds expected
        assert len(splits) == 5

    def test_split_returns_empty_when_not_enough_data(self):
        """split() should return empty iterator when n_total is too small."""
        ctx = EvaluationContext(train_bars=100, test_bars=20)
        splits = list(ctx.split(n_total=50, n_splits=2))
        assert len(splits) == 0

    def test_split_fold_ids_sequential(self):
        """Fold IDs should be 0, 1, 2, ..."""
        ctx = EvaluationContext(train_bars=100, test_bars=20)
        splits = list(ctx.split(n_total=200, n_splits=3))
        for i, s in enumerate(splits):
            assert s.fold_id == i

    def test_split_no_overlap(self):
        """Train and test indices should not overlap."""
        ctx = EvaluationContext(train_bars=100, test_bars=20)
        for s in ctx.split(n_total=200, n_splits=3):
            overlap = np.intersect1d(s.train_indices, s.test_indices)
            assert len(overlap) == 0

    # ── get_train_test() ────────────────────────────────────────────

    def test_get_train_test_basic(self):
        """get_train_test() should return a single CVSplit."""
        ctx = EvaluationContext(train_bars=100, test_bars=20, purge_bars=5)
        split = ctx.get_train_test(n_total=200, fold_index=0)
        assert isinstance(split, CVSplit)
        assert split.fold_id == 0

    def test_get_train_test_purge_applied(self):
        """Purge bars should be removed from the end of training."""
        ctx = EvaluationContext(train_bars=100, test_bars=20, purge_bars=5)
        split = ctx.get_train_test(n_total=200, fold_index=0)
        # Training: first 100 bars, last 5 purged → [0, 95)
        assert len(split.train_indices) == 95
        assert split.train_indices[-1] == 94

    def test_get_train_test_no_purge(self):
        """Without purge, full training range is used."""
        ctx = EvaluationContext(train_bars=100, test_bars=20, purge_bars=0)
        split = ctx.get_train_test(n_total=200, fold_index=0)
        assert len(split.train_indices) == 100
        assert split.train_indices[-1] == 99

    def test_get_train_test_test_indices(self):
        """Test indices should cover the correct window."""
        ctx = EvaluationContext(train_bars=100, test_bars=20)
        split = ctx.get_train_test(n_total=200, fold_index=0)
        assert list(split.test_indices) == [100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
                                             110, 111, 112, 113, 114, 115, 116, 117, 118, 119]

    def test_get_train_test_no_overlap(self):
        """Train and test should not overlap with purge applied."""
        ctx = EvaluationContext(train_bars=100, test_bars=20, purge_bars=5)
        split = ctx.get_train_test(n_total=200, fold_index=0)
        overlap = np.intersect1d(split.train_indices, split.test_indices)
        assert len(overlap) == 0
        # Also check gap: purged bars are between train and test
        assert split.train_indices[-1] < 100 - 5  # last train bar is before purge zone
        assert split.test_indices[0] == 100

    def test_get_train_test_fold_index_advances(self):
        """Higher fold index should shift test window forward."""
        ctx = EvaluationContext(train_bars=100, test_bars=20, purge_bars=5)
        s0 = ctx.get_train_test(n_total=200, fold_index=0)
        s1 = ctx.get_train_test(n_total=200, fold_index=1)
        assert s0.test_indices[0] == 100
        assert s1.test_indices[0] == 120

    def test_get_train_test_raises_on_empty(self):
        """Should raise ValueError if train or test is empty."""
        ctx = EvaluationContext(train_bars=100, test_bars=50)
        with pytest.raises(ValueError):
            # fold_index too large → test starts beyond n_total
            ctx.get_train_test(n_total=120, fold_index=5)

    # ── total_bars_needed ───────────────────────────────────────────

    def test_total_bars_needed(self):
        """total_bars_needed should compute correctly."""
        ctx = EvaluationContext(train_bars=100, test_bars=20, embargo_bars=5, purge_bars=3)
        needed = ctx.total_bars_needed(n_splits=3)
        assert needed == 100 + 3 + 3 * 20 + 5  # = 168

    def test_total_bars_needed_default(self):
        """total_bars_needed with n_splits=1."""
        ctx = EvaluationContext(train_bars=100, test_bars=20)
        assert ctx.total_bars_needed() == 100 + 1 * 20  # = 120

    def test_total_bars_needed_with_n_splits(self):
        """Total bars grows with n_splits."""
        ctx = EvaluationContext(train_bars=100, test_bars=20)
        assert ctx.total_bars_needed(5) == 100 + 5 * 20  # = 200
