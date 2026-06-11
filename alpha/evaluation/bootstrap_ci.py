"""alpha/evaluation/bootstrap_ci.py — Bootstrap confidence intervals for Sharpe and IC.

Uses non-parametric bootstrap with replacement on paired (signal, return)
samples to estimate confidence intervals for:

- **Mean** of a sample (``ci_mean``)
- **Sharpe ratio** (``ci_sharpe``) — annualised Sharpe from mean/std of returns
- **Information coefficient** (``ci_ic``) — Spearman rank correlation between
  signal and forward returns

All intervals use the percentile method (default alpha=0.05 → 95% CI).

Usage::

    bootstrap = BootstrapCI(alpha=0.05, n_iterations=2000)

    # Sharpe CI
    lo, hi = bootstrap.ci_sharpe(returns)

    # IC CI
    lo, hi = bootstrap.ci_ic(signal, forward_returns)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from scipy import stats as scipy_stats

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CIResult:
    """Result of a bootstrap confidence interval calculation.

    Attributes:
        point_estimate: The original (non-bootstraped) estimate.
        ci_lower: Lower bound of the confidence interval.
        ci_upper: Upper bound of the confidence interval.
        alpha: Significance level used.
        n_iterations: Number of bootstrap iterations performed.
        bootstrap_samples: Array of bootstrap replicate estimates
            (useful for histogram or advanced diagnostics).
    """

    point_estimate: float
    ci_lower: float
    ci_upper: float
    alpha: float
    n_iterations: int
    bootstrap_samples: np.ndarray


class BootstrapCI:
    """Non-parametric bootstrap confidence intervals.

    Uses ``np.random.choice`` with replacement on paired samples to
    generate the bootstrap distribution of the target statistic.

    Parameters
    ----------
    alpha : float, default 0.05
        Significance level.  The resulting confidence interval has
        ``(1 - alpha) * 100%`` coverage.  For example, alpha=0.05 gives a
        95% CI.
    n_iterations : int, default 1000
        Number of bootstrap resamples.
    random_seed : int, optional
        Seed for reproducibility.
    annualization_factor : float, default 252
        Annualization factor for Sharpe ratio (e.g., 252 for daily bars,
        52 for weekly, 12 for monthly).
    """

    def __init__(
        self,
        alpha: float = 0.05,
        n_iterations: int = 1000,
        random_seed: Optional[int] = None,
        annualization_factor: float = 252.0,
    ) -> None:
        if not 0 < alpha < 1:
            raise ValueError(f"alpha must be in (0, 1), got {alpha}")
        if n_iterations < 10:
            raise ValueError(f"n_iterations must be >= 10, got {n_iterations}")

        self.alpha = alpha
        self.n_iterations = n_iterations
        self.annualization_factor = annualization_factor
        self._rng = np.random.default_rng(random_seed)

    # ── Public API ──────────────────────────────────────────────────────

    def ci_mean(
        self,
        samples: np.ndarray,
    ) -> CIResult:
        """Bootstrap confidence interval for the mean.

        Parameters
        ----------
        samples : np.ndarray of shape (n,)
            1-D array of observations.

        Returns
        -------
        CIResult
        """
        samples = self._validate(samples)
        point_estimate = float(np.mean(samples))
        bootstrap_means = self._bootstrap_stat(samples, np.mean)
        ci_lower, ci_upper = self._percentile_interval(bootstrap_means)

        return CIResult(
            point_estimate=point_estimate,
            ci_lower=ci_lower,
            ci_upper=ci_upper,
            alpha=self.alpha,
            n_iterations=self.n_iterations,
            bootstrap_samples=bootstrap_means,
        )

    def ci_sharpe(
        self,
        returns: np.ndarray,
    ) -> CIResult:
        """Bootstrap confidence interval for the annualised Sharpe ratio.

        The Sharpe ratio is computed as:
        ``sqrt(annualization_factor) * mean(returns) / std(returns)``

        Parameters
        ----------
        returns : np.ndarray of shape (n,)
            1-D array of period returns.

        Returns
        -------
        CIResult
        """
        returns = self._validate(returns)
        point_estimate = self._compute_sharpe(returns)
        bootstrap_sharpes = self._bootstrap_stat(returns, self._compute_sharpe)
        ci_lower, ci_upper = self._percentile_interval(bootstrap_sharpes)

        return CIResult(
            point_estimate=point_estimate,
            ci_lower=ci_lower,
            ci_upper=ci_upper,
            alpha=self.alpha,
            n_iterations=self.n_iterations,
            bootstrap_samples=bootstrap_sharpes,
        )

    def ci_ic(
        self,
        signal: np.ndarray,
        forward_returns: np.ndarray,
    ) -> CIResult:
        """Bootstrap confidence interval for the Information Coefficient (IC).

        IC is defined as the Spearman rank correlation between signal and
        forward returns.  Paired bootstrapping resamples (signal, return)
        pairs together to preserve the joint distribution.

        Parameters
        ----------
        signal : np.ndarray of shape (n,)
            Factor or signal values.
        forward_returns : np.ndarray of shape (n,)
            Corresponding forward returns.

        Returns
        -------
        CIResult
        """
        signal, forward_returns = self._validate_paired(signal, forward_returns)
        n = len(signal)

        def _spearman_ic(sig: np.ndarray, ret: np.ndarray) -> float:
            """Compute Spearman rank IC from paired arrays."""
            return float(scipy_stats.spearmanr(sig, ret)[0])

        point_estimate = _spearman_ic(signal, forward_returns)

        # Paired bootstrap: resample indices jointly.
        indices = np.arange(n)
        bootstrap_ics = np.empty(self.n_iterations, dtype=np.float64)
        for i in range(self.n_iterations):
            idx = self._rng.choice(indices, size=n, replace=True)
            bootstrap_ics[i] = _spearman_ic(signal[idx], forward_returns[idx])

        ci_lower, ci_upper = self._percentile_interval(bootstrap_ics)

        return CIResult(
            point_estimate=point_estimate,
            ci_lower=ci_lower,
            ci_upper=ci_upper,
            alpha=self.alpha,
            n_iterations=self.n_iterations,
            bootstrap_samples=bootstrap_ics,
        )

    # ── Internal helpers ────────────────────────────────────────────────

    def _validate(self, arr: np.ndarray) -> np.ndarray:
        arr = np.asarray(arr, dtype=np.float64)
        if arr.ndim != 1:
            raise ValueError(f"Expected 1-D array, got shape {arr.shape}")
        # Drop NaN.
        return arr[~np.isnan(arr)]

    def _validate_paired(
        self, a: np.ndarray, b: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray]:
        a = np.asarray(a, dtype=np.float64).ravel()
        b = np.asarray(b, dtype=np.float64).ravel()
        if a.shape != b.shape:
            raise ValueError(
                f"Signal shape {a.shape} != forward_returns shape {b.shape}"
            )
        # Drop rows where either is NaN.
        mask = ~(np.isnan(a) | np.isnan(b))
        return a[mask], b[mask]

    def _compute_sharpe(self, returns: np.ndarray) -> float:
        if len(returns) < 2:
            return 0.0
        mean_ret = float(np.mean(returns))
        std_ret = float(np.std(returns, ddof=1))
        if std_ret < 1e-12:
            return 0.0
        return float(np.sqrt(self.annualization_factor)) * mean_ret / std_ret

    def _bootstrap_stat(
        self,
        samples: np.ndarray,
        statistic,
    ) -> np.ndarray:
        """Generate bootstrap distribution of a statistic.

        Parameters
        ----------
        samples : np.ndarray of shape (n,)
        statistic : callable
            A function ``np.ndarray -> float``.

        Returns
        -------
        np.ndarray of shape (n_iterations,)
        """
        n = len(samples)
        estimates = np.empty(self.n_iterations, dtype=np.float64)
        for i in range(self.n_iterations):
            resample = self._rng.choice(samples, size=n, replace=True)
            estimates[i] = statistic(resample)
        return estimates

    def _percentile_interval(self, bootstrap_dist: np.ndarray) -> Tuple[float, float]:
        """Compute the percentile confidence interval.

        Returns ``(lower, upper)`` such that ``(alpha/2) * 100%`` of the
        bootstrap distribution falls below ``lower`` and above ``upper``.
        """
        lower_pct = 100.0 * (self.alpha / 2)
        upper_pct = 100.0 * (1.0 - self.alpha / 2)
        lower, upper = np.percentile(bootstrap_dist, [lower_pct, upper_pct])
        return float(lower), float(upper)

    def __repr__(self) -> str:
        return (
            f"BootstrapCI(alpha={self.alpha}, n_iterations={self.n_iterations}, "
            f"annualization_factor={self.annualization_factor})"
        )
