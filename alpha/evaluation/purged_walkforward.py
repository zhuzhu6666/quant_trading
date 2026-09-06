"""alpha/evaluation/purged_walkforward.py — Purged walk-forward cross-validation.

Implements the purged walk-forward (combinatorial purged cross-validation)
described in *Advances in Financial Machine Learning* (López de Prado, 2018).

This module builds on :class:`EvaluationContext` to produce folds that
respect a *purge* region (exclude training data immediately preceding
the test set).  Purge-only: the configured ``embargo_bars`` is recorded
for reference but no embargo mask is applied to any fold.

Usage::

    ctx = EvaluationContext(train_bars=250, test_bars=50, embargo_bars=10, purge_bars=5)
    pwf = PurgedWalkForward(ctx, n_folds=5)
    for fold in pwf.folds(n_total=1000):
        print(fold)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, Optional

import numpy as np

from alpha.evaluation.evaluation_context import CVSplit, EvaluationContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FoldContext:
    """Context for a single purged walk-forward fold.

    Wraps the train/test indices together with metadata about the fold.

    Attributes:
        train_indices: 1D array of training indices.
        test_indices: 1D array of testing indices.
        fold_id: Zero-based fold number.
        embargo_size: Configured embargo bars (not applied; purge-only CV,
            kept for reference).
        purge_size: Number of purge bars applied (for reference).
    """

    train_indices: np.ndarray
    test_indices: np.ndarray
    fold_id: int
    embargo_size: int
    purge_size: int


class PurgedWalkForward:
    """Purged walk-forward cross-validation generator.

    Parameters
    ----------
    context : EvaluationContext
        Holds the train/test/embargo/purge bar counts.
    n_folds : int
        Number of folds to generate.  The test bar count from ``context``
        determines the size of each test window.
    """

    def __init__(self, context: EvaluationContext, n_folds: int) -> None:
        if n_folds < 1:
            raise ValueError(f"n_folds must be >= 1, got {n_folds}")
        self.context = context
        self.n_folds = n_folds

    # ── Public API ──────────────────────────────────────────────────────

    def folds(self, n_total: Optional[int] = None) -> Iterator[FoldContext]:
        """Generate purged walk-forward folds.

        Each fold applies:
        1. **Purge**: the last ``purge_bars`` bars of the training set
           (immediately before the test window) are dropped.

        No embargo is applied: ``embargo_bars`` is recorded for reference
        only and does not alter any fold (purge-only CV).

        The training window expands with each fold: the first fold uses
        ``train_bars`` bars; subsequent folds add ``test_bars`` bars to the
        training pool before applying purge.

        Parameters
        ----------
        n_total : int, optional
            Total number of bars in the full series.  If omitted, inferred
            from ``context.total_bars_needed(self.n_folds)``.

        Yields
        ------
        FoldContext
            Each fold with purge-only train/test indices.

        Raises
        ------
        ValueError
            If the series is too short for the requested number of folds.
        """
        if n_total is None:
            n_total = self.context.total_bars_needed(self.n_folds)

        required = self.context.total_bars_needed(self.n_folds)
        if n_total < required:
            raise ValueError(
                f"n_total={n_total} < required={required} for "
                f"{self.n_folds} folds (train={self.context.train_bars}, "
                f"test={self.context.test_bars}, purge={self.context.purge_bars}, "
                f"embargo={self.context.embargo_bars})"
            )

        for i in range(self.n_folds):
            fold = self._build_fold(n_total, i)
            yield fold

    def get_fold(self, n_total: int, fold_index: int) -> FoldContext:
        """Return a specific fold by index without iterating.

        This is a convenience wrapper around the same logic as ``folds()``
        but returns a single fold.

        Parameters
        ----------
        n_total : int
            Total number of bars in the full series.
        fold_index : int
            Zero-based fold index.

        Returns
        -------
        FoldContext
        """
        return self._build_fold(n_total, fold_index)

    def __len__(self) -> int:
        return self.n_folds

    def __repr__(self) -> str:
        return (
            f"PurgedWalkForward(context={self.context!r}, n_folds={self.n_folds})"
        )

    # ── Internal helpers ────────────────────────────────────────────────

    def _build_fold(self, n_total: int, fold_index: int) -> FoldContext:
        """Construct a single purged walk-forward fold."""
        ctx = self.context
        test_bars = ctx.test_bars
        purge_bars = ctx.purge_bars
        embargo_bars = ctx.embargo_bars
        train_bars = ctx.train_bars

        # Compute test window boundaries.
        test_start = train_bars + fold_index * test_bars
        test_end = min(test_start + test_bars, n_total)

        # --- Purge: remove the last ``purge_bars`` from training.
        # The raw training region is [0, test_start).  We drop the last
        # ``purge_bars`` bars, so the effective training end is:
        train_raw_end = test_start
        train_end = max(0, train_raw_end - purge_bars)

        # Purge-only: the configured embargo region [test_end, test_end +
        # embargo_bars) always lies after train_end (<= test_start), so no
        # embargo mask is applied; ``embargo_bars`` is reported for
        # reference only.
        train_idx = np.arange(0, max(0, train_end), dtype=np.intp)

        test_idx = np.arange(test_start, test_end, dtype=np.intp)

        if len(train_idx) == 0 or len(test_idx) == 0:
            raise ValueError(
                f"Empty train (n={len(train_idx)}) or test (n={len(test_idx)}) "
                f"at fold_index={fold_index}. Check parameters."
            )

        return FoldContext(
            train_indices=train_idx,
            test_indices=test_idx,
            fold_id=fold_index,
            embargo_size=embargo_bars,
            purge_size=purge_bars,
        )
