"""tests/alpha/evaluation/test_purged_walkforward.py — Tests for PurgedWalkForward."""
import numpy as np
import pytest

from alpha.evaluation.evaluation_context import EvaluationContext
from alpha.evaluation.purged_walkforward import PurgedWalkForward, FoldContext


class TestPurgedWalkForward:
    """Tests for PurgedWalkForward."""

    def test_init(self):
        """Should initialise with context and n_folds."""
        ctx = EvaluationContext(train_bars=200, test_bars=50, embargo_bars=10, purge_bars=5)
        pwf = PurgedWalkForward(ctx, n_folds=5)
        assert pwf.n_folds == 5
        assert pwf.context is ctx

    def test_init_raises_on_invalid_n_folds(self):
        """Should raise ValueError when n_folds < 1."""
        ctx = EvaluationContext(train_bars=200, test_bars=50)
        with pytest.raises(ValueError, match="n_folds"):
            PurgedWalkForward(ctx, n_folds=0)
        with pytest.raises(ValueError, match="n_folds"):
            PurgedWalkForward(ctx, n_folds=-1)

    def test_folds_generates_correct_count(self):
        """folds() should yield n_folds FoldContext objects."""
        ctx = EvaluationContext(train_bars=200, test_bars=50, embargo_bars=5, purge_bars=5)
        pwf = PurgedWalkForward(ctx, n_folds=4)
        folds = list(pwf.folds(n_total=500))
        assert len(folds) == 4

    def test_all_folds_are_foldcontext(self):
        """Each fold should be a FoldContext."""
        ctx = EvaluationContext(train_bars=200, test_bars=50)
        pwf = PurgedWalkForward(ctx, n_folds=3)
        for fold in pwf.folds(n_total=500):
            assert isinstance(fold, FoldContext)

    def test_fold_has_metadata(self):
        """FoldContext should contain embargo_size, purge_size, fold_id."""
        ctx = EvaluationContext(train_bars=200, test_bars=50, embargo_bars=5, purge_bars=3)
        pwf = PurgedWalkForward(ctx, n_folds=2)
        for fold in pwf.folds(n_total=500):
            assert fold.embargo_size == 5
            assert fold.purge_size == 3
            assert isinstance(fold.fold_id, int)

    def test_purge_removes_last_bars(self):
        """Purge should remove purge_bars from end of training."""
        ctx = EvaluationContext(train_bars=100, test_bars=20, purge_bars=5)
        pwf = PurgedWalkForward(ctx, n_folds=1)
        folds = list(pwf.folds(n_total=200))
        fold = folds[0]
        # Training was [0, 100), purge removes [95, 100) → [0, 95)
        assert len(fold.train_indices) == 95
        assert fold.train_indices[-1] == 94

    def test_embargo_excludes_after_test(self):
        """Embargo should not affect the current fold's training set when
        there is no overlap (the default case)."""
        ctx = EvaluationContext(train_bars=100, test_bars=20, embargo_bars=10, purge_bars=0)
        pwf = PurgedWalkForward(ctx, n_folds=1)
        folds = list(pwf.folds(n_total=200))
        fold = folds[0]
        # Training: [0, 100), test: [100, 120)
        # Embargo region is [120, 130), which does not overlap training [0, 100).
        assert len(fold.train_indices) == 100

    def test_no_overlap_between_train_and_test(self):
        """Train and test indices should never overlap."""
        ctx = EvaluationContext(train_bars=100, test_bars=20, embargo_bars=5, purge_bars=3)
        pwf = PurgedWalkForward(ctx, n_folds=4)
        for fold in pwf.folds(n_total=300):
            overlap = np.intersect1d(fold.train_indices, fold.test_indices)
            assert len(overlap) == 0

    def test_test_indices_advance_each_fold(self):
        """Test windows should shift forward each fold."""
        ctx = EvaluationContext(train_bars=100, test_bars=20)
        pwf = PurgedWalkForward(ctx, n_folds=3)
        folds = list(pwf.folds(n_total=300))
        for i, fold in enumerate(folds):
            expected_start = 100 + i * 20
            assert fold.test_indices[0] == expected_start

    def test_raises_on_insufficient_data(self):
        """Should raise ValueError when n_total is too small."""
        ctx = EvaluationContext(train_bars=200, test_bars=50)
        pwf = PurgedWalkForward(ctx, n_folds=10)
        with pytest.raises(ValueError, match="n_total"):
            list(pwf.folds(n_total=300))

    def test_get_fold_returns_specific_fold(self):
        """get_fold() should return the correct fold without iterating."""
        ctx = EvaluationContext(train_bars=100, test_bars=20)
        pwf = PurgedWalkForward(ctx, n_folds=5)
        fold = pwf.get_fold(n_total=300, fold_index=2)
        assert fold.fold_id == 2
        assert fold.test_indices[0] == 100 + 2 * 20  # = 140

    def test_get_fold_second_call_consistency(self):
        """Multiple calls to get_fold should return identical results."""
        ctx = EvaluationContext(train_bars=100, test_bars=20, purge_bars=5)
        pwf = PurgedWalkForward(ctx, n_folds=5)
        f1 = pwf.get_fold(n_total=300, fold_index=0)
        f2 = pwf.get_fold(n_total=300, fold_index=0)
        np.testing.assert_array_equal(f1.train_indices, f2.train_indices)
        np.testing.assert_array_equal(f1.test_indices, f2.test_indices)

    def test_len(self):
        """__len__ should return n_folds."""
        ctx = EvaluationContext(train_bars=100, test_bars=20)
        pwf = PurgedWalkForward(ctx, n_folds=7)
        assert len(pwf) == 7

    def test_repr(self):
        """__repr__ should include context and n_folds."""
        ctx = EvaluationContext(train_bars=100, test_bars=20, embargo_bars=5, purge_bars=3)
        pwf = PurgedWalkForward(ctx, n_folds=4)
        r = repr(pwf)
        assert "n_folds=4" in r
        assert "embargo_bars=5" in r
        assert "purge_bars=3" in r

    def test_fold_context_frozen(self):
        """FoldContext should be frozen."""
        ctx = EvaluationContext(train_bars=100, test_bars=20)
        pwf = PurgedWalkForward(ctx, n_folds=1)
        fold = pwf.get_fold(n_total=200, fold_index=0)
        with pytest.raises(AttributeError):
            fold.fold_id = 99

    def test_purge_gap_between_train_and_test(self):
        """There should be a gap of exactly purge_bars between train and test."""
        ctx = EvaluationContext(train_bars=100, test_bars=20, purge_bars=5)
        pwf = PurgedWalkForward(ctx, n_folds=1)
        fold = pwf.get_fold(n_total=200, fold_index=0)
        # Last train index = 94 (was [0,100), purge removes 95-99)
        # First test index = 100
        # Gap: indices 95-99 are excluded → 5 bars gap = purge_bars
        assert fold.train_indices[-1] == 94
        assert fold.test_indices[0] == 100
        # Check no indices in the purge region
        assert not np.any((fold.train_indices >= 95) & (fold.train_indices < 100))
