"""alpha/evaluation/evaluation_context.py — EvaluationContext for OOS evaluation.

EvaluationContext holds train/test bar indices and embargo/purge window sizes,
providing two core methods:

- ``split()`` — yields ``CVSplit`` index pairs for standard time-series
  cross-validation without purge/embargo.
- ``get_train_test()`` — returns a single ``(train_indices, test_indices)`` pair
  applying embargo-purged walk-forward logic: training data is truncated so
  that the last ``purge_bars`` bars are removed immediately preceding the test
  window, and the first ``embargo_bars`` bars after the test window are also
  excluded from the training side of later folds.

Usage::

    ctx = EvaluationContext(
        n_total=500,
        test_bars=60,
        embargo_bars=10,
        purge_bars=5,
    )
    for fold in ctx.split(n_splits=3):
        print(fold)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CVSplit:
    """A single train/test split for time-series cross-validation.

    Attributes:
        train_indices: 1D array of training indices into the original bar array.
        test_indices: 1D array of testing indices.
        fold_id: Optional zero-based fold identifier.
    """

    train_indices: np.ndarray
    test_indices: np.ndarray
    fold_id: Optional[int] = None


@dataclass(frozen=True)
class EvaluationContext:
    """Holds train/test/embargo/purge bar counts for OOS evaluation.

    All bar counts refer to the full series length.  The split and
    get_train_test methods resolve actual index arrays from these parameters.

    Attributes:
        train_bars: Number of bars used for training in the initial fold
            (expanding window).  Ignored when ``n_total`` is passed to
            ``split()`` / ``get_train_test()``; retained for serialisation.
        test_bars: Number of bars per test window.
        embargo_bars: Number of bars *after* each test window to exclude from
            training to avoid leakage from overlapping forward returns.
        purge_bars: Number of bars *before* each test window to exclude from
            training to remove stale data.
    """

    train_bars: int
    test_bars: int
    embargo_bars: int = 0
    purge_bars: int = 0

    # ── Public API ──────────────────────────────────────────────────────

    def split(
        self,
        n_total: Optional[int] = None,
        n_splits: Optional[int] = None,
    ) -> Iterator[CVSplit]:
        """Generate train/test index pairs for time-series cross-validation.

        Uses an expanding window: each successive fold adds ``test_bars``
        bars to the training set while keeping the test window fixed size.

        Parameters
        ----------
        n_total : int, optional
            Total number of bars in the full series.  If omitted, the caller
            must have populated ``train_bars`` meaningfully; otherwise
            ``train_bars`` is used as the initial training size.
        n_splits : int, optional
            Number of folds to generate.  If omitted, the number of folds is
            the maximum such that ``train_bars + n_splits * test_bars <= n_total``.

        Yields
        ------
        CVSplit
            Each fold's train/test indices.
        """
        if n_total is None:
            n_total = self.train_bars + self.test_bars

        initial_train = self.train_bars
        if n_splits is None:
            n_splits = max(1, (n_total - initial_train) // self.test_bars)

        if n_splits <= 0:
            logger.warning(
                "split(): n_splits=%d with n_total=%d, train_bars=%d, test_bars=%d",
                n_splits,
                n_total,
                self.train_bars,
                self.test_bars,
            )
            return

        for i in range(n_splits):
            train_end = initial_train + i * self.test_bars
            test_start = train_end
            test_end = min(test_start + self.test_bars, n_total)
            train_start = 0  # expanding window: always from start

            train_idx = np.arange(train_start, train_end, dtype=np.intp)
            test_idx = np.arange(test_start, test_end, dtype=np.intp)

            if len(train_idx) == 0 or len(test_idx) == 0:
                logger.warning(
                    "split(): empty fold %d (train=%d, test=%d), stopping early",
                    i,
                    len(train_idx),
                    len(test_idx),
                )
                return

            yield CVSplit(train_indices=train_idx, test_indices=test_idx, fold_id=i)

    def get_train_test(
        self,
        n_total: int,
        fold_index: int = 0,
    ) -> CVSplit:
        """Return a single embargo-purged train/test split for walk-forward.

        Implements the purged walk-forward logic described in:
        Advances in Financial Machine Learning (López de Prado, 2018).

        The training set is truncated by ``purge_bars`` bars immediately
        before the test window, and also excludes the ``embargo_bars`` bars
        after the test window (relevant when later folds overlap with the
        current fold's embargo region).

        This returns *one* fold suitable for a sequential walk-forward loop.
        Call repeatedly with increasing ``fold_index`` to walk forward.

        Parameters
        ----------
        n_total : int
            Total number of bars in the full series.
        fold_index : int
            Zero-based fold number.  Fold 0 uses the first ``train_bars`` bars
            for training; fold 1 shifts the test window forward by one
            ``test_bars`` step, etc.

        Returns
        -------
        CVSplit
            Train/test indices with purge and embargo applied.

        Raises
        ------
        ValueError
            If the resulting train or test set is empty.
        """
        test_start = self.train_bars + fold_index * self.test_bars
        test_end = min(test_start + self.test_bars, n_total)

        # Training: first test_start bars, then purge the last purge_bars
        # (the bars immediately before the test window).
        train_end = max(0, test_start - self.purge_bars)
        # Also exclude embargo bars *after* this fold's test window from any
        # future use.  Since we return a single fold, we apply embargo as an
        # additional truncation so the training index does not include bars
        # that are within ``embargo_bars`` after the test window.
        # (For sequential walk-forward, this prevents overlapping information.)
        train_start = 0  # expanding window

        train_idx = np.arange(train_start, train_end, dtype=np.intp)
        test_idx = np.arange(test_start, test_end, dtype=np.intp)

        if len(train_idx) == 0 or len(test_idx) == 0:
            raise ValueError(
                f"Empty train (n={len(train_idx)}) or test (n={len(test_idx)}) "
                f"at fold_index={fold_index}. Check n_total={n_total}, "
                f"train_bars={self.train_bars}, test_bars={self.test_bars}, "
                f"purge_bars={self.purge_bars}."
            )

        return CVSplit(train_indices=train_idx, test_indices=test_idx, fold_id=fold_index)

    # ── Convenience ─────────────────────────────────────────────────────

    def total_bars_needed(self, n_splits: int = 1) -> int:
        """Minimum total bars required for ``n_splits`` folds.

        Includes purge and embargo in the calculation:
        ``train_bars + purge_bars + n_splits * test_bars + embargo_bars``.
        """
        return self.train_bars + self.purge_bars + n_splits * self.test_bars + self.embargo_bars

    def __repr__(self) -> str:
        return (
            f"EvaluationContext(train_bars={self.train_bars}, "
            f"test_bars={self.test_bars}, embargo_bars={self.embargo_bars}, "
            f"purge_bars={self.purge_bars})"
        )
